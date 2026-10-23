"""Queue every game connection's frames on disk and upload them, in order, to the server.

Each game connection is a stream of rows numbered from 1: a row is an exact game frame, or the battle reports of a report
list that were not sent before. The server decides whose data a connection is (with
several tokens, which one) and tells back its role, the key of the report deduplication
here. The queue survives restarts and server outages; rows are deleted once acknowledged.
It is kept only for the server and the tokens it was filled for (`Queue.bind`). A stream the
server holds back (its token awaits approval) waits on its own while the others go on.
"""

import base64
import hashlib
import json
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError

from .frames import Splitter, body, message_id

PATH = "/v2/capture/frames"
# Report lists: the personal one and the alliance one.
BATTLE_LISTS = {0x0A: "personal", 0x5C: "alliance"}
MAX_ROWS = 100  # per request, as the server takes them
MAX_BODY = 3 * 1024 * 1024
MAX_QUEUED = 50000
LINK_IDLE = 3600  # a connection unseen this long is forgotten
WAIT_FIRST, WAIT_MAX = 15, 300  # a held-back stream is tried again after this, doubling up to the max


def battle_reports(value):
    """Every dict under a report list that looks like a battle report."""
    found, stack = [], [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            try:
                battle_id = int(item.get("battle_id"))
            except (TypeError, ValueError):
                battle_id = 0
            if battle_id and "attack_name" in item and "defend_name" in item:
                found.append(item)
            else:
                stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(reversed(item))
    return found


class Queue:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS rows (stream_id TEXT NOT NULL, seq INTEGER NOT NULL, "
                        "row TEXT NOT NULL, PRIMARY KEY(stream_id, seq))")
        # Reports sent per role: (battle id) -> channel and digest; the stream that sent it,
        # so a stream the server refuses sends its reports again later.
        self.db.execute("CREATE TABLE IF NOT EXISTS reports (role INTEGER NOT NULL, battle_id INTEGER NOT NULL, "
                        "channel TEXT NOT NULL, digest TEXT NOT NULL, stream_id TEXT NOT NULL, "
                        "PRIMARY KEY(role, battle_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        self.lock = threading.Lock()
        self.full_logged = 0

    def bind(self, server, tokens):
        """Keep the queued rows only for the server and the tokens they were captured for:
        another server, or a token taken out, empties the queue and forgets the reports sent,
        so nothing goes where the player no longer sends it. Added tokens keep it.
        Returns the rows dropped."""
        tokens = sorted(hashlib.sha256(token.encode()).hexdigest() for token in tokens)
        with self.lock, self.db:
            row = self.db.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
            old = json.loads(row[0]) if row else None
            dropped = 0
            if old is not None and (old.get("server") != server or not set(old.get("tokens") or ()) <= set(tokens)):
                dropped = self.db.execute("DELETE FROM rows").rowcount
                self.db.execute("DELETE FROM reports")
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('scope', ?)",
                            (json.dumps({"server": server, "tokens": tokens}),))
        return dropped

    def add(self, stream_id, seq, row):
        with self.lock:
            if self._room(1):
                self.db.execute("INSERT INTO rows VALUES (?,?,?)", (stream_id, seq, json.dumps(row)))
                self.db.commit()
                return True
        self._warn_full()
        return False

    def _room(self, count):
        return self.db.execute("SELECT count(*) FROM rows").fetchone()[0] + count <= MAX_QUEUED

    def _warn_full(self):
        # Logging may acquire the window's lock; never do it while holding the queue lock.
        if time.time() - self.full_logged > 60:
            self.full_logged = time.time()
            print("upload queue full: new frames are dropped until the server is reachable",
                  file=sys.stderr, flush=True)

    def add_reports(self, role, stream_id, seq, captured_at, channel, reports):
        """Queue reports and their deduplication records in one transaction; return rows added.

        A full queue must not mark unsaved reports as already sent.
        """
        groups = {}
        with self.lock, self.db:
            for report in reports:
                battle_id = int(report["battle_id"])
                text = json.dumps(report, ensure_ascii=False, sort_keys=True)
                row = self.db.execute("SELECT channel,digest FROM reports WHERE role=? AND battle_id=?",
                                      (role, battle_id)).fetchone() if role else None
                merged = "alliance" if row and "alliance" in (row[0], channel) else channel
                digest = hashlib.sha256((merged + text).encode()).hexdigest()
                if row and row[1] == digest:
                    continue
                if role:
                    self.db.execute("INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?)",
                                    (role, battle_id, merged, digest, stream_id))
                groups.setdefault(merged, []).append(report)
            full = not self._room(len(groups))
            if full:
                self.db.rollback()
            else:
                for offset, (name, items) in enumerate(groups.items()):
                    row = {"captured_at": captured_at, "channel": name, "reports": items}
                    self.db.execute("INSERT INTO rows VALUES (?,?,?)", (stream_id, seq + offset, json.dumps(row)))
        if full:
            self._warn_full()
            return 0
        return len(groups)

    def batch(self, skip=()):
        """(stream_id, first seq, rows) of the oldest stream not in `skip`, or None."""
        with self.lock:
            if skip:
                first = next(((stream_id,) for stream_id, _first in self.db.execute(
                    "SELECT stream_id, min(rowid) AS first FROM rows GROUP BY stream_id ORDER BY first")
                    if stream_id not in skip), None)
            else:
                first = self.db.execute("SELECT stream_id FROM rows ORDER BY rowid LIMIT 1").fetchone()
            if first is None:
                return None
            rows = self.db.execute("SELECT seq,row FROM rows WHERE stream_id=? ORDER BY seq LIMIT ?",
                                   (first[0], MAX_ROWS)).fetchall()
        start, items, size = rows[0][0], [], 0
        for seq, text in rows:
            if seq != start + len(items) or (items and size + len(text) > MAX_BODY):
                break
            items.append(json.loads(text))
            size += len(text)
        return first[0], start, items

    def acknowledge(self, stream_id, cursor):
        with self.lock:
            self.db.execute("DELETE FROM rows WHERE stream_id=? AND seq<=?", (stream_id, cursor))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM rows").fetchone()[0]

    def drop(self, stream_id):
        with self.lock:
            count = self.db.execute("DELETE FROM rows WHERE stream_id=?", (stream_id,)).rowcount
            self.db.execute("DELETE FROM reports WHERE stream_id=?", (stream_id,))
            self.db.commit()
        return count


def frame_row(captured_at, client, frame):
    trimmed = frame.rstrip(b"\0")  # some messages pad their body with NULs to a buffer size
    row = {"captured_at": captured_at, "client": client, "message_id": message_id(frame, client),
           "frame": base64.b64encode(trimmed).decode("ascii")}
    if len(trimmed) < len(frame):
        row["pad"] = len(frame) - len(trimmed)
    return row


class Uploader:
    """Packets in (from the capture thread), rows out to the server (upload thread)."""

    def __init__(self, queue, server, tokens, log=None):
        self.queue, self.server, self.tokens = queue, server, tokens
        self.log = log or (lambda **fields: print(json.dumps(fields, ensure_ascii=False), flush=True))
        dropped = queue.bind(server.origin, tokens)
        if dropped:
            self.log(event="queue_reset", reason="server or tokens changed", rows=dropped)
        self.splitter = Splitter()
        self.links = {}  # connection -> [stream id, last seq, last seen]
        self.roles = {}  # stream id -> role, as the server said
        self.dead = set()  # streams the server refused: their connection starts a new one
        self.drop = {"client": set(), "server": set()}  # message ids the server does not want
        self.waiting = {}  # stream id -> [monotonic time to try again, wait]: held back by the server
        self.swept = time.monotonic()
        self.stop = threading.Event()
        self.error = None
        self.uploaded_at = None  # time of the last acknowledged batch
        self.uploaded = 0  # rows acknowledged since the start
        # Called when the server names a stream's role the first time (a login just claimed it, or
        # a waiting token was approved): the window looks at the accounts at once instead of
        # showing "waiting" for up to a minute (2026-10-23: a player removed the token just
        # approved and joined again).
        self.on_role = None

    # capture thread -----------------------------------------------------------------

    def on_packet(self, captured_at, linktype, packet):
        for ends, at, client, frame in self.splitter.feed(captured_at, linktype, packet):
            if message_id(frame, client) in self.drop["client" if client else "server"]:
                continue
            self.add_frame(ends, at, client, frame)
        now = time.monotonic()
        if now - self.swept > 60:
            self.swept = now
            for ends, link in list(self.links.items()):
                if now - link[2] > LINK_IDLE:
                    del self.links[ends]
                    self.splitter.forget(ends)

    def add_frame(self, ends, at, client, frame):
        channel = None if client else BATTLE_LISTS.get(message_id(frame, client))
        if channel is None:
            self.add(ends, frame_row(at, client, frame))
            return
        reports = battle_reports(body(frame, client)[1])
        if not reports:
            return
        link = self.stream(ends)
        link[1] += self.queue.add_reports(self.roles.get(link[0]), link[0], link[1] + 1, at, channel, reports)

    def connections(self, within=300):
        """Open game connections seen in the last `within` seconds (a closed one, as after
        switching accounts, no longer counts)."""
        now = time.monotonic()
        closed = self.splitter.closed
        return sum(1 for ends, link in list(self.links.items())
                   if now - link[2] < within and ends not in closed)

    def stream(self, ends):
        link = self.links.get(ends)
        if link is None or link[0] in self.dead:
            link = self.links[ends] = [uuid.uuid4().hex, 0, time.monotonic()]
        link[2] = time.monotonic()
        return link

    def add(self, ends, row):
        link = self.stream(ends)
        if self.queue.add(link[0], link[1] + 1, row):
            link[1] += 1

    # upload thread ------------------------------------------------------------------

    def upload_once(self):
        """Send one batch; False when the queue is empty."""
        now = time.monotonic()
        batch = self.queue.batch({stream_id for stream_id, (until, _wait) in self.waiting.items() if until > now})
        if batch is None:
            return False
        stream_id, start, rows = batch
        try:
            result = self.server.json(PATH, self.tokens, {"stream_id": stream_id, "start_seq": start,
                                                          "frames": rows}, compress=True, timeout=30)
        except HTTPError as exc:
            if exc.code == 503 and _reply(exc).get("retry") == "stream":
                # Only this stream waits (its token awaits approval): the others go on meanwhile.
                wait = min(WAIT_MAX, self.waiting[stream_id][1] * 2) if stream_id in self.waiting else WAIT_FIRST
                if stream_id not in self.waiting:
                    self.log(event="stream_waiting", reason=_error(exc))
                self.waiting[stream_id] = [time.monotonic() + wait, wait]
                return True
            if exc.code not in (400, 409):
                raise
            # The server will never take this stream (a gap, invalid data): it would block the rest.
            self.dead.add(stream_id)
            self.waiting.pop(stream_id, None)
            self.log(event="stream_dropped", status=exc.code, reason=_error(exc),
                     rows=self.queue.drop(stream_id))
            return True
        if result.get("stream_id") != stream_id or type(result.get("cursor")) is not int:
            raise ValueError("server did not acknowledge the batch")
        self.queue.acknowledge(stream_id, result["cursor"])
        self.waiting.pop(stream_id, None)
        self.uploaded_at = time.time()
        self.uploaded += len(rows)
        if type(result.get("role")) is int:
            new = self.roles.get(stream_id) != result["role"]
            self.roles[stream_id] = result["role"]
            if new and self.on_role:
                self.on_role()
        if isinstance(result.get("drop"), dict):
            self.drop = {side: set(result["drop"].get(side) or ()) for side in ("client", "server")}
        if result.get("rejected_tokens"):
            self.log(event="tokens_rejected", count=result["rejected_tokens"])
        return True

    def run(self):
        last_error = None
        while not self.stop.is_set():
            try:
                busy = self.upload_once()
                last_error = None
            except HTTPError as exc:
                if exc.code == 401:
                    self.error = RuntimeError("every token was rejected (HTTP 401)")
                    self.stop.set()
                    return
                busy, summary = False, f"HTTP {exc.code} {_error(exc)}".strip()
                if summary != last_error:
                    # 503 includes a new token waiting for approval: the queue keeps its rows.
                    print(f"upload deferred: {summary}", file=sys.stderr, flush=True)
                    last_error = summary
            except (OSError, ValueError) as exc:
                busy, summary = False, f"{type(exc).__name__}: {exc}"
                if summary != last_error:
                    print(f"upload deferred: {summary}", file=sys.stderr, flush=True)
                    last_error = summary
            self.stop.wait(0.1 if busy else 1 if last_error is None else 10)


def _reply(exc):
    """The JSON body of an HTTP error ({} when there is none); read once, kept on the error."""
    if not hasattr(exc, "reply"):
        try:
            value = json.loads(exc.read() or b"{}")
        except (OSError, ValueError, AttributeError):
            value = {}
        exc.reply = value if isinstance(value, dict) else {}
    return exc.reply


def _error(exc):
    return str(_reply(exc).get("error", ""))
