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
text, never as markup. The zero-width joiner stays (it makes an emoji sequence one picture).

An image is a message of its own (``image``: its name on the server and its size): sent with
``/v1/chat/image``, fetched only when the page shows it (``image()``) and kept in ``chat-images`` in
the state folder (IMAGE_CACHE at most, the oldest go first), so it is downloaded once.

A message can be recalled (``/v1/chat/recall``; server 2026-11-12): one's own within the minutes the
server says in ``hello``, any by an admin. The server sends the recalled message again as a ``msg``
with ``"recalled": true`` (also, as the stream opens with ``recall=1``, those recalled while this
client was away): what this computer kept of it, its text and its image, is forgotten, and the page
shows "… 撤回了一条消息".
"""

import collections
import hashlib
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

from .notify import events

STREAM = "/v1/chat/stream"
SEND = "/v1/chat/send"
RECALL = "/v1/chat/recall"
RECALLED_TEXT = "撤回了一条消息"
KEEP = 2000  # messages kept on this computer
FIRST = 30  # messages a page opening the chat is given; older ones when it scrolls up
MAX_TEXT = 500  # the server's limit too
IMAGE = "/v1/chat/image"
IMAGE_MAX = 1024 * 1024  # the server's limit
ANIMATED_MAX = 2 * 1024 * 1024  # a 动图 (GIF, animated WebP / PNG) is sent as it is, up to this
PREFS = "/v1/chat/prefs"
WEB_LOGOUT = "/v1/chat/web-logout"
IMAGE_TYPES = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
IMAGE_NAME = re.compile(r"[0-9a-f]{32}\.(?:jpg|png|gif|webp)\Z")
IMAGE_CACHE = 300 * 1024 * 1024  # bytes of images kept on this computer
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f​‌‎‏‪-‮⁠-⁩﻿\ud800-\udfff]")


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
    # `official`: the administrator, named for the service; `level` 1-3: how much the sender plays, the
    # avatar standing out more the higher (server 2026-11-08); anything else is the plain avatar. `staff`: a
    # player the administrator made 管理员 (server 2026-11-12, sent with level 3), shown with a 管理员 tag.
    level = data.get("level")
    message = {"id": message_id if type(message_id) is int and message_id > 0 else None,
               "name": name, "text": text, "time": float(stamp), "mine": bool(mine), "official": data.get("official") is True,
               "level": level if type(level) is int and 1 <= level <= 3 else 0}
    if data.get("staff") is True:
        message["staff"] = True
    if data.get("recalled") is True:
        return dict(message, text=RECALLED_TEXT, recalled=True)
    image = image_of(data.get("image"))
    if image:
        message["image"] = image
    return message


def image_of(value):
    """A message's image ({"name", "w", "h"}) as kept, None when it has none (or not one right)."""
    if not isinstance(value, dict):
        return None
    name, width, height = value.get("name"), value.get("w"), value.get("h")
    if not (isinstance(name, str) and IMAGE_NAME.match(name)):
        return None
    if not all(type(v) is int and 0 < v <= 20000 for v in (width, height)):
        return None
    return {"name": name, "w": width, "h": height}


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
        self.admin, self.recall_seconds = False, 0  # what the server said in hello
        self.mention_push, self.phone = None, False  # another player's @ pushed too; a phone bound to push to
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
                self.conn, self.name, self.online, self.admin, self.recall_seconds = None, None, None, False, 0
                self.mention_push, self.phone = None, False
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
        keys = ("seq", "id", "name", "text", "time", "mine", "official", "level") + (("image",) if message.get("image") else ()) \
            + (("staff",) if message.get("staff") else ()) \
            + (("recalled",) if message.get("recalled") else ())
        return json.dumps({key: message.get(key) for key in keys}, ensure_ascii=False) + "\n"

    def _forget(self, recalled, streamed=False):
        """A message recalled: what is kept of it goes (the file written again, its image deleted);
        one not kept is kept recalled only when it is new to this client."""
        with self.lock:
            if self.path is None:
                return
            kept = next((m for m in self.messages if m.get("id") == recalled["id"]), None)
            if kept is None and recalled["id"] <= self.after:
                return  # recalled before this client had it (it was away), or long gone from here
            if kept is not None:
                if kept.get("recalled"):
                    return
                image = kept.pop("image", None)
                kept.update(text=RECALLED_TEXT, recalled=True)
                self._rewrite()
        if kept is None:
            self._keep(recalled, streamed)
            return
        if image:
            (self.dir / "chat-images" / image["name"]).unlink(missing_ok=True)
        self.changed()

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
                    "messages": messages, "oldest": self.messages[0]["seq"] if self.messages else None,
                    "admin": self.admin, "recall": self.recall_seconds,
                    "mention_push": self.mention_push, "phone": self.phone,
                    # those the page has already: shown recalled in place
                    "recalled": [m["seq"] for m in self.messages if m.get("recalled")]}

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
        return self._sent(lambda token, conn: self.server.json(SEND, token, {"conn": conn, "text": text}, timeout=15))

    def send_image(self, data, kind, width, height):
        """Send an image (the page has made it small: chat.js) -> the message, as `send`."""
        if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= ANIMATED_MAX:
            raise ValueError(f"图片太大了，动图最多 {ANIMATED_MAX // 1024 // 1024} MB")
        if kind not in IMAGE_TYPES.values():
            raise ValueError("只能发 JPG、PNG、GIF、WebP 图片")
        if not all(type(v) is int and 0 < v <= 20000 for v in (width, height)):
            raise ValueError("图片尺寸不对")

        def post(token, conn):
            path = f"{IMAGE}?{urlencode({'conn': conn, 'w': width, 'h': height})}"
            with self.server.post_bytes(path, token, bytes(data), kind, timeout=60) as response:
                return json.load(response)
        return self._sent(post)

    def _sent(self, post):
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
            value = post(token, conn)
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

    def set_mention_push(self, on):
        """The 群聊 page's switch: another player's @ also pushed to the phone (an admin's always is)."""
        if not isinstance(on, bool):
            raise ValueError("设置不对")
        value = self._post(PREFS, {"mention_push": on})
        self._set(mention_push=value.get("mention_push") is True if isinstance(value, dict) else on,
                  phone=isinstance(value, dict) and value.get("phone") is True)
        return {"mention_push": self.mention_push}

    def web_logout(self):
        """发群聊链接到手机: the server makes this account a new phone web chat link (the old one ends) and
        pushes it to the phone bound; the link never comes here -> {"ok", "message"} as the server says."""
        value = self._post(WEB_LOGOUT, {})
        value = value if isinstance(value, dict) else {}
        message = value.get("message") if isinstance(value.get("message"), str) else ""
        return {"ok": value.get("ok") is True, "message": message[:200]}

    def _post(self, path, body):
        token = self.token()
        if token is None:
            raise ValueError("账号还没通过服务器检查，稍后再试")
        try:
            return self.server.json(path, token, body, timeout=15)
        except HTTPError as exc:
            raise ValueError(server_error(exc)) from None
        except OSError as exc:
            raise ValueError(f"连不上服务器：{exc}") from None

    def recall(self, message_id):
        """Recall a message (its id): one's own within the minutes the server allows, any by an admin."""
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("这条消息不能撤回")
        token = self.token()
        if token is None:
            raise ValueError("账号还没通过服务器检查，稍后再试")
        try:
            value = self.server.json(RECALL, token, {"id": message_id}, timeout=15)
        except HTTPError as exc:
            raise ValueError(server_error(exc)) from None
        except OSError as exc:
            raise ValueError(f"连不上服务器：{exc}") from None
        message = message_of(value, value.get("mine") is True) if isinstance(value, dict) else None
        if message is None or not message.get("recalled") or message["id"] != message_id:
            raise ValueError("服务器的回复不对")
        if self.key_of(token) == self.key:
            self._forget(message)
        return {"id": message_id}

    def image(self, name):
        """(bytes, content type) of a message's image: kept on this computer, else fetched from the
        server and kept; None when it is not there (gone after a week) or cannot be fetched now."""
        if not isinstance(name, str) or not IMAGE_NAME.match(name):
            return None
        kind = IMAGE_TYPES[name.rsplit(".", 1)[1]]
        folder = self.dir / "chat-images"
        path = folder / name
        try:
            data = path.read_bytes()
            os.utime(path)  # looked at: the last to go
            return data, kind
        except OSError:
            pass
        token = self.token()
        if token is None:
            return None
        try:
            with self.server.open(f"{IMAGE}/{name}", token, timeout=30) as response:
                data = response.read(ANIMATED_MAX + 1)
        except (OSError, ValueError):
            return None
        if not data or len(data) > ANIMATED_MAX:
            return None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            temporary = folder / f".{name}.tmp"
            temporary.write_bytes(data)
            temporary.replace(path)
            self._trim_images(folder)
        except OSError:
            pass  # shown still, only not kept
        return data, kind

    @staticmethod
    def _trim_images(folder, limit=None):
        """The oldest looked at go once those kept pass IMAGE_CACHE."""
        limit = IMAGE_CACHE if limit is None else limit
        files = []
        for path in folder.iterdir():
            if IMAGE_NAME.match(path.name):
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _at, size, _path in files)
        for _at, size, path in sorted(files):
            if total <= limit:
                break
            path.unlink(missing_ok=True)
            total -= size

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
        with self.server.open(f"{STREAM}?after={after}&recall=1", token, timeout=60) as stream:
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
            recall = data.get("recall")
            self._set(state="online", conn=data["conn"][:64], name=text_of(data.get("name"), 32),
                      online=data.get("online") if type(data.get("online")) is int else None, error=None,
                      admin=data.get("admin") is True, recall_seconds=recall if type(recall) is int and recall > 0 else 0,
                      mention_push=data["mention_push"] if isinstance(data.get("mention_push"), bool) else None,
                      phone=data.get("phone") is True)
        elif kind == "online" and type(data.get("online")) is int:
            self._set(online=data["online"])
        # `mine`: this account's own, sent from another computer or the console (server 2026-10-31)
        elif kind == "msg" and (message := message_of(data, data.get("mine") is True)) is not None:
            if message.get("recalled"):
                if message["id"]:
                    self._forget(message, streamed=True)
            else:
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
