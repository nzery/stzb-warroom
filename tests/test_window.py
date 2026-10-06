"""The window: its state and actions (app.py) and the page server (webui.py)."""

import io
import json
import tempfile
import threading
import unittest
import unittest.mock
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError

from stzb_warroom import app as appmod, vault, webui, winsys
from stzb_warroom.app import App

GOOD, BAD, NEW, TAKEN = "sta_" + "a" * 43, "sta_" + "b" * 43, "sta_" + "c" * 43, "sta_" + "d" * 43
ROLE = {"id": 7, "label": "甲 · 某盟 · 服务器 1"}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Offline:
    def open(self, *args, **kwargs):
        raise OSError("offline")


class FakeServer:
    origin = "https://example.test"
    opener = Offline()

    def __init__(self):
        self.calls = []

    def json(self, path, token, body=None, timeout=45):
        if token == BAD:
            raise HTTPError(path, 401, "unauthorized", {}, io.BytesIO(b"{}"))
        if token == NEW:
            return {"name": "n", "approved": False, "role": None, "bark": {"bound": False}}
        if token == TAKEN:
            return {"name": "n", "approved": True, "role": None, "bark": {"bound": False},
                    "problem": {"title": "角色 7 已被另一个 Token 绑定", "text": "角色 7 已被同域的另一个 Token 绑定"}}
        return {"name": "n", "approved": True, "role": ROLE, "bark": {"bound": False}}

    def open(self, path, token, body=None, timeout=45, method=None):
        self.calls.append((method, path, body))
        if body == {"address": "bad"}:
            raise HTTPError(path, 400, "bad", {}, io.BytesIO('{"error": "推送没有送达"}'.encode()))
        return Response(json.dumps({"bound": method != "DELETE", "address": "https://api.day.app/ABCD…"}).encode())


def plain_vault(case):
    """A vault that 'encrypts' by reversing, as if on Windows."""
    for name, value in (("AVAILABLE", True), ("protect", lambda data: data[::-1]),
                        ("unprotect", lambda data: data[::-1])):
        patcher = unittest.mock.patch.object(vault, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)


class AppTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.server = FakeServer()

    def app(self, saved="", env=""):
        """An App whose window already holds the tokens `saved`, with ST_CLIENT_TOKEN set to `env`."""
        plain_vault(self)
        if saved:
            vault.save(self.folder / "tokens.bin", saved)
        with unittest.mock.patch.dict("os.environ", {"ST_CLIENT_TOKEN": env}):
            app = App(self.server, self.folder, opener=lambda *a, **k: Response(b'{"tag_name": "v9.0.0", "html_url": "u"}'))
        app.find_dumpcap = lambda: (None, ["nowhere"])  # never capture in tests
        return app

    def test_only_the_players_tokens_are_kept_and_nothing_is_saved_unasked(self):
        plain_vault(self)
        first = self.app(env=BAD)  # ST_CLIENT_TOKEN is not the window's
        self.assertEqual(first.tokens, [])
        self.assertEqual(list(self.folder.iterdir()), [])
        first.add_token(GOOD)
        again = self.app(env=BAD)
        self.assertEqual(again.tokens, [GOOD])
        self.assertNotIn(GOOD.encode(), (self.folder / "tokens.bin").read_bytes())

    def test_tokens_are_checked_before_they_are_kept(self):
        app = self.app()
        for text in ("", "hello", "sta_short"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                app.add_token(text)
        app.add_token(f" {GOOD} , {NEW} ")
        self.assertEqual(app.tokens, [GOOD, NEW])

    def test_accounts_show_what_the_server_says_and_never_a_whole_token(self):
        app = self.app(saved=f"{GOOD},{BAD},{NEW},{TAKEN}")
        app.check_accounts()
        app.on_notice({"title": "t", "level": "alarm", "role": 7})
        snapshot = app.snapshot()
        self.assertEqual([a["state"] for a in snapshot["tokens"]], ["ok", "invalid", "pending", "problem"])
        self.assertIn("另一个 Token", snapshot["tokens"][3]["problem"]["text"])
        self.assertEqual(snapshot["tokens"][0]["role"], ROLE)
        self.assertEqual(snapshot["tokens"][1]["token"], "sta_…bbbb")
        self.assertEqual(snapshot["notices"][0]["title"], "t")
        self.assertEqual(app.snapshot(notices_after=snapshot["notices"][0]["seq"])["notices"], [])
        text = json.dumps(snapshot) + app.diagnostics()
        for token in (GOOD, BAD, NEW, TAKEN):
            self.assertNotIn(token, text)

    def test_a_hint_goes_when_the_server_drops_it(self):
        app = self.app(saved=GOOD)
        show = app.prompt_for(GOOD)
        show("[上报] 角色 7 已被另一个 Token 绑定", None)
        self.assertEqual(len(app.snapshot()["hints"]), 1)
        show(None, None)
        self.assertEqual(app.snapshot()["hints"], [])

    def test_bark_is_bound_through_the_server(self):
        app = self.app(saved=GOOD)
        app.check_accounts()
        self.assertTrue(app.bark_bind(0, "https://api.day.app/KEY/")["bound"])
        self.assertEqual(self.server.calls[-1], ("POST", "/v1/client/bark", {"address": "https://api.day.app/KEY/"}))
        self.assertTrue(app.snapshot()["tokens"][0]["bark"]["bound"])
        with self.assertRaisesRegex(ValueError, "没有送达"):
            app.bark_bind(0, "bad")
        self.assertFalse(app.bark_unbind(0)["bound"])
        self.assertEqual(self.server.calls[-1][:2], ("DELETE", "/v1/client/bark"))
        with self.assertRaises(ValueError):
            app.bark_test(5)

    def test_a_missing_dumpcap_stops_with_a_hint(self):
        app = self.app(saved=GOOD)
        app.start()
        capture = app.snapshot()["capture"]
        self.assertEqual(capture["state"], "error")
        self.assertIn("Wireshark", capture["hint"])

    def test_newer_releases_are_noticed(self):
        app = self.app()
        self.assertEqual(app.check_update(), {"latest": "9.0.0", "newer": True})
        self.assertEqual(app.snapshot()["update"], {"version": "9.0.0", "url": "u"})


class PageServerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        plain_vault(self)
        vault.save(Path(folder.name) / "tokens.bin", GOOD)
        self.app = App(FakeServer(), folder.name)
        self.window = webui.Window(self.app)
        threading.Thread(target=self.window.serve_forever, daemon=True).start()
        self.addCleanup(self.window.server_close)
        self.addCleanup(self.window.shutdown)
        self.port = self.window.server_address[1]

    def request(self, method, path, headers=None, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data

    def cookie(self):
        return {"Cookie": f"{webui.COOKIE}={self.window.key}"}

    def test_a_closed_page_is_noticed_and_a_reload_is_not(self):
        with unittest.mock.patch.object(webui, "CLOSED_AFTER", -1):  # no wait (and no clock resolution)
            self.assertFalse(self.window.closed())  # no page yet: nothing to close
            self.window.page_open("a")
            self.window.act("closing", {"page": "a"})  # the page says it is going
            self.window.page_open("b")  # ...and comes back (a reload)
            self.window.page_gone("a")  # the old stream breaks late
            self.assertFalse(self.window.closed())
            self.window.act("closing", {"page": "b"})
            self.assertTrue(self.window.closed())

    def test_one_window_at_a_time(self):
        opened, front = [], []
        self.window.opener = lambda: opened.append(1)
        with unittest.mock.patch.object(winsys, "focus_window", lambda title: front.append(title) or True):
            self.assertEqual(self.window.show(), "opened")
            self.assertEqual(self.window.show(), "opening")  # clicked again while the window starts
            self.window.page_open("a")
            self.assertEqual(self.window.show(), "front")
            self.assertEqual(front, [webui.TITLE])
            self.window.act("closing", {"page": "a"})  # closed to the tray
            self.assertEqual(self.window.show(), "opened")
        self.assertEqual(opened, [1, 1])
        with unittest.mock.patch.object(webui, "OPENING", -1):  # the window never came: open again
            self.assertEqual(self.window.show(), "opened")

    def test_a_second_start_asks_the_first_to_show_its_window(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        (Path(folder.name) / "window.json").write_text(json.dumps({"url": self.window.url, "key": self.window.key}))
        shown = []
        self.window.opener = lambda: shown.append("first")
        webui.main(None, folder.name, show=lambda url, profile: shown.append("second"))
        self.assertEqual(shown, ["first"])

    def test_the_page_needs_the_key(self):
        self.assertEqual(self.request("GET", "/")[0], 403)
        self.assertEqual(self.request("GET", "/api/state")[0], 403)
        status, headers, _ = self.request("GET", "/?k=" + self.window.key)
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.request("GET", "/", self.cookie())[0], 200)
        status, _, data = self.request("GET", "/api/state", self.cookie())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["tokens"][0]["token"], "sta_…aaaa")

    def test_other_hosts_and_sites_are_refused(self):
        self.assertEqual(self.request("GET", "/", {"Host": "evil.test", **self.cookie()})[0], 403)
        action = json.dumps({"action": "refresh_accounts"})
        json_type = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/action", {**self.cookie(), **json_type}, action)[0], 403)
        origin = {"Origin": "https://evil.test"}
        self.assertEqual(self.request("POST", "/api/action", {**self.cookie(), **json_type, **origin}, action)[0], 403)
        origin = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.request("POST", "/api/action", {**self.cookie(), **json_type, **origin}, action)[0], 200)
        bad = json.dumps({"action": "rm -rf"})
        status, _, data = self.request("POST", "/api/action", {**self.cookie(), **json_type, **origin}, bad)
        self.assertEqual(status, 400)

    def test_the_window_is_electron_from_source_too(self):
        with unittest.mock.patch.dict("os.environ", {"ST_ELECTRON": "/opt/electron/electron"}):
            command = winsys.window_command()
        self.assertEqual(command[0], "/opt/electron/electron")
        self.assertTrue((Path(command[1]) / "main.js").is_file())

    def test_only_the_ui_folder_is_served(self):
        self.assertEqual(self.request("GET", "/ui/app.css")[0], 200)
        for path in ("/ui/../app.py", "/ui/%2e%2e/app.py", "/ui/missing.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(winsys.WINDOWS, "the tray icon is Windows only")
class TrayTests(unittest.TestCase):
    def test_the_icon_comes_and_goes(self):
        opened = []
        tray = winsys.Tray("率土战局 测试", on_open=lambda: opened.append(1), on_quit=lambda: None)
        tray.start()
        self.assertTrue(tray.hwnd)
        tray._procedure(tray.hwnd, 0x8000 + 1, 0, 0x0202)  # a click on the icon
        self.assertEqual(opened, [1])
        tray.close()
        tray.thread.join(5)
        self.assertFalse(tray.thread.is_alive())

