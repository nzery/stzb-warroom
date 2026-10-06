"""The server's address (ST_SERVER, or the one the package was built for) and the tokens (ST_CLIENT_TOKEN)."""

import gzip
import json
import os
import re
import ssl
import sys
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit


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
            context=ssl.create_default_context(cafile=ca)))

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
