"""The release files, after build.ps1 has put the package together in build\\dist\\stzb-warroom:

- stzb-warroom-windows.zip  the whole package (a folder with stzb-warroom.exe)
- stzb-warroom-app.zip      only the program's code (stzb_warroom), for packages on the same runtime
- release.json              {"version", "runtime", "files": {"full"|"app": {"name", "size", "sha256"}}}

The runtime names everything an update of the code alone cannot change: Python and the modules
PyInstaller put in the exe, PyInstaller, the launcher, Electron and the window's own files
(electron\\). Two builds with the same runtime run each other's code, so a player who has one
downloads only the other's stzb-warroom-app.zip (stzb_warroom/update.py). It goes into
config.json with the version, for the launcher.

    python packaging\\windows\\release.py --version 1.4.0 --electron 44.5.1
"""

import argparse
import ast
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def modules(toc):
    """The modules in a PyInstaller PYZ table of contents ((name, path, "PYMODULE") entries)."""
    found = set()

    def walk(value):
        if isinstance(value, (tuple, list)):
            if len(value) == 3 and value[2] == "PYMODULE" and isinstance(value[0], str):
                found.add(value[0])
            else:
                for item in value:
                    walk(item)

    walk(ast.literal_eval(Path(toc).read_text(encoding="utf-8")))
    return found


def runtime(package, work, electron):
    import PyInstaller
    tocs = sorted(Path(work).glob("PYZ-*.toc"))
    if len(tocs) != 1:
        raise SystemExit(f"expected one PYZ table of contents in {work}, found {len(tocs)}")
    found = modules(tocs[0])
    if "stzb_warroom.app" not in found or "encodings" not in found:
        raise SystemExit(f"{tocs[0]} does not list the modules it should")
    files = []
    for path in sorted(Path(package).rglob("*")):
        name = path.relative_to(package).as_posix()
        if path.is_file() and name.startswith(("_internal/", "window/")) \
                and not name.startswith("_internal/stzb_warroom/"):
            files.append(name)
    app = Path(package) / "window" / "resources" / "app"
    fingerprint = {
        "python": sys.version, "pyinstaller": PyInstaller.__version__, "electron": electron,
        "launcher": sha256(ROOT / "packaging" / "windows" / "launcher.py"),
        "modules": sorted(name for name in found if name != "stzb_warroom" and not name.startswith("stzb_warroom.")),
        "files": files,
        "window": {path.name: sha256(path) for path in sorted(app.iterdir()) if path.is_file()},
    }
    return hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:16]


def code_zip(path):
    """stzb_warroom's sources and page, as the package has them (with its _version.py)."""
    source = ROOT / "stzb_warroom"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(source.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                archive.write(file, file.relative_to(ROOT).as_posix())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--electron", required=True)
    parser.add_argument("--package", default=ROOT / "build" / "dist" / "stzb-warroom", type=Path)
    parser.add_argument("--work", default=ROOT / "build" / "work" / "stzb-warroom", type=Path)
    parser.add_argument("--out", default=ROOT / "build", type=Path)
    args = parser.parse_args()

    built = runtime(args.package, args.work, args.electron)
    config_path = args.package / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(version=args.version, runtime=built)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    full, app = args.out / "stzb-warroom-windows.zip", args.out / "stzb-warroom-app.zip"
    # zipfile writes the Chinese file names with the UTF-8 flag, which every unzip tool reads.
    shutil.make_archive(str(full.with_suffix("")), "zip", args.package.parent, args.package.name)
    code_zip(app)
    files = {kind: {"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)}
             for kind, path in (("full", full), ("app", app))}
    release = {"version": args.version, "runtime": built, "files": files}
    (args.out / "release.json").write_text(json.dumps(release, indent=2), encoding="utf-8")
    print(f"runtime {built}; built {full.name}, {app.name}, release.json")


if __name__ == "__main__":
    main()
