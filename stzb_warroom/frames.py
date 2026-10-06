"""Cut the captured TCP connections into the game's frames; nothing else is understood here.

Every server message is a frame of ``length(4) message_id(4) ...(4) encoding(1) body``;
a client request is ``length(4) ...(8) session(32) message_id(4) seq(4) ...(3) key(1)
encoding(1) body``. Bodies are JSON, maybe zlib-compressed, maybe XOR-ed. The client only
decodes a body to tell the game from another program on the same port, and to find the
battle reports in a report list (see collector.py); the server decodes everything else.
"""

import json
import re
import struct
import zlib

from .capture import GAME_PORT
from .pcap import tcp_segment

CLIENT_HEADER = 57
MAX_FRAME = 16 * 1024 * 1024
MAX_PENDING_BYTES = 8 * 1024 * 1024
# A capture can miss a segment TCP itself delivered; past this many out-of-order segments
# the hole is skipped (retransmissions arrive much sooner).
MAX_PENDING_SEGMENTS = 64
# A connection is held until a frame decodes as the game's JSON; it is dropped as another
# program's after this many encoded frames that do not, or this many frames held.
MAX_STRIKES = 5
MAX_HELD = 256
_BARE_INT_KEY = re.compile(r"([{,])(-?\d+):")


def _json(data):
    data = data.rstrip(b"\0")
    for encoding in ("utf-8", "gbk"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            return json.loads(text)
        except ValueError:
            pass
        if text[:1] in "[{":  # some bodies use bare integer keys ({12182:[...]})
            try:
                return json.loads(_BARE_INT_KEY.sub(r'\1"\2":', text))
            except ValueError:
                pass
        return None
    return None


def _inflate(data):
    try:
        return zlib.decompress(data)
    except zlib.error:
        return None


def _xor(data, key):
    return data.translate(bytes(i ^ key for i in range(256)))


def body(frame, client):
    """(encoding, JSON value or None) of a frame's body."""
    header = CLIENT_HEADER if client else 13
    if len(frame) < header:
        return None, None
    encoding, data = frame[header - 1], frame[header:]
    if client:
        plain = _xor(data, frame[55]) if encoding == 5 else data
        candidates = [plain, _inflate(plain)]
    elif encoding == 2:
        candidates = [data]
    elif encoding == 3:
        candidates = [_inflate(data), _inflate(data[4:])]
    elif encoding == 5:
        candidates = []
        for key in dict.fromkeys((0x98, frame[7])):
            plain = _xor(data, key)
            candidates += [plain, _inflate(plain)]
    else:
        candidates = []
    for candidate in candidates:
        if candidate:
            value = _json(candidate)
            if value is not None:
                return encoding, value
    return encoding, None


def is_game(frame, client):
    """True: decodes as the game's JSON; False: an encoded body that does not; None: no body."""
    encoding, value = body(frame, client)
    if encoding not in (2, 3, 5) or not frame[CLIENT_HEADER if client else 13:].strip(b"\0"):
        return None
    return isinstance(value, (dict, list))


def message_id(frame, client):
    return struct.unpack_from(">I", frame, 44 if client else 4)[0]


def _plausible(buf, strict, client):
    length = struct.unpack_from(">I", buf)[0]
    if client:
        return CLIENT_HEADER - 4 <= length <= MAX_FRAME and (
            not strict or (buf[56] in (2, 3, 5) and buf[12:44].isalnum()))
    # Frames without a body (length 8) exist, e.g. acknowledgements.
    return 8 <= length <= MAX_FRAME and (not strict or (length > 8 and buf[12] in (2, 3, 5)))


class Direction:
    """One direction of one TCP connection: reorders segments and cuts whole frames. A capture
    that starts mid-connection or loses a segment waits for a segment starting a frame."""

    def __init__(self, client):
        self.client = client
        self.next_seq = None
        self.pending, self.pending_bytes = {}, 0
        self.buf = bytearray()
        self.synced = False

    def feed(self, seq, payload, syn=False):
        if syn:
            self.next_seq, self.synced = (seq + 1) & 0xFFFFFFFF, True
            self.buf.clear()
            self.pending, self.pending_bytes = {}, 0
            return []
        if not payload:
            return []
        if self.next_seq is None:
            self.next_seq = seq
        ahead = (seq - self.next_seq) & 0xFFFFFFFF
        if ahead and ahead < 1 << 31:
            if seq not in self.pending:
                self.pending[seq] = payload
                self.pending_bytes += len(payload)
            if self.pending_bytes > MAX_PENDING_BYTES or len(self.pending) > MAX_PENDING_SEGMENTS:
                # A segment was lost for good: skip the hole and resynchronize.
                self.next_seq = min(self.pending, key=lambda s: (s - self.next_seq) & 0xFFFFFFFF)
                self.synced = False
                self.buf.clear()
                return self._drain()
            return []
        return self._append(seq, payload) + self._drain()

    def _append(self, seq, payload):
        behind = (self.next_seq - seq) & 0xFFFFFFFF
        if behind >= len(payload):
            return []  # a retransmission of data already seen
        chunk = payload[behind:]
        self.next_seq = (self.next_seq + len(chunk)) & 0xFFFFFFFF
        if not self.synced:
            if behind or len(chunk) < (CLIENT_HEADER if self.client else 13) or not _plausible(
                    chunk, True, self.client):
                return []
            self.synced = True
        self.buf += chunk
        frames = []
        while len(self.buf) >= 12:
            if not _plausible(self.buf, False, self.client):
                self.synced = False
                self.buf.clear()
                break
            total = struct.unpack_from(">I", self.buf)[0] + 4
            if len(self.buf) < total:
                break
            frames.append(bytes(self.buf[:total]))
            del self.buf[:total]
        return frames

    def _drain(self):
        frames, progress = [], True
        while progress and self.pending:
            progress = False
            for seq in list(self.pending):
                if (seq - self.next_seq) & 0xFFFFFFFF >= 1 << 31 or seq == self.next_seq:
                    payload = self.pending.pop(seq)
                    self.pending_bytes -= len(payload)
                    frames += self._append(seq, payload)
                    progress = True
        return frames


class Connection:
    def __init__(self):
        self.directions = {True: Direction(True), False: Direction(False)}
        self.verdict = None  # None pending, True the game, False another program
        self.strikes = 0
        self.held = []


class Splitter:
    """Packets in; (connection, time, client, frame) out for the game's connections only."""

    def __init__(self, port=GAME_PORT):
        self.port = port
        self.connections = {}
        self.closed = set()  # connections that sent FIN or RST

    def feed(self, captured_at, linktype, packet):
        segment = tcp_segment(linktype, packet)
        if segment is None:
            return []
        src, sport, dst, dport, seq, syn, payload, end = segment
        if self.port not in (sport, dport):
            return []
        client = dport == self.port
        ends = tuple(sorted(((src, sport), (dst, dport))))
        if end:
            self.closed.add(ends)
        elif syn:
            self.closed.discard(ends)
        connection = self.connections.get(ends)
        if connection is None:
            connection = self.connections[ends] = Connection()
        frames = connection.directions[client].feed(seq, payload, syn)
        if connection.verdict is False:
            return []
        ready = []
        for frame in frames:
            item = (ends, captured_at, client, frame)
            if connection.verdict is None:
                evidence = is_game(frame, client)
                connection.strikes += evidence is False
                if evidence:
                    connection.verdict = True
                elif connection.strikes >= MAX_STRIKES or len(connection.held) >= MAX_HELD:
                    connection.verdict, connection.held = False, []
                    return []
            if connection.verdict:
                ready += connection.held + [item]
                connection.held = []
            else:
                connection.held.append(item)
        return ready

    def forget(self, ends):
        self.connections.pop(ends, None)
        self.closed.discard(ends)
