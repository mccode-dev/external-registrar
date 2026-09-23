#!/usr/bin/env python3
"""Open (or refresh) a McCode pull request carrying rendered *.ext manifests.

Everything goes through the GitHub REST API -- no clone of McCode, which is
large:

  1. read the tip of the upstream base branch, and each manifest already
     there, so an unchanged release proposes nothing;
  2. skip a release whose pull request maintainers already closed unmerged,
     and skip the push when the head branch already carries these manifests,
     so that re-running (or polling on a schedule) is quiet;
  3. otherwise build a tree on top of the base tip in the head repository
     (the upstream itself, or a fork of it: forks share upstream's objects,
     so the fork need not be in sync), commit it, and force-move the head
     branch there -- a new release supersedes an earlier one that was never
     merged, rather than piling up a second pull request;
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

    def __call__(self, method: str, path: str, body=None, missing_ok=()):
        """missing_ok: HTTP status codes to answer with None instead of raising."""
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
            if error.code in missing_ok:
                return None
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"GitHub API {method} {path}: {error.code} {detail}") from None


def current_content(gh: GitHub, repository: str, path: str, ref: str):
    quoted = urllib.parse.quote(path)
    found = gh("GET", f"/repos/{repository}/contents/{quoted}?ref={urllib.parse.quote(ref, safe='')}",
               missing_ok=(404,))
    if found is None or isinstance(found, list):
        return None
    return base64.b64decode(found["content"]).decode()


def default_branch(repository: str) -> str:
    return "external/" + repository.replace("/", "-")


def propose(gh: GitHub, *, rendered: Path, paths, upstream: str, base: str, repository: str,
            version: str, head_repository=None, branch=None, release_url=None, title=None,
            labels=(), draft=False, dry_run=False, log=print) -> dict:
    """Propose the manifests under rendered to upstream; returns what happened.

    The result's "state" is one of: unchanged, declined, dry-run,
    already-proposed, opened, updated. "url" is set whenever there is a pull
    request to point at.
    """
    head_repo = head_repository or upstream
    branch = branch or default_branch(repository)
    title = title or f"Register {repository} {version}"
    owner = head_repo.split("/")[0]
    head = f"{owner}:{branch}"

    changes = []                                         # (path, text, is_new)
    for path in paths:
        text = (rendered / path).read_text()
        before = current_content(gh, upstream, path, base)
        if before == text:
            log(f"unchanged  {path}")
            continue
        log(f"{'new' if before is None else 'updated':9}  {path}")
        changes.append((path, text, before is None))
    if not changes:
        log(f"{upstream}@{base} already records {repository} {version}; nothing to propose")
        return {"state": "unchanged", "changed": False}

    quoted_head = urllib.parse.quote(head, safe="")
    for pull in gh("GET", f"/repos/{upstream}/pulls?state=closed&per_page=100&head={quoted_head}") or []:
        if pull["title"] == title and not pull.get("merged_at"):
            log(f"{pull['html_url']} proposed {version} and was closed unmerged; not proposing it again")
            return {"state": "declined", "changed": True, "url": pull["html_url"]}

    on_branch = [current_content(gh, head_repo, path, branch) for path, _, _ in changes]
    need_push = any(there != text for there, (_, text, _) in zip(on_branch, changes))
    open_pulls = gh("GET", f"/repos/{upstream}/pulls?state=open&head={quoted_head}") or []

    if dry_run:
        action = "push and " if need_push else ""
        action += "update" if open_pulls else "open"
        log(f"dry run: would {action} the pull request from {head}")
        return {"state": "dry-run", "changed": True}

    if need_push:
        base_sha = gh("GET", f"/repos/{upstream}/git/ref/heads/{base}")["object"]["sha"]
        base_tree = gh("GET", f"/repos/{upstream}/git/commits/{base_sha}")["tree"]["sha"]
        tree = gh("POST", f"/repos/{head_repo}/git/trees", {
            "base_tree": base_tree,
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": t} for p, t, _ in changes],
        })["sha"]
        message = f"{title}\n\n" + "".join(f"- {p}\n" for p, _, _ in changes)
        if release_url:
            message += f"\n{release_url}\n"
        commit = gh("POST", f"/repos/{head_repo}/git/commits",
                    {"message": message, "tree": tree, "parents": [base_sha]})["sha"]
        moved = gh("PATCH", f"/repos/{head_repo}/git/refs/heads/{branch}",
                   {"sha": commit, "force": True}, missing_ok=(404, 422))   # 422: no such ref yet
        if moved is None:
            gh("POST", f"/repos/{head_repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
        log(f"pushed {commit[:12]} to {head_repo}:{branch}")
    elif open_pulls:
        log(f"{open_pulls[0]['html_url']} already proposes these manifests")
        return {"state": "already-proposed", "changed": True, "url": open_pulls[0]["html_url"]}

    body = [f"Automated registration of **{repository} {version}**"
            + (f" ([release]({release_url}))" if release_url else "") + ".", "",
            "The hashes were computed by [mcext](https://github.com/mccode-dev/external-registrar) "
            "from the files as published, not from a copy.", "",
            "| Manifest | |", "| --- | --- |"]
    body += [f"| `{p}` | {'new' if new else 'updated'} |" for p, _, new in changes]
    if any(new for _, _, new in changes):
        body += ["", "New manifests only take effect in a directory that has a "
                 "`mccode_install_externals()` call (or sits below one); please check."]
    body = "\n".join(body) + "\n"

    if open_pulls:
        number = open_pulls[0]["number"]
        pull = gh("PATCH", f"/repos/{upstream}/pulls/{number}", {"title": title, "body": body})
        log(f"updated {pull['html_url']}")
        return {"state": "updated", "changed": True, "url": pull["html_url"], "number": pull["number"]}

    pull = gh("POST", f"/repos/{upstream}/pulls", {
        "title": title, "head": head, "base": base, "body": body,
        "draft": draft, "maintainer_can_modify": head_repo != upstream,
    })
    log(f"opened {pull['html_url']}")
    labels = [label.strip() for label in labels if label.strip()]
    if labels and gh("POST", f"/repos/{upstream}/issues/{pull['number']}/labels",
                     {"labels": labels}, missing_ok=(403, 404, 422)) is None:
        log("could not add labels (the token may lack triage rights upstream)")
    return {"state": "opened", "changed": True, "url": pull["html_url"], "number": pull["number"]}


def write_outputs(path, **values):
    if path:
        with open(path, "a") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rendered", required=True, help="directory mcext render wrote into")
    parser.add_argument("--list", required=True, help="file listing the McCode paths, one per line")
    parser.add_argument("--upstream", required=True, help="OWNER/REPO of McCode")
    parser.add_argument("--base", required=True, help="branch of McCode to target")
    parser.add_argument("--head-repository", help="fork to push the branch to (default: upstream)")
    parser.add_argument("--branch", help="head branch (default: external/OWNER-REPO)")
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
    try:
        result = propose(
            GitHub(token), rendered=Path(args.rendered), paths=Path(args.list).read_text().split(),
            upstream=args.upstream, base=args.base, head_repository=args.head_repository,
            branch=args.branch, repository=args.repository, version=args.version,
            release_url=args.release_url, title=args.title, labels=args.labels.split(","),
            draft=args.draft, dry_run=args.dry_run)
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    outputs = {"changed": str(result["changed"]).lower()}
    if result.get("url"):
        outputs["pull-request-url"] = result["url"]
    write_outputs(args.github_output, **outputs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
