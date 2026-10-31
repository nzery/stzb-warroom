"""The group chat (chat.py): what is kept, what the page is given, the stream and sending."""

import io
import json
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
        assert path == chat.SEND
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
                         {"id": None, "name": "甲", "text": "<b>a</b>", "time": 5.0, "mine": False})
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
        self.assertEqual(room.server.opened, [chat.STREAM + "?after=0"])  # never connected: the recent ones
        room.server.reply = mine
        room.send("我说的")
        # Started again: asks after the newest it got (not its own: one of the others' may be before it),
        # and what it has already is not kept twice.
        again = self.chat(Server(sse(("hello", {"conn": "c2", "name": "沉稳的赤兔", "online": 1}),
                                     ("msg", {"id": 4, "name": "机灵的白马", "text": "一", "time": 140}),
                                     ("msg", mine), ("msg", {"id": 6, "name": "机灵的白马", "text": "二", "time": 160}))))
        again.follow_once(TOKEN, threading.Event())
        self.assertEqual(again.server.opened, [chat.STREAM + "?after=4"])
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
        self.assertEqual(room.server.opened[-1], chat.STREAM + "?after=0")
        self.assertEqual([(m["name"], m["mine"]) for m in room.view()["messages"]], [("仗义的犀牛（曹操）", False)])
        self.assertIsNone(room.view()["online"])  # not told: none shown
        self.token = other  # and the other's are as they were
        again = self.chat(Server())
        self.assertEqual([(m["text"], m["mine"]) for m in again.messages], [("有人么？", True)])

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
