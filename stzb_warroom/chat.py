"""The group chat of every client online (the window's 群聊 page).

This client keeps what it got and sent (``chat.jsonl`` in the state folder, the newest KEEP). The
chat follows ``/v1/chat/stream`` with the first account whose role is approved, asking for what
came after the newest message it got (the server keeps a week of them), and sends with
``/v1/chat/send`` naming its stream. A message is kept with its id, so one given again (what this
account sent from another computer, or a stream that broke) is not kept twice.

Everything that comes from the stream is checked before it is kept or shown: the page shows it as
text, never as markup.
"""

import collections
import json
import re
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
        self.path = Path(state_dir) / "chat.jsonl"
        self.lock = threading.Lock()
        self.messages = collections.deque(maxlen=KEEP)
        self.seq = 0
        self.after = 0  # the newest id the stream gave: what came after it is asked for
        self.state, self.name, self.online, self.error = "connecting", None, None, None
        self.conn = None  # this client's stream, named when sending
        self._load()

    # what is kept ---------------------------------------------------------------------

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
            return {"state": self.state, "name": self.name, "online": self.online, "error": self.error,
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
            conn = self.conn
        if token is None:
            raise ValueError("账号审批通过、登录过游戏后才能群聊")
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
        with self.lock:
            after = self.after
        with self.server.open(f"{STREAM}?after={after}", token, timeout=60) as stream:
            for kind, _id, data in events(stream, stop):
                if not isinstance(data, dict):
                    continue
                if kind == "hello" and isinstance(data.get("conn"), str):
                    self._set(state="online", conn=data["conn"][:64], name=text_of(data.get("name"), 32),
                              online=data.get("online") if type(data.get("online")) is int else None, error=None)
                elif kind == "online" and type(data.get("online")) is int:
                    self._set(online=data["online"])
                elif kind == "msg" and (message := message_of(data)) is not None:
                    self._keep(message, streamed=True)
                if self.token() != token:  # the account was removed or another comes first now
                    return

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
                delay = min(delay * 2, 60)
                self._set(state="offline", conn=None, error=f"连不上群聊：{type(exc).__name__}")
            # The stream ended (the server restarted, another account comes first): connect again.
            self._set(conn=None, **({"state": "connecting"} if self.state in ("online", "unavailable") else {}))
            stop.wait(wait or delay)
