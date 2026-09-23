# external-registrar

A GitHub Action for repositories that contribute components, instruments or
library files to McStas/McXtrace. It writes the `*.ext` manifests McCode uses
to fetch and verify those files
([mccode-dev/McCode#2666](https://github.com/mccode-dev/McCode/pull/2666)).
The action computes the hashes from the release as published, and can open
the McCode pull request itself.

It removes the chicken-and-egg step. A manifest's hashes can only be known
once the release exists. Until now, someone had to notice the release, run
`buildscripts/mcext update` in a McCode checkout, and open a PR by hand. Now
the release does that.

```yaml
on:
  release:
    types: [published]

jobs:
  register:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: mccode-dev/external-registrar@v1
        with:
          pull-request: true
          token: ${{ secrets.MCCODE_REGISTRAR_TOKEN }}
```

See [`examples/release.yml`](examples/release.yml) for a fuller workflow.

## Saying what to register

**Templates** (recommended). Keep a `.mccode/` directory whose layout mirrors
the McCode tree. Each `*.ext` file in it is a manifest without hashes:

```
.mccode/
  mcstas-comps/share/chopper-lib.ext
  mcstas-comps/contrib/chopper-lib.ext
  mcstas-comps/examples/Tests_optics/NXdisk_chopper_image/NXdisk_chopper_image.ext
```
```json
{
  "name": "mcstas-chopper-lib",
  "homepage": "https://github.com/@REPOSITORY@",
  "git": "https://github.com/@REPOSITORY@.git",
  "version": "@VERSION@",
  "base": "https://raw.githubusercontent.com/@REPOSITORY@/@VERSION@/",
  "files": [ { "name": "chopper-lib.h" }, { "name": "chopper-lib.c" } ]
}
```

The action substitutes `@VERSION@` and `@REPOSITORY@`. A template that
carries a concrete `version`, such as a manifest copied out of McCode, is
repointed at the new tag, as `mcext update -v` does. Every `sha256` (the
archive's too) is then recomputed. The full manifest format is described in
McCode's `docs/EXTERNAL-CONTRIBUTIONS.md`.
[`examples/chopper-lib/.mccode`](examples/chopper-lib/.mccode) holds the
templates for the manifests in McCode#2666.

**A file list**, for the simple case of one manifest:

```yaml
      - uses: mccode-dev/external-registrar@v1
        with:
          files: |
            src/*.comp
            examples/Demo.instr -> Demo/Demo.instr
          destination: mcstas-comps/contrib/my-lib.ext
          license: BSD-3-Clause
          source: raw          # or: archive (with optional archive-url)
```

Each line is a path in this repository. `path -> name` installs the file under
another name, relative to the manifest; globs are expanded against the checkout.

## What is hashed

The action hashes the bytes McCode's CMake will download: the raw file at the
tag, or the member of the release archive. It does not hash the checkout.
They can differ, for example through `.gitattributes` `export-subst` or
`export-ignore`, or when the workflow checked out the wrong ref. So for files
that should be byte-identical to the tagged tree (raw files, and GitHub's
generated archives), the action also compares them with the checkout and
fails on any difference. Uploaded release assets may be built, so they are
not compared. Set `checkout: ''` to skip the comparison.

Downloads retry briefly on 404 and 5xx, because a tag that was pushed moments
ago can take a little while to appear on `raw.githubusercontent.com`.

## The pull request

With `pull-request: true` the action works entirely through the REST API and
never clones McCode:

- It reads each manifest from McCode's `mccode-branch` (default `main`). If
  nothing changed, it proposes nothing.
- It commits the changed manifests on top of that branch to
  `external/OWNER-REPO`. This branch name is stable, so a later release
  force-updates a PR that is still open instead of opening a second one.
- It opens the PR, or retitles the existing one. The PR body marks new
  manifests, which only take effect in a directory covered by
  `mccode_install_externals()`.

`dry-run: true` reports what would change and pushes nothing.

**Tokens.** The workflow's own `GITHUB_TOKEN` cannot write to another
repository. There are two workable setups:

| Setup | Token |
| --- | --- |
| A GitHub App owned by `mccode-dev`, installed on McCode and on the contributing repository (recommended) | `actions/create-github-app-token` in the workflow; the PR comes from the app, and nobody's personal token is in play |
| A contributor's fork of McCode, with `fork: user/McCode` | a classic PAT with `public_repo` (fine-grained PATs cannot open PRs on repositories their owner doesn't control) |

## `mcext`

`mcext.py` is a superset of McCode's `buildscripts/mcext`. It has the same
`check`, `update` and `hash` commands and the same resolution rules, plus
`render`, which the action runs. It needs only the standard library. It can
also be installed (`pip install git+https://github.com/mccode-dev/external-registrar.git`),
so that McCode and external repositories use one implementation rather than
two copies that drift apart.

```sh
mcext render -v v4.2.1 -r mcdotstar/mcstas-chopper-lib --templates .mccode --local . --out /tmp/ext
mcext check /tmp/ext
```

## Tests

```sh
python3 -m unittest discover -s tests   # offline
```

CI also runs the action end to end. It renders `mcstas-chopper-lib` v4.1.0
from the example templates and requires the output to match the manifests in
McCode#2666 byte for byte. It then dry-runs the PR step against that branch,
where it must find nothing to propose.
