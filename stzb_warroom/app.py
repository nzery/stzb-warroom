"""The window's state and actions, apart from how they are shown (webui.py serves them).

It runs the same capture and upload as ``capture`` (runner.run_capture) and follows the
same notifications and prompts as ``notify``, for the tokens the player typed in (kept
encrypted, vault.py). Everything shown about an account (its name, role, approval, phone
binding: Bark or ntfy) comes from the server (``/v1/client/status``); phones are bound there too. So does the
notice settings page (``/v1/client/notice-prefs``): the player's choice is given to every token,
so all the accounts of this computer follow the same settings. The server keeps them: the
minute's look only reads them, and a choice is sent only when the player makes it, to a token
added here since, or again to a token it did not reach; a change made elsewhere (another
computer with the same token) is followed, not undone.

The 玩家信息 page (any player, looked up by name: union, main city, armies) is the server's too
(``/v1/client/personal/players``): asked for when the player opens it, passed to the window
as it comes, the name typed, the page's filters and page number passed along unread.

The 群聊 page (chat.py) runs from the start to the end of the program, captured or not, with
the account chosen on that page, else the first whose role is approved, else the first the
server lets chat.
"""

import collections
import hashlib
import json
import os
import platform
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

from . import __version__, chat, notify, relay, update as updates, vault, winsys
from .api import client_tokens, invite_code, site
from .capture import find_dumpcap
from .runner import run_capture

ACCOUNT_EVERY = 60  # seconds between two looks at the accounts' status
INVALID_EVERY = 1800  # a token the server rejected (401) is looked at again this rarely, unless asked
PREFS = "/v1/client/notice-prefs"
PERSONAL = "/v1/client/personal/"
PERSONAL_PAGES = ("players",)
PHONE = "/v1/client/phone"
RELAYS_EVERY = 60  # seconds between two asks for the relays while none is known
RELAYS_REFRESH = 600  # and once there are some (a token approved or moved since: more relays)
UPDATE_EVERY = 6 * 3600
HINTS = (  # what a player can do about a capture failure, from its text
    ("another capture process", "已经有一个程序在上报（可能是另一个窗口），先关掉它。"),
    ("401", "所有 Token 都无效：到“账号与推送”检查 Token。"),
    ("dumpcap not found", "没有找到 Wireshark：到“设置”里安装或手动选择 dumpcap.exe。"),
    ("dumpcap", "网络组件没能启动：确认装了 Wireshark 并勾选了 Npcap，装好后重启电脑再试。"),
)


