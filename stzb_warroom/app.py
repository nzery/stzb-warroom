"""The window's state and actions, apart from how they are shown (webui.py serves them).

It runs the same capture and upload as ``capture`` (runner.run_capture) and follows the
same notifications and prompts as ``notify``, for the tokens the player typed in (kept
encrypted, vault.py). Everything shown about an account (its name, role, approval, Bark
binding) comes from the server (``/v1/client/status``); Bark is bound there too.
"""

import collections
import json
import os
import platform
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

from . import __version__, notify, vault, winsys
from .api import client_tokens, site
from .capture import find_dumpcap
from .runner import run_capture

RELEASES = "https://api.github.com/repos/nzery/stzb-warroom/releases/latest"
ACCOUNT_EVERY = 60  # seconds between two looks at the accounts' status
UPDATE_EVERY = 6 * 3600
HINTS = (  # what a player can do about a capture failure, from its text
    ("another capture process", "已经有一个程序在上报（可能是另一个窗口），先关掉它。"),
    ("401", "所有 Token 都无效：到“账号与推送”检查 Token。"),
    ("dumpcap not found", "没有找到 Wireshark：到“设置”里安装或手动选择 dumpcap.exe。"),
    ("dumpcap", "网络组件没能启动：确认装了 Wireshark 并勾选了 Npcap，装好后重启电脑再试。"),
)


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


class App:
    def __init__(self, server, state_dir, opener=None):
        self.server, self.state_dir = server, Path(state_dir)
        self.opener = opener or server.opener.open  # for the release check
        self.vault_path = self.state_dir / "tokens.bin"
        self.dumpcap_path = self.state_dir / "dumpcap.txt"
        self.settings_path = self.state_dir / "settings.json"
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.version = 0  # bumped on every change the page should fetch
        self.notices = collections.deque(maxlen=500)
        self.logs = collections.deque(maxlen=1000)
        self.seq = 0
        self.hints = {}  # token -> {"id", "text", "time"}
        self.accounts = {}  # token -> what /v1/client/status said, or why it could not
        self.capture = {"state": "stopped", "error": None, "hint": None, "since": None, "dumpcap": None}
        self.session = None
        self.update = None  # {"version", "url"} of a newer release
        self.quit = threading.Event()
        self.poke = threading.Event()  # look at the accounts now
        self.settings = self._read_settings()
        # Only what the player added in the window; ST_CLIENT_TOKEN where there is no vault
        # (development off Windows). Nothing is saved until the player changes something.
        try:
            self.tokens = client_tokens(vault.load(self.vault_path) if vault.AVAILABLE
                                        else os.environ.get("ST_CLIENT_TOKEN", ""))
        except SystemExit:
            self.tokens = []

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

    def snapshot(self, notices_after=0, logs_after=0):
        with self.lock:
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
                "settings": {"keep_running": self.settings.get("keep_running", False),
                             "autostart": winsys.autostart(), "autostart_available": winsys.autostart_command() is not None,
                             "dumpcap": self.find_dumpcap()[0], "dumpcap_picked": self._picked(),
                             "state_dir": str(self.state_dir), "windows": winsys.WINDOWS},
                "update": self.update,
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
        return value if isinstance(value, dict) else {}

    def set_setting(self, name, value):
        if name == "autostart":
            winsys.set_autostart(bool(value))
        elif name == "keep_running":
            with self.lock:
                self.settings["keep_running"] = bool(value)
                self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                self.settings_path.write_text(json.dumps(self.settings), encoding="utf-8")
        else:
            raise ValueError(f"unknown setting {name}")
        self.touch()

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
        try:
            new = client_tokens(text)
        except SystemExit:
            raise ValueError("请粘贴管理员发给你的 Token")
        bad = [token for token in new if not token.startswith("sta_") or len(token) != 47]
        if bad:
            raise ValueError("Token 应该是 sta_ 开头的 47 个字符，请检查有没有复制完整")
        with self.lock:
            self.tokens = list(dict.fromkeys(self.tokens + new))
            self._save_tokens()
        self.log(f"已添加 Token {', '.join(mask(token) for token in new)}")
        self.restart()

    def remove_token(self, index):
        with self.lock:
            token = self.tokens.pop(index)
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

    def check_accounts(self):
        for token in list(self.tokens):
            try:
                value = self.server.json("/v1/client/status", token, timeout=20)
                # A problem the server names comes first (a role another token holds, another
                # NetEase account): the game's data is not uploaded.
                state = "problem" if value.get("problem") else (
                    "ok" if value.get("approved") and value.get("role") else (
                        "pending" if not value.get("approved") else "no_role"))
                value = dict(value, state=state)
            except HTTPError as exc:
                value = {"state": "invalid"} if exc.code == 401 else dict(
                    self.accounts.get(token) or {}, state="offline", error=f"HTTP {exc.code}")
            except (OSError, ValueError) as exc:
                value = dict(self.accounts.get(token) or {}, state="offline", error=f"{type(exc).__name__}: {exc}")
            with self.lock:
                if token in self.tokens:
                    self.accounts[token] = value
        self.touch()

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

    def bark_unbind(self, index):
        return self._bark("DELETE", "/v1/client/bark", index)

    def bark_test(self, index):
        return self._bark("POST", "/v1/client/bark/test", index, {})

    # notifications and prompts ------------------------------------------------------

    def on_notice(self, notice):
        with self.lock:
            self.seq += 1
            self.notices.append(dict(notice, seq=self.seq, time=notice.get("time") or time.time()))
        self.touch()

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
            session = self.session = {"stop": stop, "uploader": None, "tokens": list(self.tokens)}
            self.capture.update(state="starting", error=None, hint=None, since=time.time(), dumpcap=dumpcap)
            self.hints.clear()
        session["thread"] = threading.Thread(target=self._capture, args=(session, dumpcap), name="capture",
                                             daemon=True)
        session["thread"].start()
        notify.start(self.server, session["tokens"], self.state_dir, stop, prompt_for=self.prompt_for,
                     show=self.on_notice, warn=lambda text: self.log(text, "warn"))
        self.touch()

    def _capture(self, session, dumpcap):
        def started(uploader):
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
        try:
            with self.opener(RELEASES, timeout=15) as response:
                release = json.load(response)
        except (OSError, ValueError) as exc:
            raise ValueError(f"检查更新失败：{exc}")
        tag = str(release.get("tag_name") or "").lstrip("v")
        newer = _version(tag) > _version(__version__)
        with self.lock:
            self.update = {"version": tag, "url": release.get("html_url")} if newer else None
        self.touch()
        return {"latest": tag, "newer": newer}

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
                 f"连接 {snapshot['stats']['connections']}"]
        for number, account in enumerate(snapshot["tokens"], 1):
            role = (account.get("role") or {}).get("label") or "-"
            bark = account.get("bark") or {}
            lines.append(f"账号 {number}：{account['state']} {role}；Bark {'已绑定' if bark.get('bound') else '未绑定'}"
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
                self.poke.clear()
                last_accounts = now
                self.check_accounts()
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
        self.log(f"率土战局 {__version__} 已启动")
        threading.Thread(target=self.run_background, name="background", daemon=True).start()
        if self.tokens:
            self.start()

    def shutdown(self):
        # dumpcap is gone within a second; an upload still under way is kept in the queue for next time.
        self.stop(wait=True, timeout=5)
        self.quit.set()
        self.poke.set()
        self.touch()


def _version(text):
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return ()
