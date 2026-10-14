"""Let the phone play through this computer.

The phone's Shadowrocket sends its game connections to the relays the server names (and the
server as a backup); this computer keeps a connection open to each, and for each game
connection it is asked to make, it connects to the game itself and copies the bytes both ways.
The game then sees this computer's address, and the capture here reports the connection like
a game played on this computer.

Whatever the relays ask, this computer only connects to the game's port on a public address,
never to its own network, its own public address or another port, and keeps a connection only
while the phone speaks the game's protocol: the relays cannot use it as a proxy for anything
else. Nothing listens here; every connection goes out.
"""

import base64
import hashlib
import ipaddress
import json
import os
import selectors
import socket
import ssl
import struct
import threading
import time
import uuid
from urllib.parse import urlsplit

from .capture import GAME_PORT
from .frames import CLIENT_HEADER, Direction, is_game

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 1 << 20
TEXT, BINARY, CLOSE, PING, PONG = 1, 2, 8, 9, 10
# The phone's first bytes must become the game's request frames: one that decodes as the game's
# JSON within CHECK_BYTES, before CHECK_STRIKES frames that do not (frames.Splitter's rule).
CHECK_BYTES = 256 * 1024
CHECK_STRIKES = 5
MAX_PIPES = 8
BLOCK_SECONDS = 600
CONTROL_TIMEOUT = 90  # the relay pings every 25 s
NAT64 = ipaddress.ip_network("64:ff9b::/96")
# This run of the program: a relay lets one computer at a time serve a token's phone, and knows
# this one by it when it reconnects (another computer gets 409 until this one is gone).
INSTANCE = uuid.uuid4().hex
BUSY_WAIT = 60  # seconds between two tries while another computer serves the phone


