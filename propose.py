#!/usr/bin/env python3
"""Open (or refresh) a McCode pull request carrying rendered *.ext manifests.

Everything goes through the GitHub REST API -- no clone of McCode, which is
large -- in four steps:

  1. read the tip of the upstream base branch, and each manifest already
     there, so an unchanged release proposes nothing;
  2. build a tree on top of that tip in the head repository (the upstream
     itself, or a fork of it: forks share upstream's objects, so the fork
     need not be in sync);
  3. commit it and force-move the head branch to the commit, so a new release
     supersedes an earlier one that was never merged rather than piling up a
     second pull request;
  4. open the pull request, or retitle the one already open from that branch.

The token needs contents:write on the head repository and permission to open
pull requests on the upstream one.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def __call__(self, method: str, path: str, body=None, missing_ok=False):
        request = urllib.request.Request(
            f"{API}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "mccode-external-registrar",
            })
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            if missing_ok and error.code in (403, 404, 422):
                return None
            detail = error.read().decode(errors="replace")
            raise SystemExit(f"GitHub API {method} {path}: {error.code} {detail}") from None


def current_content(gh: GitHub, repository: str, path: str, ref: str):
    quoted = urllib.parse.quote(path)
    found = gh("GET", f"/repos/{repository}/contents/{quoted}?ref={urllib.parse.quote(ref)}", missing_ok=True)
    if found is None:
        return None
    return base64.b64decode(found["content"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rendered", required=True, help="directory mcext render wrote into")
    parser.add_argument("--list", required=True, help="file listing the McCode paths, one per line")
    parser.add_argument("--upstream", required=True, help="OWNER/REPO of McCode")
    parser.add_argument("--base", required=True, help="branch of McCode to target")
    parser.add_argument("--head-repository", help="fork to push the branch to (default: upstream)")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repository", required=True, help="the external repository, OWNER/REPO")
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-url")
    parser.add_argument("--title")
    parser.add_argument("--labels", default="", help="comma-separated labels to add, if permitted")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="report what would change and stop")
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token and not args.dry_run:
        raise SystemExit("no token: set GH_TOKEN")
    gh = GitHub(token)
    upstream, head_repo = args.upstream, args.head_repository or args.upstream

    base_sha = gh("GET", f"/repos/{upstream}/git/ref/heads/{args.base}")["object"]["sha"]
    base_tree = gh("GET", f"/repos/{upstream}/git/commits/{base_sha}")["tree"]["sha"]

    rendered = Path(args.rendered)
    changes = []                                         # (path, text, is_new)
    for path in Path(args.list).read_text().split():
        text = (rendered / path).read_text()
        before = current_content(gh, upstream, path, args.base)
        if before is not None and before.decode() == text:
            print(f"unchanged  {path}")
            continue
        print(f"{'new' if before is None else 'updated':9}  {path}")
        changes.append((path, text, before is None))

    def output(**values):
        if args.github_output:
            with open(args.github_output, "a") as handle:
                for key, value in values.items():
                    handle.write(f"{key}={value}\n")

    if not changes:
        print(f"{upstream}@{args.base} already records {args.repository} {args.version}; nothing to propose")
        output(changed="false")
        return 0
    output(changed="true")
    if args.dry_run:
        print("dry run: no branch pushed, no pull request opened")
        return 0

    title = args.title or f"Register {args.repository} {args.version}"
    tree = gh("POST", f"/repos/{head_repo}/git/trees", {
        "base_tree": base_tree,
        "tree": [{"path": p, "mode": "100644", "type": "blob", "content": t} for p, t, _ in changes],
    })["sha"]
    message = f"{title}\n\n" + "".join(f"- {p}\n" for p, _, _ in changes)
    if args.release_url:
        message += f"\n{args.release_url}\n"
    commit = gh("POST", f"/repos/{head_repo}/git/commits",
                {"message": message, "tree": tree, "parents": [base_sha]})["sha"]
    moved = gh("PATCH", f"/repos/{head_repo}/git/refs/heads/{args.branch}",
               {"sha": commit, "force": True}, missing_ok=True)
    if moved is None:
        gh("POST", f"/repos/{head_repo}/git/refs", {"ref": f"refs/heads/{args.branch}", "sha": commit})
    print(f"pushed {commit[:12]} to {head_repo}:{args.branch}")

    body = [f"Automated registration of **{args.repository} {args.version}**"
            + (f" ([release]({args.release_url}))" if args.release_url else "") + ".", "",
            "The hashes were computed by [mcext](https://github.com/mccode-dev/external-registrar) "
            "from the files as published, not from a copy.", "",
            "| Manifest | |", "| --- | --- |"]
    body += [f"| `{p}` | {'new' if new else 'updated'} |" for p, _, new in changes]
    if any(new for _, _, new in changes):
        body += ["", "New manifests only take effect in a directory that has a "
                 "`mccode_install_externals()` call (or sits below one); please check."]
    body = "\n".join(body) + "\n"

    owner = head_repo.split("/")[0]
    head = f"{owner}:{args.branch}"
    existing = gh("GET", f"/repos/{upstream}/pulls?state=open&head={urllib.parse.quote(head)}")
    if existing:
        number = existing[0]["number"]
        pull = gh("PATCH", f"/repos/{upstream}/pulls/{number}", {"title": title, "body": body})
        print(f"updated {pull['html_url']}")
    else:
        pull = gh("POST", f"/repos/{upstream}/pulls", {
            "title": title, "head": head, "base": args.base, "body": body,
            "draft": args.draft, "maintainer_can_modify": head_repo != upstream,
        })
        print(f"opened {pull['html_url']}")
        labels = [label.strip() for label in args.labels.split(",") if label.strip()]
        if labels and gh("POST", f"/repos/{upstream}/issues/{pull['number']}/labels",
                         {"labels": labels}, missing_ok=True) is None:
            print("could not add labels (the token may lack triage rights upstream)")
    output(**{"pull-request-url": pull["html_url"], "pull-request-number": pull["number"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
