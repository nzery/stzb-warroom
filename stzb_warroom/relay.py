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
import re
import selectors
import socket
import ssl
import struct
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .api import tls_context
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
STOP_CHECK = 1  # seconds between a game connection's looks at whether the exit was closed
CONTROL_TIMEOUT = 90  # the relay pings every 25 s
NAT64 = ipaddress.ip_network("64:ff9b::/96")
SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
MAX_HANDLERS = 2 * MAX_PIPES  # orders being handled at once; more are dropped (a relay gone wrong)
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
        # An IPv6 address carrying an IPv4 one is judged by the IPv4 one (NAT64, 6to4, Teredo).
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address in NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address in SIX_TO_FOUR:
            address = address.sixtofour
        elif address.teredo:
            address = address.teredo[1]
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


# How the first bytes of the TLS handshake (the ClientHello, which names the relay in clear) are cut:
# into TLS records of these sizes, each in its own TCP packet, the rest in one. A filter that reads
# the name from a single packet (some networks' filters on 443 do: they reset the
# connection) does not find it (2026-10-15).
HELLO_PIECES = (16, 24, 32, 48, 64, 64, 64)
PIECE_GAP = 0.002  # seconds between two pieces, so that they leave as separate packets


def split_hello(data, host=""):
    """The first TLS flight with its handshake record cut into smaller records (RFC 8446 lets a
    handshake message span records): at HELLO_PIECES, and always inside `host` where it appears,
    so no piece holds the whole name. Anything not a single handshake record is left as it is."""
    if len(data) < 5 or data[0] != 0x16 or int.from_bytes(data[3:5], "big") != len(data) - 5:
        return [data]
    head, body = data[:3], data[5:]
    cuts, at = set(), 0
    for size in HELLO_PIECES:
        at += size
        cuts.add(at)
    name = host.encode("ascii", "ignore")
    found = body.find(name) if name else -1
    if found >= 0:
        cuts.add(found + max(1, len(name) // 2))
    cuts = sorted(cut for cut in cuts if 0 < cut < len(body))
    pieces = [body[a:b] for a, b in zip([0] + cuts, cuts + [len(body)])]
    return [head + len(piece).to_bytes(2, "big") + piece for piece in pieces]


class TLS:
    """TLS over a connected socket, its handshake sent in pieces (split_hello). Looks enough like a
    socket for WebSocket and select(): recv, sendall, pending, fileno, settimeout, shutdown, close.
    One lock around the TLS state: one thread reads while another sends."""

    def __init__(self, sock, context, host):
        self.sock = sock
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.tls = context.wrap_bio(self.incoming, self.outgoing, server_hostname=host)
        self.lock = threading.Lock()
        self.plain, self.eof = b"", False  # decrypted and not yet handed out; the peer ended TLS
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        first = True
        while True:
            try:
                self.tls.do_handshake()
                self._flush()
                return
            except ssl.SSLWantReadError:
                pass
            data = self.outgoing.read()
            if first and data:
                first = False
                for piece in split_hello(data, host):
                    sock.sendall(piece)
                    time.sleep(PIECE_GAP)
            elif data:
                sock.sendall(data)
            received = sock.recv(65536)
            if not received:
                raise ConnectionError("closed during the TLS handshake")
            self.incoming.write(received)

    def _flush(self):
        data = self.outgoing.read()
        if data:
            self.sock.sendall(data)

    def _decrypt(self):
        """Everything the bytes received so far decrypt to. A read takes one record only, and the
        records left encrypted in the memory BIO are seen neither by select() (they were read from
        the socket) nor by SSLObject.pending(): a phone's first message coming right behind the
        relay's 101 answer waited there until the next packet, the phone giving up (2026-10-16)."""
        parts = []
        try:
            while not self.eof:
                data = self.tls.read(65536)
                if not data:
                    self.eof = True
                parts.append(data)
        except ssl.SSLWantReadError:
            pass
        except ssl.SSLZeroReturnError:
            self.eof = True
        finally:
            self._flush()  # what reading made TLS answer (key updates, …)
        return b"".join(parts)

    def recv(self, size):
        while True:
            with self.lock:
                if not self.plain:
                    self.plain = self._decrypt()
                if self.plain:
                    chunk, self.plain = self.plain[:size], self.plain[size:]
                    return chunk
                if self.eof:
                    return b""
            data = self.sock.recv(65536)
            if not data:
                return b""
            with self.lock:
                self.incoming.write(data)

    def sendall(self, data):
        with self.lock:
            self.tls.write(data)
            self._flush()

    def pending(self):
        with self.lock:
            if not self.plain:
                self.plain = self._decrypt()
            return bool(self.plain) or self.eof

    def fileno(self):
        return self.sock.fileno()

    def settimeout(self, value):
        self.sock.settimeout(value)

    def shutdown(self, how):
        self.sock.shutdown(how)

    def close(self):
        self.sock.close()


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
                context = tls_context()
                context.set_alpn_protocols(["http/1.1"])  # no WebSocket over HTTP/2 at the relay
                sock = TLS(sock, context, host)
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
            try:
                status = int(head.split(b" ", 2)[1])
            except (IndexError, ValueError):
                raise ConnectionError("not an HTTP answer") from None
            if status != 101:
                raise Refused(status)
            expected = base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID).digest()).decode("ascii")
            fields = dict(line.partition(":")[::2] for line in head.decode("latin-1").split("\r\n")[1:])
            if {name.strip().lower(): value.strip() for name, value in fields.items()}.get(
                    "sec-websocket-accept") != expected:
                raise ConnectionError("bad WebSocket answer")
        except ssl.SSLError as exc:
            sock.close()
            raise ConnectionError(f"TLS: {exc}") from None
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        self.lock = threading.Lock()
        self.closed = False

    def pending(self):
        """Bytes already read from the socket but not yet handed out (select() cannot see them)."""
        return bool(self.buffer) or (isinstance(self.sock, TLS) and self.sock.pending() > 0)

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