class Refused(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def allowed_address(text, own=()):
    """True for a public unicast address that is not this computer's own public address."""
    try:
        address = ipaddress.ip_address(str(text).split("%")[0])
    except ValueError:
        return False
    if address.version == 6:
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address in NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address.is_global and not address.is_multicast and str(address) not in own


def valid_host(host):
    if not isinstance(host, str) or not 0 < len(host) <= 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return all(c.isalnum() or c in ".-_" for c in host)


def resolve(host, port, own=()):
    """The allowed addresses of `host`, resolved once (the connection uses these, so a second
    lookup cannot point it elsewhere)."""
    found = []
    for family, _type, _proto, _name, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if family in (socket.AF_INET, socket.AF_INET6) and allowed_address(address[0], own):
            found.append((family, address[0]))
    return sorted(dict.fromkeys(found), key=lambda item: item[0] != socket.AF_INET)


def _mask(data, key):
    if not data:
        return b""
    size = len(data)
    pad = (key * (size // 4 + 1))[:size]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(size, "big")


class WebSocket:
    """The client end of a WebSocket over TLS. One thread reads; sending takes a lock."""

    def __init__(self, url, headers, timeout=10):
        parts = urlsplit(url)
        secure = parts.scheme in ("https", "wss")
        host, port = parts.hostname, parts.port or (443 if secure else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            if secure:
                context = ssl.create_default_context()
                context.set_alpn_protocols(["http/1.1"])  # no WebSocket over HTTP/2 at the relay
                sock = context.wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            lines = [f"GET {parts.path} HTTP/1.1", f"Host: {parts.netloc}", "Upgrade: websocket",
                     "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{name}: {value}" for name, value in headers.items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            raw = b""
            while b"\r\n\r\n" not in raw:
                data = sock.recv(4096)
                if not data or len(raw) > 16384:
                    raise ConnectionError("no answer")
                raw += data
            head, _sep, self.buffer = raw.partition(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            if status != 101:
                raise Refused(status)
            expected = base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")
            fields = dict(line.partition(":")[::2] for line in head.decode("latin-1").split("\r\n")[1:])
            if {name.strip().lower(): value.strip() for name, value in fields.items()}.get(
                    "sec-websocket-accept") != expected:
                raise ConnectionError("bad WebSocket answer")
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        self.lock = threading.Lock()
        self.closed = False

    def pending(self):
        """Bytes already read from the socket but not yet handed out (select() cannot see them)."""
        return bool(self.buffer) or (isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0)

    def _read(self, size):
        while len(self.buffer) < size:
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("closed")
            self.buffer += data
        chunk, self.buffer = self.buffer[:size], self.buffer[size:]
        return chunk

    def recv(self):
        """(opcode, data) of the next message; None once the relay closed."""
        try:
            parts, size, kind = [], 0, None
            while True:
                head = self._read(2)
                fin, opcode = head[0] & 0x80, head[0] & 0x0F
                if head[0] & 0x70 or head[1] & 0x80:  # frames from a server are never masked
                    raise ConnectionError("bad frame")
                length = head[1] & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._read(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._read(8))[0]
                if opcode >= 8 and (length > 125 or not fin):
                    raise ConnectionError("bad control frame")
                if opcode < 8 and size + length > MAX_MESSAGE:
                    raise ConnectionError("message too long")
                payload = self._read(length)
                if opcode == PING:
                    self.send(payload, PONG)
                elif opcode == PONG:
                    pass
                elif opcode == CLOSE:
                    raise ConnectionError("closed")
                elif opcode in (0, TEXT, BINARY):
                    if (opcode == 0) == (kind is None):
                        raise ConnectionError("bad fragment")
                    kind = kind or opcode
                    parts.append(payload)
                    size += length
                    if fin:
                        return kind, b"".join(parts)
                else:
                    raise ConnectionError("bad opcode")
        except (OSError, ValueError):
            self.close()
            return None

    def send(self, data, opcode=BINARY):
        head = bytearray([0x80 | opcode])
        size = len(data)
        if size < 126:
            head.append(0x80 | size)
        elif size < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", size)
        key = os.urandom(4)
        with self.lock:
            self.sock.sendall(bytes(head) + key + _mask(data, key))

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.send(b"", CLOSE)
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes a thread blocked reading it
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class GameCheck:
    """Whether the bytes the phone sends are the game's requests (S10 of docs/phone-relay.md)."""

    def __init__(self):
        self.direction, self.seq = Direction(True), 0
        self.checked = self.strikes = 0
        self.start = bytearray()  # the first bytes, until a whole request header is there
        self.verdict = None  # None: not yet; True: the game; False: something else

    def feed(self, data):
        if self.verdict is not None:
            return self.verdict
        self.checked += len(data)
        if self.start is not None:
            # The splitter starts on a piece that begins with a whole frame header (a TCP segment);
            # the phone's WebSocket messages can cut anywhere, so the first ones are joined first.
            self.start += data
            if len(self.start) < CLIENT_HEADER:
                return None
            data, self.start = bytes(self.start), None
        frames = self.direction.feed(self.seq, data)
        self.seq = (self.seq + len(data)) & 0xFFFFFFFF
        for frame in frames:
            evidence = is_game(frame, True)
            if evidence:
                self.verdict = True
                return True
            self.strikes += evidence is False
        if self.strikes >= CHECK_STRIKES or self.checked > CHECK_BYTES:
            self.verdict = False
        return self.verdict


class Exit:
    """Keeps the connection to one relay; makes the game connections it asks for. `state` is
    what the window shows: connecting, online, busy (another computer serves the phone now),
    refused (the tokens are not allowed there), unreachable or stopped."""

    def __init__(self, server, tokens, log=None, enforce_game=True):
        self.origin = server.rstrip("/")
        self.auth = "Bearer " + ",".join(tokens)
        self.log = log or (lambda **fields: print(json.dumps(fields, ensure_ascii=False), flush=True))
        self.enforce_game = enforce_game
        self.own = set()  # this computer's public address, as the relay saw it
        self.blocked = {}  # address -> monotonic time it may be tried again
        self.pipes = threading.BoundedSemaphore(MAX_PIPES)
        self.open_pipes = 0
        self.stop = threading.Event()
        self.control = None
        self.state, self.since = "connecting", time.time()

    def _state(self, state):
        if state != self.state:
            self.state, self.since = state, time.time()

    def run(self):
        wait = 2
        while not self.stop.is_set():
            try:
                self.control = WebSocket(self.origin + "/relay/agent", {"Authorization": self.auth,
                                                                        "X-Relay-Agent": INSTANCE})
                self.control.sock.settimeout(CONTROL_TIMEOUT)
                self._state("online")
                self.log(event="relay_connected", relay=self.origin)
                wait = 2
                while not self.stop.is_set():
                    message = self.control.recv()
                    if message is None:
                        break
                    self.handle(message[1])
                if not self.stop.is_set():
                    self._state("connecting")
                    self.log(event="relay_disconnected", relay=self.origin)
            except Refused as exc:
                if exc.status == 409:
                    self._state("busy")
                    self.log(event="relay_busy", relay=self.origin)
                    self.stop.wait(BUSY_WAIT)
                    continue
                self._state("refused")
                self.log(event="relay_refused", relay=self.origin, status=exc.status)
                wait = 300 if exc.status == 401 else wait
            except OSError as exc:
                self._state("unreachable")
                self.log(event="relay_unreachable", relay=self.origin, error=str(exc))
            finally:
                if self.control:
                    self.control.close()
            self.stop.wait(wait)
            wait = min(wait * 2, 300)
        self._state("stopped")

    def close(self):
        self.stop.set()
        if self.control:
            self.control.close()

    def handle(self, data):
        try:
            message = json.loads(data)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        hello = message.get("hello")
        if isinstance(hello, dict) and isinstance(hello.get("ip"), str):
            self.own = {hello["ip"]}
        conn = message.get("open")
        if isinstance(conn, str) and len(conn) == 32 and all(c in "0123456789abcdef" for c in conn):
            threading.Thread(target=self.open, args=(conn, message.get("host"), message.get("port")),
                             name="relay-pipe", daemon=True).start()

    def refuse(self, conn, reason):
        self.log(event="open_refused", reason=reason)
        try:
            WebSocket(self.origin + "/relay/pipe", {"Authorization": self.auth, "X-Relay-Conn": conn,
                                                    "X-Relay-Refuse": reason}).close()
        except (Refused, OSError):
            pass  # the relay answers a refusal with 204: no WebSocket is opened

    def open(self, conn, host, port):
        if port != GAME_PORT or not valid_host(host):
            return self.refuse(conn, "target")
        if not self.pipes.acquire(blocking=False):
            return self.refuse(conn, "busy")
        try:
            try:
                addresses = [a for a in resolve(host, port, self.own)
                             if self.blocked.get(a[1], 0) < time.monotonic()]
            except OSError:
                addresses = []
            if not addresses:
                return self.refuse(conn, "address")
            family, address = addresses[0]
            game = socket.socket(family, socket.SOCK_STREAM)
            try:
                game.settimeout(8)
                game.connect((address, port))
            except OSError:
                game.close()
                return self.refuse(conn, "connect")
            try:
                pipe = WebSocket(self.origin + "/relay/pipe", {"Authorization": self.auth, "X-Relay-Conn": conn})
            except (Refused, OSError) as exc:
                game.close()
                self.log(event="pipe_failed", error=str(exc))
                return
            self.log(event="pipe_open", relay=self.origin, host=address)
            self.open_pipes += 1
            try:
                self.copy(game, pipe, address)
            finally:
                self.open_pipes -= 1
                game.close()
                pipe.close()
        finally:
            self.pipes.release()

    def copy(self, game, pipe, address):
        """Both ways until either side closes, in this one thread (a TLS socket must not be
        read and written by two threads at once)."""
        game.settimeout(None)
        pipe.sock.settimeout(None)
        selector = selectors.DefaultSelector()
        selector.register(game, selectors.EVENT_READ, "game")
        selector.register(pipe.sock, selectors.EVENT_READ, "pipe")
        check, up, down = GameCheck(), 0, 0
        try:
            while True:
                ready = ["pipe"] if pipe.pending() else [key.data for key, _ in selector.select()]
                for side in ready:
                    if side == "game":
                        data = game.recv(65536)
                        if not data:
                            return
                        down += len(data)
                        pipe.send(data)
                        continue
                    message = pipe.recv()
                    if message is None:
                        return
                    data = message[1]
                    if check.verdict is None and check.feed(data) is not None:
                        self.log(event="game_check", ok=check.verdict, checked=check.checked, down=down)
                        if not check.verdict and self.enforce_game:
                            self.blocked[address] = time.monotonic() + BLOCK_SECONDS
                            return
                    up += len(data)
                    game.sendall(data)
        except OSError:
            return
        finally:
            selector.close()
            self.log(event="pipe_closed", up=up, down=down)


def start(origins, tokens, log=None):
    """An Exit for each relay, each in its own thread; returns them (close() each to stop)."""
    exits = [Exit(origin, tokens, log=log) for origin in dict.fromkeys(origins)]
    for exit_ in exits:
        threading.Thread(target=exit_.run, name="relay", daemon=True).start()
    return exits


def relays_of(server, tokens):
    """The relays the server's phone page names (GET /v1/client/phone), from the first token
    that has them (an approved one); [] when none has."""
    for token in tokens:
        try:
            page = server.json("/v1/client/phone", token, timeout=20)
        except (OSError, ValueError):
            continue
        relays = page.get("relays") if isinstance(page, dict) else None
        if isinstance(relays, list) and relays:
            return [str(r) for r in relays if isinstance(r, str) and r.startswith("https://")][:4]
    return []


def run(server, tokens, stop, log=None, origins=None):
    """The `relay` command: keep the exits open until `stop` is set."""
    origins = origins or relays_of(server, tokens)
    if not origins:
        raise SystemExit("服务器没有给出中转：Token 要先审批通过")
    exits = start(origins, tokens, log=log)
    stop.wait()
    for exit_ in exits:
        exit_.close()
