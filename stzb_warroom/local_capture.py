"""Optional Unix capture transport. Its reply is never an upload acknowledgement."""
import hashlib
import http.client
import json
import socket
import time
from collections import OrderedDict


class LocalCapture:
    def __init__(self, path):
        self.path = path
        self.grants = OrderedDict()
        self.unavailable_until = 0.0

    def prepare(self, tokens, batch):
        if time.monotonic() < self.unavailable_until:
            return None
        stream = batch["stream_id"]
        grant = self.grants.pop(stream, None)
        # Only a fingerprint crosses the local socket. The normal HTTPS request authenticates
        # the player, including when an optional local request times out or is retried.
        header = "Bearer " + (tokens if isinstance(tokens, str) else ",".join(tokens))
        scope = hashlib.sha256(header.encode()).hexdigest()
        # Until the server delegates a stream, offer only its position. The server sends what it
        # does not take (a login) through the original upload path.
        local = {"stream_id": stream, "start_seq": batch["start_seq"]}
        frames = [row for row in batch["frames"] if "reports" not in row] if grant else []
        if frames:
            local["frames"] = frames
        else:
            grant = None
        body = json.dumps({"scope": scope, "batch": local, "grant": grant}, ensure_ascii=False,
                          separators=(",", ":")).encode()
        if len(body) > 3 * 1024 * 1024:
            return None
        conn = http.client.HTTPConnection("localhost", timeout=3)
        try:
            conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.sock.settimeout(3)
            conn.sock.connect(self.path)
            conn.request("POST", "/capture", body, {"Content-Type": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                return None
            raw = response.read(128 * 1024 + 1)
            if len(raw) <= 128 * 1024:
                value = json.loads(raw)
                if isinstance(value, dict) and isinstance(value.get("offer"), str):
                    return {k: v for k, v in value.items() if k in ("offer", "receipt") and isinstance(v, str)}
        except (OSError, ValueError, http.client.HTTPException):
            self.unavailable_until = time.monotonic() + 2
        finally:
            conn.close()
        return None

    def remember(self, stream, result):
        grant = result.get("captureGrant")
        if isinstance(grant, str) and len(grant) < 96 * 1024:
            self.grants[stream] = grant
            if len(self.grants) > 128:
                self.grants.popitem(last=False)
