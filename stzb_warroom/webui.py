"""The window: a page served on this computer only, shown in the program's own window (Electron).

The server listens on 127.0.0.1 at a random port, and answers only a page that presents
the key it was opened with (a cookie set from ``/?k=<key>``) and names it as its host, so
neither other sites in a browser nor other computers can use it. The page polls
``/api/state`` when ``/api/events`` (server-sent events) says something changed, and does
everything through ``POST /api/action``.

One window per computer: a second start finds the first through ``window.json`` in the
state folder and asks it to show its window; a second start of a newer version (code an update
downloaded) asks it to quit instead and takes its place. Showing it (a start, the tray icon) brings an
open window to the front, and opens one only when none is open or opening. When the last page
closes (it says so as it goes, else its events stream breaks), the program stays running by
default. The player can disable this, except when started at login (``--background``). On Windows an icon
in the notification area shows the window again or quits.
"""

import hmac
import json
import mimetypes
import secrets
import shutil
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import __version__, winsys
from .app import App
from .runner import lock
from .update import version

UI = Path(__file__).resolve().parent / "ui"
TITLE = "工作台"  # the page's <title>, which names the window; plain so it passes for office software (2026-10-29 user)
OPENING = 15  # seconds a window may take to open (Electron starting) before another is opened
BANNER_POLL = 20  # seconds a banner request waits for a notice before answering "nothing"
BANNER_QUIET = 30  # seconds without a banner request before its window is taken to be gone
BANNER_RETRY = 60  # seconds between tries to start the banner window
CLOSED_AFTER = 3  # seconds without any page open before the program ends (a reload comes back sooner)
KEEP_ALIVE = 2  # seconds between writes to a quiet events stream, which find a page that is gone
FIRST_STARTING = 20  # seconds a second start waits for the first one's window to be up
TAKE_OVER = 30  # seconds an older program asked to quit (for this newer start) may take to go
COOKIE = "stzb_key"


