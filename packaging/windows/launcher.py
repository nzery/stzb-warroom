"""Entry of the Windows exe: the window (``--background``: started at login, no window yet),
unless a command is given (python -m stzb_warroom ...).

The program's code is the copy built into the exe, or a newer one an update downloaded
(stzb_warroom/update.py) into %LOCALAPPDATA%\\stzb-warroom\\app\\<version> for this same runtime
(config.json's "runtime": Python, its modules, Electron). Code that does not even import falls
back to the built-in copy, and that version is not downloaded again (app\\failed.json).

This file is part of the runtime: it changes only with a whole new package.
"""

import json
import os
import sys
from importlib.machinery import FileFinder, SourceFileLoader
from pathlib import Path

NAME = "stzb_warroom"


def version(text):
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return ()


def updates():
    base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return Path(base) / "stzb-warroom" / "app"


def downloaded():
    """The folder of the downloaded code to run, or None."""
    try:
        built = json.loads((Path(sys.executable).resolve().parent / "config.json").read_text(encoding="utf-8"))
        current = json.loads((updates() / "current.json").read_text(encoding="utf-8"))
        folder = updates() / str(current["version"])
        if (built.get("runtime") and current.get("runtime") == built["runtime"]
                and version(current["version"]) > version(built.get("version"))
                and (folder / NAME / "__main__.py").is_file()):
            return folder
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


class Downloaded:
    """Finds stzb_warroom and its modules in the downloaded folder, before the exe's own copy."""

    def __init__(self, folder):
        self.folder = str(folder)

    def find_spec(self, name, path=None, target=None):
        if name != NAME and not name.startswith(NAME + "."):
            return None
        where = self.folder if name == NAME else next(iter(path or ()), None)
        if where is None:
            return None
        return FileFinder(where, (SourceFileLoader, [".py"])).find_spec(name, target)


def load():
    """stzb_warroom's main(): the downloaded code when there is one that imports, else the exe's."""
    folder = downloaded()
    if folder is not None:
        finder = Downloaded(folder)
        sys.meta_path.insert(0, finder)
        try:
            from stzb_warroom import webui  # noqa: F401  (the window and all it uses)
            from stzb_warroom.__main__ import main
            return main
        except Exception:
            sys.meta_path.remove(finder)
            for name in [name for name in sys.modules if name == NAME or name.startswith(NAME + ".")]:
                del sys.modules[name]
            try:
                (updates() / "current.json").replace(updates() / "failed.json")
            except OSError:
                pass
    from stzb_warroom.__main__ import main
    return main


if __name__ == "__main__":
    args = sys.argv[1:]
    load()(["gui", *args] if not args or args == ["--background"] else args)