def tag(token):
    """What settings.json keeps to know a token again, never the token itself."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def mask(token):
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "…"


def hint(error):
    return next((text for key, text in HINTS if key in error), "")


class Lines:
    """sys.stdout / sys.stderr of the window: each line goes to the log."""

    def __init__(self, log):
        self.log, self.buffer, self.lock = log, "", threading.Lock()

    def write(self, text):
        with self.lock:
            self.buffer += text
            *lines, self.buffer = self.buffer.split("\n")
        for line in lines:
            if line.strip():
                self.log(line)
        return len(text)

    def flush(self):
        pass


def expired(notice, now):
    expires = notice.get("expires")
    return isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= now


class App:
    def __init__(self, server, state_dir, opener=None):
        self.server, self.state_dir = server, Path(state_dir)
        self.opener = opener or server.opener.open  # for the release check and its download
        self.vault_path = self.state_dir / "tokens.bin"
        self.dumpcap_path = self.state_dir / "dumpcap.txt"
        self.settings_path = self.state_dir / "settings.json"
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0  # bumped on every change the page should fetch
        self.notices = collections.deque(maxlen=500)
        self.logs = collections.deque(maxlen=1000)
        # Numbers the notices and log lines; the page keeps "cleared up to" across launches, so a
        # new launch numbers on from where any earlier one stopped: counting from 0 again hid every
        # notice after a restart (2026-10-16 user) until it passed the old number.
        self.seq = int(time.time() * 1000)
        self.hints = {}  # token -> {"id", "text", "time"}
        self.accounts = {}  # token -> what /v1/client/status said, or why it could not
        self.capture = {"state": "stopped", "error": None, "hint": None, "since": None, "dumpcap": None}
        self.notice_prefs = {"note": None, "groups": None, "values": None, "error": None}  # as the server gave it
        # One look or save of the notice settings at a time: a minute's look still under way would
        # otherwise end after a save and put back what it read before it, or clear the save's retries.
        self.prefs_lock = threading.Lock()
        self.session = None
        self.update = None  # a newer release: {"version", "url", "code_only", "state", "error"}
        self.update_lock = threading.Lock()  # one download at a time
        self.relaunch = False  # start again once quit (to run the code an update downloaded)
        self.quit = threading.Event()
        self.poke = threading.Event()  # look at the accounts now
        self.exit_said = {}  # (event, relay) -> when the phone exit last said it in the log
        self.settings = self._read_settings()
        self.updated = None  # what this version brought, in the run that shows it (_updated)
        # Only what the player added in the window; ST_CLIENT_TOKEN where there is no vault
        # (development off Windows). Nothing is saved until the player changes something.
        try:
            self.tokens = client_tokens(vault.load(self.vault_path) if vault.AVAILABLE
                                        else os.environ.get("ST_CLIENT_TOKEN", ""))
        except SystemExit:
            self.tokens = []
        self.chat = chat.Chat(server, self.state_dir, self.chat_token, self.touch)

    # state for the page -----------------------------------------------------------------

    def touch(self):
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    def wait(self, version, timeout):
        """The version once it differs from `version`, or after `timeout`."""
        with self.changed:
            self.changed.wait_for(lambda: self.version != version or self.quit.is_set(), timeout)
            return self.version

    def log(self, text, level="info"):
        with self.lock:
            self.seq += 1
            self.logs.append({"seq": self.seq, "time": time.time(), "text": str(text), "level": level})
        self.touch()

    def snapshot(self, notices_after=0, logs_after=0, chat_after=0):
        with self.lock:
            self._drop_expired()
            uploader = self.session and self.session.get("uploader")
            stats = {"pending": uploader.queue.pending() if uploader and self.capture["state"] == "running" else None,
                     "uploaded": uploader.uploaded if uploader else 0,
                     "uploaded_at": uploader.uploaded_at if uploader else None,
                     "connections": uploader.connections() if uploader else 0}
            return {
                "version": self.version, "app_version": __version__, "server": self.server.origin, "site": site(),
                "capture": dict(self.capture), "stats": stats,
                "tokens": [self._account(token) for token in self.tokens],
                "hints": [dict(hint, token=mask(token)) for token, hint in self.hints.items()],
                "notices": [n for n in self.notices if n["seq"] > notices_after],
                "logs": [line for line in self.logs if line["seq"] > logs_after],
                "phone": self._phone_status(),
                "settings": {"keep_running": self.settings["keep_running"], "phone_exit": self.settings["phone_exit"],
                             "autostart": winsys.autostart(), "autostart_available": winsys.autostart_command() is not None,
                             "dumpcap": self.find_dumpcap()[0], "dumpcap_picked": self._picked(),
                             "state_dir": str(self.state_dir), "windows": winsys.WINDOWS},
                "update": self.update,
                "updated": self._updated(),
                "notice_prefs": dict(self.notice_prefs),
                "chat": {**self.chat.view(chat_after), **self.chat_accounts()},
            }

    def _account(self, token):
        value = dict(self.accounts.get(token) or {"state": "checking"})
        value["token"] = mask(token)
        return value

    # settings ---------------------------------------------------------------------------

    def _read_settings(self):
        try:
            value = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        # phone_exit: the phone may play through this computer (relay.py); off until the player says.
        return {"keep_running": True, "phone_exit": False, **(value if isinstance(value, dict) else {})}

    def set_setting(self, name, value):
        if name == "autostart":
            winsys.set_autostart(bool(value))
        elif name in ("keep_running", "phone_exit"):
            with self.lock:
                self.settings[name] = bool(value)
                self._write_settings()
                session = self.session
            if name == "phone_exit" and session is not None:
                if value:
                    self._start_exits(session)
                else:
                    self._stop_exits(session)
                    self.log("已关闭手机经本机连接游戏")
        else:
            raise ValueError(f"unknown setting {name}")
        self.touch()

    def _updated(self):
        """{"version", "notes"} in the run of this version that first shows the notes kept before
        updating (a page asks for them), else None; marked shown then, so later starts say nothing."""
        kept = self.settings.get("update_notes")
        # Said once (2026-10-14 user: it was said at every start).
        if (self.updated is None and isinstance(kept, dict) and kept.get("version") == __version__
                and kept.get("notes") and not kept.get("shown")):
            self.updated = {"version": kept["version"], "notes": str(kept["notes"])}
            kept["shown"] = True
            self._write_settings()
        return self.updated

    def set_ui(self, key, value):
        """A setting of the window's page (the sound, a closed banner, …). Kept here, not in the
        page's own storage, which the program's new port at each start empties (2026-10-14 user:
        what was new was said at every start)."""
        if not isinstance(key, str) or not key or len(key) > 40 or len(json.dumps(value)) > 4000:
            raise ValueError("bad setting")
        with self.lock:
            self.settings.setdefault("ui", {})[key] = value
            self._write_settings()

    def _write_settings(self):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")

    def _picked(self):
        try:
            return self.dumpcap_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def find_dumpcap(self):
        """(dumpcap or None, places looked at): ST_DUMPCAP, the one picked by hand, then the search."""
        picked = self._picked()
        return find_dumpcap([picked] if picked else [])

    def pick_dumpcap(self):
        chosen = winsys.pick_file("选择 Wireshark 文件夹里的 dumpcap.exe", ("dumpcap", "dumpcap.exe"))
        if not chosen:
            return None
        if Path(chosen).name.lower() != "dumpcap.exe":
            raise ValueError("请选择名字是 dumpcap.exe 的文件")
        self.dumpcap_path.parent.mkdir(parents=True, exist_ok=True)
        self.dumpcap_path.write_text(chosen, encoding="utf-8")
        self.log(f"以后使用 {chosen}")
        return chosen

    def forget_dumpcap(self):
        self.dumpcap_path.unlink(missing_ok=True)
        self.touch()

    # tokens -----------------------------------------------------------------------------

    def _save_tokens(self):
        if not vault.AVAILABLE:
            return
        try:
            if self.tokens:
                vault.save(self.vault_path, ",".join(self.tokens))
            else:
                vault.forget(self.vault_path)
        except OSError as exc:
            self.log(f"Token 没能保存：{exc}", "error")

    def add_token(self, text):
        """Add the pasted tokens, or join with a pasted invitation code (the server gives a token).
        Returns what the server said about the joining, if any."""
        code = invite_code(text)
        joined = None
        if code:
            with self.lock:
                held = list(self.tokens)
            joined = self.server.join(code, held)
            text = joined["token"]
        try:
            new = client_tokens(text)
        except SystemExit:
            raise ValueError("请粘贴同盟发的邀请码（stj_ 开头）或管理员发给你的 Token（sta_ 开头）")
        bad = [token for token in new if not token.startswith("sta_") or len(token) != 47]
        if bad:
            raise ValueError("Token 应该是 sta_ 开头的 47 个字符（邀请码是 stj_ 开头的 26 个字符），请检查有没有复制完整")
        with self.lock:
            added = [token for token in new if token not in self.tokens]
            self.tokens = list(dict.fromkeys(self.tokens + new))
            self._save_tokens()
            if self._chosen_prefs() is not None and added:  # given this computer's settings
                self._prefs_due(add=added)
        self.log(f"已添加 Token {', '.join(mask(token) for token in new)}")
        if joined:
            self.log(joined.get("text") or "已用邀请码加入")
        self.restart()
        return {"joined": joined.get("text") if joined else None}

    def remove_token(self, index):
        with self.lock:
            token = self._token(index)
            self.tokens.pop(index)
            self.accounts.pop(token, None)
            self.hints.pop(token, None)
            self._save_tokens()
        self.log(f"已删除 Token {mask(token)}")
        self.restart()

    def _token(self, index):
        with self.lock:
            if not 0 <= index < len(self.tokens):
                raise ValueError("没有这个账号")
            return self.tokens[index]

    # the accounts' status and Bark --------------------------------------------------

    def check_accounts(self, asked=False):
        for token in list(self.tokens):
            known = self.accounts.get(token) or {}
            # A rejected token stays rejected until the player acts (asked) or a long while passed.
            if (not asked and known.get("state") == "invalid"
                    and time.monotonic() - known.get("checked", 0) < INVALID_EVERY):
                continue
            try:
                value = self.server.json("/v1/client/status", token, timeout=20)
                # A problem the server names comes first (a role another token holds, another
                # NetEase account): the game's data is not uploaded.
                state = "problem" if value.get("problem") else (
                    "ok" if value.get("approved") and value.get("role") else (
                        "pending" if not value.get("approved") else "no_role"))
                value = dict(value, state=state)
            except HTTPError as exc:
                value = {"state": "invalid", "checked": time.monotonic()} if exc.code == 401 else dict(
                    self.accounts.get(token) or {}, state="offline", error=f"HTTP {exc.code}")
            except (OSError, ValueError) as exc:
                value = dict(self.accounts.get(token) or {}, state="offline", error=f"{type(exc).__name__}: {exc}")
            with self.lock:
                if token in self.tokens:
                    self.accounts[token] = value
        self.touch()
        self.sync_notice_prefs()

    # notice settings ----------------------------------------------------------------

    def _chosen_prefs(self):
        value = self.settings.get("notice_prefs")
        return value if isinstance(value, dict) else None

    def _prefs_due(self, add=()):
        """The tags of the tokens still to be given the player's choice (kept in settings.json)."""
        due = self.settings.get("notice_prefs_due")
        due = set(due) if isinstance(due, list) else set()
        if add and not {tag(t) for t in add} <= due:
            due |= {tag(t) for t in add}
            self.settings["notice_prefs_due"] = sorted(due)
            self._write_settings()
        return due

    def _prefs_request(self, token, body=None):
        """The server's settings page for a token (saving `body` first); ValueError says why not."""
        try:
            value = self.server.json(PREFS, token, body, timeout=20)
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("服务器还不支持通知设置")
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except (OSError, ValueError):
                message = None
            raise ValueError(message or f"服务器返回 HTTP {exc.code}")
        except OSError as exc:
            raise ValueError(f"连不上服务器：{exc}")
        if not isinstance(value, dict) or not isinstance(value.get("values"), dict):
            raise ValueError("服务器的回复不对")
        return value

    def sync_notice_prefs(self):
        """Read the settings page; send the player's choice only to the tokens still due it (added
        here since, or not reached when it was made). The first token's settings are shown, and
        kept as this computer's choice: one made elsewhere is followed, not undone."""
        with self.prefs_lock:
            self._sync_notice_prefs()
        self.touch()

    def _sync_notice_prefs(self):
        with self.lock:
            chosen, due = self._chosen_prefs(), self._prefs_due()
        page, errors, left = None, [], set()  # left: the tags still due after this look
        for token in list(self.tokens):
            if (self.accounts.get(token) or {}).get("state") == "invalid":
                continue
            send = chosen is not None and tag(token) in due
            try:
                value = self._prefs_request(token, {"values": chosen} if send else None)
                page = page or value
            except ValueError as exc:
                errors.append(f"{mask(token)}：{exc}")
                if send:
                    left.add(tag(token))
        with self.lock:
            current = self._prefs_due()
            left |= current - due  # a token added here meanwhile is still due the choice
            if left != current:  # sent, or a token gone or no longer valid
                self.settings["notice_prefs_due"] = sorted(left)
                self._write_settings()
            if page is not None:
                self.notice_prefs = {"note": page.get("note"), "groups": page.get("groups"),
                                     "values": page["values"], "error": None}
                if not left and page["values"] != chosen:
                    self.settings["notice_prefs"] = page["values"]
                    self._write_settings()
            self.notice_prefs["error"] = "；".join(errors) or None

    def set_notice_pref(self, key, value):
        """The player changed one setting: saved here and given to every token."""
        with self.prefs_lock:
            values = self._set_notice_pref(key, value)
        self.touch()
        return values

    def _set_notice_pref(self, key, value):
        with self.lock:
            values = dict(self.notice_prefs.get("values") or {})
        if not isinstance(key, str) or key not in values:
            raise ValueError("没有这个设置")
        values[key] = value
        page, errors, failed = None, [], []
        for token in list(self.tokens):
            if (self.accounts.get(token) or {}).get("state") == "invalid":
                continue
            try:
                page = self._prefs_request(token, {"values": values})
            except ValueError as exc:
                errors.append(f"{mask(token)}：{exc}")
                failed.append(token)
        if page is None:
            raise ValueError("没能保存：" + ("；".join(errors) or "没有可用的账号"))
        with self.lock:
            self.settings["notice_prefs"] = page["values"]
            self.settings["notice_prefs_due"] = sorted({tag(t) for t in failed})  # retried each minute
            self._write_settings()
            self.notice_prefs = {"note": page.get("note"), "groups": page.get("groups"), "values": page["values"],
                                 "error": ("有账号没保存上，稍后自动重试：" + "；".join(errors)) if errors else None}
        return page["values"]

    def _bark(self, method, path, index, body=None):
        token = self._token(index)
        try:
            with self.server.open(path, token, body if method == "POST" else None, timeout=40,
                                  method=method) as response:
                value = json.load(response)
        except HTTPError as exc:
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except (OSError, ValueError):
                message = None
            raise ValueError(message or f"服务器返回 HTTP {exc.code}")
        except OSError as exc:
            raise ValueError(f"连不上服务器：{exc}")
        with self.lock:
            if token in self.accounts:
                self.accounts[token]["bark"] = value
        self.touch()
        return value

    def bark_bind(self, index, address):
        return self._bark("POST", "/v1/client/bark", index, {"address": str(address or "")})

    def ntfy_bind(self, index):
        """An Android phone: the server gives the token a topic on its ntfy server, and how to subscribe."""
        return self._bark("POST", "/v1/client/bark", index, {"kind": "ntfy"})

    def bark_unbind(self, index):
        return self._bark("DELETE", "/v1/client/bark", index)

    def bark_test(self, index):
        return self._bark("POST", "/v1/client/bark/test", index, {})

    # the server's pages (玩家信息) ----------------------------------------------------

    def personal(self, index, page, query=None):
        """One of the server's pages (玩家信息), as it made it. `query`: the name typed, the page's
        filters and page number ({name: text or number}), sent along as they are."""
        token = self._token(index)
        if page not in PERSONAL_PAGES:
            raise ValueError("没有这个页面")
        query = {str(key)[:20]: str(value)[:40] for key, value in (query or {}).items()
                 if isinstance(value, (str, int)) and not isinstance(value, bool)} if isinstance(query, dict) else {}
        path = PERSONAL + page + ("?" + urlencode(sorted(query.items())[:10]) if query else "")
        try:
            value = self.server.json(path, token, timeout=30)
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("服务器还不支持这个页面")
            if exc.code == 403:
                raise ValueError("这个账号还没审批，或还没登录过游戏")
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except (OSError, ValueError):
                message = None
            raise ValueError(message or f"服务器返回 HTTP {exc.code}")
        except OSError as exc:
            raise ValueError(f"连不上服务器：{exc}")
        if not isinstance(value, dict):
            raise ValueError("服务器的回复不对")
        return value

    # the phone through this computer (relay.py) -----------------------------------

    def phone(self, index, reset=False):
        """The server's phone page for the account (nodes with QR codes, the rule, the steps), as
        it made it; `reset` asks for a new key first. Nothing of it is logged."""
        token = self._token(index)
        try:
            with self.server.open(PHONE + ("/reset" if reset else ""), token, {} if reset else None,
                                  timeout=30, method="POST" if reset else "GET") as response:
                value = json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("服务器还不支持用手机玩")
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except (OSError, ValueError):
                message = None
            raise ValueError(message or f"服务器返回 HTTP {exc.code}")
        except OSError as exc:
            raise ValueError(f"连不上服务器：{exc}")
        if not isinstance(value, dict):
            raise ValueError("服务器的回复不对")
        if reset:
            self.log("已重置手机节点：旧的二维码 30 秒内失效，手机要重新扫码")
        return value

    def _phone_status(self):
        session = self.session
        exits = (session or {}).get("exits") or []
        return {"enabled": self.settings["phone_exit"], "running": session is not None,
                "waiting": bool(session and self.settings["phone_exit"] and not exits),
                "exits": [{"relay": e.origin.removeprefix("https://"), "state": e.state, "since": e.since,
                           "pipes": e.open_pipes} for e in exits]}

    def _start_exits(self, session):
        """The exits of this capture session: the relays come from the server (any of the tokens'),
        asked again every RELAYS_EVERY until there are some, then every RELAYS_REFRESH for a relay
        a token may use since (approved, moved to another domain): an exit is added for it."""
        mine = session["exits_run"] = object()  # an older run (the exit turned off and on) ends

        def run():
            while not session["stop"].is_set() and self.settings["phone_exit"] and session["exits_run"] is mine:
                origins = relay.relays_of(self.server, session["tokens"])
                with self.lock:
                    exits = session.get("exits") or []
                    known = {e.origin for e in exits}
                    new = [o for o in origins if o.rstrip("/") not in known]
                    if new and not session["stop"].is_set() and self.settings["phone_exit"] \
                            and session["exits_run"] is mine:
                        session["exits"] = exits + relay.start(new, session["tokens"], log=self._exit_log)
                        if not exits:
                            self.log("手机经本机连接游戏：已开启")
                if not origins and not exits:
                    self.log("手机经本机连接游戏：服务器还没给中转（账号要先审批通过），稍后再试", "warn")
                self.touch()
                session["stop"].wait(RELAYS_REFRESH if session.get("exits") else RELAYS_EVERY)
            self.touch()

        threading.Thread(target=run, name="relays", daemon=True).start()
        self.touch()

    def _stop_exits(self, session):
        with self.lock:
            exits, session["exits"] = session.get("exits") or [], []
        for exit_ in exits:
            exit_.close()
        self.touch()

    def _exit_log(self, **fields):
        """The exits' events in the window's log: what the player can act on, once per change."""
        event, host = fields.get("event"), (fields.get("relay") or "").removeprefix("https://")
        text = {
            "relay_connected": ("info", f"手机中转：已连上 {host}"),
            "relay_refused": ("warn", f"手机中转：{host} 不接受这台电脑的账号（要已审批的账号）"),
            "relay_busy": ("warn", "手机中转：这个账号的手机正经另一台电脑转发；那台关掉出口或下线后，这台自动接上"),
            "relay_unreachable": ("warn", f"手机中转：连不上 {host}，稍后重试（{fields.get('error') or '原因不明'}）"),
            "pipe_open": ("info", "手机的一条游戏连接经本机转出"),
            "open_refused": ("warn", f"手机中转要本机连的目标不对，已拒绝（{fields.get('reason')}）"),
            "pipe_failed": ("warn", "手机中转：数据通道没建立起来"),
        }.get(event)
        if event == "game_check" and not fields.get("ok"):
            text = ("warn", "拦下一条不是游戏的手机连接，这个目标 10 分钟内不再连")
        if text is None:
            return
        key = (event, host)
        now = time.monotonic()
        with self.lock:
            if event in ("relay_unreachable", "relay_refused", "relay_busy") and now - self.exit_said.get(key, -3600) < 600:
                return  # retried every few minutes: said once in ten
            self.exit_said[key] = now
        self.log(text[1], text[0])
        self.touch()

    # notifications and prompts ------------------------------------------------------

    def _drop_expired(self):
        """Notices past the time the server gave them (``expires``) are no longer shown."""
        now = time.time()
        if any(expired(n, now) for n in self.notices):
            kept = [n for n in self.notices if not expired(n, now)]
            self.notices.clear()
            self.notices.extend(kept)

    def on_notice(self, notice):
        if expired(notice, time.time()):
            return
        with self.lock:
            self.seq += 1
            self.notices.append(dict(notice, seq=self.seq, time=notice.get("time") or time.time()))
        self.touch()

    def notify_ready(self, token):
        """Whether the token's notifications can be asked: once the account check found its role
        approved (before, the server refuses them with 403 every minute). When the check cannot
        reach the server, the stream is tried and reports its own problem."""
        def ready():
            with self.lock:
                value = self.accounts.get(token)
            if value is None:  # not checked yet: the check runs at start
                return False
            return value.get("state") in ("offline", "invalid") or bool(value.get("approved") and value.get("role"))
        return ready

    def prompt_for(self, token):
        def show(text, ask):  # only hints now; nothing is ever asked
            with self.lock:
                if text is None:  # the server says the problem is over
                    self.hints.pop(token, None)
                else:
                    self.hints[token] = {"id": f"{mask(token)}:{time.time()}", "text": text, "time": time.time()}
            if text is not None:
                self.log("服务器提示：" + (text.splitlines() or [""])[0])
            self.touch()
            return None
        return show

    def _chat_tokens(self):
        """The tokens that may chat: approved, or let chat by the server (``chat`` in its status:
        one not bound to a role yet too). Under self.lock."""
        accounts = [self.accounts.get(token) or {} for token in self.tokens]
        return [token for token, value in zip(self.tokens, accounts)
                if value.get("state") == "ok" or (value.get("chat") is True and value.get("state") != "invalid")]

    def chat_token(self):
        """The token the chat uses: the one the player chose on the 群聊 page (settings.json keeps
        its tag) while it may chat, else the first one whose role is approved, else the first the
        server lets chat, else None."""
        with self.lock:
            able = self._chat_tokens()
            chosen = self.settings.get("chat_account")
            return next((token for token in able if tag(token) == chosen), None) \
                or next((token for token in able if (self.accounts.get(token) or {}).get("state") == "ok"),
                        able[0] if able else None)

    def chat_accounts(self):
        """For the page: the accounts the chat may use (their indexes) and the one it uses."""
        using = self.chat_token()
        with self.lock:
            able = set(self._chat_tokens())
            return {"choices": [index for index, token in enumerate(self.tokens) if token in able],
                    "using": self.tokens.index(using) if using in self.tokens else None}

    def use_chat(self, index):
        """Chat with the account `index` from now on (2026-10-31 user: to test with another
        account without removing one); its messages are shown instead."""
        token = self._token(index)
        with self.lock:
            if token not in self._chat_tokens():
                raise ValueError("这个账号还不能群聊：服务器确认后才能用")
            self.settings["chat_account"] = tag(token)
            self._write_settings()
        self.chat.switch(token)
        self.touch()

    # capture ------------------------------------------------------------------------

    def start(self):
        with self.lock:
            if self.session is not None:
                return
            if not self.tokens:
                raise ValueError("先到“账号与推送”添加 Token")
            dumpcap, places = self.find_dumpcap()
            if dumpcap is None:
                self.log("没有找到 dumpcap，找过：" + "；".join(places), "error")
                self.capture.update(state="error", error="dumpcap not found",
                                    hint=hint("dumpcap not found"), since=None, dumpcap=None)
                self.touch()
                return
            stop = threading.Event()
            session = self.session = {"stop": stop, "uploader": None, "tokens": list(self.tokens), "exits": []}
            self.capture.update(state="starting", error=None, hint=None, since=time.time(), dumpcap=dumpcap)
            self.hints.clear()
            session["thread"] = threading.Thread(target=self._capture, args=(session, dumpcap), name="capture",
                                                 daemon=True)
            session["thread"].start()
        notify.start(self.server, session["tokens"], self.state_dir, stop, prompt_for=self.prompt_for,
                     ready_for=self.notify_ready, show=self.on_notice, warn=lambda text: self.log(text, "warn"))
        if self.settings["phone_exit"]:
            self._start_exits(session)  # the phone's connections are reported by this capture
        self.touch()

    def _capture(self, session, dumpcap):
        def started(uploader):
            uploader.on_role = self.poke.set  # check_accounts now, not at the next minute
            with self.lock:
                session["uploader"] = uploader
                if session is self.session:
                    self.capture["state"] = "running"
            self.log("开始上报")

        error = None
        try:
            run_capture(self.server, session["tokens"], self.state_dir, session["stop"], dumpcap=dumpcap,
                        started=started)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        finally:
            session["stop"].set()  # also stop notifications when setup failed before capture began
            self._stop_exits(session)
        with self.lock:
            if session is self.session:
                self.session = None
                self.capture.update(state="error" if error else "stopped", error=error,
                                    hint=hint(error) if error else None, since=None)
        if error:
            self.log("上报停止：" + error, "error")
            if "401" in error:
                self.poke.set()
        else:
            self.log("已停止上报")
        self.touch()

    def stop(self, wait=False, timeout=40):
        with self.lock:
            session = self.session
            if session is None:
                return
            session["stop"].set()
            self.capture["state"] = "stopping"
        self.touch()
        if wait:
            session["thread"].join(timeout=timeout)

    def restart(self):
        self.stop(wait=True)
        self.poke.set()
        if self.tokens:
            try:
                self.start()
            except ValueError:
                pass
        self.touch()

    # release check, diagnostics -----------------------------------------------------

    def check_update(self):
        """Look for a newer release. When only its code is needed it is downloaded right away
        (in the background) and runs from the next start; otherwise the player downloads it."""
        release = updates.latest(self.opener)
        newer = updates.newer(release)
        update = None
        if newer:
            code_only = updates.code_only(release)
            update = {"version": release["version"], "url": release["url"], "code_only": code_only,
                      "state": "ready" if code_only and updates.installed(release) else
                      "downloading" if code_only else "available", "error": None,
                      "notes": release.get("notes") or ""}
        with self.lock:
            busy = self.update and self.update["state"] == "downloading" and update and \
                self.update["version"] == update["version"]
            if not busy:
                self.update = update
            # Kept, so the first start of that version says what it brought (``updated``); also when this
            # already is the newest version and nothing was kept for it (updated by a version before this one).
            kept = self.settings.get("update_notes")
            kept = kept if isinstance(kept, dict) else {}
            target = update or (release if release["version"] == __version__ else None)
            if target and release.get("notes") and kept.get("version") != target["version"]:
                self.settings["update_notes"] = {"version": target["version"], "notes": release["notes"]}
                self._write_settings()
        if update and update["state"] == "downloading" and not busy:
            threading.Thread(target=self._download_update, args=(release,), name="update", daemon=True).start()
        self.touch()
        return {"latest": release["version"], "newer": newer}

    def _download_update(self, release):
        with self.update_lock:
            try:
                updates.install(release, self.opener)
                state, error = "ready", None
                self.log(f"新版本 {release['version']} 已下载，重启程序后生效")
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                state, error = "failed", str(exc)
                self.log(f"下载新版本失败：{exc}", "warn")
        with self.lock:
            if self.update and self.update["version"] == release["version"]:
                self.update.update(state=state, error=error)
        self.touch()

    def restart_for_update(self):
        """Quit and start again, running the code the update downloaded."""
        with self.lock:
            if not (self.update and self.update["state"] == "ready"):
                raise ValueError("新版本还没有下载好")
            self.relaunch = True

    def diagnostics(self):
        """Text to send to the maintainer: no token, no device key."""
        with self.lock:
            snapshot = self.snapshot()
        reach = "?"
        try:
            with self.server.opener.open(self.server.origin + "/health", timeout=10) as response:
                reach = f"HTTP {response.status}"
        except OSError as exc:
            reach = f"{type(exc).__name__}: {exc}"
        lines = [f"率土战局 {__version__}  {datetime.now():%Y-%m-%d %H:%M:%S}",
                 f"系统：{winsys.system()}  Python {platform.python_version()}",
                 f"服务器：{self.server.origin}（{reach}）",
                 f"dumpcap：{snapshot['settings']['dumpcap'] or '没有找到'}；Npcap：{ {True: '已安装', False: '未安装', None: '-'}[winsys.npcap_installed()]}",
                 f"上报：{snapshot['capture']['state']} {snapshot['capture']['error'] or ''}".rstrip(),
                 f"统计：已上传 {snapshot['stats']['uploaded']} 条，待上传 {snapshot['stats']['pending']}，"
                 f"连接 {snapshot['stats']['connections']}",
                 "手机经本机：" + ("开" if snapshot["phone"]["enabled"] else "关") + "".join(
                     f"；{e['relay']} {e['state']}（{e['pipes']} 条）" for e in snapshot["phone"]["exits"])]
        for number, account in enumerate(snapshot["tokens"], 1):
            role = (account.get("role") or {}).get("label") or "-"
            bark = account.get("bark") or {}
            waits = (account.get("pending") or {}).get("label")
            lines.append(f"账号 {number}：{account['state']}{f'（{waits}）' if waits else ''} {role}；手机推送 {({'ntfy': '已绑定安卓', 'bark': '已绑定 iPhone'}.get(bark.get('kind'), '已绑定')) if bark.get('bound') else '未绑定'}"
                         + (f"（{bark['error']}）" if bark.get("error") else ""))
        lines.append("最近日志：")
        lines += [f"  {datetime.fromtimestamp(line['time']):%H:%M:%S} {line['text']}" for line in snapshot["logs"][-60:]]
        return "\n".join(lines)

    # background work ----------------------------------------------------------------

    def run_background(self):
        """Look at the accounts and the release now and then, until quit."""
        last_accounts = last_update = 0
        while not self.quit.is_set():
            now = time.monotonic()
            if self.poke.is_set() or now - last_accounts >= ACCOUNT_EVERY:
                asked = self.poke.is_set()
                self.poke.clear()
                last_accounts = now
                self.check_accounts(asked)
            if now - last_update >= UPDATE_EVERY:
                last_update = now
                try:
                    self.check_update()
                except ValueError:
                    pass
            self.poke.wait(1)

    def begin(self):
        """At launch: the background work, and the capture when it can start."""
        sys.stdout = sys.stderr = Lines(self.log)
        self.log(f"程序 {__version__} 已启动")
        threading.Thread(target=self.run_background, name="background", daemon=True).start()
        threading.Thread(target=self.chat.run, args=(self.quit,), name="chat", daemon=True).start()
        if self.tokens:
            self.start()

    def shutdown(self):
        # dumpcap is gone within a second; an upload still under way is kept in the queue for next time.
        self.stop(wait=True, timeout=5)
        self.quit.set()
        self.poke.set()
        self.touch()
