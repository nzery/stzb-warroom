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

from stzb_warroom import app as appmod, frames, pcap, winpktmon, winsys
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

    def test_segment_deduper_drops_repeats_and_evicts_old(self):
        dedup = winpktmon.SegmentDeduper(maxlen=2)
        s1 = ("1.2.3.4", 8001, "192.168.1.2", 50000, 100, False, b"abcd", False)
        s2 = ("1.2.3.4", 8001, "192.168.1.2", 50000, 104, False, b"efgh", False)
        s3 = ("1.2.3.4", 8001, "192.168.1.2", 50000, 108, False, b"ijkl", False)
        s1_longer = ("1.2.3.4", 8001, "192.168.1.2", 50000, 100, False, b"abcdefgh", False)
        self.assertTrue(dedup.accept(s1))
        self.assertFalse(dedup.accept(s1))
        self.assertTrue(dedup.accept(s1_longer))  # different payload length is kept
        self.assertTrue(dedup.accept(s2))
        self.assertTrue(dedup.accept(s3))
        self.assertTrue(dedup.accept(s1))  # evicted after maxlen=2

    def test_pcap_handles_windows_lso_and_wifi_80211(self):
        payload = b"x" * 2000
        tcp = struct.pack(">HHIIBBHHH", 50000, 8001, 1234, 0, 5 << 4, 0x18, 65535, 0, 0) + payload
        # IPv4 LSO v2: total length == 0
        ip_lso = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 0, 0, 0x4000, 64, 6, 0,
                             bytes([192, 168, 1, 2]), bytes([1, 2, 3, 4])) + tcp
        eth_lso = b"\x00" * 12 + b"\x08\x00" + ip_lso
        seg = pcap.tcp_segment(1, eth_lso)
        self.assertIsNotNone(seg)
        self.assertEqual((seg[1], seg[3], seg[4], len(seg[6])), (50000, 8001, 1234, 2000))
        # Raw IP (linktype 101) with LSO
        self.assertEqual(len(pcap.tcp_segment(101, ip_lso)[6]), 2000)
        # IEEE 802.11 QoS Data frame (linktype 105) + LLC/SNAP
        wifi_qos = bytes([0x88, 0x01]) + b"\x00" * 22 + b"\x00\x00" + b"\xaa\xaa\x03\x00\x00\x00\x08\x00" + ip_lso
        seg_wifi = pcap.tcp_segment(105, wifi_qos)
        self.assertIsNotNone(seg_wifi)
        self.assertEqual((seg_wifi[1], seg_wifi[3], len(seg_wifi[6])), (50000, 8001, 2000))

    def test_pktmon_start_args_handles_win11_and_win10_help(self):
        win11 = "pktmon start --capture --comp nics --pkt-size 0 --log-mode real-time"
        self.assertEqual(
            winpktmon.pktmon_start_args("pktmon.exe", win11),
            ["pktmon.exe", "start", "--capture", "--pkt-size", "0", "--log-mode", "real-time"],
        )
        self.assertEqual(
            winpktmon.pktmon_start_args("pktmon.exe", win11, comp_nics=True),
            ["pktmon.exe", "start", "--capture", "--comp", "nics", "--pkt-size", "0", "--log-mode", "real-time"],
        )
        win10 = "pktmon start --etw -p 0 -m real-time"
        self.assertEqual(
            winpktmon.pktmon_start_args("pktmon.exe", win10),
            ["pktmon.exe", "start", "--etw", "-p", "0", "-m", "real-time"],
        )
        # Empty/unrecognized help output defaults to modern --capture --pkt-size 0 (never -p 0).
        self.assertEqual(
            winpktmon.pktmon_start_args("pktmon.exe", ""),
            ["pktmon.exe", "start", "--capture", "--pkt-size", "0", "--log-mode", "real-time"],
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

            def snapshot_stats(self):
                return {"events": 2, "types": {"1": 1, "3": 1}, "tcp": 2, "dedup": 0, "truncated": 0, "dropped": 0}

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
    def test_live_pktmon_captures_outbound_and_large_bidirectional_traffic(self):
        import ssl
        # 1) Port 8001 filter: verify outbound TCP 8001 SYN is captured on the real NIC.
        seen_syn = []
        got_syn = threading.Event()

        def on_8001(at, linktype, packet):
            seg = pcap.tcp_segment(linktype, packet)
            if seg is not None and seg[3] == 8001 and seg[5]:
                seen_syn.append((at, linktype, seg))
                got_syn.set()

        session = winpktmon.PktMonSession(on_8001, port=8001)
        session.start()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            try:
                sock.connect(("223.5.5.5", 8001))
            except OSError:
                pass
            finally:
                sock.close()
            got_syn.wait(4.0)
            session.check()
            stats_8001 = session.snapshot_stats()
            print(f"live pktmon 8001 stats: {stats_8001}", flush=True)
        finally:
            session.close()
        self.assertTrue(seen_syn, f"expected outbound TCP 8001 SYN; stats={stats_8001}")

        # 2) Real bidirectional NIC traffic (TLS handshake on 443 to github.com): verifies both directions
        #    and full non-truncated (>128B, >1000B) payloads pass through PktMon ETW + tcp_segment.
        outbound_bytes, inbound_bytes, max_payload = [0], [0], [0]
        got_both = threading.Event()

        def on_443(at, linktype, packet):
            seg = pcap.tcp_segment(linktype, packet)
            if seg is None:
                return
            _src, sport, _dst, dport, _seq, _syn, payload, _end = seg
            if dport == 443:
                outbound_bytes[0] += len(payload)
            elif sport == 443:
                inbound_bytes[0] += len(payload)
            if len(payload) > max_payload[0]:
                max_payload[0] = len(payload)
            if outbound_bytes[0] > 100 and inbound_bytes[0] > 1000:
                got_both.set()

        session443 = winpktmon.PktMonSession(on_443, port=443)
        session443.start()
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection(("github.com", 443), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="github.com") as tls:
                    tls.sendall(b"HEAD / HTTP/1.1\r\nHost: github.com\r\nConnection: close\r\n\r\n")
                    tls.recv(1024)
            got_both.wait(4.0)
            session443.check()
            stats_443 = session443.snapshot_stats()
            print(f"live pktmon 443 stats: {stats_443} out={outbound_bytes[0]} in={inbound_bytes[0]} max={max_payload[0]}", flush=True)
        finally:
            session443.close()
        self.assertGreater(outbound_bytes[0], 100, f"expected outbound payload; stats={stats_443}")
        self.assertGreater(inbound_bytes[0], 1000, f"expected inbound >1KB payload; stats={stats_443}")
        self.assertGreater(max_payload[0], 200, f"payload looks truncated; stats={stats_443}")
        self.assertEqual(stats_443["truncated"], 0, f"unexpected truncated packets; stats={stats_443}")


if __name__ == "__main__":
    unittest.main()
