"""The window: a page served on this computer only, shown by Edge as an app window.

The server listens on 127.0.0.1 at a random port, and answers only a page that presents
the key it was opened with (a cookie set from ``/?k=<key>``) and names it as its host, so
neither other sites in a browser nor other computers can use it. The page polls
``/api/state`` when ``/api/events`` (server-sent events) says something changed, and does
everything through ``POST /api/action``.

One window per computer: a second start finds the first through ``window.json`` in the
state folder and only opens its page. When the last page closes (it says so as it goes, else
its events stream breaks), the program ends, unless it was started at login (``--background``)
or the player chose to keep it running. On Windows an icon in the notification area opens
the window again or quits.
"""

import hmac
import json
import mimetypes
import secrets
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import winsys
from .app import App

UI = Path(__file__).resolve().parent / "ui"
CLOSED_AFTER = 3  # seconds without any page open before the program ends (a reload comes back sooner)
KEEP_ALIVE = 2  # seconds between writes to a quiet events stream, which find a page that is gone
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
        if url.path == "/":
            key = parse_qs(url.query).get("k", [""])[0]
            if key and hmac.compare_digest(key.encode(), self.server.key.encode()):
                cookie = f"{COOKIE}={self.server.key}; Path=/; HttpOnly; SameSite=Strict"
                return self.send(303, b"", headers=(("Location", "/"), ("Set-Cookie", cookie)))
            if not self.key_ok():
                return self.send(403, "请双击 stzb-warroom.exe 打开窗口。", "text/plain; charset=utf-8")
            return self.static("index.html")
        if url.path.startswith("/ui/"):
            return self.static(url.path[4:])
        if not self.key_ok():
            return self.send(403, {"error": "forbidden"})
        if url.path == "/api/state":
            query = parse_qs(url.query)
            numbers = [int(query.get(name, ["0"])[0] or 0) for name in ("n", "l")]
            return self.send(200, self.server.app.snapshot(*numbers))
        if url.path == "/api/events":
            return self.events(parse_qs(url.query).get("page", [""])[0] or secrets.token_hex(8))
        if url.path == "/api/ping":
            return self.send(200, {"ok": True})
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

    def page_open(self, page):
        with self.count_lock:
            self.open_pages.add(page)
            self.seen_page, self.alone_since = True, None

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
        actions = {
            "start": app.start,
            "stop": app.stop,
            "quit": lambda: threading.Thread(target=self.quit, daemon=True).start(),
            "closing": lambda: self.page_gone(str(body.get("page") or "")),
            "add_token": lambda: app.add_token(body.get("token") or ""),
            "remove_token": lambda: app.remove_token(int(index)),
            "refresh_accounts": app.poke.set,
            "bark_bind": lambda: app.bark_bind(int(index), body.get("address")),
            "bark_unbind": lambda: app.bark_unbind(int(index)),
            "bark_test": lambda: app.bark_test(int(index)),
            "set_setting": lambda: app.set_setting(body.get("name"), body.get("value")),
            "pick_dumpcap": app.pick_dumpcap,
            "forget_dumpcap": app.forget_dumpcap,
            "open_folder": lambda: winsys.open_folder(app.state_dir),
            "check_update": app.check_update,
            "diagnostics": app.diagnostics,
            "open_installer": lambda: open_installer(),
        }
        if action not in actions:
            raise ValueError(f"unknown action {action}")
        return actions[action]()

    def quit(self):
        self.app.shutdown()
        self.shutdown()


def open_installer():
    """Run a Wireshark installer shipped next to the exe, else open Wireshark's download page."""
    installer = next(iter(sorted(winsys.frozen_folder().glob("Wireshark-*.exe"), reverse=True)), None)
    if installer is not None and winsys.WINDOWS:
        import os
        os.startfile(installer)
        return "installer"
    import webbrowser
    webbrowser.open("https://www.wireshark.org/download.html")
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


def main(server, state_dir, background=False, show=None):
    """Run the window until it is closed (or quit). `show(url, profile)` opens the page."""
    show = show or winsys.open_window
    state_dir = Path(state_dir)
    url = running_window(state_dir)
    if url:
        if not background:
            show(url, state_dir / "browser")
        return
    app = App(server, state_dir)
    window = Window(app)
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "window.json"
    marker.write_text(json.dumps({"url": window.url, "key": window.key}))
    threading.Thread(target=window.serve_forever, name="window", daemon=True).start()
    app.begin()
    tray = winsys.Tray("率土战局", on_open=lambda: show(window.url, state_dir / "browser"),
                       on_quit=lambda: threading.Thread(target=window.quit, daemon=True).start())
    tray.start()
    if not background:
        show(window.url, state_dir / "browser")
    try:
        while not app.quit.wait(1):
            # Started at login it stays, as does a player's choice to keep it running.
            if window.closed() and not (background or app.settings.get("keep_running")):
                window.quit()
    except KeyboardInterrupt:
        window.quit()
    finally:
        tray.close()
        marker.unlink(missing_ok=True)
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
