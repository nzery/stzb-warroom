"""The phone exit: what this computer agrees to connect to and keep, whatever the relays ask."""

import json
import os
import socket
import struct
import threading
import time
import unittest
import unittest.mock

from stzb_warroom import relay

SESSION = b"0123456789abcdef0123456789abcdef"


def client_frame(message_id, value, key=0x5A):
    data = bytes(b ^ key for b in json.dumps(value).encode())
    head = struct.pack(">II", 1, 2) + SESSION + struct.pack(">II", message_id, 1) + b"\0\0\0" + bytes([key, 5])
    return struct.pack(">I", len(head) + len(data)) + head + data


class AddressTests(unittest.TestCase):
    def test_only_public_unicast(self):
        for address in ("1.2.3.4", "2606:4700::1111", "::ffff:1.2.3.4", "64:ff9b::102:304", "2002:102:304::1"):
            self.assertTrue(relay.allowed_address(address), address)
        for address in ("127.0.0.1", "10.0.0.8", "172.16.3.4", "192.168.1.1", "169.254.1.1",
                        "100.64.0.1",  # shared address space (carrier NAT, Tailscale)
                        "198.18.0.1", "0.0.0.0", "255.255.255.255", "224.0.0.1", "240.0.0.1",
                        "::1", "fe80::1", "fd00::1", "ff02::1", "::ffff:192.168.1.1",
                        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
                        "2002:c0a8:101::1",  # 6to4 of 192.168.1.1
                        "2001:0:4136:e378:8000:63bf:3f57:fefe",  # Teredo (its client 192.168.1.1)
                        "not an address", ""):
            self.assertFalse(relay.allowed_address(address), address)

    def test_not_this_computers_own_public_address(self):
        self.assertFalse(relay.allowed_address("1.2.3.4", own={"1.2.3.4"}))
        self.assertTrue(relay.allowed_address("1.2.3.5", own={"1.2.3.4"}))

    def test_hosts(self):
        self.assertTrue(relay.valid_host("game.example.com"))
        self.assertTrue(relay.valid_host("1.2.3.4"))
        for host in ("", "a" * 254, "a b", "a/b", "a\r\nb", None, 5):
            self.assertFalse(relay.valid_host(host), host)

    def test_resolves_once_and_drops_private_answers(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 8001)),
                   (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 8001, 0, 0)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 8001))]
        with unittest.mock.patch("socket.getaddrinfo", return_value=answers) as lookup:
            self.assertEqual(relay.resolve("game.example", 8001),
                             [(socket.AF_INET, "1.2.3.4"), (socket.AF_INET6, "2606:4700::1111")])
        lookup.assert_called_once()


class OpenTests(unittest.TestCase):
    def setUp(self):
        self.exit = relay.Exit("https://relay.example", ["t"], log=lambda **fields: None)
        self.refused = []
        self.exit.refuse = lambda conn, reason: self.refused.append(reason)

    def test_refuses_without_connecting(self):
        with unittest.mock.patch("socket.socket") as make, unittest.mock.patch("socket.getaddrinfo") as lookup:
            self.exit.open("0" * 32, "game.example", 22)
            self.exit.open("0" * 32, "bad host!", 8001)
            lookup.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 8001))]
            self.exit.open("0" * 32, "router.example", 8001)
        self.assertEqual(self.refused, ["target", "target", "address"])
        make.assert_not_called()

    def test_only_well_formed_orders(self):
        with unittest.mock.patch("threading.Thread") as thread:
            for message in (b"not json", b"[]", b'{"open": "../x"}', b'{"open": 5}', b'{"open": "' + b"g" * 32 + b'"}'):
                self.exit.handle(message)
            thread.assert_not_called()
            self.exit.handle(b'{"open": "' + b"a" * 32 + b'", "host": "h", "port": 8001}')
            thread.assert_called_once()

    def test_hello_gives_the_own_address(self):
        self.exit.handle(b'{"hello": {"ip": "1.2.3.4"}}')
        self.assertEqual(self.exit.own, {"1.2.3.4"})


