#!/usr/bin/env python3
"""Create, refresh and verify McCode external-contribution manifests (*.ext).

A *.ext manifest records, next to the directory an external contribution is
populated into, where each of its files comes from and what its SHA256 is --
see McCode's docs/EXTERNAL-CONTRIBUTIONS.md for the format and
cmake/Modules/External.cmake for the CMake side that consumes it. This script
is the other half: it computes those hashes so they need not be transcribed
by hand, and re-checks them against upstream.

  mcext check  [PATH ...]            do the recorded hashes still match
                                     upstream? (CI-friendly: non-zero exit on
                                     any mismatch). PATH may be a manifest or
                                     a directory to search; default: the
                                     current directory.
  mcext update MANIFEST [-v TAG]     recompute every sha256 in MANIFEST, after
                                     optionally repointing it at a new
                                     upstream tag.
  mcext hash   URL [URL ...]         print the sha256 of each URL.
  mcext render -v TAG -r OWNER/REPO  write complete manifests for one release
               [--templates DIR]     of an external repository, from template
               [--files SPEC ...     manifests (a tree mirroring McCode's) or
                --destination PATH]  from a plain list of files. This is what
               --out DIR             the external-registrar action runs.

"check" is the one to wire into McCode's CI. A mismatch means the bytes
behind a fixed reference changed -- a moved tag, a regenerated release
archive, a compromised host -- and that is exactly what the manifest exists to
make visible. "render" is the one to wire into an external repository's
release workflow, so that the hashes are computed from what was actually
published rather than transcribed by whoever opens the McCode pull request.
"""

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

CHUNK = 1 << 16
USER_AGENT = "mccode-mcext/1.1"
RETRIES = (2, 5, 15)            # seconds between attempts; a just-pushed tag can 404 briefly

# Template placeholders, substituted textually before the JSON is parsed.
VERSION_PLACEHOLDER = "@VERSION@"
REPOSITORY_PLACEHOLDER = "@REPOSITORY@"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for delay in (*RETRIES, None):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if delay is None or not (error.code == 404 or error.code == 429 or error.code >= 500):
                raise
        except urllib.error.URLError:
            if delay is None:
                raise
        time.sleep(delay)
    raise AssertionError("unreachable")


class Archive:
    """A downloaded release archive, with "strip" leading components dropped."""

    _cache: dict = {}

    @classmethod
    def get(cls, spec: dict) -> "Archive":
        """One download per URL, however many manifests share the archive."""
        key = (spec["url"], int(spec.get("strip", 1)))
        if key not in cls._cache:
            cls._cache[key] = cls(spec)
        return cls._cache[key]

    def __init__(self, spec: dict):
        self.url = spec["url"]
        self.strip = int(spec.get("strip", 1))
        self.blob = fetch(self.url)
        self.sha256 = sha256_bytes(self.blob)
        if self.url.endswith(".zip"):
            self._zip = zipfile.ZipFile(io.BytesIO(self.blob))
            self._tar = None
        else:
            self._tar = tarfile.open(fileobj=io.BytesIO(self.blob), mode="r:*")
            self._zip = None

    def _members(self):
        if self._tar is not None:
            return [m.name for m in self._tar.getmembers() if m.isfile()]
        return [n for n in self._zip.namelist() if not n.endswith("/")]

    def read(self, member: str) -> bytes:
        wanted = member
        while wanted.startswith("./"):                   # a prefix, not characters: ".mccode/" must survive
            wanted = wanted[2:]
        for name in self._members():
            stripped = "/".join(name.split("/")[self.strip:])
            if stripped == wanted:
                if self._tar is not None:
                    return self._tar.extractfile(name).read()
                return self._zip.read(name)
        raise KeyError(f"{member!r} is not in {self.url}")


def inherit(entry: dict, defaults: dict, key: str):
    value = entry.get(key)
    return defaults.get(key) if value is None else value


def split_manifest(document):
    """Return (entries, contribution-wide defaults) for either manifest form."""
    if isinstance(document, list):
        return document, {}
    if isinstance(document, dict):
        entries = document.get("files")
        if not isinstance(entries, list):
            raise ValueError('object manifest has no "files" array')
        return entries, document
    raise ValueError("manifest must be a JSON array or object")


