"""The server's address (ST_SERVER, or the one the package was built for), the tokens (ST_CLIENT_TOKEN),
and joining an alliance with an invitation code (which gives a token)."""

import gzip
import http.client
import io
import json
import os
import re
import ssl
import sys
import time
from pathlib import Path
from urllib import request
from urllib.error import HTTPError
from urllib.parse import urlsplit

from . import __version__

INVITE = re.compile(r"stj_[A-Za-z0-9_-]{22}")
# A code or token inside what was copied ("邀请码：stj_…", a chat line, full-width punctuation around it).
INVITE_IN = re.compile(r"(?<![A-Za-z0-9_-])stj_[A-Za-z0-9_-]{22}(?![A-Za-z0-9_-])")
TOKEN_IN = re.compile(r"(?<![A-Za-z0-9_-])sta_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
# Every request and relay socket names the client's version (0.0.0 run from source): the server
# tells an old major version to upgrade, and its access log tells this client from other callers.
CLIENT_HEADERS = (("User-Agent", f"stzb-warroom/{__version__}"), ("X-Client-Version", __version__))

# Let's Encrypt's roots (ISRG Root X1 and X2), which sign the server's certificate. Python checks a certificate with
# OpenSSL against the roots Windows has stored, and Windows stores a root only once something it verifies itself
# needs it; a computer without X1 that kept the old copy cross-signed by DST Root CA X3 (expired 2024) failed with
# "certificate has expired". Added beside the system's roots, never instead of them.
ISRG_ROOTS = """\
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAw
TzELMAkGA1UEBhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2Vh
cmNoIEdyb3VwMRUwEwYDVQQDEwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4
WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJu
ZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBY
MTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54rVygc
h77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+
0TM8ukj13Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6U
A5/TR5d8mUgjU+g4rk8Kb4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sW
T8KOEUt+zwvo/7V3LvSye0rgTBIlDHCNAymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyH
B5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ4Q7e2RCOFvu396j3x+UC
B5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf1b0SHzUv
KBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWn
OlFuhjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTn
jh8BCNAw1FtxNrQHusEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbw
qHyGO0aoSCqI3Haadr8faqU9GY/rOPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CI
rU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4GA1UdDwEB/wQEAwIBBjAPBgNV
HRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY9umbbjANBgkq
hkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ
3BebYhtF8GaV0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KK
NFtY2PwByVS5uCbMiogziUwthDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5
ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJwTdwJx4nLCgdNbOhdjsnvzqvHu7Ur
TkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nxe5AW0wdeRlN8NwdC
jNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZAJzVc
oyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq
4RgqsahDYVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPA
mRGunUHBcnWEvgJBQl9nJEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57d
emyPxgcYxn/eR44/KJ4EBs+lVDR3veyJm+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIICGzCCAaGgAwIBAgIQQdKd0XLq7qeAwSxs6S+HUjAKBggqhkjOPQQDAzBPMQsw
CQYDVQQGEwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2gg
R3JvdXAxFTATBgNVBAMTDElTUkcgUm9vdCBYMjAeFw0yMDA5MDQwMDAwMDBaFw00
MDA5MTcxNjAwMDBaME8xCzAJBgNVBAYTAlVTMSkwJwYDVQQKEyBJbnRlcm5ldCBT
ZWN1cml0eSBSZXNlYXJjaCBHcm91cDEVMBMGA1UEAxMMSVNSRyBSb290IFgyMHYw
EAYHKoZIzj0CAQYFK4EEACIDYgAEzZvVn4CDCuwJSvMWSj5cz3es3mcFDR0HttwW
+1qLFNvicWDEukWVEYmO6gbf9yoWHKS5xcUy4APgHoIYOIvXRdgKam7mAHf7AlF9
ItgKbppbd9/w+kHsOdx1ymgHDB/qo0IwQDAOBgNVHQ8BAf8EBAMCAQYwDwYDVR0T
AQH/BAUwAwEB/zAdBgNVHQ4EFgQUfEKWrt5LSDv6kviejM9ti6lyN5UwCgYIKoZI
zj0EAwMDaAAwZQIwe3lORlCEwkSHRhtFcP9Ymd70/aTSVaYgLXTWNLxBo1BfASdW
tL4ndQavEi51mI38AjEAi/V3bNTIZargCyzuFJ0nN6T5U6VR5CmD1/iQMVtCnwr1
/q4AaOeMSQ+2b1tbFfLn
-----END CERTIFICATE-----
"""


def tls_context(cafile=None):
    """The default TLS context, plus Let's Encrypt's roots (not when a CA file is given)."""
    context = ssl.create_default_context(cafile=cafile)
    if cafile is None:
        context.load_verify_locations(cadata=ISRG_ROOTS)
    return context


def packaged():
    """What the Windows package was built with: config.json beside the exe ({"server", "site"})."""
    if not getattr(sys, "frozen", False):
        return {}
    try:
        return json.loads((Path(sys.executable).parent / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default_server():
    """ST_SERVER, else the package's server; None when neither is set."""
    return os.environ.get("ST_SERVER") or packaged().get("server")


def site():
    """The website the window links to (ST_SITE, else the package's); None when there is none."""
    return os.environ.get("ST_SITE") or packaged().get("site")


def invite_code(text):
    """The invitation code in what the player pasted (words around it allowed), or None when there is
    none or the paste also holds a token (that one is added instead)."""
    text = str(text or "")
    found = INVITE_IN.findall(text)
    return found[0] if len(found) == 1 and not TOKEN_IN.search(text) else None


def tokens_in(text):
    """The tokens in what the player pasted, words around them allowed."""
    return list(dict.fromkeys(TOKEN_IN.findall(str(text or ""))))


def client_tokens(value=None):
    """ST_CLIENT_TOKEN: one token, or several (one per game account) separated by commas."""
    raw = os.environ.get("ST_CLIENT_TOKEN", "") if value is None else value
    tokens = list(dict.fromkeys(token for token in re.split(r"[\s,]+", raw) if token))
    if not tokens:
        raise SystemExit("ST_CLIENT_TOKEN is required")
    return tokens


def request_body(tokens, body=None, compress=False):
    """(data, headers) of a request naming `tokens` (one, or a list) and carrying `body` as JSON."""
    headers = {"Authorization": "Bearer " + (tokens if isinstance(tokens, str) else ",".join(tokens))}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
        if compress:
            data, headers["Content-Encoding"] = gzip.compress(data), "gzip"
    return data, headers


class Server:
    """Requests to the server, never through a local proxy."""

    def __init__(self, origin, ca=None):
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"))):
            raise SystemExit("server must be an HTTPS origin (or loopback HTTP)")
        self.origin = origin.rstrip("/")
        self.context = tls_context(ca)
        self.opener = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(context=self.context))
        self.opener.addheaders = list(CLIENT_HEADERS)
        self.outdated = None  # called when the server answers 426: it no longer serves this version

    def _send(self, req, timeout):
        try:
            return self.opener.open(req, timeout=timeout)
        except HTTPError as exc:
            if exc.code == 426 and self.outdated:
                self.outdated()
            raise

    def open(self, path, tokens, body=None, compress=False, timeout=45, method=None):
        """`tokens`: one token, or a list (the capture upload names all of them)."""
        data, headers = request_body(tokens, body, compress)
        return self._send(request.Request(self.origin + path, data=data, headers=headers, method=method), timeout)

    def connection(self):
        """A connection kept open between requests, for one thread (the upload's)."""
        return Connection(self)

    def post_bytes(self, path, token, data, kind, timeout=45):
        """POST `data` as it is (a chat image), typed `kind`."""
        headers = {"Authorization": "Bearer " + token, "Content-Type": kind}
        return self._send(request.Request(self.origin + path, data=data, headers=headers, method="POST"), timeout)

    def json(self, path, tokens, body=None, compress=False, timeout=45):
        with self.open(path, tokens, body, compress, timeout) as response:
            return json.load(response)

    def join(self, code, tokens=(), timeout=30):
        """Trade an invitation code for a new token -> {"token", "scope", "text"}; code None joins
        the public domain with its current code, which the server looks up (one-click account).
        ValueError with the server's own message when it is refused or unreachable.
        `tokens`: the ones already added, so the server can tell a code pasted twice."""
        data = json.dumps({"code": code} if code else {"public": True}).encode()
        headers = {"Content-Type": "application/json"}
        if tokens:
            headers["Authorization"] = "Bearer " + ",".join(tokens)
        try:
            with self.opener.open(request.Request(self.origin + "/v1/client/join", data=data, method="POST",
                                                  headers=headers),
                                  timeout=timeout) as response:
                reply = json.load(response)
        except HTTPError as exc:
            try:
                message = json.loads(exc.read() or b"{}").get("error")
            except (OSError, ValueError):
                message = None
            raise ValueError(message or f"服务器返回 HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise ValueError(f"连不上服务器：{exc}") from None
        if not isinstance(reply, dict) or not str(reply.get("token") or "").startswith("sta_"):
            raise ValueError("服务器的回复不对，请稍后再试")
        return reply


class Connection:
    """Requests over one connection kept open between them (`Server.connection`), for one thread.

    Each request of `Server` opens its own connection: a TCP and a TLS handshake, two round trips more,
    before the request goes, and a handshake packet lost on the way waits a second or more to be sent
    again. The upload sends a batch every second or so while a game is played, each after the one
    before is answered: on a slow line in the evening (2026-11-13: a player's frames took 1–5 s to
    arrive and what he clicked showed 10 s late) the handshakes were most of the wait.
    A connection left idle is not trusted (the server closes it, a router may drop it silently, the
    computer may have changed network): it is opened again, so a request never waits out its timeout
    on a dead one. One that the server closed while it was in use is retried once on a new one: the
    server takes the same rows again without harm (it acknowledges by sequence number).
    """

    IDLE = 20  # seconds; the server keeps an idle connection for longer than this

    def __init__(self, server):
        self.server, self.conn, self.used_at = server, None, 0.0
        parsed = urlsplit(server.origin)
        self.https, self.host, self.port = parsed.scheme == "https", parsed.hostname, parsed.port

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _open(self, timeout):
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=self.server.context)
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def json(self, path, tokens, body=None, compress=False, timeout=45):
        data, headers = request_body(tokens, body, compress)
        headers.update(dict(self.server.opener.addheaders))
        if time.monotonic() - self.used_at > self.IDLE:
            self.close()
        while True:
            reused = self.conn is not None
            if not reused:
                self.conn = self._open(timeout)
            elif self.conn.sock is not None:
                self.conn.sock.settimeout(timeout)
            try:
                self.conn.request("GET" if data is None else "POST", path, body=data, headers=headers)
                response = self.conn.getresponse()
                payload = response.read()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                    http.client.RemoteDisconnected) as exc:
                self.close()
                if reused:
                    continue  # the server closed it between requests: once more on a new one
                raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc
            except http.client.HTTPException as exc:  # not an OSError: the upload counts it as one
                self.close()
                raise ConnectionError(f"{type(exc).__name__}: {exc}") from exc
            except BaseException:
                self.close()
                raise
            break
        self.used_at = time.monotonic()
        if response.will_close:
            self.close()
        if response.status >= 300:
            if response.status == 426 and self.server.outdated:
                self.server.outdated()
            raise HTTPError(self.server.origin + path, response.status, response.reason, response.headers,
                            io.BytesIO(payload))
        return json.loads(payload)
