"""The server's address (ST_SERVER, or the one the package was built for), the tokens (ST_CLIENT_TOKEN),
and joining an alliance with an invitation code (which gives a token)."""

import gzip
import json
import os
import re
import ssl
import sys
from pathlib import Path
from urllib import request
from urllib.error import HTTPError
from urllib.parse import urlsplit

INVITE = re.compile(r"stj_[A-Za-z0-9_-]{22}")

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
    """The invitation code in what the player pasted, or None when it is not one."""
    text = str(text or "").strip()
    return text if INVITE.fullmatch(text) else None


def client_tokens(value=None):
    """ST_CLIENT_TOKEN: one token, or several (one per game account) separated by commas."""
    raw = os.environ.get("ST_CLIENT_TOKEN", "") if value is None else value
    tokens = list(dict.fromkeys(token for token in re.split(r"[\s,]+", raw) if token))
    if not tokens:
        raise SystemExit("ST_CLIENT_TOKEN is required")
    return tokens


class Server:
    """Requests to the server, never through a local proxy."""

    def __init__(self, origin, ca=None):
        parsed = urlsplit(origin)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"))):
            raise SystemExit("server must be an HTTPS origin (or loopback HTTP)")
        self.origin = origin.rstrip("/")
        self.opener = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(
            context=tls_context(ca)))

    def open(self, path, tokens, body=None, compress=False, timeout=45, method=None):
        """`tokens`: one token, or a list (the capture upload names all of them)."""
        headers = {"Authorization": "Bearer " + (tokens if isinstance(tokens, str) else ",".join(tokens))}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
            if compress:
                data, headers["Content-Encoding"] = gzip.compress(data), "gzip"
        return self.opener.open(request.Request(self.origin + path, data=data, headers=headers, method=method),
                                timeout=timeout)

    def json(self, path, tokens, body=None, compress=False, timeout=45):
        with self.open(path, tokens, body, compress, timeout) as response:
            return json.load(response)

    def join(self, code, timeout=30):
        """Trade an invitation code for a new token -> {"token", "scope", "text"}.
        ValueError with the server's own message when it is refused or unreachable."""
        data = json.dumps({"code": code}).encode()
        try:
            with self.opener.open(request.Request(self.origin + "/v1/client/join", data=data, method="POST",
                                                  headers={"Content-Type": "application/json"}),
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