def github_slug(git: str):
    """"owner/repo" for a github.com repository URL, else None."""
    repo = re.sub(r"\.git$", "", git.rstrip("/"))
    match = re.fullmatch(r"https?://github\.com/([^/]+)/([^/]+)", repo)
    return f"{match[1]}/{match[2]}" if match else None


def raw_base(slug: str, version: str) -> str:
    return f"https://raw.githubusercontent.com/{slug}/{version}/"


def derived_base(entry: dict, defaults: dict):
    git = inherit(entry, defaults, "git")
    version = inherit(entry, defaults, "version")
    if not git or not version:
        return None
    slug = github_slug(git)
    return raw_base(slug, version) if slug else None


def entry_bytes(entry: dict, defaults: dict, archive):
    """Fetch one entry's content; mirrors External.cmake's resolution order."""
    name = entry["name"]
    source = entry.get("from", name)
    url = inherit(entry, defaults, "url")
    base = inherit(entry, defaults, "base")
    if url:
        return fetch(url), url
    if base:
        full = base.rstrip("/") + "/" + source
        return fetch(full), full
    if archive is not None:
        return archive.read(source), f"{archive.url}!{source}"
    base = derived_base(entry, defaults)
    if base:
        full = base + source
        return fetch(full), full
    raise ValueError(f'entry {name!r} names no source')


def load(path: Path):
    document = json.loads(path.read_text())
    entries, defaults = split_manifest(document)
    return document, entries, defaults


def dump(document) -> str:
    return json.dumps(document, indent=2) + "\n"


def open_archive(defaults: dict, entries):
    """Download the release archive only if some entry actually needs it."""
    spec = defaults.get("archive")
    if not spec:
        return None
    for entry in entries:
        if not inherit(entry, defaults, "url") and not inherit(entry, defaults, "base"):
            return Archive.get(spec)
    return None


def manifests_under(paths):
    found = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(path.rglob("*.ext")))
        else:
            found.append(path)
    return found


def repoint(document, old: str, new: str) -> None:
    """Replace one upstream tag with another wherever the manifest records it."""

    def swap(holder, *keys):
        for key in keys:
            if isinstance(holder.get(key), str):
                holder[key] = holder[key].replace(old, new)

    entries, _ = split_manifest(document)
    holders = entries if isinstance(document, list) else [document, *entries]
    for holder in holders:
        swap(holder, "version", "base", "url")
        if isinstance(holder.get("archive"), dict):
            swap(holder["archive"], "url")


def rehash(document, report=print) -> list:
    """Recompute every sha256 in a manifest in place.

    Returns (entry, origin, content) for each file, so callers can compare the
    fetched bytes against something else without downloading them twice.
    """
    entries, defaults = split_manifest(document)
    archive = open_archive(defaults, entries)
    if archive is not None:
        spec = defaults["archive"]
        rest = {k: v for k, v in spec.items() if k not in ("url", "sha256")}
        spec.clear()
        spec.update(url=archive.url, sha256=archive.sha256, **rest)
        report(f"archive {archive.url}\n    sha256 {archive.sha256}")
    fetched = []
    for entry in entries:
        blob, origin = entry_bytes(entry, defaults, archive)
        entry["sha256"] = sha256_bytes(blob)
        report(f"{entry['name']}\n    {origin}\n    sha256 {entry['sha256']}")
        fetched.append((entry, origin, blob))
    return fetched


def command_check(args) -> int:
    manifests = manifests_under(args.paths or [Path.cwd()])
    if not manifests:
        print("no *.ext manifests found", file=sys.stderr)
        return 1
    failures = 0
    for path in manifests:
        try:
            _, entries, defaults = load(path)
            archive = open_archive(defaults, entries)
        except Exception as error:                       # noqa: BLE001
            print(f"{path}: FAILED to read: {error}")
            failures += 1
            continue
        if archive is not None and defaults["archive"].get("sha256") not in (None, archive.sha256):
            print(f"{path}: archive MISMATCH {defaults['archive']['url']}")
            print(f"    recorded {defaults['archive']['sha256']}")
            print(f"    upstream {archive.sha256}")
            failures += 1
        for entry in entries:
            name = entry.get("name", "<unnamed>")
            try:
                blob, origin = entry_bytes(entry, defaults, archive)
            except Exception as error:                   # noqa: BLE001
                print(f"{path}: {name}: FAILED: {error}")
                failures += 1
                continue
            actual = sha256_bytes(blob)
            recorded = inherit(entry, defaults, "sha256")
            if recorded is None:
                print(f"{path}: {name}: no recorded sha256 (upstream is {actual})")
                failures += 1
            elif actual != recorded.lower():
                print(f"{path}: {name}: MISMATCH ({origin})")
                print(f"    recorded {recorded}")
                print(f"    upstream {actual}")
                failures += 1
            elif args.verbose:
                print(f"{path}: {name}: ok")
    if failures:
        print(f"\n{failures} problem(s) across {len(manifests)} manifest(s)")
        return 1
    print(f"{len(manifests)} manifest(s) verified against upstream")
    return 0


