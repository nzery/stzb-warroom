"""The group chat of every client online (the window's 群聊 page).

This client keeps what it got and sent, each account apart (``chat-<key>.jsonl`` in the state
folder, the newest KEEP; the key is drawn from the token and tells nothing of it): switching
accounts is like another player opening the chat, what the other account sent is someone
else's. The
chat follows ``/v1/chat/stream`` with the account the player chose on the page (else the first
whose role is approved, else the first the server lets chat: App.chat_token), asking for what
came after the newest message it got (the server keeps a week of them), and sends with
``/v1/chat/send`` naming its stream. A message is kept with its id, so one given again (what this
account sent from another computer, or a stream that broke) is not kept twice.

Everything that comes from the stream is checked before it is kept or shown: the page shows it as
text, never as markup.
"""

import collections
import hashlib
import json
import re
import socket
import threading
import time
from pathlib import Path
from urllib.error import HTTPError

from .notify import events

STREAM = "/v1/chat/stream"
SEND = "/v1/chat/send"
KEEP = 2000  # messages kept on this computer
FIRST = 30  # messages a page opening the chat is given; older ones when it scrolls up
MAX_TEXT = 500  # the server's limit too
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿\ud800-\udfff]")


def text_of(value, limit):
    """`value` as text to keep and show, or None when it is not one."""
    if not isinstance(value, str):
        return None
    value = CONTROL.sub("", value).strip()
    return value[:limit] if value else None


def message_of(data, mine=False):
    """A message from the server (or the file) as kept, or None when it is not one."""
    if not isinstance(data, dict):
        return None
    name, text, stamp = text_of(data.get("name"), 32), text_of(data.get("text"), MAX_TEXT * 2), data.get("time")
    if name is None or text is None or isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    message_id = data.get("id")
    return {"id": message_id if type(message_id) is int and message_id > 0 else None,
            "name": name, "text": text, "time": float(stamp), "mine": bool(mine)}


def server_error(exc):
    try:
        message = json.loads(exc.read() or b"{}").get("error")
    except (OSError, ValueError, AttributeError):
        message = None
    return message if isinstance(message, str) and message else f"服务器返回 HTTP {exc.code}"