HEAD_MESSAGES = 6  # messages whose beginning the log shows when the check fails
HEAD_BYTES = 64
SECRET = re.compile(rb"[0-9A-Za-z]{16,}")


def head(data):
    """A message's size and first bytes in hex, a run of 16+ letters and digits (the session token
    of a request frame) left out, for finding out what the check did not take."""
    shown = SECRET.sub(lambda m: b"\0" * len(m.group()), bytes(data[:HEAD_BYTES])).hex()
    return f"{len(data)}:{shown}"


class GameCheck:
    """Whether the bytes the phone sends are the game's requests (S10 of docs/phone-relay.md)."""

    def __init__(self):
        self.direction, self.seq = Direction(True), 0
        self.checked = self.strikes = 0
        self.start = bytearray()  # the first bytes, until a whole request header is there
        self.verdict = None  # None: not yet; True: the game; False: something else
        self.head = []  # the first messages' beginnings, for the log when it is not the game

    def feed(self, data):
        if self.verdict is not None:
            return self.verdict
        self.checked += len(data)
        if len(self.head) < HEAD_MESSAGES:
            self.head.append(head(data))
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
        self.pipes = threading.BoundedSemaphore(MAX_PIPES)
        self.handlers = threading.BoundedSemaphore(MAX_HANDLERS)
        self.games = set()  # (game socket, pipe) of the connections being copied now (close() ends them)
        self.games_lock = threading.Lock()
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

    @property
    def open_pipes(self):
        return len(self.games)

    def close(self):
        """Stop: the connection to the relay and every game connection made through it (the
        phone must not go on through this computer once the exit is off)."""
        self.stop.set()
        if self.control:
            self.control.close()
        with self.games_lock:
            games = list(self.games)
        # Both sockets are ended here, not through pipe.close(): copy() may be blocked on either one
        # (the rest of a frame from a stalled relay, or a send the relay does not take), and
        # pipe.close() would first send CLOSE, which can block the same way. A shutdown wakes a
        # blocked call on Linux but not on Windows, where only closing the socket cancels it (and on
        # Linux closing it would not). Its copy() then fails and closes both.
        for game, pipe in games:
            for sock in (game, pipe.sock):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                if os.name == "nt":
                    try:
                        sock.close()
                    except OSError:
                        pass

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
            if not self.handlers.acquire(blocking=False):
                self.log(event="open_refused", relay=self.origin, reason="too many orders")
                return
            threading.Thread(target=self._handled, args=(conn, message.get("host"), message.get("port")),
                             name="relay-pipe", daemon=True).start()

    def _handled(self, conn, host, port):
        try:
            self.open(conn, host, port)
        except Exception as exc:  # one order going wrong never takes the exit down
            self.log(event="pipe_failed", relay=self.origin, error=f"{type(exc).__name__}: {exc}")
        finally:
            self.handlers.release()

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
                addresses = resolve(host, port, self.own)
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
            with self.games_lock:
                stopped = self.stop.is_set()  # close() came while this was connecting
                if not stopped:
                    self.games.add((game, pipe))
            try:
                if not stopped:
                    self.log(event="pipe_open", relay=self.origin, host=address)
                    self.copy(game, pipe, address)
            finally:
                with self.games_lock:
                    self.games.discard((game, pipe))
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
                # Shutting the game socket down (close()) wakes select() on Linux but not on Windows,
                # so it also wakes each second to see whether the exit was closed.
                if self.stop.is_set():
                    return
                ready = ["pipe"] if pipe.pending() else [key.data for key, _ in selector.select(STOP_CHECK)]
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
                        self.log(event="game_check", ok=check.verdict, checked=check.checked, down=down,
                                 **({} if check.verdict else {"head": check.head}))
                        # Only this connection ends: the target is not blocked, so a reconnect the
                        # check does not take (2026-10-15) does not shut out the login that follows.
                        if not check.verdict and self.enforce_game:
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
    """The relays any of the tokens may use, as the server names them (GET /v1/client/phone?only=relays,
    asked for each token: the public domain's and a private one's differ), in the order given;
    [] when none has (not approved yet). A server without ?only=relays gets the whole page asked."""
    found = []
    for token in tokens:
        try:
            try:
                page = server.json("/v1/client/phone?only=relays", token, timeout=20)
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                page = server.json("/v1/client/phone", token, timeout=20)
        except (OSError, ValueError):
            continue
        relays = page.get("relays") if isinstance(page, dict) else None
        if isinstance(relays, list):
            found += [r for r in relays if isinstance(r, str) and r.startswith("https://")]
    return list(dict.fromkeys(found))[:4]


def run(server, tokens, stop, log=None, origins=None):
    """The `relay` command: keep the exits open until `stop` is set."""
    origins = origins or relays_of(server, tokens)
    if not origins:
        raise SystemExit("服务器没有给出中转：Token 要先审批通过")
    exits = start(origins, tokens, log=log)
    stop.wait()
    for exit_ in exits:
        exit_.close()
