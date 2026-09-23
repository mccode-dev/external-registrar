#!/usr/bin/env python3
"""Find new releases of McCode's external contributions and propose them.

Run from a McCode checkout, typically on a schedule. For each upstream
repository named (by "git") in the checkout's *.ext manifests:

  1. look up its latest release, and stop if every manifest already records it;
  2. build the new manifests: the repository's own templates at that tag
     (".mccode/" by default, laid out as in the McCode tree), and for any
     existing McCode manifest the templates do not cover, that manifest
     repointed at the new tag -- so a repository needs no templates at all to
     be kept up to date;
  3. hash what the release publishes, and propose the result as one pull
     request per repository, on the same external/OWNER-REPO branch that the
     release-triggered mode uses.

Nothing here needs a secret from the external repository, and none of McCode's
secrets leave McCode: the contributing side only has to publish releases.
"""

import argparse
import json
import os
import sys
from pathlib import Path, PurePosixPath

import mcext
from propose import GitHub, propose, write_outputs


def recorded_versions(mccode: Path):
    """{"owner/repo": {mccode-relative manifest path: recorded version}}."""
    found = {}
    for path in mcext.manifests_under([mccode]):
        relative = path.relative_to(mccode)
        if any(part.startswith(".") for part in relative.parts):
            continue
        try:
            _, entries, defaults = mcext.load(path)
        except (ValueError, json.JSONDecodeError) as error:
            print(f"{relative}: skipped, unreadable: {error}")
            continue
        first = entries[0] if entries else {}
        git, version = mcext.inherit(first, defaults, "git"), mcext.inherit(first, defaults, "version")
        slug = mcext.github_slug(git) if git else None
        if slug and version:
            found.setdefault(slug, {})[relative.as_posix()] = version
    return found


def latest_release(gh: GitHub, slug: str, prereleases: bool):
    if prereleases:
        releases = gh("GET", f"/repos/{slug}/releases?per_page=20", missing_ok=(404,)) or []
        return next((r for r in releases if not r["draft"]), None)
    return gh("GET", f"/repos/{slug}/releases/latest", missing_ok=(404,))


def is_newer(gh: GitHub, slug: str, release: dict, recorded) -> bool:
    """Never "upgrade" a manifest pinned to a release published after the latest."""
    for version in set(recorded):
        if version == release["tag_name"]:
            continue
        pinned = gh("GET", f"/repos/{slug}/releases/tags/{version}", missing_ok=(404,))
        if pinned is None or pinned["published_at"] <= release["published_at"]:
            return True
    return False


def templates_at(slug: str, tag: str, root: str):
    """{mccode path: template text} from the repository's source archive."""
    archive = mcext.Archive.get({"url": f"https://github.com/{slug}/archive/refs/tags/{tag}.tar.gz"})
    prefix = PurePosixPath(root)
    found = {}
    for member in archive.files():
        path = PurePosixPath(member)
        if path.suffix == ".ext" and prefix in path.parents:
            found[mcext.mccode_path(path.relative_to(prefix).as_posix())] = archive.read(member).decode()
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mccode", default=".", help="McCode checkout to read manifests from")
    parser.add_argument("--upstream", required=True, help="OWNER/REPO of McCode, to propose to")
    parser.add_argument("--base", required=True, help="branch of McCode to target")
    parser.add_argument("--only", action="append", default=[], help="poll just this OWNER/REPO (repeatable)")
    parser.add_argument("--templates-path", default=".mccode", help="template directory inside each repository")
    parser.add_argument("--prereleases", action="store_true", help="consider pre-releases too")
    parser.add_argument("--out", required=True, help="directory to write rendered manifests under, per repository")
    parser.add_argument("--propose", action="store_true", help="open or update the pull requests")
    parser.add_argument("--labels", default="")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if args.propose and not args.dry_run and not token:
        raise SystemExit("no token: set GH_TOKEN")
    gh = GitHub(token)
    mccode, out = Path(args.mccode).resolve(), Path(args.out)

    registered = recorded_versions(mccode)
    only = {slug.lower() for slug in args.only}
    rows, urls, failures = [], [], 0
    for slug in sorted(registered):
        if only and slug.lower() not in only:
            continue
        manifests = registered[slug]
        recorded = ", ".join(sorted(set(manifests.values())))
        print(f"\n### {slug} (recorded {recorded})")
        try:
            release = latest_release(gh, slug, args.prereleases)
            if release is None:
                print("no releases")
                rows.append((slug, recorded, "–", "no releases", ""))
                continue
            tag = release["tag_name"]
            if set(manifests.values()) == {tag} or not is_newer(gh, slug, release, manifests.values()):
                print(f"up to date with {tag}")
                rows.append((slug, recorded, tag, "up to date", ""))
                continue

            jobs = {path: mcext.from_template((mccode / path).read_text(), tag, slug) for path in manifests}
            templates = templates_at(slug, tag, args.templates_path)
            for path, text in templates.items():
                jobs[path] = mcext.from_template(text, tag, slug)
            print(f"{tag}: {len(templates)} template(s), {len(set(manifests) - set(templates))} repointed manifest(s)")

            rendered = out / slug.replace("/", "-")
            mcext.write_rendered(sorted(jobs.items()), rendered, slug, tag)
            if not args.propose:
                rows.append((slug, recorded, tag, f"rendered to `{rendered}`", ""))
                continue
            result = propose(gh, rendered=rendered, paths=sorted(jobs), upstream=args.upstream, base=args.base,
                             repository=slug, version=tag, release_url=release.get("html_url"),
                             labels=args.labels.split(","), draft=args.draft, dry_run=args.dry_run)
            rows.append((slug, recorded, tag, result["state"], result.get("url", "")))
            if result.get("url"):
                urls.append(result["url"])
        except Exception as error:                       # noqa: BLE001 -- one repository must not stop the rest
            print(f"FAILED: {error}")
            rows.append((slug, recorded, "?", f"failed: {error}".replace("|", "\\|")[:200], ""))
            failures += 1

    if args.summary:
        with open(args.summary, "a") as handle:
            handle.write("### External contributions\n\n| Repository | Recorded | Latest | Result | Pull request |\n"
                         "| --- | --- | --- | --- | --- |\n")
            for slug, recorded, latest, state, url in rows:
                handle.write(f"| {slug} | {recorded} | {latest} | {state} | {url} |\n")
    write_outputs(args.github_output, **{"pull-request-urls": " ".join(urls)})
    if not rows:
        print("no external contributions found" + (f" matching {', '.join(args.only)}" if only else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
