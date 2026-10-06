import base64
import io
import json
import sqlite3
import struct
import tempfile
import threading
import unittest
import unittest.mock
import zlib
from pathlib import Path
from urllib.error import HTTPError

from stzb_warroom import api, vault, winsys
from stzb_warroom.capture import Capture, command_folder, find_dumpcap
from stzb_warroom.frames import Splitter, body
from stzb_warroom.notify import Follower, follow_prompts, render
from stzb_warroom.runner import lock
from stzb_warroom.upload import Queue, Uploader, battle_reports

CLIENT, SERVER = "192.168.1.2", "1.2.3.4"
SESSION = b"0123456789abcdef0123456789abcdef"


def server_frame(message_id, value, encoding=2, pad=0):
    data = json.dumps(value, ensure_ascii=False).encode()
    if encoding == 3:
        data = zlib.compress(data)
    data += b"\0" * pad
    return struct.pack(">II", len(data) + 9, message_id) + b"\0" * 4 + bytes([encoding]) + data


def client_frame(message_id, value, key=0x5A):
    data = bytes(b ^ key for b in json.dumps(value).encode())
    head = struct.pack(">II", 1, 2) + SESSION + struct.pack(">II", message_id, 1) + b"\0\0\0" + bytes([key, 5])
    return struct.pack(">I", len(head) + len(data)) + head + data


def packet(payload, seq, from_client=False, syn=False, port=50000, flags=None):
    sport, dport = (port, 8001) if from_client else (8001, port)
    src, dst = (CLIENT, SERVER) if from_client else (SERVER, CLIENT)
    flags = flags if flags is not None else 0x02 if syn else 0x18
    tcp = struct.pack(">HHIIBBHHH", sport, dport, seq, 0, 5 << 4, flags, 65535, 0, 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 0, 0x4000, 64, 6, 0,
                     bytes(map(int, src.split("."))), bytes(map(int, dst.split("."))))
    return b"\0" * 12 + b"\x08\x00" + ip + tcp


def report(battle_id, hp=9000):
    return {"battle_id": battle_id, "attack_name": "甲", "defend_name": "乙", "attack_hp": hp}


class SplitterTests(unittest.TestCase):
    def test_frames_are_cut_reordered_and_only_the_game_is_kept(self):
        one, two = server_frame(5, [0, {"a": 1}]), server_frame(6, [1], encoding=3)
        splitter = Splitter()
        out = splitter.feed(1.0, 1, packet(b"", 99, syn=True))
        out += splitter.feed(1.1, 1, packet(two, 100 + len(one)))  # out of order
        out += splitter.feed(1.2, 1, packet(one, 100))
        out += splitter.feed(1.3, 1, packet(client_frame(7, {"x": 1}), 500, from_client=True))
        self.assertEqual([(client, frame) for _ends, _at, client, frame in out],
                         [(False, one), (False, two), (True, client_frame(7, {"x": 1}))])
        self.assertEqual(body(two, False)[1], [1])

    def test_another_program_on_the_port_is_dropped(self):
        splitter = Splitter(port=8001)
        junk = struct.pack(">II", 20, 1) + b"\0" * 4 + bytes([5]) + b"\x01" * 11
        out = []
        for index in range(6):
            out += splitter.feed(1.0, 1, packet(junk, 100 + index * len(junk), port=40000))
        self.assertEqual(out, [])


class FakeServer:
    origin = "https://example.test"

    def __init__(self, answers):
        self.answers, self.requests = list(answers), []

    def json(self, path, tokens, body=None, compress=False, timeout=45):
        self.requests.append((path, tokens, body))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer(body) if callable(answer) else answer


def accept(role=None, drop=None):
    return lambda body: {"stream_id": body["stream_id"], "cursor": body["start_seq"] + len(body["frames"]) - 1,
                         "role": role, "drop": drop or {"client": [], "server": []}}


def refuse(code):
    return HTTPError("https://example.test", code, "", {}, io.BytesIO(b'{"error": "x"}'))


class UploadTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.queue = Queue(Path(folder.name) / "queue.sqlite")
        self.addCleanup(self.queue.close)  # Windows cannot delete an open database

    def uploader(self, *answers):
        return Uploader(self.queue, FakeServer(answers), ["t1", "t2"], log=lambda **_: None)

    def feed(self, uploader, *frames, port=50000, skip=0):
        seq = {True: 1000, False: 100 + skip}
        for frame, client in frames:
            uploader.on_packet(1.0, 1, packet(frame, seq[client], from_client=client, port=port))
            seq[client] += len(frame)

    def test_a_closed_connection_no_longer_counts(self):
        uploader = self.uploader()
        self.feed(uploader, (server_frame(9, [1]), False), port=50000)
        self.feed(uploader, (server_frame(9, [1]), False), port=50001)
        self.assertEqual(uploader.connections(), 2)
        # The old account's connection ends (FIN, or RST) once another account logs in.
        uploader.on_packet(2.0, 1, packet(b"", 999, from_client=True, port=50000, flags=0x11))
        self.assertEqual(uploader.connections(), 1)
        uploader.on_packet(3.0, 1, packet(b"", 999, from_client=True, port=50001, flags=0x04))
        self.assertEqual(uploader.connections(), 0)

    def test_frames_go_with_every_token_and_reports_are_sent_once_per_role(self):
        uploader = self.uploader(accept(role=7), accept(role=7))
        reports = server_frame(0x0A, [0, [report(5), report(6)]])
        self.feed(uploader, (server_frame(9, [0, {"login": 1}], pad=8), False), (reports, False))
        self.assertTrue(uploader.upload_once())
        path, tokens, sent = uploader.server.requests[0]
        self.assertEqual((path, tokens, sent["start_seq"]), ("/v2/capture/frames", ["t1", "t2"], 1))
        self.assertEqual(sent["frames"][0]["pad"], 8)
        self.assertEqual(base64.b64decode(sent["frames"][0]["frame"]) + b"\0" * 8,
                         server_frame(9, [0, {"login": 1}], pad=8))
        # Before the server named the role, the reports were sent as they came.
        self.assertEqual([r["battle_id"] for r in sent["frames"][1]["reports"]], [5, 6])
        self.assertFalse(uploader.upload_once())
        self.assertEqual(set(uploader.roles.values()), {7})
        # The role is known now: a list is sent once, then only a changed report or the alliance list.
        changed = server_frame(0x5C, [0, [report(5, hp=1), report(6)]])
        self.feed(uploader, (reports, False), (reports, False), (changed, False),
                  skip=len(server_frame(9, [0, {"login": 1}], pad=8)) + len(reports))
        _stream, start, rows = self.queue.batch()
        self.assertEqual(start, 3)
        self.assertEqual([(row["channel"], [r["battle_id"] for r in row["reports"]]) for row in rows],
                         [("personal", [5, 6]), ("alliance", [5, 6])])

    def test_a_refused_stream_is_dropped_and_its_connection_starts_another(self):
        uploader = self.uploader(refuse(409), accept())
        self.feed(uploader, (server_frame(9, [1]), False))
        first = next(iter(uploader.links.values()))[0]
        self.assertTrue(uploader.upload_once())
        self.assertIsNone(self.queue.batch())
        uploader.on_packet(1.0, 1, packet(server_frame(9, [2]), 100 + len(server_frame(9, [1]))))
        stream, start, _rows = self.queue.batch()
        self.assertNotEqual(stream, first)
        self.assertEqual(start, 1)

    def test_transient_failures_keep_the_queue_and_401_stops(self):
        uploader = self.uploader(refuse(503), refuse(401))
        self.feed(uploader, (server_frame(9, [1]), False))
        uploader.stop.wait = lambda _delay: uploader.stop.is_set()
        uploader.run()
        self.assertIsNotNone(self.queue.batch())
        self.assertIn("401", str(uploader.error))

    def test_the_server_drop_list_is_applied(self):
        uploader = self.uploader(accept(drop={"client": [], "server": [9]}))
        self.feed(uploader, (server_frame(9, [1]), False))
        uploader.upload_once()
        uploader.on_packet(1.0, 1, packet(server_frame(9, [2]), 100 + len(server_frame(9, [1]))))
        self.assertIsNone(self.queue.batch())

    def test_battle_reports_are_found_anywhere_in_a_list(self):
        self.assertEqual([r["battle_id"] for r in battle_reports([0, {"x": [report(5)]}, {"battle_id": 1}])], [5])

    def test_full_queue_does_not_remember_reports_that_were_not_saved(self):
        self.queue.add("other", 1, {"frame": "occupied"})
        with unittest.mock.patch("stzb_warroom.upload.MAX_QUEUED", 1):
            self.assertEqual(self.queue.add_reports(7, "stream", 1, 1.0, "personal", [report(5)]), 0)
        self.queue.drop("other")
        self.assertEqual(self.queue.add_reports(7, "stream", 1, 1.0, "personal", [report(5)]), 1)
        self.assertEqual(self.queue.batch()[2][0]["reports"], [report(5)])
        self.assertEqual(self.queue.add_reports(7, "stream", 2, 2.0, "personal", [report(5)]), 0)

    def test_report_groups_are_queued_together_or_retried_together(self):
        self.queue.add_reports(7, "stream", 1, 1.0, "alliance", [report(5)])
        self.queue.acknowledge("stream", 1)
        with unittest.mock.patch("stzb_warroom.upload.MAX_QUEUED", 1):
            self.assertEqual(self.queue.add_reports(7, "stream", 2, 2.0, "personal",
                                                     [report(5, hp=1), report(6)]), 0)
        self.assertEqual(self.queue.add_reports(7, "stream", 2, 2.0, "personal",
                                                 [report(5, hp=1), report(6)]), 2)
        self.assertEqual([r["channel"] for r in self.queue.batch()[2]], ["alliance", "personal"])

    def test_failed_report_insert_rolls_back_deduplication(self):
        self.queue.add("stream", 1, {"frame": "occupied"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.add_reports(7, "stream", 1, 1.0, "personal", [report(5)])
        self.queue.acknowledge("stream", 1)
        self.assertEqual(self.queue.add_reports(7, "stream", 2, 2.0, "personal", [report(5)]), 1)

    def test_full_queue_logs_without_holding_database_lock(self):
        def log(*args, **kwargs):
            acquired = self.queue.lock.acquire(blocking=False)
            if acquired:
                self.queue.lock.release()
            self.assertTrue(acquired, "logging under the queue lock can deadlock the window")
        with unittest.mock.patch("stzb_warroom.upload.MAX_QUEUED", 0), \
                unittest.mock.patch("builtins.print", side_effect=log) as printed:
            self.assertFalse(self.queue.add("stream", 1, {}))
            self.queue.full_logged = 0
            self.assertEqual(self.queue.add_reports(7, "stream", 1, 1.0, "personal", [report(5)]), 0)
            self.assertEqual(printed.call_count, 2)


class NotifyTests(unittest.TestCase):
    def test_non_object_cursor_file_is_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cursor.json"
            for data in ("null", "[]", "42"):
                path.write_text(data)
                follower = Follower(None, "t", path)
                self.assertIsNone(follower.after)
                self.assertIsNone(follower.epoch)

    def test_render_tags_colours_and_strips_control_characters(self):
        text = render({"time": 1790000000, "tag": "敌袭", "level": "alarm", "title": "x\033[2J",
                       "body": "a\n\nb", "role": 7}, color=False, label=True)
        self.assertEqual(text, "\a" + "22:13:20 [敌袭] [7] x[2J\n         a\n         b")

    def test_follower_prints_notifications_and_keeps_its_cursor(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        lines = [b"event: ready\n", b'data: {"event_epoch": "e"}\n', b"\n",
                 b"id: 12\n", b"event: notify\n", b'data: {"title": "t", "level": "info", "tag": "x"}\n', b"\n"]

        class Server:
            origin = "o"

            def json(self, path, token):
                return {"event_epoch": "e", "next_id": 10}

            def open(self, path, token, timeout):
                self.path = path
                return io.BytesIO(b"".join(lines))

        server = Server()
        follower = Follower(server, "t", Path(folder.name) / "c.json", False, False)
        follower.follow_once(threading.Event())
        self.assertIn("notify=1", server.path)
        self.assertEqual(json.loads((Path(folder.name) / "c.json").read_text()), {"epoch": "e", "after": 12})

    def test_prompts_are_shown_and_answered(self):
        stop = threading.Event()
        server = FakeServer([{"id": "p1", "text": "扫码\033[2J", "ask": "验证码："}, {"ok": True},
                             lambda _body: stop.set() or {"id": "p1", "text": "扫码"}])
        shown = []
        stop.wait = lambda _delay: stop.is_set()
        follow_prompts(server, "t", stop, show=lambda text, ask: shown.append((text, ask)) or " 1234 ")
        self.assertEqual(shown, [("扫码[2J", "验证码：")])
        self.assertEqual(server.requests[1], ("/v1/client/prompt", "t", {"id": "p1", "answer": "1234"}))

    def test_an_unanswered_prompt_sends_nothing(self):
        stop = threading.Event()
        server = FakeServer([lambda _body: stop.set() or {"id": "p1", "text": "x", "ask": "y"}])
        follow_prompts(server, "t", stop, show=lambda text, ask: None)
        self.assertEqual(len(server.requests), 1)


    def test_a_prompt_the_server_drops_is_taken_down(self):
        for gone in ({"id": None, "text": None, "ask": None}, refuse(403)):
            with self.subTest(gone=gone):
                stop = threading.Event()
                server = FakeServer([{"id": "p1", "text": "x", "ask": None}, gone,
                                     lambda _body: stop.set() or {"id": None}])
                stop.wait = lambda _delay: stop.is_set()
                shown = []
                follow_prompts(server, "t", stop, show=lambda text, ask: shown.append(text))
                self.assertEqual(shown, ["x", None])

class LocalTests(unittest.TestCase):
    def test_capture_close_waits_for_buffered_packets(self):
        capture = Capture(lambda *_: None)
        process = capture.process = unittest.mock.Mock()
        process.poll.return_value = None
        terminated = threading.Event()
        process.terminate.side_effect = terminated.set
        finished = []
        def read_buffered():
            terminated.wait(5)
            finished.append((process.stdout.close.called, process.stderr.close.called))
        reader = threading.Thread(target=read_buffered)
        capture.threads = [reader]
        reader.start()
        capture.close()
        self.assertFalse(reader.is_alive())
        self.assertEqual(finished, [(False, False)])
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_one_capture_per_state_directory(self):
        held = lock(self.folder / "capture.lock")
        self.addCleanup(held.close)
        with self.assertRaises(RuntimeError):
            lock(self.folder / "capture.lock")

    def test_the_package_carries_its_server_and_site(self):
        (self.folder / "config.json").write_text('{"server": "https://api.example", "site": "https://example"}')
        with unittest.mock.patch.dict("os.environ", {}, clear=True), \
                unittest.mock.patch.object(api.sys, "frozen", True, create=True), \
                unittest.mock.patch.object(api.sys, "executable", str(self.folder / "stzb-warroom.exe")):
            self.assertEqual((api.default_server(), api.site()), ("https://api.example", "https://example"))
            with unittest.mock.patch.dict("os.environ", {"ST_SERVER": "https://other.example"}):
                self.assertEqual(api.default_server(), "https://other.example")
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(api.default_server())

    def test_saved_tokens_are_encrypted_and_read_back(self):
        with unittest.mock.patch.object(vault, "protect", lambda data: data[::-1]), \
                unittest.mock.patch.object(vault, "unprotect", lambda data: data[::-1]):
            self.assertEqual(vault.load(self.folder / "tokens.bin"), "")
            vault.save(self.folder / "tokens.bin", "sta_a,sta_b")
            self.assertNotIn(b"sta_a", (self.folder / "tokens.bin").read_bytes())
            self.assertEqual(vault.load(self.folder / "tokens.bin"), "sta_a,sta_b")
            vault.forget(self.folder / "tokens.bin")
            self.assertEqual(vault.load(self.folder / "tokens.bin"), "")

    @unittest.skipUnless(vault.AVAILABLE, "DPAPI is Windows only")
    def test_dpapi_round_trip(self):
        vault.save(self.folder / "tokens.bin", "sta_x")
        self.assertNotIn(b"sta_x", (self.folder / "tokens.bin").read_bytes())
        self.assertEqual(vault.load(self.folder / "tokens.bin"), "sta_x")

    @unittest.skipUnless(winsys.WINDOWS, "Windows only")
    def test_windows_helpers_answer(self):
        self.assertIn(winsys.npcap_installed(), (True, False))
        self.assertFalse(winsys.autostart())  # not a packaged exe

    def test_dumpcap_is_looked_for_in_order(self):
        picked = self.folder / "dumpcap"
        with unittest.mock.patch.dict("os.environ", {"ST_DUMPCAP": str(self.folder / "missing")}):
            found, places = find_dumpcap([str(picked)])
            self.assertEqual(places[:2], [str(self.folder / "missing"), str(picked)])
            self.assertNotEqual(found, str(picked))
            picked.write_bytes(b"")
            self.assertEqual(find_dumpcap([str(picked)])[0], str(picked))

    def test_registry_commands_name_the_wireshark_folder(self):
        for command in (r'"C:\Program Files\Wireshark\uninstall-wireshark.exe" /S',
                        r"C:\Program Files\Wireshark\Wireshark.exe",
                        r"C:\Program Files\Wireshark\Wireshark.exe,0"):
            self.assertEqual(command_folder(command), r"C:\Program Files\Wireshark")
        self.assertEqual(command_folder(""), "")


if __name__ == "__main__":
    unittest.main()
