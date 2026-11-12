"""The group chat (chat.py): what is kept, what the page is given, the stream and sending."""

import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError

from stzb_warroom import chat
from stzb_warroom.chat import Chat, message_of

TOKEN = "sta_" + "a" * 43


class Stream(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def sse(*events):
    return Stream("".join(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                          for kind, data in events).encode())


class Server:
    def __init__(self, stream=None, reply=None):
        self.stream, self.reply, self.sent, self.opened = stream, reply, [], []

    def open(self, path, token, timeout=45):
        assert path.startswith(chat.STREAM + "?after=")
        self.opened.append(path)
        if isinstance(self.stream, Exception):
            raise self.stream
        return self.stream

    def json(self, path, token, body=None, timeout=45):
        assert path in (chat.SEND, chat.RECALL, chat.PREFS, chat.WEB_LOGOUT)
        self.sent.append(body)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class ChatTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.token = TOKEN

    def chat(self, server):
        return Chat(server, self.dir, lambda: self.token)

    def test_what_comes_is_checked(self):
        self.assertIsNone(message_of({"name": "甲", "text": "", "time": 1}))
        self.assertIsNone(message_of({"name": ["x"], "text": "a", "time": 1}))
        self.assertIsNone(message_of({"name": "甲", "text": "a", "time": True}))
        self.assertIsNone(message_of("text"))
        self.assertEqual(message_of({"name": "甲\x1b", "text": "<b>a‮</b>", "time": 5, "extra": 1}),
                         {"id": None, "name": "甲", "text": "<b>a</b>", "time": 5.0, "mine": False, "official": False, "level": 0})
        # the administrator's, marked by the server: kept marked; anything but true is not
        self.assertTrue(message_of({"name": "率土战局", "text": "a", "time": 1, "official": True})["official"])
        self.assertFalse(message_of({"name": "率土战局", "text": "a", "time": 1, "official": "true"})["official"])
        # how much the sender plays: 1-3 kept, anything else the plain avatar
        for value, kept in ((1, 1), (3, 3), (0, 0), (4, 0), ("2", 0), (True, 0), (2.0, 0)):
            self.assertEqual(message_of({"name": "甲", "text": "a", "time": 1, "level": value})["level"], kept)
        # a 群管理员 (server 2026-11-12): kept marked only when true, and in the file
        staff = message_of({"name": "姜君", "text": "a", "time": 1, "level": 3, "staff": True})
        self.assertEqual((staff["staff"], staff["level"]), (True, 3))
        self.assertNotIn("staff", message_of({"name": "姜君", "text": "a", "time": 1, "staff": "true"}))
        self.assertTrue(message_of(json.loads(Chat._line(dict(staff, seq=1))))["staff"])
        for bad in (0, -1, "3", 2.0, True):
            self.assertIsNone(message_of({"id": bad, "name": "甲", "text": "a", "time": 1})["id"])

    def test_the_stream_keeps_messages_and_follows_the_count(self):
        stream = sse(("hello", {"conn": "c1", "name": "沉稳的赤兔", "online": 3}), ("msg", {"bad": 1}),
                     ("msg", {"id": 1, "name": "机灵的白马", "text": "集合", "time": 100}),
                     ("online", {"online": 2}), ("online", {"online": "x"}))
        room = self.chat(Server(stream))
        room.follow_once(TOKEN, threading.Event())
        view = room.view()
        self.assertEqual((view["state"], view["name"], view["online"]), ("online", "沉稳的赤兔", 2))
        self.assertEqual([(m["name"], m["text"], m["mine"]) for m in view["messages"]], [("机灵的白马", "集合", False)])
        self.assertEqual(room.conn, "c1")

    def test_what_came_while_away_is_asked_for_and_kept_once(self):
        mine = {"id": 5, "name": "沉稳的赤兔", "text": "我说的", "time": 150}
        room = self.chat(Server(sse(("hello", {"conn": "c1", "name": "沉稳的赤兔", "online": 1}),
                                    ("msg", {"id": 4, "name": "机灵的白马", "text": "一", "time": 140}))))
        room.follow_once(TOKEN, threading.Event())
        self.assertEqual(room.server.opened, [chat.STREAM + "?after=0&recall=1"])  # never connected: the recent ones
        room.server.reply = mine
        room.send("我说的")
        # Started again: asks after the newest it got (not its own: one of the others' may be before it),
        # and what it has already is not kept twice.
        again = self.chat(Server(sse(("hello", {"conn": "c2", "name": "沉稳的赤兔", "online": 1}),
                                     ("msg", {"id": 4, "name": "机灵的白马", "text": "一", "time": 140}),
                                     ("msg", mine), ("msg", {"id": 6, "name": "机灵的白马", "text": "二", "time": 160}))))
        again.follow_once(TOKEN, threading.Event())
        self.assertEqual(again.server.opened, [chat.STREAM + "?after=4&recall=1"])
        self.assertEqual([(m["text"], m["mine"]) for m in again.messages], [("一", False), ("我说的", True), ("二", False)])
        self.assertEqual(again.after, 6)

    def test_what_the_account_sent_elsewhere_shows_as_its_own(self):
        room = self.chat(Server(sse(("hello", {"conn": "c1", "name": "沉稳的赤兔"}),
                                    ("msg", {"id": 7, "name": "沉稳的赤兔", "text": "后台回的", "time": 170, "mine": True}),
                                    ("msg", {"id": 8, "name": "机灵的白马", "text": "谢谢", "time": 180, "mine": "yes"}))))
        room.follow_once(TOKEN, threading.Event())
        self.assertEqual([(m["text"], m["mine"]) for m in room.messages], [("后台回的", True), ("谢谢", False)])
        self.assertEqual(room.after, 8)

    def test_sending(self):
        room = self.chat(Server(reply={"id": 3, "name": "沉稳的赤兔", "text": "好", "time": 200}))
        with self.assertRaises(ValueError):
            room.send("好")  # not connected yet
        room.conn = "c1"
        for bad in ("", "   ", None, 5, "字" * 501):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                room.send(bad)
        message = room.send(" 好 ")
        self.assertEqual(room.server.sent, [{"conn": "c1", "text": "好"}])
        self.assertTrue(message["mine"])
        self.token = None
        with self.assertRaises(ValueError):
            room.send("好")

    def test_a_recalled_message_is_forgotten_with_its_image(self):
        name = "b" * 32 + ".webp"
        picture = {"id": 4, "name": "机灵的白马", "text": "[图片]", "time": 140, "image": {"name": name, "w": 3, "h": 2}}
        room = self.chat(Server(sse(("hello", {"conn": "c1", "name": "沉稳的赤兔", "recall": 600}),
                                    ("msg", {"id": 3, "name": "机灵的白马", "text": "一", "time": 130}), ("msg", picture),
                                    ("msg", {"id": 4, "name": "机灵的白马", "text": "撤回了一条消息", "time": 140, "recalled": True}),
                                    # recalled while away, never had here: nothing shown
                                    ("msg", {"id": 2, "name": "机灵的白马", "text": "撤回了一条消息", "time": 120, "recalled": True}),
                                    # new to this client and already recalled: shown recalled
                                    ("msg", {"id": 5, "name": "机灵的白马", "text": "撤回了一条消息", "time": 150, "recalled": True}))))
        (self.dir / "chat-images").mkdir()
        (self.dir / "chat-images" / name).write_bytes(b"x")
        room.follow_once(TOKEN, threading.Event())
        view = room.view()
        self.assertEqual((view["recall"], view["admin"]), (600, False))
        self.assertEqual([(m["id"], m["text"], m.get("recalled", False), "image" in m) for m in view["messages"]],
                         [(3, "一", False, False), (4, "撤回了一条消息", True, False), (5, "撤回了一条消息", True, False)])
        self.assertEqual(view["recalled"], [m["seq"] for m in view["messages"][1:]])
        self.assertFalse((self.dir / "chat-images" / name).exists())
        kept = (self.dir / f"chat-{Chat.key_of(TOKEN)}.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(name, kept)
        self.assertEqual([m.get("recalled", False) for m in self.chat(Server()).messages], [False, True, True])  # after a restart
        # recalling one's own: the server's tombstone, kept in place
        room.server.reply = {"id": 3, "name": "机灵的白马", "text": "撤回了一条消息", "time": 130, "recalled": True}
        self.assertEqual(room.recall(3), {"id": 3})
        self.assertEqual(room.server.sent[-1], {"id": 3})
        self.assertTrue(room.messages[0]["recalled"])
        for bad in (0, "3", None):
            self.assertRaises(ValueError, room.recall, bad)
        room.server.reply = HTTPError("u", 409, "late", {}, io.BytesIO('{"error": "超过 10 分钟的消息不能撤回了"}'.encode()))
        with self.assertRaisesRegex(ValueError, "超过 10 分钟"):
            room.recall(5)

    def test_the_push_switch_and_leaving_the_web_chat(self):
        room = self.chat(Server(sse(("hello", {"conn": "c1", "name": "沉稳的赤兔", "mention_push": False, "phone": True}))))
        room.follow_once(TOKEN, threading.Event())
        self.assertEqual((room.view()["mention_push"], room.view()["phone"]), (False, True))
        room.server.reply = {"mention_push": True, "phone": True}
        self.assertEqual(room.set_mention_push(True), {"mention_push": True})
        self.assertEqual(room.server.sent[-1], {"mention_push": True})
        self.assertRaises(ValueError, room.set_mention_push, "yes")
        room.server.reply = {"ok": True}
        self.assertEqual(room.web_logout(), {"ok": True})
        room.server.reply = HTTPError("u", 503, "x", {}, io.BytesIO('{"error": "暂时改不了，稍后再试"}'.encode()))
        with self.assertRaisesRegex(ValueError, "暂时改不了"):
            room.set_mention_push(False)

    def test_a_moving_image_may_be_larger(self):
        room = self.chat(Server())
        room.conn = "c1"
        self.assertRaises(ValueError, room.send_image, b"x" * (chat.ANIMATED_MAX + 1), "image/gif", 1, 1)

    def test_an_emoji_sequence_keeps_its_joiner(self):
        family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
        self.assertEqual(message_of({"name": "甲", "text": f"{family}\u200b👍", "time": 1})["text"], f"{family}👍")

    def test_an_image_message_is_checked_kept_and_sent(self):
        name = "a" * 32 + ".webp"
        value = {"id": 4, "name": "甲", "text": "[图片]", "time": 1, "image": {"name": name, "w": 30, "h": 20}}
        self.assertEqual(message_of(value)["image"], {"name": name, "w": 30, "h": 20})
        for bad in ({"name": "../x.png", "w": 1, "h": 1}, {"name": name, "w": 0, "h": 1}, {"name": name, "w": "1", "h": 1},
                    {"name": name.upper(), "w": 1, "h": 1}, "x", None):
            with self.subTest(bad=bad):
                self.assertNotIn("image", message_of({**value, "image": bad}))
        room = self.chat(Server())
        room._keep(message_of(value))
        room._keep(message_of({**value, "id": 5, "image": None, "text": "字"}))
        again = self.chat(Server())  # kept across a start
        self.assertEqual([m.get("image") for m in again.messages], [{"name": name, "w": 30, "h": 20}, None])
        # sent as its bytes, named by the stream
        posted = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def post_bytes(path, token, data, kind, timeout=45):
            posted.append((path, data, kind))
            return Response(json.dumps({**value, "id": 9}).encode())
        room.server.post_bytes = post_bytes
        room.conn = "c1"
        message = room.send_image(b"RIFF....WEBP", "image/webp", 30, 20)
        self.assertEqual(posted, [(chat.IMAGE + "?conn=c1&w=30&h=20", b"RIFF....WEBP", "image/webp")])
        self.assertTrue(message["mine"] and message["image"]["name"] == name)
        for data, kind, w, h in ((b"", "image/webp", 1, 1), (b"x" * (chat.ANIMATED_MAX + 1), "image/webp", 1, 1),
                                 (b"x", "text/html", 1, 1), (b"x", "image/png", 0, 1), (b"x", "image/png", 1, "2")):
            with self.subTest(kind=kind, w=w), self.assertRaises(ValueError):
                room.send_image(data, kind, w, h)

    def test_an_image_is_fetched_once_and_kept(self):
        name = "b" * 32 + ".png"
        fetched = []

        class Images(Server):
            def open(self, path, token, timeout=45):
                fetched.append(path)
                if path.endswith(name):
                    return Stream(b"\x89PNG data")
                raise HTTPError(path, 404, "gone", {}, io.BytesIO(b"{}"))
        room = self.chat(Images())
        self.assertEqual(room.image(name), (b"\x89PNG data", "image/png"))
        self.assertEqual(room.image(name), (b"\x89PNG data", "image/png"))
        self.assertEqual(fetched, [chat.IMAGE + "/" + name])  # the second from this computer
        self.assertIsNone(room.image("c" * 32 + ".png"))  # gone from the server
        for bad in ("../../x.png", "x.exe", None):
            self.assertIsNone(room.image(bad))
        self.assertEqual(len(fetched), 2)
        # the oldest looked at go past the limit
        folder = self.dir / "chat-images"
        for n in range(3):
            (folder / f"{n:032x}.png").write_bytes(b"x" * 10)
            os.utime(folder / f"{n:032x}.png", (n, n))
        Chat._trim_images(folder, limit=25)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted([name, f"{2:032x}.png"]))

    def test_the_servers_refusal_is_said(self):
        refusal = HTTPError(chat.SEND, 429, "too many", {}, io.BytesIO('{"error": "发得太快了"}'.encode()))
        room = self.chat(Server(reply=refusal))
        room.conn = "c1"
        with self.assertRaisesRegex(ValueError, "发得太快了"):
            room.send("好")
        room.server.reply = {"name": "x"}
        with self.assertRaisesRegex(ValueError, "回复不对"):
            room.send("好")

    def test_kept_across_starts_and_the_page_gets_the_newest_first(self):
        room = self.chat(Server(reply=None))
        for n in range(chat.FIRST + 5):
            room._keep({"name": "甲", "text": str(n), "time": n, "mine": n % 2 == 0})
        with room.path.open("a", encoding="utf-8") as file:
            file.write("not json\n[1]\n{\"name\": 1}\n")
        again = self.chat(Server())
        view = again.view()
        self.assertEqual(len(view["messages"]), chat.FIRST)
        self.assertEqual(view["messages"][-1]["text"], str(chat.FIRST + 4))
        self.assertEqual([m["seq"] for m in again.messages], [m["seq"] for m in room.messages])  # same numbers
        first = view["messages"][0]["seq"]
        self.assertEqual([m["text"] for m in again.older(first)], [str(n) for n in range(5)])
        self.assertEqual(again.view(first + 1)["messages"][0]["seq"], first + 2)
        with self.assertRaises(ValueError):
            again.older("1")

    def test_the_file_is_cut_back(self):
        with (self.dir / "chat.jsonl").open("w", encoding="utf-8") as file:
            for n in range(chat.KEEP * 2):
                file.write(json.dumps({"seq": n + 1, "name": "甲", "text": str(n), "time": n, "mine": False}) + "\n")
        room = self.chat(Server())
        self.assertEqual(len(room.messages), chat.KEEP)
        self.assertEqual(len(room.path.read_text(encoding="utf-8").splitlines()), chat.KEEP)
        self.assertFalse((self.dir / "chat.jsonl").exists())  # the one for all before: the first account took it
        self.assertEqual(room.messages[-1]["seq"], chat.KEEP * 2)

    def test_each_account_keeps_its_own_messages(self):
        other = "sta_" + "b" * 43
        room = self.chat(Server(sse(), reply={"id": 7, "name": "仗义的犀牛", "text": "有人么？", "time": 300}))
        room.follow_once(other, threading.Event())  # the chat moved to another account
        self.token, room.conn = other, "c1"
        room.send("有人么？")
        self.assertEqual(room.view()["account"], Chat.key_of(other))
        self.assertNotIn(other, room.path.name)
        # back to the first: the other account's message is someone else's, given again by the server
        self.token = TOKEN
        room.server.stream = sse(("hello", {"conn": "c2", "name": "沉稳的赤兔"}),
                                 ("msg", {"id": 7, "name": "仗义的犀牛（曹操）", "text": "有人么？", "time": 300}))
        room.follow_once(TOKEN, threading.Event())
        self.assertEqual(room.server.opened[-1], chat.STREAM + "?after=0&recall=1")
        self.assertEqual([(m["name"], m["mine"]) for m in room.view()["messages"]], [("仗义的犀牛（曹操）", False)])
        self.assertIsNone(room.view()["online"])  # not told: none shown
        self.token = other  # and the other's are as they were
        again = self.chat(Server())
        self.assertEqual([(m["text"], m["mine"]) for m in again.messages], [("有人么？", True)])

    def test_switching_shows_the_other_accounts_messages_and_connects_with_it(self):
        other = "sta_" + "b" * 43
        stop = threading.Event()
        lines = threading.Event()

        class Quiet(Stream):  # a stream with only keep-alives, as when nobody speaks
            def __iter__(self):
                while not stop.is_set():
                    lines.set()
                    yield b": keepalive\n"
                    stop.wait(0.01)

        room = self.chat(Server(sse(("msg", {"id": 1, "name": "甲", "text": "第一个号的", "time": 1}))))
        room.follow_once(TOKEN, threading.Event())
        room.server.stream = Quiet()
        opened = []
        open_ = room.server.open
        room.server.open = lambda path, token, timeout=45: (opened.append(token), open_(path, token, timeout))[1]
        thread = threading.Thread(target=room.run, args=(stop,))
        thread.start()
        self.assertTrue(lines.wait(5))
        self.token = other
        room.switch(other)
        self.assertEqual((room.view()["account"], room.view()["messages"], room.state), (Chat.key_of(other), [], "connecting"))
        for _ in range(500):  # not left on the first account's quiet stream: connected with the other
            if opened[-1:] == [other]:
                break
            stop.wait(0.01)
        stop.set()
        thread.join(5)
        self.assertEqual(opened, [TOKEN, other])
        self.token = TOKEN  # the first one's are where they were
        self.assertEqual([m["text"] for m in self.chat(Server()).messages], ["第一个号的"])

    def test_run_waits_for_an_approved_account_and_reports_problems(self):
        stop = threading.Event()
        self.token = None
        room = self.chat(Server(HTTPError(chat.STREAM, 503, "full", {}, io.BytesIO('{"error": "人太多"}'.encode()))))
        states = []

        def changed():
            states.append(room.state)
            self.token = TOKEN  # approved once the chat said it waits for it
            if len(states) >= 2:
                stop.set()
        room.changed = changed
        thread = threading.Thread(target=room.run, args=(stop,))
        thread.start()
        thread.join(10)
        self.assertEqual(states[:1], ["unavailable"])
        self.assertEqual((room.state, room.error), ("offline", "人太多"))


if __name__ == "__main__":
    unittest.main()
