"""Updates: finding the newest release, downloading only the code, and the launcher running it."""

import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from stzb_warroom import update

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://site.test"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def code(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return data.getvalue()


GOOD = code({"stzb_warroom/__init__.py": "", "stzb_warroom/__main__.py": "def main(argv): return 'new'\n",
             "stzb_warroom/webui.py": "", "stzb_warroom/ui/index.html": "<html>"})


def index(app=GOOD, runtime="r1", version="9.0.0"):
    return {"version": version, "runtime": runtime,
            "full": {"file": f"stzb-warroom-{version}-windows.zip", "size": 10, "sha256": "a" * 64},
            "app": {"file": f"stzb-warroom-{version}-app.zip", "size": len(app),
                    "sha256": hashlib.sha256(app).hexdigest()}}


class Opener:
    """The site's latest.json and files, and GitHub's release; `down` names what does not answer."""

    def __init__(self, files, down=()):
        self.files, self.down, self.opened, self.agents = files, down, [], []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.opened.append(url)
        self.agents.append(request.get_header("User-agent"))
        if any(url.startswith(prefix) for prefix in self.down) or url not in self.files:
            raise OSError("unreachable")
        value = self.files[url]
        return Response(value if isinstance(value, bytes) else json.dumps(value).encode())


class LatestTests(unittest.TestCase):
    def setUp(self):
        patcher = unittest.mock.patch.object(update, "site", lambda: SITE + "/")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_site_is_asked_first_and_its_files_are_beside_its_index(self):
        opener = Opener({SITE + "/download/latest.json": index()})
        release = update.latest(opener)
        self.assertEqual(release["version"], "9.0.0")
        self.assertEqual(release["url"], SITE + "/download/stzb-warroom-9.0.0-windows.zip")
        self.assertEqual(release["app"]["url"], SITE + "/download/stzb-warroom-9.0.0-app.zip")
        self.assertEqual(release["runtime"], "r1")
        self.assertTrue(opener.agents[0].startswith(f"stzb-warroom/{update.__version__} ("))

    def test_github_when_the_site_does_not_answer(self):
        opener = Opener({update.GITHUB: {"tag_name": "v9.1.0", "html_url": "page"}}, down=(SITE,))
        self.assertEqual(update.latest(opener), {"version": "9.1.0", "runtime": None, "url": "page", "app": None})
        with self.assertRaisesRegex(ValueError, "检查更新失败"):
            update.latest(Opener({}))

    def test_a_file_elsewhere_is_refused(self):
        bad = index()
        bad["app"]["file"] = "../../evil.zip"
        opener = Opener({SITE + "/download/latest.json": bad, update.GITHUB: {"tag_name": "v9.0.0"}})
        self.assertIsNone(update.latest(opener)["app"])


class InstallTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.place = Path(folder.name) / "app"
        self.opener = Opener({SITE + "/download/stzb-warroom-9.0.0-app.zip": GOOD})

    def release(self, app=GOOD):
        with unittest.mock.patch.object(update, "site", lambda: SITE):
            return update._from_site(SITE + "/download/latest.json", index(app))

    def test_only_a_package_on_the_same_runtime_downloads_the_code(self):
        release = self.release()
        with unittest.mock.patch.object(update, "packaged", lambda: {"runtime": "r1"}):
            self.assertFalse(update.code_only(release, self.place))  # from source
            with unittest.mock.patch.object(sys, "frozen", True, create=True):
                self.assertTrue(update.code_only(release, self.place))
                self.assertFalse(update.code_only(dict(release, runtime="r2"), self.place))
                self.place.mkdir()
                (self.place / "failed.json").write_text(json.dumps({"version": "9.0.0"}))
                self.assertFalse(update.code_only(release, self.place))

    def test_the_code_is_checked_unpacked_and_named_current(self):
        (self.place / "8.0.0" / "stzb_warroom").mkdir(parents=True)
        update.install(self.release(), self.opener, self.place)
        self.assertTrue((self.place / "9.0.0" / "stzb_warroom" / "ui" / "index.html").is_file())
        self.assertEqual(json.loads((self.place / "current.json").read_text()), {"version": "9.0.0", "runtime": "r1"})
        self.assertFalse((self.place / "8.0.0").exists())
        self.assertTrue(update.installed(self.release(), self.place))
        update.install(self.release(), Opener({}), self.place)  # already there: nothing downloaded

    def test_a_damaged_or_strange_download_is_refused(self):
        release = self.release()
        release["app"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "不完整"):
            update.install(release, self.opener, self.place)
        evil = code({"stzb_warroom/__main__.py": "", "../evil.py": ""})
        opener = Opener({SITE + "/download/stzb-warroom-9.0.0-app.zip": evil})
        with self.assertRaisesRegex(ValueError, "内容不对"):
            update.install(self.release(evil), opener, self.place)
        self.assertFalse((self.place / "current.json").exists())