def command_update(args) -> int:
    path = Path(args.manifest)
    document, _, defaults = load(path)

    if args.version:
        old = defaults.get("version")
        if not old:
            print(f"{path}: no \"version\" recorded, so there is nothing to repoint; "
                  "edit the URLs by hand and re-run without --version", file=sys.stderr)
            return 1
        repoint(document, old, args.version)

    rehash(document)
    path.write_text(dump(document))
    print(f"\nwrote {path}")
    return 0


def command_hash(args) -> int:
    for url in args.urls:
        print(f"{sha256_bytes(fetch(url))}  {url}")
    return 0


# ---------------------------------------------------------------------------
# render: manifests for one release of an external repository
# ---------------------------------------------------------------------------

def mccode_path(text: str) -> str:
    """Validate a destination inside the McCode tree; it must not escape it."""
    path = PurePosixPath(text.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"{text!r} is not a relative path inside the McCode tree")
    if path.suffix != ".ext":
        raise ValueError(f"{text!r}: a manifest must be named *.ext")
    return path.as_posix()


def from_template(text: str, version: str, repository: str):
    """A template is a manifest with placeholders, or an old manifest to repoint."""
    text = text.replace(VERSION_PLACEHOLDER, version).replace(REPOSITORY_PLACEHOLDER, repository)
    document = json.loads(text)
    entries, defaults = split_manifest(document)
    old = defaults.get("version")
    if old and old != version:
        repoint(document, old, version)
    elif not old and isinstance(document, dict):
        document["version"] = version
    return document


def parse_file_specs(specs, local: Path):
    """"path" or "path -> as" lines; globs are expanded against the checkout."""
    entries = []
    for spec in specs:
        for line in spec.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            source, _, target = (part.strip() for part in line.partition("->"))
            if any(c in source for c in "*?["):
                matches = sorted(p.relative_to(local).as_posix()
                                 for p in local.glob(source) if p.is_file())
                if not matches:
                    raise ValueError(f"{source!r} matches no file in {local}")
                if target:
                    raise ValueError(f"{line!r}: a glob cannot be renamed with '->'")
                sources = [(m, None) for m in matches]
            else:
                sources = [(source, target or None)]
            for path, rename in sources:
                name = PurePosixPath(path).name
                entry = {"name": name}
                if path != name:
                    entry["from"] = path
                if rename and rename != name:
                    entry["as"] = rename
                entries.append(entry)
    names = [e.get("as", e["name"]) for e in entries]
    clashes = sorted({n for n in names if names.count(n) > 1})
    if clashes:
        raise ValueError(f"more than one file would be installed as {', '.join(clashes)}; "
                         "rename with 'path -> name'")
    return entries


def from_files(entries, *, version, repository, name=None, description=None,
               license=None, source="raw", archive_url=None, strip=1):
    document = {"name": name or repository.split("/")[-1]}
    if description:
        document["description"] = description
    if license:
        document["license"] = license
    document["homepage"] = f"https://github.com/{repository}"
    document["git"] = f"https://github.com/{repository}.git"
    document["version"] = version
    if source == "raw":
        document["base"] = raw_base(repository, version)
    elif source == "archive":
        url = archive_url or f"https://github.com/{repository}/archive/refs/tags/{version}.tar.gz"
        document["archive"] = {"url": url, "strip": strip}
    else:
        raise ValueError(f"unknown source {source!r}: expected 'raw' or 'archive'")
    document["files"] = entries
    return document


def repository_path(origin: str, repository: str, version: str):
    """Where in the external repository a fetched file lives, if we can tell.

    Only raw files and GitHub's generated source archives are a byte-for-byte
    image of the tagged tree; an uploaded release asset may be built, so it is
    not compared with the checkout.
    """
    prefix = raw_base(repository, version)
    if origin.startswith(prefix):
        return origin[len(prefix):]
    url, bang, member = origin.partition("!")
    if bang and url.startswith(f"https://github.com/{repository}/archive/"):
        return member
    return None