class Chat:
    """`token()`: the token to chat with now (None: no approved account yet); `changed()`: the
    window has something new to show."""

    def __init__(self, server, state_dir, token, changed=lambda: None):
        self.server, self.token, self.changed = server, token, changed
        self.dir = Path(state_dir)
        self.lock = threading.Lock()
        self.key, self.path = None, None  # the account whose messages are kept and shown
        self.messages = collections.deque(maxlen=KEEP)
        self.seq = 0
        self.after = 0  # the newest id the stream gave: what came after it is asked for
        self.state, self.name, self.online, self.error = "connecting", None, None, None
        self.conn = None  # this client's stream, named when sending
        self.stream = None  # the response being read, cut when the player switches accounts
        first = token()
        if first is not None:
            self.use(first)

    # what is kept ---------------------------------------------------------------------

    @staticmethod
    def key_of(token):
        return hashlib.sha256(f"stzb-chat:{token}".encode()).hexdigest()[:16]

    def use(self, token):
        """Keep and show `token`'s messages from now on (they were another account's)."""
        key = self.key_of(token)
        with self.lock:
            if key == self.key:
                return
            if self.key is not None:  # the other account's stream and name are not this one's
                self.conn, self.name, self.online = None, None, None
            self.key, self.path = key, self.dir / f"chat-{key}.jsonl"
            self.messages.clear()
            self.seq = self.after = 0
            legacy = self.dir / "chat.jsonl"  # before v1.13.2 one for all: the first account used takes it
            if legacy.is_file() and not self.path.exists():
                try:
                    legacy.replace(self.path)
                except OSError:
                    pass
            self._load()
        self.changed()

    def switch(self, token):
        """The player chose `token` to chat with: show its messages now, and connect with it
        (the stream of the other account is cut, run connects again at once)."""
        self.use(token)
        with self.lock:
            stream = self.stream
            self.state, self.conn, self.online, self.error = "connecting", None, None, None
        if stream is not None:
            cut(stream)
        self.changed()

    def _load(self):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-KEEP:]:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            message = message_of(value, value.get("mine") is True) if isinstance(value, dict) else None
            if message:
                # Its number kept with it: the page's "read up to" means the same after a restart.
                seq = value.get("seq")
                self.seq = seq if type(seq) is int and seq > self.seq else self.seq + 1
                self.messages.append(dict(message, seq=self.seq))
                if message["id"] and not message["mine"]:
                    self.after = max(self.after, message["id"])
        if len(lines) > KEEP + KEEP // 2:  # the file only grows between starts: cut it back
            self._rewrite()

    def _rewrite(self):
        try:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text("".join(self._line(m) for m in self.messages), encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            pass

    @staticmethod
    def _line(message):
        return json.dumps({key: message.get(key) for key in ("seq", "id", "name", "text", "time", "mine")}, ensure_ascii=False) + "\n"

    def _keep(self, message, streamed=False):
        with self.lock:
            if self.path is None:
                return message
            if message.get("id"):
                if streamed:
                    self.after = max(self.after, message["id"])
                kept = next((m for m in self.messages if m.get("id") == message["id"]), None)
                if kept:
                    return kept
            self.seq += 1
            message = dict(message, seq=self.seq)
            self.messages.append(message)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(self._line(message))
            except OSError:
                pass  # shown still, only not kept
        self.changed()
        return message

    # for the page ---------------------------------------------------------------------

    def view(self, after=0):
        """The chat's state, and the messages after seq `after` (the newest FIRST when 0: a page
        opening it is not handed everything kept)."""
        with self.lock:
            messages = [m for m in self.messages if m["seq"] > after] if after else list(self.messages)[-FIRST:]
            return {"account": self.key, "state": self.state, "name": self.name, "online": self.online, "error": self.error,
                    "messages": messages, "oldest": self.messages[0]["seq"] if self.messages else None}

    def older(self, before, limit=FIRST):
        """Up to `limit` messages before seq `before`, oldest first."""
        if type(before) is not int:
            raise ValueError("bad cursor")
        with self.lock:
            return [m for m in self.messages if m["seq"] < before][-max(1, min(int(limit), 200)):]

    def send(self, text):
        text = text_of(text, MAX_TEXT + 1) if isinstance(text, str) else None
        if text is None:
            raise ValueError("消息是空的")
        if len(text) > MAX_TEXT:
            raise ValueError(f"消息太长了，最多 {MAX_TEXT} 个字")
        token = self.token()  # not under self.lock: the window's lock is taken before it (view)
        with self.lock:
            conn, key = self.conn, self.key
        if token is None:
            raise ValueError("账号还没通过服务器检查，稍后再试")
        if self.key_of(token) != key:
            raise ValueError("群聊正在切换账号，稍等一下")
        if conn is None:
            raise ValueError("群聊还没连上，稍等一下")
        try:
            value = self.server.json(SEND, token, {"conn": conn, "text": text}, timeout=15)
        except HTTPError as exc:
            raise ValueError(server_error(exc)) from None
        except OSError as exc:
            raise ValueError(f"连不上服务器：{exc}") from None
        message = message_of(value, mine=True)
        if message is None:
            raise ValueError("服务器的回复不对")
        if self.key != key:  # switched meanwhile: it is the other account's
            return message
        return self._keep(message)

    # the stream -----------------------------------------------------------------------

    def _set(self, **fields):
        with self.lock:
            if all(getattr(self, key) == value for key, value in fields.items()):
                return
            for key, value in fields.items():
                setattr(self, key, value)
        self.changed()

    def follow_once(self, token, stop):
        self.use(token)
        with self.lock:
            after = self.after
        # Looked at on every line (the keep-alives too), not only on a message: a switch is not
        # left waiting for someone to speak.
        gone = Either(stop, lambda: self.token() != token)
        with self.server.open(f"{STREAM}?after={after}", token, timeout=60) as stream:
            with self.lock:
                self.stream = stream
            try:
                for kind, _id, data in events(stream, gone):
                    self._take(kind, data)
                    if self.token() != token:
                        return  # the account was removed, or another chosen
            finally:
                with self.lock:
                    if self.stream is stream:
                        self.stream = None

    def _take(self, kind, data):
        """Keep or show one event of the stream."""
        if not isinstance(data, dict):
            return
        if kind == "hello" and isinstance(data.get("conn"), str):
            self._set(state="online", conn=data["conn"][:64], name=text_of(data.get("name"), 32),
                      online=data.get("online") if type(data.get("online")) is int else None, error=None)
        elif kind == "online" and type(data.get("online")) is int:
            self._set(online=data["online"])
        # `mine`: this account's own, sent from another computer or the console (server 2026-10-31)
        elif kind == "msg" and (message := message_of(data, data.get("mine") is True)) is not None:
            self._keep(message, streamed=True)

    def run(self, stop):
        delay = 2
        while not stop.is_set():
            token = self.token()
            if token is None:
                self._set(state="unavailable", conn=None, online=None, error=None)
                stop.wait(5)
                continue
            wait = None
            try:
                self.follow_once(token, stop)
                delay = 2
            except HTTPError as exc:
                # 403: not approved after all (the account check will say); 503: the chat is full.
                wait = 60 if exc.code == 403 else 30
                self._set(state="offline", conn=None, error=server_error(exc))
            except (OSError, ValueError) as exc:
                if self.token() == token:  # not the stream cut by a switch
                    delay = min(delay * 2, 60)
                    self._set(state="offline", conn=None, error=f"连不上群聊：{type(exc).__name__}")
            if self.token() != token:  # another account chosen (or this one removed): connect with it now
                self._set(state="connecting", conn=None, online=None, error=None)
                delay = 2
                continue
            # The stream ended (the server restarted): connect again.
            self._set(conn=None, **({"state": "connecting"} if self.state in ("online", "unavailable") else {}))
            self._pause(stop, wait or delay, token)

    def _pause(self, stop, seconds, token):
        """Wait `seconds` before connecting again, less when the program quits or another account is chosen."""
        end = time.monotonic() + seconds
        while not stop.is_set() and self.token() == token and time.monotonic() < end:
            stop.wait(min(1, max(0, end - time.monotonic())))


class Either:
    """A stop that is also set when `gone()` says so (notify.events looks at it on every line)."""

    def __init__(self, stop, gone):
        self.stop, self.gone = stop, gone

    def is_set(self):
        return self.stop.is_set() or self.gone()


def cut(stream):
    """End the reading of `stream` from another thread (closing it alone leaves a read blocked)."""
    try:
        stream.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError):
        pass
