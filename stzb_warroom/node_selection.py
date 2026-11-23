"""Time the phone WebSocket handshake and the relay's exit-readiness check."""
import re
import threading
import time
import uuid

from . import relay


def candidates(page):
    rows = page.get("candidates", [])
    for row in rows:
        host = row["host"]
        if (len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                      for label in host.split("."))
                or not re.fullmatch("[0-9a-f]{64}", row["exit"])):
            raise ValueError("服务器给的节点无效")
        uuid.UUID(row["phone"])
    return rows


def latency(row):
    request = b"\x00" + uuid.UUID(row["phone"]).bytes + b"\x00\x01\x00\x50\x01\x00\x00\x00\x00"
    samples = []
    for _ in range(3):
        start = time.monotonic()
        ws = None
        try:
            ws = relay.WebSocket("https://" + row["host"] + "/relay/vless", {}, timeout=2)
            ws.send(request)
            reply = ws.recv()
            if not reply or not reply[1].startswith(b"\x00\x00HTTP/1.1 204 "):
                return None
            samples.append(time.monotonic() - start)
        except (OSError, ValueError, relay.Refused):
            return None
        finally:
            if ws:
                ws.close()
    return sorted(samples)[1]  # all three probes succeeded


def choose(rows):
    pending = iter(rows)
    lock = threading.Lock()
    usable = []
    errors = []

    def probe():
        while True:
            with lock:
                row = next(pending, None)
            if row is None:
                return
            try:
                elapsed = latency(row)
                if elapsed is not None:
                    with lock:
                        usable.append((elapsed, row["host"]))
            except Exception as exc:
                with lock:
                    errors.append(exc)
                return

    workers = [threading.Thread(target=probe) for _ in range(min(8, len(rows)))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if errors:
        raise errors[0]
    if not usable:
        raise ValueError("暂时没有可用的手机线路，请确认电脑上报已开启，稍后再试")
    return min(usable)
