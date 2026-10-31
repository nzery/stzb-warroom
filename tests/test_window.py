"""The window: its state and actions (app.py) and the page server (webui.py)."""

import io
import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError

from stzb_warroom import app as appmod, vault, webui, winsys
from stzb_warroom.app import App

GOOD, BAD, NEW, TAKEN = "sta_" + "a" * 43, "sta_" + "b" * 43, "sta_" + "c" * 43, "sta_" + "d" * 43
ROLE = {"id": 7, "label": "甲 · 某盟 · 服务器 1"}
INVITE = "stj_" + "i" * 22


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
        self.prefs = {}  # token -> its notice settings on the server

    def json(self, path, token, body=None, timeout=45):
        if token == BAD:
            raise HTTPError(path, 401, "unauthorized", {}, io.BytesIO(b"{}"))
        if path == "/v1/client/notice-prefs":
            self.calls.append(("POST" if body is not None else "GET", path, token, body))
            values = self.prefs.setdefault(token, {"mode": "passive", "skip_spartan": True})
            if body is not None:
                if body["values"].get("mode") not in ("passive", "active"):
                    raise HTTPError(path, 400, "bad", {}, io.BytesIO('{"error": "提醒方式 的取值不对"}'.encode()))
                values.update(body["values"])
            return {"note": "说明", "groups": [{"title": "敌情", "items": []}], "values": dict(values)}
        if path.startswith("/v1/client/personal/"):
            self.calls.append(("GET", path, token, None))
            if token == NEW:
                raise HTTPError(path, 403, "forbidden", {}, io.BytesIO(b'{"error": "account not bound to token"}'))
            return {"note": "说明", "filters": [], "cards": [{"title": "曹操队"}], "empty": None, "page": None}
        if token == NEW:
            return {"name": "n", "approved": False, "role": None, "bark": {"bound": False}}
        if token == TAKEN:
            return {"name": "n", "approved": True, "role": None, "bark": {"bound": False},
                    "problem": {"title": "角色 7 已被另一个 Token 绑定", "text": "角色 7 已被同域的另一个 Token 绑定"}}
        return {"name": "n", "approved": True, "role": ROLE, "bark": {"bound": False}}

    def join(self, code, tokens=()):
        self.calls.append(("JOIN", code, list(tokens)))
        if code != INVITE:
            raise ValueError("邀请码无效")
        return {"token": NEW, "text": "已加入「某盟」"}

    def open(self, path, token, body=None, timeout=45, method=None):
        self.calls.append((method, path, body))
        if path.startswith("/v1/client/phone"):
            if token == NEW:
                return Response(json.dumps({"problem": "还没审核通过", "nodes": [], "relays": []}).encode())
            key = "k2" if path.endswith("/reset") else "k1"
            return Response(json.dumps({"problem": None, "nodes": [{"name": "率土战局-主节点", "link": f"vless://{key}@r"}],
                                        "relays": ["https://r.example"]}).encode())
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
            app = App(self.server, self.folder, opener=lambda *a, **k: Response('{"tag_name": "v9.0.0", "html_url": "u", "body": "本次更新……"}'.encode()))
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

    def test_notifications_wait_for_an_approved_role(self):
        app = self.app(saved=GOOD)
        ready = app.notify_ready(GOOD)
        self.assertFalse(ready())  # not checked yet
        for value, expected in (({"state": "pending", "approved": False}, False),
                                ({"state": "no_role", "approved": True, "role": None}, False),
                                ({"state": "ok", "approved": True, "role": {"id": 1}}, True),
                                ({"state": "offline", "error": "HTTP 502"}, True)):  # the stream says why
            with self.subTest(value=value):
                app.accounts[GOOD] = value
                self.assertEqual(ready(), expected)

    def test_tokens_are_checked_before_they_are_kept(self):
        app = self.app()
        for text in ("", "hello", "sta_short"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                app.add_token(text)
        app.add_token(f" {GOOD} , {NEW} ")
        self.assertEqual(app.tokens, [GOOD, NEW])

    def test_an_invitation_code_is_traded_for_a_token(self):
        app = self.app()
        self.assertEqual(app.add_token(f" {INVITE} "), {"joined": "已加入「某盟」"})
        self.assertEqual(app.tokens, [NEW])
        with self.assertRaisesRegex(ValueError, "邀请码无效"):
            app.add_token("stj_" + "x" * 22)
        self.assertEqual(app.tokens, [NEW])
        self.assertIn(("JOIN", "stj_" + "x" * 22, [NEW]), app.server.calls)  # what it holds goes along

    def test_the_page_settings_outlive_a_restart(self):
        # 2026-10-14 user: the page's own storage is emptied at each start (another port).
        app = self.app()
        app.set_ui("sound", False)
        app.set_ui("dismissed", ["a"])
        self.assertEqual(self.app().settings["ui"], {"sound": False, "dismissed": ["a"]})
        with self.assertRaises(ValueError):
            app.set_ui("", 1)

    def test_background_default_and_saved_choice(self):
        app = self.app()
        self.assertTrue(app.snapshot()["settings"]["keep_running"])
        app.set_setting("keep_running", False)
        self.assertFalse(self.app().snapshot()["settings"]["keep_running"])
        app.set_setting("keep_running", True)
        self.assertTrue(self.app().snapshot()["settings"]["keep_running"])

    def test_bad_account_index_does_not_remove_another_account(self):
        app = self.app(saved=f"{GOOD},{NEW}")
        for index in (-1, 2):
            with self.subTest(index=index), self.assertRaises(ValueError):
                app.remove_token(index)
        self.assertEqual(app.tokens, [GOOD, NEW])

    def test_capture_setup_failure_stops_notification_workers(self):
        app = self.app(saved=GOOD)
        session = app.session = {"stop": threading.Event(), "uploader": None, "tokens": app.tokens}
        with unittest.mock.patch.object(appmod, "run_capture", side_effect=RuntimeError("locked")):
            app._capture(session, "dumpcap")
        self.assertTrue(session["stop"].is_set())
        self.assertIsNone(app.session)
        self.assertEqual(app.capture["state"], "error")

    def test_a_rejected_token_is_looked_at_again_only_when_asked_or_much_later(self):
        app = self.app(saved=f"{GOOD},{BAD}")
        with unittest.mock.patch.object(app.server, "json", wraps=app.server.json) as asked:
            app.check_accounts()
            app.check_accounts()
            self.assertEqual([c.args[1] for c in asked.call_args_list if c.args[0] == "/v1/client/status"].count(BAD), 1)
            app.check_accounts(asked=True)
            app.accounts[BAD]["checked"] -= appmod.INVALID_EVERY
            app.check_accounts()
            self.assertEqual([c.args[1] for c in asked.call_args_list if c.args[0] == "/v1/client/status"].count(BAD), 3)
        self.assertEqual(app.accounts[BAD]["state"], "invalid")

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
        # notices leave once past the time the server gave them
        app.on_notice({"title": "过期", "level": "info", "expires": time.time() - 1})
        app.on_notice({"title": "将过期", "level": "info", "expires": time.time() + 600})
        self.assertEqual([n["title"] for n in app.snapshot()["notices"]], ["t", "将过期"])
        app.notices[-1]["expires"] = time.time() - 1
        self.assertEqual([n["title"] for n in app.snapshot()["notices"]], ["t"])
        text = json.dumps(snapshot) + app.diagnostics()
        for token in (GOOD, BAD, NEW, TAKEN):
            self.assertNotIn(token, text)

    def test_a_new_launch_numbers_notices_past_the_last_one(self):
        # 2026-10-16 user: after the program restarted no notice showed. The page keeps "cleared up
        # to" (and its last number while it stays open) across launches; counting from 0 again put
        # every new notice below it.
        app = self.app()
        app.on_notice({"title": "旧", "level": "info"})
        cleared = app.snapshot()["notices"][-1]["seq"]
        time.sleep(0.05)  # a launch takes far longer than a millisecond for each line the last one numbered
        again = self.app()
        again.on_notice({"title": "新", "level": "info"})
        self.assertEqual([n["title"] for n in again.snapshot(notices_after=cleared)["notices"]], ["新"])

    def test_a_hint_goes_when_the_server_drops_it(self):
        app = self.app(saved=GOOD)
        show = app.prompt_for(GOOD)
        show("[上报] 角色 7 已被另一个 Token 绑定", None)
        self.assertEqual(len(app.snapshot()["hints"]), 1)
        show(None, None)
        self.assertEqual(app.snapshot()["hints"], [])

    def test_notice_settings_are_the_same_for_every_account(self):
        app = self.app(saved=f"{GOOD},{NEW},{BAD}")
        app.check_accounts()
        prefs = app.snapshot()["notice_prefs"]
        self.assertEqual((prefs["note"], prefs["values"]), ("说明", {"mode": "passive", "skip_spartan": True}))
        self.assertIsNone(prefs["error"])  # the invalid token is not asked
        self.assertEqual(app.set_notice_pref("skip_spartan", False), {"mode": "passive", "skip_spartan": False})
        self.assertEqual(self.server.prefs[GOOD], self.server.prefs[NEW])
        self.assertNotIn(BAD, self.server.prefs)
        with self.assertRaisesRegex(ValueError, "取值不对"):
            app.set_notice_pref("mode", "sometimes")
        with self.assertRaisesRegex(ValueError, "没有这个设置"):
            app.set_notice_pref("nonsense", True)
        posts = lambda: [c[2] for c in self.server.calls if c[0] == "POST" and c[1] == "/v1/client/notice-prefs"]
        # a token added here is given this computer's choice, once
        again = self.app()
        again.add_token(THIRD := "sta_" + "e" * 43)
        again.check_accounts()
        self.assertFalse(self.server.prefs[THIRD]["skip_spartan"])
        self.assertEqual(posts()[-1], THIRD)
        sent = len(posts())
        again.check_accounts()
        self.assertEqual(len(posts()), sent)
        # changed elsewhere (another computer with the same token): followed, never sent back
        for token in (GOOD, NEW, THIRD):
            self.server.prefs[token]["skip_spartan"] = True
        again.check_accounts()
        again.check_accounts()
        self.assertEqual(len(posts()), sent)
        self.assertTrue(self.server.prefs[NEW]["skip_spartan"])
        self.assertTrue(again.snapshot()["notice_prefs"]["values"]["skip_spartan"])
        self.assertTrue(self.app().settings["notice_prefs"]["skip_spartan"])  # the choice follows it too
        # a token the save did not reach is given it at the next look, and only it
        real = self.server.json
        self.server.json = lambda path, token, body=None, timeout=45: (
            (_ for _ in ()).throw(OSError("down")) if token == NEW and body else real(path, token, body, timeout))
        again.set_notice_pref("mode", "active")
        self.assertIn("没保存上", again.snapshot()["notice_prefs"]["error"])
        self.server.json = real
        sent = len(posts())
        later = self.app()  # even after a restart
        later.check_accounts()
        self.assertEqual(posts()[sent:], [NEW])
        self.assertEqual(self.server.prefs[NEW]["mode"], "active")
        self.assertNotIn(NEW, json.dumps(later.settings))

    def test_a_look_under_way_does_not_undo_a_save(self):
        # Codex 2026-10-21: the minute's look and a save ran side by side; a look that read the old
        # settings before the save ended after it, and the page showed (and the next save sent) the
        # old values again. A token added during a look was dropped from those due the choice.
        app = self.app(saved=f"{GOOD},{NEW}")
        app.check_accounts()
        reading, release, real = threading.Event(), threading.Event(), self.server.json

        def slow(path, token, body=None, timeout=45):
            value = real(path, token, body, timeout)
            if body is None and token == GOOD and not release.is_set():
                reading.set()
                release.wait(5)
            return value
        self.server.json = slow
        look = threading.Thread(target=app.sync_notice_prefs)
        look.start()
        self.assertTrue(reading.wait(5))
        app.add_token(THIRD := "sta_" + "e" * 43)
        release.set()
        look.join(5)
        self.assertIn(appmod.tag(THIRD), app._prefs_due())
        reading.clear()
        release.clear()
        look = threading.Thread(target=app.sync_notice_prefs)
        look.start()
        self.assertTrue(reading.wait(5))
        save = threading.Thread(target=app.set_notice_pref, args=("skip_spartan", False))
        save.start()
        time.sleep(0.2)
        release.set()
        look.join(5)
        save.join(5)
        self.server.json = real
        self.assertFalse(app.snapshot()["notice_prefs"]["values"]["skip_spartan"])
        self.assertFalse(app.settings["notice_prefs"]["skip_spartan"])
        self.assertFalse(self.server.prefs[GOOD]["skip_spartan"])

    def test_the_players_page_is_the_servers(self):
        app = self.app(saved=f"{GOOD},{NEW}")
        page = app.personal(0, "players", {"name": "张三", "page": 2, "x": True, "y": None})
        self.assertEqual(page["cards"], [{"title": "曹操队"}])
        self.assertEqual(self.server.calls[-1][:3],
                         ("GET", "/v1/client/personal/players?name=%E5%BC%A0%E4%B8%89&page=2", GOOD))
        for gone in ("teams", "../admin/tokens"):  # 我的队伍 and the others are no longer pages here
            with self.assertRaisesRegex(ValueError, "没有这个页面"):
                app.personal(0, gone)
        with self.assertRaisesRegex(ValueError, "还没审批"):
            app.personal(1, "players")
        with self.assertRaisesRegex(ValueError, "没有这个账号"):
            app.personal(5, "players")

    def test_the_phone_page_is_the_servers_and_never_logged(self):
        app = self.app(saved=f"{GOOD},{NEW}")
        page = app.phone(0)
        self.assertEqual(self.server.calls[-1], ("GET", "/v1/client/phone", None))
        self.assertEqual(page["nodes"][0]["link"], "vless://k1@r")
        page = app.phone(0, reset=True)
        self.assertEqual(self.server.calls[-1], ("POST", "/v1/client/phone/reset", {}))
        self.assertEqual(page["nodes"][0]["link"], "vless://k2@r")
        self.assertEqual(app.phone(1)["problem"], "还没审核通过")
        self.assertNotIn("k1", json.dumps(list(app.logs)) + app.diagnostics())
        with self.assertRaisesRegex(ValueError, "没有这个账号"):
            app.phone(5)

    def test_the_phone_exit_is_off_until_asked_and_follows_the_capture(self):
        app = self.app(saved=GOOD)
        self.assertFalse(app.snapshot()["settings"]["phone_exit"])
        self.assertFalse(app.snapshot()["phone"]["enabled"])

        class FakeExit:
            def __init__(self, origin):
                self.origin, self.state, self.since, self.open_pipes, self.closed = origin, "online", 1.0, 2, False

            def close(self):
                self.closed = True

        made = []

        def start(origins, tokens, log=None):
            made.extend(FakeExit(origin) for origin in origins)
            return list(made)

        session = {"stop": threading.Event(), "tokens": [GOOD], "exits": []}
        with unittest.mock.patch.object(appmod.relay, "relays_of", return_value=["https://r.example"]), \
                unittest.mock.patch.object(appmod.relay, "start", side_effect=start):
            app.session = session
            app.set_setting("phone_exit", True)
            for _ in range(100):
                if session["exits"]:
                    break
                time.sleep(0.01)
            status = app.snapshot()["phone"]
            self.assertEqual(status["exits"], [{"relay": "r.example", "state": "online", "since": 1.0, "pipes": 2}])
            self.assertIn("r.example", app.diagnostics())
            app.set_setting("phone_exit", False)
            self.assertTrue(made[0].closed)
            self.assertEqual(session["exits"], [])
        self.assertFalse(App(self.server, self.folder).settings["phone_exit"])  # saved off again
        session["stop"].set()

    def test_a_relay_named_later_gets_its_exit(self):
        app = self.app(saved=GOOD)

        class FakeExit:
            def __init__(self, origin):
                self.origin, self.state, self.since, self.open_pipes = origin, "online", 1.0, 0

            def close(self):
                pass

        answers = [["https://a.example"], ["https://a.example"], ["https://b.example", "https://a.example"]]
        session = {"stop": threading.Event(), "tokens": [GOOD], "exits": []}
        with unittest.mock.patch.object(appmod.relay, "relays_of", side_effect=lambda *_: answers.pop(0) if answers
                                        else ["https://b.example", "https://a.example"]), \
                unittest.mock.patch.object(appmod.relay, "start", side_effect=lambda origins, *_a, **_k: [
                    FakeExit(o) for o in origins]), \
                unittest.mock.patch.object(appmod, "RELAYS_REFRESH", 0.01):
            app.session = session
            app.set_setting("phone_exit", True)
            for _ in range(200):
                if len(session["exits"]) == 2:
                    break
                time.sleep(0.01)
            self.assertEqual([e.origin for e in session["exits"]], ["https://a.example", "https://b.example"])
            app.set_setting("phone_exit", False)
        session["stop"].set()

    def test_the_exits_say_a_failing_relay_once_in_a_while(self):
        app = self.app()
        for _ in range(3):
            app._exit_log(event="relay_unreachable", relay="https://r.example", error="x")
        app._exit_log(event="game_check", ok=False)
        texts = [line["text"] for line in app.logs]
        self.assertEqual(sum("连不上 r.example" in t for t in texts), 1)
        self.assertTrue(any("拦下" in t for t in texts))

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
        app.ntfy_bind(0)  # an Android phone: the server makes up its topic
        self.assertEqual(self.server.calls[-1], ("POST", "/v1/client/bark", {"kind": "ntfy"}))

    def test_a_missing_dumpcap_stops_with_a_hint(self):
        app = self.app(saved=GOOD)
        app.start()
        capture = app.snapshot()["capture"]
        self.assertEqual(capture["state"], "error")
        self.assertIn("Wireshark", capture["hint"])

    def test_newer_releases_are_noticed(self):
        app = self.app()
        with unittest.mock.patch.object(appmod.updates, "site", lambda: None):
            self.assertEqual(app.check_update(), {"latest": "9.0.0", "newer": True})
        self.assertEqual(app.snapshot()["update"], {"version": "9.0.0", "url": "u", "code_only": False,
                                                    "state": "available", "error": None, "notes": "本次更新……"})
        self.assertIsNone(app.snapshot()["updated"])  # still the old version
        # the first start of that version says what it brought
        with unittest.mock.patch.object(appmod, "__version__", "9.0.0"):
            first = self.app()
            self.assertEqual(first.snapshot()["updated"], {"version": "9.0.0", "notes": "本次更新……"})
            self.assertEqual(first.snapshot()["updated"], {"version": "9.0.0", "notes": "本次更新……"})  # that run
            self.assertIsNone(self.app().snapshot()["updated"])  # once (2026-10-14 user): not at the next start
        with unittest.mock.patch.object(appmod, "__version__", "9.0.1"):
            self.assertIsNone(self.app().snapshot()["updated"])
        # updated by a version that kept nothing: the newest release's notes, once it is this version
        (self.folder / "settings.json").unlink()
        with unittest.mock.patch.object(appmod, "__version__", "9.0.0"), \
                unittest.mock.patch.object(appmod.updates, "__version__", "9.0.0"), \
                unittest.mock.patch.object(appmod.updates, "site", lambda: None):
            fresh = self.app()
            self.assertIsNone(fresh.snapshot()["updated"])
            self.assertEqual(fresh.check_update(), {"latest": "9.0.0", "newer": False})
            self.assertEqual(fresh.snapshot()["updated"], {"version": "9.0.0", "notes": "本次更新……"})
        with self.assertRaisesRegex(ValueError, "还没有下载好"):
            app.restart_for_update()
        self.assertFalse(app.relaunch)


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
        # the page's settings, kept by the program
        self.assertEqual(self.request("GET", "/api/ui")[0], 403)
        self.app.set_ui("sound", False)
        status, _, data = self.request("GET", "/api/ui", self.cookie())
        self.assertEqual((status, json.loads(data)), (200, {"sound": False}))

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

    def test_malformed_actions_and_cursors_return_errors(self):
        headers = {**self.cookie(), "Content-Type": "application/json",
                   "Origin": f"http://127.0.0.1:{self.port}"}
        for body in ({"action": []}, {"action": "remove_token"},
                     {"action": "remove_token", "index": -1},
                     {"action": "remove_token", "index": 3},
                     {"action": "remove_token", "index": False}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/action", headers, json.dumps(body))[0], 400)
        self.assertEqual(self.app.tokens, [GOOD])
        self.assertEqual(self.request("GET", "/api/state?n=invalid", self.cookie())[0], 400)
        self.assertEqual(self.request("GET", "/api/state?c=x", self.cookie())[0], 400)
        # the chat's actions: what the page sends is checked, never sent on unchecked
        for body in ({"action": "chat_send"}, {"action": "chat_send", "text": ["x"]},
                     {"action": "chat_send", "text": "好"},  # not connected (no approved account here)
                     {"action": "chat_older", "before": "1"}):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/action", headers, json.dumps(body))[0], 400)

    def test_the_chat_in_the_state(self):
        for n in range(40):
            self.app.chat._keep({"name": "甲", "text": str(n), "time": n, "mine": False})
        status, _, data = self.request("GET", "/api/state", self.cookie())
        view = json.loads(data)["chat"]
        self.assertEqual((status, len(view["messages"]), view["messages"][-1]["text"]), (200, 30, "39"))
        last = view["messages"][-2]["seq"]
        view = json.loads(self.request("GET", f"/api/state?c={last}", self.cookie())[2])["chat"]
        self.assertEqual([m["text"] for m in view["messages"]], ["39"])

    def test_the_chat_uses_an_approved_role_first_else_whichever_the_server_lets_chat(self):
        tokens = ["sta_" + c * 43 for c in "abc"]
        with self.app.lock:
            self.app.tokens[:] = tokens
            self.app.accounts.clear()
        self.assertIsNone(self.app.chat_token())  # nothing checked yet
        with self.app.lock:
            self.app.accounts.update({tokens[0]: {"state": "invalid", "chat": True},
                                      tokens[1]: {"state": "pending", "chat": True}, tokens[2]: {"state": "no_role"}})
        self.assertEqual(self.app.chat_token(), tokens[1])  # not bound yet: chats when the server says so
        with self.app.lock:
            self.app.accounts[tokens[2]] = {"state": "ok", "chat": True}
        self.assertEqual(self.app.chat_token(), tokens[2])

    def test_the_window_is_electron_from_source_too(self):
        with unittest.mock.patch.dict("os.environ", {"ST_ELECTRON": "/opt/electron/electron"}):
            command = winsys.window_command()
        self.assertEqual(command[0], "/opt/electron/electron")
        self.assertTrue((Path(command[1]) / "main.js").is_file())

    def test_only_the_ui_folder_is_served(self):
        self.assertEqual(self.request("GET", "/ui/app.css")[0], 200)
        self.assertEqual(self.request("GET", "/ui/classic.css")[0], 200)
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