def command_render(args) -> int:
    local = Path(args.local).resolve() if args.local else None
    jobs = []                                            # (destination, document)
    if args.templates:
        root = Path(args.templates)
        templates = sorted(root.rglob("*.ext"))
        if not templates:
            print(f"no *.ext templates under {root}", file=sys.stderr)
            return 1
        for template in templates:
            destination = mccode_path(template.relative_to(root).as_posix())
            jobs.append((destination, from_template(template.read_text(), args.version, args.repository)))
    if args.files:
        if not args.destination:
            print("--files needs --destination, the manifest's path in the McCode tree", file=sys.stderr)
            return 1
        entries = parse_file_specs(args.files, local or Path.cwd())
        jobs.append((mccode_path(args.destination), from_files(
            entries, version=args.version, repository=args.repository,
            name=args.name, description=args.description, license=args.license,
            source=args.source, archive_url=args.archive_url, strip=args.strip)))
    if not jobs:
        print("nothing to render: give --templates and/or --files", file=sys.stderr)
        return 1
    destinations = [destination for destination, _ in jobs]
    if len(set(destinations)) != len(destinations):
        print("two manifests render to the same McCode path", file=sys.stderr)
        return 1

    out = Path(args.out)
    problems = 0
    for destination, document in jobs:
        print(f"== {destination}")
        for entry, origin, blob in rehash(document, report=lambda line: print("  " + line.replace("\n", "\n  "))):
            relative = repository_path(origin, args.repository, args.version) if local else None
            if relative is None or not (local / relative).is_file():
                continue
            here = sha256_bytes((local / relative).read_bytes())
            if here != entry["sha256"]:
                print(f"  {relative}: the published file differs from the checkout "
                      f"({here}); is the checkout at {args.version}, or does "
                      ".gitattributes rewrite it on export?")
                problems += 1
        target = out / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dump(document))
    if args.list:
        Path(args.list).write_text("".join(d + "\n" for d in destinations))
    if problems:
        print(f"\n{problems} file(s) published differently from the checkout", file=sys.stderr)
        return 1
    print(f"\nwrote {len(jobs)} manifest(s) under {out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcext",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="verify recorded hashes against upstream")
    check.add_argument("paths", nargs="*", type=Path,
                       help="manifests or directories to search (default: the current directory)")
    check.add_argument("-v", "--verbose", action="store_true", help="also report files that match")
    check.set_defaults(run=command_check)

    update = sub.add_parser("update", help="recompute the hashes in one manifest")
    update.add_argument("manifest")
    update.add_argument("-v", "--version", help="repoint the manifest at this upstream tag first")
    update.set_defaults(run=command_update)

    hash_cmd = sub.add_parser("hash", help="print the sha256 of one or more URLs")
    hash_cmd.add_argument("urls", nargs="+")
    hash_cmd.set_defaults(run=command_hash)

    render = sub.add_parser("render", help="write the manifests for one release of an external repository")
    render.add_argument("-v", "--version", required=True, help="the released tag")
    render.add_argument("-r", "--repository", required=True, help="OWNER/REPO on github.com")
    render.add_argument("--templates", help="directory of template *.ext files laid out as in the McCode tree")
    render.add_argument("--files", action="append",
                        help="files to register, one per line: 'path' or 'path -> installed-name'; globs allowed")
    render.add_argument("--destination", help="with --files: the manifest's path in the McCode tree")
    render.add_argument("--name", help="with --files: contribution name (default: the repository name)")
    render.add_argument("--description", help="with --files")
    render.add_argument("--license", help="with --files: an SPDX identifier")
    render.add_argument("--source", choices=("raw", "archive"), default="raw",
                        help="with --files: fetch each file raw, or out of a release archive")
    render.add_argument("--archive-url", help="with --source archive: default is GitHub's generated tar.gz")
    render.add_argument("--strip", type=int, default=1, help="with --source archive")
    render.add_argument("--local", help="checkout of the release, to compare published files against")
    render.add_argument("--out", required=True, help="directory to write the McCode-relative manifests into")
    render.add_argument("--list", help="also write the McCode paths of the manifests to this file")
    render.set_defaults(run=command_render)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (ValueError, KeyError, OSError) as error:
        print(f"mcext: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
