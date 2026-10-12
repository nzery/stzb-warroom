"""Updates: the newest release, from the website (``<site>/download/latest.json``, a copy of the
GitHub release kept there for players who cannot reach GitHub quickly), else from GitHub.

A release built on the same runtime as this package (Python, its modules and Electron, which
config.json's "runtime" names) needs only the program's own code: a small zip of stzb_warroom,
downloaded into ``<state folder>/app/<version>`` and named in ``app/current.json``. The
launcher (packaging/windows/launcher.py) runs that code instead of the exe's own from the next
start. Any other release (or a run from source) is downloaded whole by the player.
"""

import hashlib
import io
import json
import os
import platform
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin
from urllib.request import Request

from . import __version__
from .api import packaged, site
from .runner import state_dir

GITHUB = "https://api.github.com/repos/nzery/stzb-warroom/releases/latest"
APP_LIMIT = 20 * 1024 * 1024  # the code is well under 1 MB
NOTES_LIMIT = 8000  # characters of a release's notes shown to the player
SHA256 = set("0123456789abcdef")


def notes(text):
    """What a release says it changed, for the player (GitHub's changelog link left out)."""
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    return "\n".join(line for line in lines if not line.startswith("**Full Changelog**")).strip()[:NOTES_LIMIT]


def version(text):
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return ()


def get(opener, url, timeout):
    """Open `url` saying which version asks (the website counts the clients by version)."""
    agent = f"stzb-warroom/{__version__} ({platform.system()} {platform.release()}; runtime {packaged().get('runtime') or '-'})"
    return opener(Request(url, headers={"User-Agent": agent}), timeout=timeout)


def folder():
    """Where updates of the code go; the launcher looks in the same place."""
    return state_dir() / "app"


def latest(opener):
    """The newest release -> {"version", "runtime", "url" (the whole package, or its page),
    "app": {"url", "sha256", "size"} or None, "notes" (what it changed, may be "")}.
    ValueError when neither place answers."""
    errors = []
    origin = site()
    if origin:
        index = origin.rstrip("/") + "/download/latest.json"
        try:
            with get(opener, index, 15) as response:
                return _from_site(index, json.load(response))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append(exc)
    try:
        with get(opener, GITHUB, 15) as response:
            release = json.load(response)
        return {"version": str(release["tag_name"]).lstrip("v"), "runtime": None,
                "url": release.get("html_url"), "app": None, "notes": notes(release.get("body"))}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(exc)
    raise ValueError(f"检查更新失败：{errors[0]}")


def _from_site(index, release):
    def file(entry):
        name = str(entry["file"])
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("bad file name")
        digest = str(entry["sha256"]).lower()
        if len(digest) != 64 or not set(digest) <= SHA256:
            raise ValueError("bad sha256")
        return {"url": urljoin(index, name), "sha256": digest, "size": int(entry["size"])}

    if not version(release["version"]):
        raise ValueError("bad version")
    app = release.get("app")
    return {"version": str(release["version"]), "runtime": release.get("runtime") or None,
            "url": file(release["full"])["url"], "app": file(app) if app else None,
            "notes": notes(release.get("notes"))}


def code_only(release, place=None):
    """Whether only the code needs downloading: this is the package, built on the release's runtime,
    and that version's code has not already failed to start here."""
    runtime = packaged().get("runtime")
    if not (getattr(sys, "frozen", False) and runtime and release.get("app") and release.get("runtime") == runtime):
        return False
    try:
        failed = json.loads(((place or folder()) / "failed.json").read_text(encoding="utf-8"))
        return failed.get("version") != release["version"]
    except (OSError, ValueError, AttributeError):
        return True


def installed(release, place=None):
    """Whether the release's code is already downloaded and is what the next start runs."""
    place = place or folder()
    try:
        current = json.loads((place / "current.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(current, dict) and current.get("version") == release["version"]
            and (place / release["version"] / "stzb_warroom" / "__main__.py").is_file())


def install(release, opener, place=None):
    """Download the release's code and make it the one the launcher runs from the next start."""
    place = place or folder()
    if installed(release, place):
        return
    app = release["app"]
    try:
        with get(opener, app["url"], 60) as response:
            data = response.read(APP_LIMIT + 1)
    except OSError as exc:
        raise ValueError(f"下载更新失败：{exc}") from None
    if len(data) != app["size"] or hashlib.sha256(data).hexdigest() != app["sha256"]:
        raise ValueError("下载的更新不完整，稍后会再试")
    name = release["version"]
    staging = place / (name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.namelist():
            path = PurePosixPath(member)
            if path.is_absolute() or ".." in path.parts or "\\" in member or path.parts[0] != "stzb_warroom":
                raise ValueError("更新包的内容不对")
        archive.extractall(staging)
    if not (staging / "stzb_warroom" / "__main__.py").is_file():
        raise ValueError("更新包的内容不对")
    target = place / name
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    temporary = place / "current.json.tmp"
    temporary.write_text(json.dumps({"version": name, "runtime": release["runtime"]}), encoding="utf-8")
    os.replace(temporary, place / "current.json")
    clean(place, keep={name, running()})


def running():
    """The version of the downloaded code this process runs, or None (the exe's own)."""
    here = Path(__file__).resolve().parent.parent
    return here.name if here.parent == folder().resolve() else None


def clean(place, keep):
    """Drop the downloaded versions other than `keep`."""
    for path in place.iterdir():
        if path.is_dir() and path.name not in keep:
            shutil.rmtree(path, ignore_errors=True)


def newer(release):
    return version(release["version"]) > version(__version__)
