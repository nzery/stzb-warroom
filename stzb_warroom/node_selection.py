"""Time the phone WebSocket handshake and the relay's exit-readiness check."""
from concurrent.futures import ThreadPoolExecutor
import re
from statistics import median
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
    return median(samples)


def choose(rows):
    with ThreadPoolExecutor(max_workers=8) as pool:
        times = list(pool.map(latency, rows))
    usable = [(t, row["host"]) for t, row in zip(times, rows) if t is not None]
    if not usable:
        raise ValueError("暂时没有可用的手机线路，请确认电脑上报已开启，稍后再试")
    return min(usable)