class Handler(BaseHTTPRequestHandler):
    server_version = "stzb"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # checks -------------------------------------------------------------------------

    def host_ok(self):
        return self.headers.get("Host") in self.server.hosts

    def key_ok(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        value = jar[COOKIE].value if COOKIE in jar else ""
        return hmac.compare_digest(value.encode(), self.server.key.encode())

    def send(self, status, body, kind="application/json; charset=utf-8", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # routes -------------------------------------------------------------------------

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "forbidden"})
        url = urlsplit(self.path)
        if url.path in ("/", "/banner"):
            key = parse_qs(url.query).get("k", [""])[0]
            if key and hmac.compare_digest(key.encode(), self.server.key.encode()):
                cookie = f"{COOKIE}={self.server.key}; Path=/; HttpOnly; SameSite=Strict"
                # The notices' small window keeps its mode (main.js).
                location = url.path + ("?mini=1" if parse_qs(url.query).get("mini") == ["1"] else "")
                return self.send(303, b"", headers=(("Location", location), ("Set-Cookie", cookie)))
            if not self.key_ok():
                return self.send(403, "请双击 stzb-warroom.exe 打开窗口。", "text/plain; charset=utf-8")
            return self.static("index.html" if url.path == "/" else "banner.html")
        if url.path.startswith("/ui/"):
            return self.static(url.path[4:])
        if not self.key_ok():
            return self.send(403, {"error": "forbidden"})
        if url.path == "/api/state":
            query = parse_qs(url.query)
            try:
                numbers = [int(query.get(name, ["0"])[0] or 0) for name in ("n", "l", "c")]
            except ValueError:
                return self.send(400, {"error": "bad cursor"})
            return self.send(200, self.server.app.snapshot(*numbers))
        if url.path == "/api/events":
            return self.events(parse_qs(url.query).get("page", [""])[0] or secrets.token_hex(8))
        if url.path == "/api/banners":
            try:
                after = int(parse_qs(url.query).get("after", ["-1"])[0])
            except ValueError:
                return self.send(400, {"error": "bad cursor"})
            self.server.banner_asked()
            value = self.server.app.banners_after(after, BANNER_POLL)
            self.server.banner_asked(value["on"])
            return self.send(200, value)
        if url.path == "/api/ping":
            return self.send(200, {"ok": True})
        if url.path == "/api/ui":  # the page's settings, read as it loads
            return self.send(200, dict(self.server.app.settings.get("ui") or {}))
        self.send(404, {"error": "not found"})

    def static(self, name):
        path = (UI / name).resolve()
        if UI not in path.parents or not path.is_file():
            return self.send(404, {"error": "not found"})
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        headers = (("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                    "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"),)
        self.send(200, path.read_bytes(), kind, headers)

    def events(self, page):
        """`change` whenever the state changes, a comment every KEEP_ALIVE seconds otherwise."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        app, version = self.server.app, -1
        self.server.page_open(page)
        try:
            while not app.quit.is_set():
                current = app.wait(version, KEEP_ALIVE)  # a closed page shows when a write fails
                if current != version:
                    version = current
                    self.wfile.write(f"event: change\ndata: {version}\n\n".encode())
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.server.page_gone(page)
            self.close_connection = True

    def do_POST(self):
        if not (self.host_ok() and self.key_ok()):
            return self.send(403, {"error": "forbidden"})
        if self.headers.get("Origin") not in self.server.origins or \
                not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send(403, {"error": "forbidden"})
        if urlsplit(self.path).path != "/api/action":
            return self.send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if 0 < length <= 65536 else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.send(400, {"error": "bad request"})
        try:
            result = self.server.act(body.get("action"), body)
        except (ValueError, RuntimeError) as exc:
            return self.send(400, {"error": str(exc)})
        except Exception as exc:  # an answer, not a dropped connection the page shows as "Failed to fetch"
            self.server.app.log(f"{body.get('action')} 出错：{type(exc).__name__}: {exc}", "error")
            return self.send(500, {"error": f"出错了：{type(exc).__name__}: {exc}"})
        self.send(200, {"ok": True, "result": result})


class Window(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app):
        super().__init__(("127.0.0.1", 0), Handler)
        self.app = app
        self.key = secrets.token_urlsafe(24)
        port = self.server_address[1]
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{host}" for host in self.hosts}
        self.url = f"http://127.0.0.1:{port}/?k={self.key}"
        self.open_pages, self.seen_page, self.alone_since = set(), False, None
        self.count_lock = threading.Lock()
        self.opener, self.opened_at = None, None  # opener(): opens a window on the page
        self.show_lock = threading.Lock()
        self.banner_opener, self.banner_seen, self.banner_tried = None, None, None

    def banner_asked(self, on=True):
        """The banner window asked for notices; told they are off, it closes."""
        with self.count_lock:
            self.banner_seen = time.monotonic() if on else None
            if not on:
                self.banner_tried = None

    def keep_banner(self):
        """Start the banner window when the player wants banners and none is asking for them: after
        the player turned them on, at a start in the background (no window), or if it ended."""
        if not self.banner_opener or (self.app.settings.get("ui") or {}).get("desktop") is not True:
            return
        now = time.monotonic()
        with self.count_lock:
            if self.banner_seen is not None and now - self.banner_seen < BANNER_QUIET:
                return
            if self.banner_tried is not None and now - self.banner_tried < BANNER_RETRY:
                return
            self.banner_tried = now
        try:
            self.banner_opener()
        except OSError as exc:
            self.app.log(f"桌面弹窗没有启动：{exc}", "warn")

    def page_open(self, page):
        with self.count_lock:
            self.open_pages.add(page)
            self.seen_page, self.alone_since, self.opened_at = True, None, None

    def show(self):
        """Bring the window to the front, or open one if none is open or on its way."""
        with self.show_lock:
            with self.count_lock:
                open_now = bool(self.open_pages)
            # Only a page of this program counts: a window left by an earlier run shows nothing.
            if open_now and winsys.focus_window(TITLE):
                return "front"
            with self.count_lock:
                if self.opened_at is not None and time.monotonic() - self.opened_at < OPENING:
                    return "opening"  # a click while the window starts must not open a second window
                self.opened_at = time.monotonic()
            self.opener()
            return "opened"

    def page_gone(self, page):
        """A page closed (it said so, or its events stream broke); said twice is fine."""
        with self.count_lock:
            if page in self.open_pages:
                self.open_pages.discard(page)
                if not self.open_pages:
                    self.alone_since = time.monotonic()

    def closed(self):
        """Whether the last page has been gone a while (not merely reloading)."""
        with self.count_lock:
            return self.seen_page and not self.open_pages and self.alone_since is not None and \
                time.monotonic() - self.alone_since > CLOSED_AFTER

    def act(self, action, body):
        app = self.app
        index = body.get("index")
        if action in ("remove_token", "chat_use", "bark_bind", "ntfy_bind", "bark_unbind", "bark_test", "bark_ally", "personal", "phone", "phone_status") and type(index) is not int:
            raise ValueError("没有这个账号")
        actions = {
            "start": app.start,
            "restart_capture": app.restart,  # a capture that saw nothing after a network change
            "quit": lambda: threading.Thread(target=self.quit, daemon=True).start(),
            "show": self.show,
            "closing": lambda: self.page_gone(str(body.get("page") or "")),
            "add_token": lambda: app.add_token(body.get("token") or ""),
            "join_public": app.join_public,
            "remove_token": lambda: app.remove_token(int(index)),
            "refresh_accounts": app.poke.set,
            "bark_bind": lambda: app.bark_bind(int(index), body.get("address")),
            "ntfy_bind": lambda: app.ntfy_bind(int(index)),
            "bark_unbind": lambda: app.bark_unbind(int(index)),
            "bark_test": lambda: app.bark_test(int(index)),
            "bark_ally": lambda: app.bark_ally(int(index), body.get("op"), body.get("id"), body.get("name"),
                                               body.get("address"), body.get("kind")),
            "set_setting": lambda: app.set_setting(body.get("name"), body.get("value")),
            "banner_test": app.banner_test,
            "set_ui": lambda: app.set_ui(body.get("key"), body.get("value")),
            "set_notice_pref": lambda: app.set_notice_pref(body.get("key"), body.get("value")),
            "personal": lambda: app.personal(int(index), body.get("page"), body.get("query")),
            "phone": lambda: app.phone(int(index), reset=body.get("reset") is True, part=body.get("part")),
            "phone_status": lambda: app.phone(int(index), status=True),
            "chat_send": lambda: app.chat.send(body.get("text")),
            "chat_older": lambda: app.chat.older(body.get("before")),
            "chat_use": lambda: app.use_chat(int(index)),
            "pick_dumpcap": app.pick_dumpcap,
            "forget_dumpcap": app.forget_dumpcap,
            "open_folder": lambda: winsys.open_folder(app.state_dir),
            "check_update": app.check_update,
            "restart_for_update": lambda: self.restart_for_update(),
            "diagnostics": app.diagnostics,
            "send_diagnostics": app.send_diagnostics,
            "open_installer": lambda: open_installer(app.opener),
        }
        if not isinstance(action, str) or action not in actions:
            raise ValueError("unknown action")
        return actions[action]()

    def restart_for_update(self):
        self.app.restart_for_update()
        threading.Thread(target=self.quit, daemon=True).start()

    def quit(self):
        self.app.shutdown()
        self.shutdown()


def open_installer(opener):
    """Run a Wireshark installer shipped next to the exe, else open the download link the website gives."""
    installer = next(iter(sorted(winsys.frozen_folder().glob("Wireshark-*.exe"), reverse=True)), None)
    if installer is not None and winsys.WINDOWS:
        import os
        os.startfile(installer)
        return "installer"
    import webbrowser
    from .update import wireshark_url
    webbrowser.open(wireshark_url(opener))
    return "download"


def running_window(state_dir):
    """The URL of a window already running for this state folder, or None."""
    try:
        value = json.loads((Path(state_dir) / "window.json").read_text())
        url = value["url"]
        request = Request(url.split("/?")[0] + "/api/ping", headers={"Cookie": f"{COOKIE}={value['key']}"})
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            return url if response.status == 200 else None
    except (OSError, ValueError, KeyError):
        return None


def ask(state_dir, action):
    """Ask the program already running to do `action`: True if it did."""
    try:
        value = json.loads((Path(state_dir) / "window.json").read_text())
        origin = value["url"].split("/?")[0]
        request = Request(origin + "/api/action", data=json.dumps({"action": action}).encode(),
                          headers={"Cookie": f"{COOKIE}={value['key']}", "Origin": origin,
                                   "Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return response.status == 200
    except (OSError, ValueError, KeyError):
        return False


def ask_to_show(state_dir):
    """Ask the program already running to show its window: True if it did."""
    winsys.let_others_focus()  # this start is what the player clicked, so it may give the front away
    return ask(state_dir, "show")


def running_version(state_dir):
    """The version of the program already running, or None when none answers (or it does not say)."""
    try:
        value = json.loads((Path(state_dir) / "window.json").read_text())
        origin = value["url"].split("/?")[0]
        far = 10 ** 15  # past every notice, log line and chat message: only the version is wanted
        request = Request(f"{origin}/api/state?n={far}&l={far}&c={far}", headers={"Cookie": f"{COOKIE}={value['key']}"})
        with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
            return str(json.load(response)["app_version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def take_over(state_dir):
    """When the program already running is older than this one, ask it to quit: True if it was.
    A player who closed only the window (the program stays in the tray) and started it again
    after an update downloaded its code would otherwise get the old version's window back
    (2026-11-11 user: "updated and restarted, still the old version")."""
    running = running_version(state_dir)
    if running is None or not version(__version__) > version(running):
        return False
    return ask(state_dir, "quit")


def gone(state_dir, held=None):
    """Wait (at most TAKE_OVER seconds) for the program asked to quit to be gone -> the window
    lock (`held`, when this start already has it), or None if it is still there."""
    deadline = time.monotonic() + TAKE_OVER
    while True:
        if held is None:
            try:
                held = lock(Path(state_dir) / "window.lock")
            except RuntimeError:
                pass
        if held is not None and not running_window(state_dir):
            return held
        if time.monotonic() >= deadline:
            if held is not None:
                held.close()
            return None
        time.sleep(0.5)


def show_running(state_dir, show, wait=0):
    """Bring up the window of the program already running, which may still be starting (`wait`
    seconds): False when no window answered."""
    deadline = time.monotonic() + wait
    while True:
        url = running_window(state_dir)
        if url:
            if not ask_to_show(state_dir):
                show(url, state_dir / "window")
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def main(server, state_dir, background=False, show=None, after=None):
    """Run the window until it is closed (or quit), one program per state folder: a second start
    only brings up the first one's window, unless it is a newer version (then the first quits). `show(url, profile)` opens the page.
    `after`: the process this one restarts (for an update), which must be gone first."""
    show = show or winsys.open_window
    state_dir = Path(state_dir)
    if after:
        winsys.wait_exit(after, timeout=30)
    # window.json alone let two run: it is written seconds after the start (a double click, or a
    # click during the start at login) and a busy first one can miss the ping. Two programs would
    # also overwrite each other's saved tokens.
    try:
        held = lock(state_dir / "window.lock")
    except RuntimeError:
        held = None
    stay = False
    if held is None or running_window(state_dir):  # running_window: a version from before this lock
        taken = gone(state_dir, held) if take_over(state_dir) else None
        if taken is None:
            if held is not None:
                held.close()
            if not background:
                show_running(state_dir, show, FIRST_STARTING if held is None else 0)
            return
        # It ran in the tray: so does this one, its window closed or not.
        held, stay = taken, True
    try:
        run(server, state_dir, background, show, stay)
    finally:
        held.close()


def run(server, state_dir, background, show, stay=False):
    """The program itself, once it is the only one."""
    winsys.sharp()
    app = App(server, state_dir)
    window = Window(app)
    window.opener = lambda: show(window.url, state_dir / "window")
    window.banner_opener = lambda: winsys.open_banner(window.url, state_dir / "window")
    state_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(state_dir / "browser", ignore_errors=True)  # Edge's data, from before the window was Electron
    marker = state_dir / "window.json"
    marker.write_text(json.dumps({"url": window.url, "key": window.key}))
    threading.Thread(target=window.serve_forever, name="window", daemon=True).start()
    app.begin()
    tray = winsys.Tray(TITLE, on_open=window.show, icon=UI / "icon.ico",
                       on_quit=lambda: threading.Thread(target=window.quit, daemon=True).start())
    tray.start()
    if not background:
        window.show()
    try:
        while not app.quit.wait(1):
            window.keep_banner()
            # Started at login it stays, as does a player's choice to keep it running.
            if window.closed() and not (background or stay or app.settings["keep_running"]):
                window.quit()
    except KeyboardInterrupt:
        window.quit()
    finally:
        tray.close()
        winsys.close_windows()
        marker.unlink(missing_ok=True)
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    if app.relaunch:
        winsys.relaunch()