def launcher():
    spec = importlib.util.spec_from_file_location("launcher_under_test", ROOT / "packaging" / "windows" / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherTests(unittest.TestCase):
    """The launcher, as in the exe: config.json beside it, updates under LOCALAPPDATA."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "package").mkdir()
        (self.root / "package" / "config.json").write_text(json.dumps({"version": "1.0.0", "runtime": "r1"}))
        self.updates = self.root / "local" / "stzb-warroom" / "app"
        for patcher in (unittest.mock.patch.object(sys, "executable", str(self.root / "package" / "stzb-warroom.exe")),
                        unittest.mock.patch.dict("os.environ", {"LOCALAPPDATA": str(self.root / "local")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        saved = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "stzb_warroom"}
        finders = list(sys.meta_path)

        def restore():
            sys.meta_path[:] = finders
            self.forget()
            sys.modules.update(saved)

        self.addCleanup(restore)
        self.launcher = launcher()

    def downloaded(self, version="2.0.0", runtime="r1", main="def main(argv): return ('new', argv)\n"):
        package = self.updates / version / "stzb_warroom"
        package.mkdir(parents=True)
        for name, text in (("__init__.py", ""), ("__main__.py", main), ("webui.py", "")):
            (package / name).write_text(text)
        (self.updates / "current.json").write_text(json.dumps({"version": version, "runtime": runtime}))

    def forget(self):
        for name in [name for name in sys.modules if name.split(".")[0] == "stzb_warroom"]:
            del sys.modules[name]

    def test_newer_code_for_this_runtime_runs(self):
        self.downloaded()
        self.forget()
        self.assertEqual(self.launcher.load()(["gui"]), ("new", ["gui"]))
        self.assertEqual(Path(sys.modules["stzb_warroom"].__file__).parent.parent, self.updates / "2.0.0")

    def test_other_runtimes_and_older_code_are_ignored(self):
        self.downloaded(runtime="r2")
        self.assertIsNone(self.launcher.downloaded())
        (self.updates / "current.json").write_text(json.dumps({"version": "0.9.0", "runtime": "r1"}))
        self.assertIsNone(self.launcher.downloaded())

    def test_code_that_does_not_import_falls_back_and_is_not_tried_again(self):
        self.downloaded(main="raise ImportError('a module this exe lacks')\n")
        self.forget()
        main = self.launcher.load()
        self.assertEqual(main.__module__, "stzb_warroom.__main__")
        self.assertEqual(Path(sys.modules["stzb_warroom"].__file__).parent, ROOT / "stzb_warroom")
        self.assertFalse((self.updates / "current.json").exists())
        self.assertEqual(json.loads((self.updates / "failed.json").read_text())["version"], "2.0.0")


class RuntimeTests(unittest.TestCase):
    """The runtime build.ps1 puts in config.json: the same for two builds that differ only in the code."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        spec = importlib.util.spec_from_file_location("release_under_test", ROOT / "packaging" / "windows" / "release.py")
        self.release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.release)
        patcher = unittest.mock.patch.dict(sys.modules, {"PyInstaller": unittest.mock.Mock(__version__="6.22.3")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, name, modules, main_js="// window"):
        package, work = self.root / name / "dist", self.root / name / "work"
        for path, text in (("_internal/base_library.zip", "x"), ("_internal/python312.dll", "x"),
                           ("_internal/stzb_warroom/ui/index.html", name), ("window/stzb-window.exe", name),
                           ("window/resources/app/main.js", main_js), ("stzb-warroom.exe", name)):
            (package / path).parent.mkdir(parents=True, exist_ok=True)
            (package / path).write_text(text)
        work.mkdir(parents=True)
        toc = [(module, f"C:\\py\\{module}.py", "PYMODULE") for module in modules]
        (work / "PYZ-00.toc").write_text(repr(("C:\\work\\PYZ-00.pyz", toc)))
        return self.release.runtime(package, work, "44.5.1")

    def test_only_what_the_code_cannot_change_counts(self):
        base = ["json", "stzb_warroom", "stzb_warroom.app"] + [f"m{n}" for n in range(20)]
        first = self.build("a", base)
        self.assertEqual(self.build("b", base + ["stzb_warroom.update"]), first)
        self.assertNotEqual(self.build("c", base + ["sqlite3"]), first)
        self.assertNotEqual(self.build("d", base, main_js="// changed"), first)
        with self.assertRaises(SystemExit):
            self.build("e", ["json"])


if __name__ == "__main__":
    unittest.main()
