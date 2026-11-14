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

from .api import ConnectTimeout
from .frames import Splitter, body, message_id

PATH = "/v2/capture/frames"
# Report lists: the personal one and the alliance one.
BATTLE_LISTS = {0x0A: "personal", 0x5C: "alliance"}
# The game's own time sync: about once a minute the game asks and its server answers [its time in
# milliseconds]. Countdowns to the game's times (an army's landing) go by it, not by the computer's
# clock, which may be off (2026-11-11 players: countdowns not right).
SYNC_SERVER_TIME = 0x2B6
MAX_ROWS = 100  # per request, as the server takes them
MAX_BODY = 3 * 1024 * 1024
MAX_QUEUED = 50000
LINK_IDLE = 3600  # a connection unseen this long is forgotten
PLAYING_WITHIN = 300  # a packet this recent when the network changed: a game was being captured
LOST_AFTER = 60  # seconds after the change with nothing captured before the window says so
LOST_FOR = 600  # and until when: by then the game was more likely closed
WAIT_FIRST, WAIT_MAX = 15, 300  # a held-back stream is tried again after this, doubling up to the max
# A batch is answered in milliseconds once it has arrived. In the evening the line to the server
# loses packets and one connection now and then stalls outright while new ones go through
# (2026-11-14 20:40-21:30: about 20 players' uploads waited 35-70 s each, notifications 30-80 s
# late): with nothing moving for this long (seconds) the batch is sent again at once on a new
# connection. The limit grows with failures in a row so that a slow but working line still gets
# a large batch through.
STALL_AFTER = (4, 4, 6, 8, 10)
RETRY_AFTER = (0, 0, 0.5, 1, 2)  # seconds before the next try, by failures in a row
SLOW_BATCH = 3  # a batch taking longer than this is logged
# This run of the program, in each batch's `timing`: the server measures how late a batch is from
# this program's monotonic clock (not the computer's, which may be off or be set meanwhile), and a
# new run's clock starts elsewhere.
RUN = uuid.uuid4().hex[:16]
MAX_STALLS = 20  # interruptions told to the server at most, the newest


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
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        # How many battle reports a row carries (0 for a game frame): the window counts reports, not
        # rows (2026-11-13 user), and keeps the count across launches in meta.
        if "reports" not in {column[1] for column in self.db.execute("PRAGMA table_info(rows)")}:
            self.db.execute("ALTER TABLE rows ADD COLUMN reports INTEGER NOT NULL DEFAULT 0")
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
                self.db.execute("INSERT INTO rows (stream_id, seq, row) VALUES (?,?,?)",
                                (stream_id, seq, json.dumps(row)))
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
                    self.db.execute("INSERT INTO rows (stream_id, seq, row, reports) VALUES (?,?,?,?)",
                                    (stream_id, seq + offset, json.dumps(row), len(items)))
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
        """Delete the rows the server took; the battle reports among them add to the total kept."""
        with self.lock, self.db:
            reports = self.db.execute("SELECT coalesce(sum(reports), 0) FROM rows WHERE stream_id=? AND seq<=?",
                                      (stream_id, cursor)).fetchone()[0]
            self.db.execute("DELETE FROM rows WHERE stream_id=? AND seq<=?", (stream_id, cursor))
            if reports:
                total, _at = self._totals()
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('uploaded', ?)",
                                (json.dumps({"reports": total + reports, "at": time.time()}),))

    def _totals(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='uploaded'").fetchone()
        try:
            value = json.loads(row[0]) if row else {}
            return int(value.get("reports") or 0), (float(value["at"]) if value.get("at") else None)
        except (ValueError, TypeError, AttributeError):
            return 0, None

    def totals(self):
        """(battle reports uploaded from this computer, when the last of them went), kept across launches."""
        with self.lock:
            return self._totals()

    def close(self):
        with self.lock:
            self.db.close()

    def pending(self):
        """Battle reports waiting to go (the window shows reports, not rows)."""
        with self.lock:
            return self.db.execute("SELECT coalesce(sum(reports), 0) FROM rows").fetchone()[0]

    def drop(self, stream_id):
        with self.lock:
            count = self.db.execute("DELETE FROM rows WHERE stream_id=?", (stream_id,)).rowcount
            self.db.execute("DELETE FROM reports WHERE stream_id=?", (stream_id,))
            self.db.commit()
        return count


def saved_totals(path):
    """Queue.totals() of the queue at `path` without opening it for writing: (0, None) when there is none."""
    try:
        db = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            row = db.execute("SELECT value FROM meta WHERE key='uploaded'").fetchone()
        finally:
            db.close()
        value = json.loads(row[0]) if row else {}
        return int(value.get("reports") or 0), (float(value["at"]) if value.get("at") else None)
    except (sqlite3.Error, ValueError, TypeError, AttributeError, OSError):
        return 0, None


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
        # The upload's own connection to the server, kept open between batches (api.Connection).
        self.http = server.connection() if hasattr(server, "connection") else server
        # Set when a row is queued: the upload thread, waiting on an empty queue, sends it at once
        # (it looked again only every second, which added up to a second to what a click shows).
        self.queued = threading.Event()
        self.log = log or (lambda **fields: print(json.dumps(fields, ensure_ascii=False), flush=True))
        dropped = queue.bind(server.origin, tokens)
        if dropped:
            self.log(event="queue_reset", reason="server or tokens changed", rows=dropped)
        self.splitter = Splitter()
        self.links = {}  # connection -> [stream id, last seq, last seen]
        self.roles = {}  # stream id -> role, as the server said
        self.dead = set()  # streams the server refused: their connection starts a new one
        self.drop = {"client": set(), "server": set()}  # message ids the server does not want
        self.whole = set()  # report lists the server also wants as they came, besides their reports
        self.waiting = {}  # stream id -> [monotonic time to try again, wait]: held back by the server
        self.swept = time.monotonic()
        self.stop = threading.Event()
        self.error = None
        self.failures, self.failed_at = 0, 0.0  # network failures in a row, since when
        # stream id -> [[seq, monotonic time it was queued]]: how long a batch's oldest row waited
        self.queued_at = {}
        self.queued_lock = threading.Lock()
        self.stalls = []  # interruptions not yet told to the server: [seconds, failures, unanswered, ended]
        self.told = False  # the last batch taken told the interruption going on
        self.unanswered = 0  # handshakes unanswered meanwhile (api.ConnectTimeout)
        # Battle reports the server took from this computer, all launches together, and when the last went.
        self.uploaded, self.uploaded_at = queue.totals()
        self.heard_at = time.monotonic()  # the last packet captured
        self.heard = False  # any packet at all since the start
        self.moved_at = None  # when the computer changed network while a game was being captured
        self.moved = False  # the network changed since the upload's last request
        # Called when the server names a stream's role the first time (a login just claimed it, or
        # a waiting token was approved): the window looks at the accounts at once instead of
        # showing "waiting" for up to a minute (2026-10-23: a player removed the token just
        # approved and joined again).
        self.on_role = None
        # Called with how far the game's clock is ahead of this computer's (seconds) at each of the
        # game's time syncs, on the capture thread.
        self.on_clock = None

    # capture thread -----------------------------------------------------------------

    def network_moved(self):
        """The computer changed network; only counts when a game was being captured just before."""
        now = time.monotonic()
        if self.heard and now - self.heard_at <= PLAYING_WITHIN:
            self.moved_at = now
        self.moved = True  # the upload's connection went by the old network: open it again

    def lost_after_move(self):
        """A game was being captured, the computer changed network and nothing was captured since.

        Without the phone's relay nothing tells a game that went quiet from a game not captured,
        so this asks the player only for a while after the move (the game may have been closed).
        """
        if self.moved_at is None or self.heard_at >= self.moved_at:
            return False
        return LOST_AFTER <= time.monotonic() - self.moved_at <= LOST_FOR

    def on_packet(self, captured_at, linktype, packet):
        self.heard_at = time.monotonic()
        self.heard = True
        for ends, at, client, frame in self.splitter.feed(captured_at, linktype, packet):
            if not client and message_id(frame, client) == SYNC_SERVER_TIME:
                self.game_clock(at, frame)
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

    def game_clock(self, at, frame):
        """The game server's time in its time sync reply, against when this computer captured it."""
        value = body(frame, False)[1]
        if not (isinstance(value, list) and len(value) == 1 and type(value[0]) in (int, float)):
            return
        game = value[0] / 1000
        if 1.5e9 < game < 4e9 and self.on_clock:  # 2017 to 2096: milliseconds since 1970
            self.on_clock(game - at)

    def add_frame(self, ends, at, client, frame):
        channel = None if client else BATTLE_LISTS.get(message_id(frame, client))
        if channel is None or message_id(frame, client) in self.whole:
            self.add(ends, frame_row(at, client, frame))
        if channel is None:
            return
        reports = battle_reports(body(frame, client)[1])
        if not reports:
            return
        link = self.stream(ends)
        added = self.queue.add_reports(self.roles.get(link[0]), link[0], link[1] + 1, at, channel, reports)
        if added:
            self._queued(link[0], range(link[1] + 1, link[1] + added + 1))
        link[1] += added
        if added:
            self.queued.set()

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
            self._queued(link[0], (link[1],))
            self.queued.set()

    def _queued(self, stream_id, seqs):
        now = time.monotonic()
        with self.queued_lock:
            times = self.queued_at.setdefault(stream_id, [])
            times.extend([seq, now] for seq in seqs)
            if len(times) > MAX_QUEUED:
                del times[:len(times) - MAX_QUEUED]

    def _waited(self, stream_id, start, now):
        """Seconds the row `start` of the stream has waited (None: queued by an earlier run)."""
        with self.queued_lock:
            for seq, at in self.queued_at.get(stream_id, ()):
                if seq == start:
                    return now - at
                if seq > start:
                    break
        return None

    def _acknowledged(self, stream_id, cursor):
        with self.queued_lock:
            times = self.queued_at.get(stream_id)
            if times is not None:
                keep = next((i for i, (seq, _at) in enumerate(times) if seq > cursor), len(times))
                del times[:keep]
                if not times:
                    del self.queued_at[stream_id]

    def _report(self, now):
        """What the batch says besides its rows: its timing and the interruptions to tell."""
        extra = {"timing": {"sent": round(now, 3), "run": RUN}}
        stalls = [{"seconds": round(seconds, 1), "failures": failures, "unanswered": unanswered,
                   "ago": round(now - ended, 1)} for seconds, failures, unanswered, ended in self.stalls]
        if self.failures or self.unanswered:  # this one, should it go through now
            stalls.append({"seconds": round(now - self.failed_at, 1), "failures": self.failures,
                           "unanswered": self.unanswered, "ago": 0.0})
        if stalls:
            extra["stalls"] = stalls[-MAX_STALLS:]
        return extra

    # upload thread ------------------------------------------------------------------

    def upload_once(self):
        """Send one batch; False when the queue is empty."""
        now = time.monotonic()
        batch = self.queue.batch({stream_id for stream_id, (until, _wait) in self.waiting.items() if until > now})
        if batch is None:
            return False
        stream_id, start, rows = batch
        if self.moved and hasattr(self.http, "close"):
            self.moved = False
            self.http.close()
        started = time.monotonic()
        extra = self._report(started)
        waited = self._waited(stream_id, start, started)
        if waited is not None:
            extra["timing"]["age"] = round(waited, 3)
        try:
            result = self.http.json(PATH, self.tokens, {"stream_id": stream_id, "start_seq": start,
                                                          "frames": rows, **extra}, compress=True,
                                    timeout=STALL_AFTER[min(self.failures, len(STALL_AFTER) - 1)])
            took = time.monotonic() - started
            if took > SLOW_BATCH:
                self.log(event="upload_slow", seconds=round(took, 1), rows=len(rows))
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
        self._acknowledged(stream_id, result["cursor"])
        self.stalls = []  # told
        self.told = bool(self.failures or self.unanswered)  # the one going on too
        self.waiting.pop(stream_id, None)
        self.uploaded, self.uploaded_at = self.queue.totals()
        if type(result.get("role")) is int:
            new = self.roles.get(stream_id) != result["role"]
            self.roles[stream_id] = result["role"]
            if new and self.on_role:
                self.on_role()
        if isinstance(result.get("drop"), dict):
            self.drop = {side: set(result["drop"].get(side) or ()) for side in ("client", "server")}
        self.whole = set(result.get("whole") or ())
        if result.get("rejected_tokens"):
            self.log(event="tokens_rejected", count=result["rejected_tokens"])
        return True

    def run(self):
        last_error = None
        while not self.stop.is_set():
            try:
                busy = self.upload_once()
                last_error = None
                if self.failures or self.unanswered:
                    now = time.monotonic()
                    self.log(event="upload_recovered", failures=self.failures, unanswered=self.unanswered,
                             seconds=round(now - self.failed_at, 1))
                    if not self.told:  # the batch that went was not taken (held back, refused): next time
                        self.stalls = (self.stalls + [[now - self.failed_at, self.failures, self.unanswered, now]])[-MAX_STALLS:]
                    self.failures = self.unanswered = 0
                self.told = False
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
            except ConnectTimeout:
                # No answer to the handshake: at once from another port, the stall limit as it was.
                if not (self.failures or self.unanswered):
                    self.failed_at = time.monotonic()
                self.unanswered += 1
                if self.unanswered == 1:
                    self.log(event="upload_failed", error="connect timed out", failures=self.failures)
                last_error = "connect timed out"
                continue
            except (OSError, ValueError) as exc:
                # A stalled or broken connection (Connection.json closed it): again on a new one.
                if not (self.failures or self.unanswered):
                    self.failed_at = time.monotonic()
                self.failures += 1
                busy, last_error = False, f"{type(exc).__name__}: {exc}"
                self.log(event="upload_failed", error=last_error[:120], failures=self.failures)
                self.stop.wait(RETRY_AFTER[min(self.failures - 1, len(RETRY_AFTER) - 1)])
                continue
            if busy or last_error is not None:
                self.stop.wait(0.1 if busy else 10)
            elif self.queued.wait(1):  # the queue was empty: go as soon as a row comes
                self.queued.clear()
        if hasattr(self.http, "close"):
            self.http.close()


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