class CloseTests(unittest.TestCase):
    def test_closing_the_exit_ends_the_game_connections_going_through_it(self):
        self.closes_the_game_connection(wakes=True)

    def test_closing_ends_them_where_a_shutdown_does_not_wake_select(self):
        # Windows: shutting the game socket down leaves the copying thread asleep in select().
        self.closes_the_game_connection(wakes=False)

    def closes_the_game_connection(self, wakes):
        exit_ = relay.Exit("https://relay.example", ["t"], log=lambda **fields: None)
        mine, _game_server = socket.socketpair()
        pipe_end, _relay_end = socket.socketpair()
        self.addCleanup(_game_server.close)
        self.addCleanup(_relay_end.close)

        class Game(socket.socket):
            def connect(self, address):
                pass

            def shutdown(self, how):
                if wakes:
                    super().shutdown(how)

        class Pipe:
            sock, closed = pipe_end, False

            def __init__(self, url, headers):
                pass

            def pending(self):
                return False

            def send(self, data):
                pass

            def close(self):
                Pipe.closed = True

        game = Game(fileno=mine.detach())
        with unittest.mock.patch.object(relay, "resolve", return_value=[(socket.AF_INET, "1.2.3.4")]), \
                unittest.mock.patch.object(relay.socket, "socket", return_value=game), \
                unittest.mock.patch.object(relay, "WebSocket", Pipe):
            worker = threading.Thread(target=exit_.open, args=("a" * 32, "game.example", 8001), daemon=True)
            worker.start()
            for _ in range(200):
                if exit_.open_pipes:
                    break
                time.sleep(0.01)
            self.assertEqual(exit_.open_pipes, 1)
            exit_.close()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(Pipe.closed)
        self.assertEqual(exit_.open_pipes, 0)
        pipe_end.close()


class BusyTests(unittest.TestCase):
    def test_another_computer_serving_the_phone_means_waiting(self):
        exit_ = relay.Exit("https://relay.example", ["t"], log=lambda **fields: logged.append(fields))
        logged, calls = [], []

        def refuse(url, headers, timeout=10):
            calls.append(headers)
            if len(calls) == 2:
                exit_.stop.set()
            raise relay.Refused(409)

        waits = []
        exit_.stop.wait = lambda seconds: waits.append(seconds)
        with unittest.mock.patch.object(relay, "WebSocket", side_effect=refuse):
            exit_.run()
        self.assertEqual(waits[0], relay.BUSY_WAIT)
        self.assertEqual(logged[0]["event"], "relay_busy")
        self.assertEqual(calls[0]["X-Relay-Agent"], relay.INSTANCE)
        self.assertEqual(exit_.state, "stopped")


class RobustnessTests(unittest.TestCase):
    def test_a_garbled_answer_is_a_connection_error(self):
        class Socket:
            def __init__(self):
                self.data = [b"HELLO\r\n\r\n"]

            def sendall(self, data):
                pass

            def recv(self, size):
                return self.data.pop(0) if self.data else b""

            def close(self):
                pass

        with unittest.mock.patch("socket.create_connection", return_value=Socket()):
            with self.assertRaises(ConnectionError):
                relay.WebSocket("http://relay.example/relay/agent", {})

    def test_orders_beyond_the_handlers_are_dropped(self):
        exit_ = relay.Exit("https://relay.example", ["t"], log=lambda **fields: logged.append(fields))
        logged, started = [], []
        with unittest.mock.patch("threading.Thread") as thread:
            thread.return_value.start = lambda: started.append(1)
            for _ in range(relay.MAX_HANDLERS + 3):
                exit_.handle(b'{"open": "' + b"a" * 32 + b'", "host": "h", "port": 8001}')
        self.assertEqual(len(started), relay.MAX_HANDLERS)
        self.assertEqual([f["reason"] for f in logged], ["too many orders"] * 3)

    def test_an_order_going_wrong_does_not_stop_the_exit(self):
        exit_ = relay.Exit("https://relay.example", ["t"], log=lambda **fields: logged.append(fields))
        logged = []
        exit_.handlers.acquire()
        with unittest.mock.patch.object(exit_, "open", side_effect=ValueError("boom")):
            exit_._handled("a" * 32, "h", 8001)
        self.assertEqual(logged[-1]["event"], "pipe_failed")
        self.assertTrue(exit_.handlers.acquire(blocking=False))  # released


