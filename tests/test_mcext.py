"""Offline tests for mcext: every download is served from an in-memory table."""

import hashlib
import io
import json
import sys
import tarfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcext  # noqa: E402

REPO = "owner/lib"
RAW = "https://raw.githubusercontent.com/owner/lib"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tarball(files: dict, prefix="lib-1.0/") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Offline(unittest.TestCase):
    def setUp(self):
        self.web = {}
        self.fetch, mcext.fetch = mcext.fetch, self.serve
        mcext.Archive._cache.clear()
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        mcext.fetch = self.fetch
        self.tmp.cleanup()

    def serve(self, url):
        if url not in self.web:
            raise OSError(f"404 {url}")
        return self.web[url]

    def publish(self, version, files):
        for name, data in files.items():
            self.web[f"{RAW}/{version}/{name}"] = data
        self.web[f"https://github.com/{REPO}/archive/refs/tags/{version}.tar.gz"] = tarball(files)

    def checkout(self, files):
        root = self.dir / "checkout"
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        return root

    def render(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            status = mcext.main(["render", "-r", REPO, "--out", str(self.dir / "out"), *argv])
        return status, out.getvalue()

    def rendered(self, path):
        return json.loads((self.dir / "out" / path).read_text())


class TestTemplates(Offline):
    def template(self, path, document):
        target = self.dir / "t" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document) if not isinstance(document, str) else document)

    def test_placeholders_are_substituted_and_hashed(self):
        files = {"lib.h": b"header", "src/lib.c": b"source"}
        self.publish("v2.0", files)
        self.template("mcstas-comps/share/lib.ext", """{
          "name": "lib", "git": "https://github.com/@REPOSITORY@.git", "version": "@VERSION@",
          "base": "https://raw.githubusercontent.com/@REPOSITORY@/@VERSION@/",
          "files": [{"name": "lib.h"}, {"name": "lib.c", "from": "src/lib.c"}]}""")
        status, log = self.render("-v", "v2.0", "--templates", str(self.dir / "t"),
                                  "--local", str(self.checkout(files)))
        self.assertEqual(status, 0, log)
        document = self.rendered("mcstas-comps/share/lib.ext")
        self.assertEqual(document["base"], f"{RAW}/v2.0/")
        self.assertEqual([f["sha256"] for f in document["files"]], [sha(b"header"), sha(b"source")])

    def test_an_existing_manifest_is_repointed(self):
        self.publish("v2.0", {"A.comp": b"new"})
        self.template("mcstas-comps/contrib/A.ext", {
            "git": f"https://github.com/{REPO}.git", "version": "v1.0",
            "archive": {"url": f"https://github.com/{REPO}/archive/refs/tags/v1.0.tar.gz",
                        "sha256": "stale", "strip": 1},
            "files": [{"name": "A.comp", "sha256": "stale"}]})
        status, log = self.render("-v", "v2.0", "--templates", str(self.dir / "t"))
        self.assertEqual(status, 0, log)
        document = self.rendered("mcstas-comps/contrib/A.ext")
        self.assertEqual(document["version"], "v2.0")
        self.assertEqual(list(document["archive"]), ["url", "sha256", "strip"])
        self.assertTrue(document["archive"]["url"].endswith("/v2.0.tar.gz"))
        self.assertEqual(document["files"][0]["sha256"], sha(b"new"))

    def test_published_bytes_must_match_the_checkout(self):
        self.publish("v2.0", {"A.comp": b"published"})
        self.template("x/A.ext", {"git": f"https://github.com/{REPO}", "version": "@VERSION@",
                                  "files": [{"name": "A.comp"}]})
        status, log = self.render("-v", "v2.0", "--templates", str(self.dir / "t"),
                                  "--local", str(self.checkout({"A.comp": b"edited"})))
        self.assertEqual(status, 1)
        self.assertIn("differs from the checkout", log)

    def test_a_template_cannot_escape_the_tree(self):
        with self.assertRaises(ValueError):
            mcext.mccode_path("../outside.ext")
        with self.assertRaises(ValueError):
            mcext.mccode_path("mcstas-comps/contrib/A.comp")


class TestFiles(Offline):
    def test_raw_files_with_rename_and_glob(self):
        files = {"comps/A.comp": b"a", "comps/B.comp": b"b", "demo/Demo.instr": b"d"}
        self.publish("v3", files)
        root = self.checkout(files)
        status, log = self.render("-v", "v3", "--local", str(root), "--destination", "mcstas-comps/contrib/lib.ext",
                                  "--files", "comps/*.comp\ndemo/Demo.instr -> Demo/Demo.instr  # example",
                                  "--license", "MIT")
        self.assertEqual(status, 0, log)
        document = self.rendered("mcstas-comps/contrib/lib.ext")
        self.assertEqual(document["license"], "MIT")
        self.assertEqual(document["files"], [
            {"name": "A.comp", "from": "comps/A.comp", "sha256": sha(b"a")},
            {"name": "B.comp", "from": "comps/B.comp", "sha256": sha(b"b")},
            {"name": "Demo.instr", "from": "demo/Demo.instr", "as": "Demo/Demo.instr", "sha256": sha(b"d")},
        ])

    def test_archive_source(self):
        files = {"A.comp": b"a"}
        self.publish("v3", files)
        status, log = self.render("-v", "v3", "--local", str(self.checkout(files)), "--source", "archive",
                                  "--destination", "d/lib.ext", "--files", "A.comp")
        self.assertEqual(status, 0, log)
        document = self.rendered("d/lib.ext")
        self.assertEqual(document["archive"]["sha256"],
                         sha(self.web[f"https://github.com/{REPO}/archive/refs/tags/v3.tar.gz"]))
        self.assertNotIn("base", document)

    def test_name_clash_is_refused(self):
        root = self.checkout({"a/X.comp": b"1", "b/X.comp": b"2"})
        with self.assertRaises(ValueError):
            mcext.parse_file_specs(["a/X.comp\nb/X.comp"], root)

    def test_files_need_a_destination(self):
        status, log = self.render("-v", "v3", "--files", "A.comp")
        self.assertEqual(status, 1)
        self.assertIn("--destination", log)


class TestCheck(Offline):
    def test_check_reports_a_moved_tag(self):
        self.publish("v1", {"A.comp": b"original"})
        manifest = self.dir / "A.ext"
        manifest.write_text(json.dumps({"git": f"https://github.com/{REPO}", "version": "v1",
                                        "files": [{"name": "A.comp", "sha256": sha(b"original")}]}))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mcext.main(["check", str(manifest)]), 0)
        self.web[f"{RAW}/v1/A.comp"] = b"moved"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mcext.main(["check", str(manifest)]), 1)


if __name__ == "__main__":
    unittest.main()
