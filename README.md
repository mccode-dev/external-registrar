# external-registrar

A GitHub Action that keeps McStas/McXtrace's `*.ext` manifests
([mccode-dev/McCode#2666](https://github.com/mccode-dev/McCode/pull/2666))
in step with the external repositories they name. It hashes each new release
as published and opens the McCode pull request that registers it.

It removes the chicken-and-egg step. A manifest's hashes can only be known
once the release exists. Until now, someone had to notice the release, run
`buildscripts/mcext update` in a McCode checkout, and open a PR by hand.

## Two ways to run it

| | **Poll** (recommended) | **Release** |
| --- | --- | --- |
| Runs in | McCode, on a schedule | the contributing repository, when it publishes a release |
| Finds new releases | reads each manifest's `git`, asks GitHub for the latest release | is triggered by it |
| Secrets | the registrar app's key, held only by McCode | a token that can push to McCode, held by the contributor |
| Contributor needs | to publish releases; `.mccode/` templates optional | the workflow, and McCode's trust |
| Latency | up to the schedule interval | immediate |

A third mode, `check`, also runs in McCode. It re-verifies every recorded
hash against upstream and fails if any bytes behind a pinned reference have
changed. It needs no token.

Both modes produce identical pull requests, one per contributing repository,
from the branch `external/OWNER-REPO`, so they can coexist. For third-party
contributors, poll mode is the only one that doesn't mean handing out write
access to McCode. Release mode suits repositories McCode's maintainers control
anyway. Without `pull-request: true`, release mode still renders and checks
the manifests, which is a useful release-time check for everyone.

### Poll mode (in McCode)

```yaml
      - uses: actions/checkout@v7
      - id: app-token
        uses: actions/create-github-app-token@v2
        with:
          app-id: ${{ vars.REGISTRAR_APP_ID }}
          private-key: ${{ secrets.REGISTRAR_PRIVATE_KEY }}
      - uses: mccode-dev/external-registrar@v1
        with:
          mode: poll
          pull-request: true
          token: ${{ steps.app-token.outputs.token }}
```

The full workflow is [`examples/mccode-poll.yml`](examples/mccode-poll.yml),
which runs `mode: check` alongside. Its poll job is skipped until the
`REGISTRAR_APP_ID` variable exists, so the workflow can be merged before the
app is set up. For each repository named by a manifest's `git`:

1. It looks up the latest release: `releases/latest`, or with
   `prereleases: true` the newest non-draft release. It skips the repository
   if every manifest already records that release, or if a manifest is pinned
   to a *more recent* release. It never downgrades.
2. It takes the repository's templates at that tag, from the `.mccode/`
   directory (`templates-path`) in its source archive. Any existing manifest
   the templates don't cover is repointed at the new tag, so a repository
   with no templates is kept up to date too.
3. It hashes the release, then opens or refreshes the pull request.

A scheduled run is quiet when nothing has changed:
- A release McCode already records is skipped.
- A branch that already carries the manifests is not pushed again.
- If maintainers closed a release's PR without merging, that release is not
  proposed again. The next release is.

`only:` limits a run to named repositories, and `dry-run: true` opens nothing.
The run's summary tabulates every repository and the outcome for it.

The first registration of a repository is still a human PR, because until a
manifest names it, poll mode doesn't know it exists. Release mode (without
`pull-request`) renders that first manifest.

### Release mode (in the contributing repository)

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
        # add pull-request: true and a token to propose directly; see below
```

The full workflow is [`examples/release.yml`](examples/release.yml).

## Saying what to register

**Templates** (recommended, and what poll mode reads). Keep a `.mccode/`
directory whose layout mirrors the McCode tree. Each `*.ext` file in it is a
manifest without hashes:

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

Templates let a contributor add or move files between releases without
touching McCode. Every such change still arrives as a PR for maintainers to
review, and the PR marks new manifests.

**A file list** (release mode only), for the simple case of one manifest:

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
tag, or the member of the release archive. It never hashes a local copy.

In release mode it also compares each file with the checkout. This applies to
files that should be byte-identical to the tagged tree: raw files, and
GitHub's generated archives. The run fails on any difference, which catches
`.gitattributes` `export-subst`/`export-ignore` rewrites, or a workflow that
checked out the wrong ref. Uploaded release assets may be built, so they are
not compared. Set `checkout: ''` to skip the comparison.

Downloads retry briefly on 404 and 5xx, because a tag that was pushed moments
ago can take a little while to appear on `raw.githubusercontent.com`.

## Setting up the GitHub App

The app is only an identity: a bot account that can be issued short-lived
tokens. There is no server behind it, and this action is not the app. Both
modes can use the same app.

The workflow's own `GITHUB_TOKEN` could open poll-mode PRs within McCode.
However, PRs opened by `GITHUB_TOKEN` don't trigger McCode's CI, and it
cannot write to another repository at all.

1. As an owner of `mccode-dev`, go to **Settings → Developer settings →
   GitHub Apps → New GitHub App**
   (`https://github.com/organizations/mccode-dev/settings/apps/new`):

   | Field | Value |
   | --- | --- |
   | GitHub App name | unique across GitHub, e.g. `McCode Registrar`. PRs show as `mccode-registrar[bot]` |
   | Homepage URL | `https://github.com/mccode-dev/external-registrar` (only a link on the app's page) |
   | Callback URL, Setup URL, "Request user authorization" | empty / unchecked |
   | Webhook → Active | **unchecked**: nothing receives events |
   | Repository permissions | **Contents: Read and write**, **Pull requests: Read and write** (Metadata: Read-only is added automatically) |
   | Where can this GitHub App be installed? | **Only on this account** |

2. On the app's page, note the **App ID**, then **Generate a private key**.
   This downloads a `.pem` file.
3. **Install App → mccode-dev → Only select repositories → McCode.** That's
   the only installation either mode needs. Tokens are issued for the McCode
   installation, so the app is never installed on contributing repositories.
4. In McCode, set the App ID as the Actions variable `REGISTRAR_APP_ID`
   and the `.pem` contents as the secret `REGISTRAR_PRIVATE_KEY`. Then delete
   the downloaded file.
5. Add the [poll workflow](examples/mccode-poll.yml) to McCode, and run it
   once from the Actions tab with `dry-run` ticked.
6. Create the label named in the workflow (`external contribution`) if you
   keep the `labels:` line.

**Restrict what the app can push.** Anyone who holds the key can create a
token with Contents: write on McCode, and that token can push to any branch
not otherwise protected. Add a repository ruleset that limits the app to
`external/**` branches:

- **Settings → Rules → Rulesets → New branch ruleset**, named e.g.
  `Registrar confined to external/**`
- Target: *Include all branches*, *Exclude* `external/**`
- Rules: *Restrict creations*, *Restrict updates*, *Restrict deletions*
- Bypass list: the *Maintain* and *Admin* roles, plus *Write* if
  collaborators push branches directly. **Not** the app.

With that in place, a leaked key can at worst open a PR.

**Release mode with the app** needs the key in the contributing repository
too, as the same variable and secret, or as `mcdotstar` organisation-level
ones shared with its repositories. Only do this for repositories whose
maintainers you would give McCode write access anyway. The ruleset above
limits the damage either way. Tokens are requested with `owner: mccode-dev`
and `repositories: McCode`, as in [`examples/release.yml`](examples/release.yml).

If an app isn't wanted, release mode also accepts a contributor's fork
(`fork: user/McCode`) with a classic PAT that has `public_repo`. Fine-grained
PATs cannot open PRs on repositories their owner doesn't control.

## `mcext`

`mcext.py` is a superset of McCode's `buildscripts/mcext`. It has the same
`check`, `update` and `hash` commands and the same resolution rules, plus
`render`, which release mode runs; `poll.py` and `propose.py` build on it.
It needs only the standard library. It can also be installed
(`pip install git+https://github.com/mccode-dev/external-registrar.git`), so
that McCode and external repositories use one implementation rather than
two copies that drift apart.

```sh
mcext render -v v4.2.1 -r mcdotstar/mcstas-chopper-lib --templates .mccode --local . --out /tmp/ext
mcext check /tmp/ext
```

## Tests

```sh
python3 -m unittest discover -s tests   # offline; the GitHub API is faked
```

CI also runs the action end to end, against the McCode#2666 branch (which pins
`mcstas-chopper-lib` v4.1.0):

- **Release mode** renders v4.1.0 from the example templates. The output must
  match the manifests in the PR byte for byte, and a dry-run of the PR step
  must find nothing to propose.
- **Poll mode** (dry run) must find the newer release and render all five
  manifests for it.