class HelloTests(unittest.TestCase):
    def hello(self, size=517, name=b"relay.host.test"):
        body = bytes([1]) + (size - 9).to_bytes(3, "big") + b"\x03\x03" + os.urandom(40) + b"\x00\x00" + name
        body += os.urandom(size - 5 - len(body))
        return b"\x16\x03\x01" + len(body).to_bytes(2, "big") + body

    def test_the_hello_is_cut_into_records_that_rebuild_it(self):
        data = self.hello()
        pieces = relay.split_hello(data, "relay.host.test")
        self.assertGreater(len(pieces), 3)
        rebuilt = b""
        for piece in pieces:
            self.assertEqual(piece[:3], b"\x16\x03\x01")
            self.assertEqual(int.from_bytes(piece[3:5], "big"), len(piece) - 5)
            rebuilt += piece[5:]
        self.assertEqual(rebuilt, data[5:])
        self.assertFalse(any(b"relay.host.test" in piece for piece in pieces))  # no packet names the relay whole
        for size in (300, 517, 1557):  # wherever the name is
            for offset in range(0, size - 40, 7):
                body = bytearray(os.urandom(size - 5))
                body[offset:offset + 15] = b"relay.host.test"
                hello = b"\x16\x03\x01" + len(body).to_bytes(2, "big") + bytes(body)
                self.assertFalse(any(b"relay.host.test" in p for p in relay.split_hello(hello, "relay.host.test")))

    def test_anything_else_is_sent_as_it_is(self):
        for data in (b"", b"\x17\x03\x03\x00\x02ab", self.hello()[:-1], self.hello() + self.hello()):
            self.assertEqual(relay.split_hello(data), [data])
        small = b"\x16\x03\x01\x00\x04abcd"
        self.assertEqual(relay.split_hello(small), [small])


class GameCheckTests(unittest.TestCase):
    def test_the_game_from_the_first_byte_even_in_pieces(self):
        first = client_frame(99991, {"passport": "x@163.com", "ver": "9.2.2"})
        stream = first + client_frame(5025, {"a": 1})
        for cut in (1, 10, 57, len(first) - 1, len(first), len(stream) - 1):
            check = relay.GameCheck()
            verdict = check.feed(stream[:cut])
            self.assertEqual(verdict, True if cut >= len(first) else None, cut)
            self.assertTrue(check.feed(stream[cut:]), cut)

    def test_anything_else_is_refused(self):
        for junk in (b"GET / HTTP/1.1\r\nHost: x\r\n\r\n" * 20000,  # a web request through the game's port
                     os.urandom(300 * 1024),  # noise
                     b"".join(client_frame(1, None) for _ in range(5)) + bytes(300 * 1024)):  # frames that are not JSON
            check = relay.GameCheck()
            verdict = None
            for start in range(0, len(junk), 16384):
                verdict = check.feed(junk[start:start + 16384])
                if verdict is not None:
                    break
            self.assertIs(verdict, False)

    def test_what_was_not_taken_is_logged_without_the_session_token(self):
        frame = client_frame(1, None)  # a request frame whose body is not JSON
        token = frame[12:44]
        check = relay.GameCheck()
        check.feed(frame)
        self.assertEqual(len(check.head), 1)
        size, shown = check.head[0].split(":")
        self.assertEqual(int(size), len(frame))
        self.assertNotIn(token.hex(), shown)
        self.assertEqual(shown[:24], frame[:12].hex())  # the length and what follows stay
        self.assertEqual(len(bytes.fromhex(shown)), min(len(frame), relay.HEAD_BYTES))
        for _ in range(10):
            check.feed(b"x")
        self.assertEqual(len(check.head), relay.HEAD_MESSAGES)

    def test_a_verdict_stays(self):
        check = relay.GameCheck()
        self.assertTrue(check.feed(client_frame(99991, {"x": 1})))
        self.assertTrue(check.feed(os.urandom(1000)))


class RelaysTests(unittest.TestCase):
    def test_relays_of_all_the_tokens(self):
        # The public domain's token names one relay; a private one's names another first: whichever
        # comes first, the computer connects to both (it serves both tokens' phones).
        pages = {"a": OSError("down"), "b": {"problem": "未审批", "relays": []},
                 "public": {"relays": ["https://backup.example"]},
                 "private": {"relays": ["https://relay.example", "http://plain.example", 5, "https://backup.example"]}}

        class Server:
            def __init__(self):
                self.paths = []

            def json(self, path, token, timeout):
                self.paths.append(path)
                value = pages[token]
                if isinstance(value, Exception):
                    raise value
                return value

        for tokens in (["a", "public", "private"], ["private", "b", "public"]):
            self.assertEqual(sorted(relay.relays_of(Server(), tokens)), ["https://backup.example", "https://relay.example"])
        self.assertEqual(relay.relays_of(Server(), ["a", "b"]), [])
        server = Server()
        relay.relays_of(server, ["public"])
        self.assertEqual(server.paths, ["/v1/client/phone?only=relays"])  # no QR codes made for this

    def test_an_older_server_gets_the_whole_page_asked(self):
        class Server:
            def json(self, path, token, timeout):
                if "?" in path:
                    raise relay.HTTPError(path, 404, "Not Found", {}, None)
                return {"relays": ["https://backup.example"]}

        self.assertEqual(relay.relays_of(Server(), ["t"]), ["https://backup.example"])


if __name__ == "__main__":
    unittest.main()
