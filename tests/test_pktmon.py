"""Windows PktMon ETW capture: pure-Python parsing/IPC tests everywhere, live ETW test on Windows CI."""

import os
import socket
import struct
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from stzb_warroom import app as appmod, pcap, winpktmon, winsys
from stzb_warroom.app import App


class PktMonUnitTests(unittest.TestCase):
    def test_parse_frame_payload_ethernet_and_invalid(self):
        eth = b"\x00" * 12 + b"\x08\x00" + b"\x45" + b"\x00" * 39
        header = struct.pack(
            "<QHHHHHHHIIHH",
            42, 7, 1, 0, winpktmon.PKTMON_TYPE_ETHERNET, 3, 1, 1, 0, 0, len(eth), len(eth),
        )
        self.assertEqual(winpktmon.parse_frame_payload(header + eth), (42, 7, 1, eth))
        self.assertIsNone(winpktmon.parse_frame_payload(b"\x00" * 10))
        truncated = struct.pack("<QHHHHHHHIIHH", 1, 1, 1, 0, 1, 1, 1, 1, 0, 0, 100, 100) + b"short"
        self.assertIsNone(winpktmon.parse_frame_payload(truncated))

    def test_packet_deduper_drops_repeats_and_evicts_old(self):
        dedup = winpktmon.PacketDeduper(maxlen=2)
        self.assertTrue(dedup.accept(10, 1))
        self.assertFalse(dedup.accept(10, 1))
        self.assertTrue(dedup.accept(10, 2))
        self.assertTrue(dedup.accept(11, 1))
        self.assertTrue(dedup.accept(10, 1))  # evicted after maxlen=2
        self.assertTrue(dedup.accept(0, 0))   # zero group_id is never suppressed
        self.assertTrue(dedup.accept(0, 0))

    def test_pktmon_start_args_handles_win11_and_win10_help(self):
        win11 = "pktmon start --capture --comp nics --pkt-size 0 --log-mode real-time"
        self.assertEqual(
            winpktmon.pktmon_start_args("pktmon.exe", win11),
            ["pktmon.exe", "start", "--capture", "--comp", "nics", "--pkt-size", "0", "--log-mode", "real-time"],
        )
        self.assertEqual(
            winpktmon.pktmon_start_args("pktmon.exe", win11, comp_nics=False),
            ["pktmon.exe", "start", "--capture", "--pkt-size", "0", "--log-mode", "real-time"],
        )
        win10 = "pktmon start --etw -p 0 -m real-time"
        self.assertEqual(
            winpktmon.pktmon_start_args("pktmon.exe", win10),
            ["pktmon.exe", "start", "--etw", "-p", "0", "-m", "real-time"],
        )

    def test_filetime_to_unix(self):
        # 2026-01-01 00:00:00 UTC = 1767225600
        ft = 1767225600 * 10_000_000 + winpktmon.FILETIME_EPOCH_DIFF
        self.assertAlmostEqual(winpktmon.filetime_to_unix(ft), 1767225600.0, places=5)
        self.assertGreater(winpktmon.filetime_to_unix(0), 1_700_000_000)

    def test_record_framing_roundtrip(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        left.sendall(winpktmon.encode_record(123.5, winpktmon.RECORD_READY))
        left.sendall(winpktmon.encode_record(124.25, 1, b"hello"))
        left.shutdown(socket.SHUT_WR)
        self.assertEqual(winpktmon.read_record(right), (123.5, winpktmon.RECORD_READY, b""))
        self.assertEqual(winpktmon.read_record(right), (124.25, 1, b"hello"))
        self.assertIsNone(winpktmon.read_record(right))

    def test_elevated_helper_ipc_handshake_packets_and_cleanup(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        pktmon_file = Path(folder.name) / "pktmon.exe"
        pktmon_file.write_bytes(b"")

        received = []
        ready_to_send = threading.Event()
        closed_session = threading.Event()

        class FakeSession:
            def __init__(self, on_packet, port=8001):
                self.on_packet = on_packet
                self.port = port

            def start(self):
                def emit():
                    ready_to_send.wait(5)
                    self.on_packet(1000.5, 1, b"eth-frame")
                    self.on_packet(1001.5, 101, b"ip-frame")
                threading.Thread(target=emit, daemon=True).start()

            def check(self):
                pass

            def close(self):
                closed_session.set()

        helper_threads = []

        def fake_elevate(_exe, args):
            cfg_path = args[args.index("--config") + 1]
            # First connect with a wrong token to make sure unauthorized callers are rejected.
            def rogue_and_real():
                import json
                cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
                bad = socket.create_connection(("127.0.0.1", cfg["port"]), timeout=5)
                bad.sendall(b"wrong-token\n")
                bad.close()
                winpktmon.run_helper(cfg_path)
            thread = threading.Thread(target=rogue_and_real, daemon=True)
            helper_threads.append(thread)
            thread.start()
            return None

        cap = winpktmon.PktMonCapture(
            lambda at, lt, pkt: received.append((at, lt, pkt)),
            state_dir=folder.name,
            pktmon=str(pktmon_file),
        )
        with unittest.mock.patch.object(winpktmon, "is_admin", return_value=False), \
             unittest.mock.patch.object(winpktmon, "_elevate", side_effect=fake_elevate), \
             unittest.mock.patch.object(winpktmon, "PktMonSession", FakeSession):
            cap.start(timeout=5)
            ready_to_send.set()
            deadline = time.monotonic() + 5
            while len(received) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            cap.check()
            cap.close()
        for t in helper_threads:
            t.join(timeout=5)
        self.assertEqual(received, [(1000.5, 1, b"eth-frame"), (1001.5, 101, b"ip-frame")])
        self.assertTrue(closed_session.is_set())

    def test_app_capture_method_prefers_working_dumpcap_else_pktmon(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)

        class Offline:
            def open(self, *_a, **_k):
                raise OSError("offline")

        class DummyServer:
            origin = "https://example.test"
            opener = Offline()

        app = App(DummyServer(), folder.name, opener=lambda *a, **k: None)
        app.find_dumpcap = lambda: (r"C:\Wireshark\dumpcap.exe", [])
        app.find_pktmon = lambda: r"C:\Windows\System32\pktmon.exe"

        with unittest.mock.patch.object(winsys, "WINDOWS", True), \
             unittest.mock.patch.object(winsys, "npcap_installed", return_value=True):
            self.assertEqual(app.capture_method()[:2], ("dumpcap", r"C:\Wireshark\dumpcap.exe"))

        # Installed Wireshark without Npcap: falls back to built-in PktMon automatically.
        with unittest.mock.patch.object(winsys, "WINDOWS", True), \
             unittest.mock.patch.object(winsys, "npcap_installed", return_value=False):
            self.assertEqual(app.capture_method()[:2], ("pktmon", r"C:\Windows\System32\pktmon.exe"))

        # Fresh Windows PC with no Wireshark: uses built-in PktMon.
        app.find_dumpcap = lambda: (None, [])
        with unittest.mock.patch.object(winsys, "WINDOWS", True), \
             unittest.mock.patch.object(winsys, "npcap_installed", return_value=False), \
             unittest.mock.patch.object(winsys, "autostart", return_value=False):
            self.assertEqual(app.capture_method()[:2], ("pktmon", r"C:\Windows\System32\pktmon.exe"))
            self.assertIn("Windows 内置（PktMon）", app.diagnostics())

        # Older Windows with neither dumpcap nor pktmon.
        app.find_pktmon = lambda: None
        with unittest.mock.patch.object(winsys, "WINDOWS", True), \
             unittest.mock.patch.object(winsys, "npcap_installed", return_value=False):
            self.assertEqual(app.capture_method()[:2], ("none", None))

        # UAC cancellation gets a clear Chinese hint.
        self.assertIn("管理员权限", appmod.hint("pktmon uac cancelled: administrator permission was declined"))


@unittest.skipUnless(os.name == "nt", "Windows only")
class WindowsStructAndLiveTests(unittest.TestCase):
    def test_etw_struct_sizes_x64(self):
        import ctypes
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("64-bit Windows only")
        api = winpktmon._etw_api()
        self.assertEqual(ctypes.sizeof(api.EVENT_TRACE_PROPERTIES), 120)
        self.assertEqual(ctypes.sizeof(api.EVENT_RECORD), 112)
        self.assertEqual(ctypes.sizeof(api.EVENT_TRACE_LOGFILEW), 448)

    @unittest.skipUnless(winpktmon.is_admin() and winpktmon.find_pktmon(), "requires admin + pktmon.exe")
    def test_live_pktmon_captures_outbound_tcp_8001_syn(self):
        seen = []
        found = threading.Event()

        def on_packet(at, linktype, packet):
            seg = pcap.tcp_segment(linktype, packet)
            if seg is not None and seg[3] == 8001 and seg[5]:  # dport == 8001 and SYN
                seen.append((at, linktype, seg))
                found.set()

        session = winpktmon.PktMonSession(on_packet, port=8001)
        session.start()
        try:
            # Trigger a single outbound TCP SYN on port 8001 to the public resolver already allowed by test_public.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            try:
                sock.connect(("223.5.5.5", 8001))
            except OSError:
                pass
            finally:
                sock.close()
            found.wait(4.0)
            session.check()
        finally:
            session.close()
        self.assertTrue(seen, "expected PktMon ETW to capture outbound TCP 8001 SYN")


if __name__ == "__main__":
    unittest.main()
