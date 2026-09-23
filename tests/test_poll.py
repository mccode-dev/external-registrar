"""Offline tests for propose() and poll.py, against a fake GitHub API."""

import base64
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcext  # noqa: E402
import poll  # noqa: E402
from propose import propose  # noqa: E402
from test_mcext import tarball  # noqa: E402

UP = "mccode-dev/McCode"
BRANCH = "external/owner-lib"
MANIFEST = "mcstas-comps/contrib/lib.ext"


class FakeGitHub:
    """Just enough of the REST API: file contents per ref, pulls, and a log of writes."""

    def __init__(self):
        self.files = {}                                  # (repo, ref, path) -> text
        self.pulls = []                                  # dicts with state/title/merged_at/html_url
        self.releases = {}                               # (repo, tag) -> release; (repo, None) -> latest
        self.writes = []

    def __call__(self, method, path, body=None, missing_ok=()):
        from urllib.parse import unquote, urlsplit, parse_qs
        url = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = unquote(url.path).split("/")
        repo = "/".join(parts[2:4])
        if method != "GET":
            self.writes.append((method, url.path, body))
            if url.path.endswith("/pulls"):
                pull = {"number": 7, "html_url": "https://pr/7", "state": "open", "title": body["title"]}
                self.pulls.append(pull)
                return pull
            if "/pulls/" in url.path:
                return {"number": 7, "html_url": "https://pr/7"}
            return {"sha": "0123456789abcdef", "object": {"sha": "base"}, "tree": {"sha": "tree"}}
        if parts[4] == "contents":
            text = self.files.get((repo, query["ref"], "/".join(parts[5:])))
            return None if text is None else {"content": base64.b64encode(text.encode()).decode()}
        if parts[4] == "pulls":
            return [p for p in self.pulls if p["state"] == query["state"]]
        if parts[4] == "git":
            return {"object": {"sha": "base"}, "tree": {"sha": "tree"}}
        if parts[4] == "releases":
            tag = parts[6] if len(parts) > 6 else None
            return self.releases.get((repo, tag))
        raise AssertionError(f"unexpected {method} {path}")


class TestPropose(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.rendered = Path(self.tmp.name)
        (self.rendered / MANIFEST).parent.mkdir(parents=True)
        (self.rendered / MANIFEST).write_text('{"version": "v2"}\n')
        self.gh = FakeGitHub()

    def tearDown(self):
        self.tmp.cleanup()

    def run_propose(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return propose(self.gh, rendered=self.rendered, paths=[MANIFEST], upstream=UP, base="main",
                           repository="owner/lib", version="v2", **kwargs)

    def test_nothing_to_do_when_mccode_already_has_it(self):
        self.gh.files[(UP, "main", MANIFEST)] = '{"version": "v2"}\n'
        self.assertEqual(self.run_propose()["state"], "unchanged")
        self.assertEqual(self.gh.writes, [])

    def test_opens_a_pull_request(self):
        self.gh.files[(UP, "main", MANIFEST)] = '{"version": "v1"}\n'
        result = self.run_propose(labels=["external", " "])
        self.assertEqual(result["state"], "opened")
        paths = [path for _, path, _ in self.gh.writes]
        self.assertIn(f"/repos/{UP}/git/trees", paths)
        self.assertIn(f"/repos/{UP}/pulls", paths)
        pull = next(body for _, path, body in self.gh.writes if path == f"/repos/{UP}/pulls")
        self.assertEqual(pull["head"], f"mccode-dev:{BRANCH}")
        self.assertIn("| `mcstas-comps/contrib/lib.ext` | updated |", pull["body"])
        labels = next(body for _, path, body in self.gh.writes if path.endswith("/labels"))
        self.assertEqual(labels, {"labels": ["external"]})

    def test_a_rerun_is_quiet(self):
        self.gh.files[(UP, BRANCH, MANIFEST)] = '{"version": "v2"}\n'
        self.gh.pulls.append({"state": "open", "title": "Register owner/lib v2", "html_url": "https://pr/7",
                              "number": 7})
        self.assertEqual(self.run_propose()["state"], "already-proposed")
        self.assertEqual(self.gh.writes, [])

    def test_a_declined_release_is_not_reproposed(self):
        self.gh.pulls.append({"state": "closed", "title": "Register owner/lib v2", "merged_at": None,
                              "html_url": "https://pr/6"})
        self.assertEqual(self.run_propose()["state"], "declined")
        self.assertEqual(self.gh.writes, [])

    def test_a_new_release_updates_the_open_pull_request(self):
        self.gh.files[(UP, BRANCH, MANIFEST)] = '{"version": "v1.5"}\n'
        self.gh.pulls.append({"state": "open", "title": "Register owner/lib v1.5", "html_url": "https://pr/7",
                              "number": 7})
        self.assertEqual(self.run_propose()["state"], "updated")
        self.assertIn(("PATCH", f"/repos/{UP}/pulls/7"), [(m, p) for m, p, _ in self.gh.writes])

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self.run_propose(dry_run=True)["state"], "dry-run")
        self.assertEqual(self.gh.writes, [])


class TestPoll(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.mccode = Path(self.tmp.name)
        self.fetch, mcext.fetch = mcext.fetch, self.serve
        mcext.Archive._cache.clear()
        self.web = {}
        self.gh = FakeGitHub()

    def tearDown(self):
        mcext.fetch = self.fetch
        self.tmp.cleanup()

    def serve(self, url):
        return self.web[url]

    def manifest(self, path, version, repo="owner/lib"):
        target = self.mccode / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"git": f"https://github.com/{repo}.git", "version": version,
                                      "files": [{"name": "A.comp"}]}))

    def test_recorded_versions_group_by_repository(self):
        self.manifest("mcstas-comps/contrib/lib.ext", "v1")
        self.manifest("mcstas-comps/share/lib.ext", "v1")
        self.manifest("mcxtrace-comps/contrib/other.ext", "2.0", repo="someone/other")
        self.manifest(".git/ignored.ext", "v0")
        self.assertEqual(poll.recorded_versions(self.mccode), {
            "owner/lib": {"mcstas-comps/contrib/lib.ext": "v1", "mcstas-comps/share/lib.ext": "v1"},
            "someone/other": {"mcxtrace-comps/contrib/other.ext": "2.0"},
        })

    def test_templates_come_from_the_release_archive(self):
        self.web["https://github.com/owner/lib/archive/refs/tags/v2.tar.gz"] = tarball({
            ".mccode/mcstas-comps/contrib/lib.ext": b'{"version": "@VERSION@", "files": []}',
            ".mccode/README.md": b"not a manifest",
            "A.comp": b"a",
        }, prefix="lib-2/")
        self.assertEqual(poll.templates_at("owner/lib", "v2", ".mccode"),
                         {"mcstas-comps/contrib/lib.ext": '{"version": "@VERSION@", "files": []}'})

    def test_never_downgrades_a_newer_pin(self):
        latest = {"tag_name": "v2", "published_at": "2026-01-01T00:00:00Z"}
        self.gh.releases[("owner/lib", "v3-beta")] = {"published_at": "2026-02-01T00:00:00Z"}
        self.gh.releases[("owner/lib", "v1")] = {"published_at": "2025-01-01T00:00:00Z"}
        self.assertFalse(poll.is_newer(self.gh, "owner/lib", latest, ["v3-beta"]))
        self.assertTrue(poll.is_newer(self.gh, "owner/lib", latest, ["v1"]))


if __name__ == "__main__":
    unittest.main()
