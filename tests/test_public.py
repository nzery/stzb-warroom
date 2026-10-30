"""The repository is public: no server's address in it, now or in its history.

Only the addresses players use appear (HOSTS); no public IPv4 address other than the examples and
the public resolver in ADDRESSES. The files, every commit's changes and message, and the tags'
notes are checked. Words that must not appear either are listed outside the repository, one per
line, in the file PRIVATE_WORDS (or $ST_PRIVATE_WORDS) names; without it that part is skipped.
"""

import ipaddress
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {"nzery.dev", "stzb.nzery.dev", "api.nzery.dev"}
ADDRESSES = {"223.5.5.5", "1.7.2.38"}  # the resolver the route is picked by; a version number
EXAMPLES = ipaddress.ip_network("1.2.3.0/24")
PRIVATE_WORDS = Path.home() / ".config" / "stzb-warroom" / "private-words.txt"
HOST = re.compile(r"(?<![\w-])(?:[a-z0-9-]+\.)*nzery\.dev\b", re.I)
IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")


def problems(text):
    found = {host.lower() for host in HOST.findall(text)} - HOSTS
    for literal in IPV4.findall(text):
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            continue
        if address.is_global and not address.is_multicast and address not in EXAMPLES \
                and literal not in ADDRESSES:
            found.add(literal)
    return found


def private_words():
    path = Path(os.environ.get("ST_PRIVATE_WORDS") or PRIVATE_WORDS)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.strip().lower() for line in lines if line.strip() and not line.startswith("#")]


def mentions(text, word):
    """Whether `text` (lowercase) has `word`: a Latin one as a whole word ("concat" has no "cat")."""
    if word.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None
    return word in text


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout.decode(
        "utf-8", "replace")


def in_git():
    try:
        return git("rev-parse", "--is-inside-work-tree").strip() == "true"
    except (OSError, subprocess.CalledProcessError):
        return False


class PublicTests(unittest.TestCase):
    def texts(self):
        """(where, text) of every file, and with git, every commit and tag."""
        if in_git():
            names = git("ls-files", "-z").split("\0")
        else:
            names = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*")
                     if path.is_file() and not {".git", "__pycache__", "node_modules"} & set(path.parts)]
        for name in filter(None, names):
            try:
                yield name, (ROOT / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        if in_git():
            for commit in git("log", "--all", "-z", "-p", "--format=COMMIT %h%n%B").split("\0"):
                if commit:
                    yield commit.strip().split("\n", 1)[0], commit
            yield "tags", git("for-each-ref", "refs/tags", "--format=%(refname:short) %(contents)")

    def test_no_server_address(self):
        found = {(where, item) for where, text in self.texts() for item in problems(text)}
        self.assertEqual(found, set())

    def test_no_private_word(self):
        words = private_words()
        if not words:
            self.skipTest("no private words file")
        found = {(where, word) for where, text in self.texts() for word in words if mentions(text.lower(), word)}
        self.assertEqual(found, set())

    def test_the_check_finds_them(self):
        other, address = "other" + ".nzery.dev", "8.8.8." + "8"  # in pieces, not to be found here
        self.assertEqual(problems(f"{other} {address} https://api.nzery.dev/ 1.2.3.4 10.0.0.1"),
                         {other, address})
        self.assertTrue(mentions("a cat here", "cat"))
        self.assertFalse(mentions("concat it", "cat"))
