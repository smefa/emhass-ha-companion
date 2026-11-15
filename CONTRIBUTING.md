# Contributing

## Licensing of contributions

The project is licensed under the AGPL-3.0-or-later (see [LICENSE](LICENSE)).

By opening a pull request you agree to two things:

1. You wrote the contribution, or otherwise have the right to submit it under
   the project licence — the [Developer Certificate of Origin](https://developercertificate.org/).
   Sign off your commits with `git commit -s` to state this.
2. You grant Tomas Smedberg a perpetual, worldwide, irrevocable, royalty-free
   licence to use your contribution and to relicense it, including under
   licences other than the AGPL. You keep the copyright to what you wrote.

Point 2 exists so the project can be relicensed or dual-licensed later without
having to track down every past contributor. If you are not comfortable with
it, say so in the pull request rather than signing off — a contribution is
still welcome, it just constrains what the project can do later.

## Development

On Ubuntu/T3, use Python 3.14 for local development, matching the Home
Assistant and CI Python minor version. `.python-version` selects 3.14 for uv;
the migration machine already has Python 3.14.4 at `/usr/bin/python3.14`.
Keep Ubuntu's system Python installed: the OS `python3` package depends on it.
The project virtual environment is independent of the default `python3` on
PATH, which may select the separately installed Python 3.15.

Python 3.15.2 also passed all 3,093 tests with the same dependency versions,
but 3.14 remains the project default. The 3.15 install required NumPy 2.3.2
and Pydantic Core 2.46.5 source builds (about eight minutes on this machine).
Do not downgrade Home Assistant or replace its transitive pins to force an
interpreter to work.

```bash
uv venv --python /usr/bin/python3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt --group dev
uv pip check --python .venv/bin/python
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
node --test .github/scripts/issue-triage.test.cjs
```

If `.venv` was created with another Python version, recreate it with the
selected interpreter before installing dependencies; an existing environment
does not change interpreter automatically.

The `dev` group selects the repository's Ruff version, also used by CI. The
Home Assistant pytest plugin owns the Home Assistant and pytest dependency
versions; do not pin those independently. Node 26 works for the dependency-free
triage tests. There is no npm install or frontend build step: the integration
ships its JavaScript directly in `custom_components/emhass_companion/`.

Tests are split in two. `tests/` includes isolated tests, and
`tests/integration/` uses `pytest-homeassistant-custom-component` to create a
Home Assistant instance inside pytest. Neither requires a running server,
Docker, real credentials, or a connection to the production installation.
Run the bare pytest command above for the full suite, rather than collecting
the directories separately. For a targeted check, append a test path.

Run directly from the Ubuntu checkout. `scripts/wsl_test.sh` is a legacy WSL
helper for slow Windows-mounted filesystems; its rsync copy, separate venv and
parallel execution are unnecessary here. Docker in the private notes describes
an optional interactive HA sandbox, not a prerequisite for tests. Windows
PowerShell paths and junction commands in those notes do not apply on Ubuntu.

### Local documentation build and deployment boundary

```bash
uv pip install --python .venv/bin/python -r requirements-docs.txt
.venv/bin/mkdocs build
```

This builds the documentation into ignored `site/` without publishing it.
On the copied checkout, `--strict` currently fails on existing links in ignored
`docs/plan/` notes. MkDocs includes those local notes in the output: do not
publish a build from this working tree without excluding private planning
material and reviewing the generated files.
The existing Docs GitHub workflow publishes with `mkdocs gh-deploy --force`;
that is a deployment and requires explicit approval. Integration deployment
likewise means installing/copying the integration into HA and restarting HA;
do not run live copy/restart commands during local setup.

### Home Assistant redeploy from Ubuntu (manual, approval required)

There is no compiled integration build. After tests pass, prepare a local
archive of the integration source (including your uncommitted edits):

```bash
tar --exclude='__pycache__' --exclude='*.pyc' -czf /tmp/emhass-companion.tar.gz \
  -C custom_components emhass_companion
tar -tzf /tmp/emhass-companion.tar.gz
```

Use the private SSH notes for the destination and identity, keeping those out
of tracked files. Linux uses `$HOME/.ssh/...`, not PowerShell's
`$env:USERPROFILE`. Use `BatchMode=yes`, `ConnectTimeout=10`, and
`StrictHostKeyChecking=yes`; verify an unknown host fingerprint independently.
Confirm read-only that the live integration symlink still targets the expected
source directory and compare the running files with this checkout first.
Old notes describe a host Git clone, then a plain directory without `.git`;
do not assume `git apply`, `git pull`, or host-side commits will work.

Only after deployment approval: back up the remote integration, transfer the
archive with `scp`, extract into a separate staging directory, review the diff,
and copy the approved files into the verified live source directory. Preserve
host-side edits and configuration; never sync the whole repo or use
`rsync --delete` against `/config`. Then run `ha core check` and, with restart
approval, `ha core restart`. Verify logs and the integration after restart.
A restart alone does not transfer local changes. Rollback requires restoring
the backed-up integration and restarting again. No live deployment command has
been verified from this Ubuntu host yet; confirm SSH access before proceeding.

Local setup and tests need no credentials. Copied private credential notes
remain under ignored `.agents/`; do not print their contents or copy them into
documentation. Before any approved remote operation, verify referenced Linux
key paths and access. Missing/expired credentials must be supplied securely;
never put tokens in tracked files or command output.

Every built-in profile must ship a fixture in `tests/fixtures/`: a recorded
blob from a real installation plus the series it should resolve to. That is
what lets a profile for an integration none of us has installed still be
verified, and what makes reviewing a contributed profile mechanical. See
[docs/profiles.md](docs/profiles.md).

`planning/` holds working design notes and roadmaps for in-progress features
— not built, not published to the docs site. Once a feature ships, its
reference material belongs in `docs/`, not there.

Contributing an inverter profile specifically? Start from
[docs/inverter_profile_roadmap.md](docs/inverter_profile_roadmap.md) — it
covers the current schema, the archetypes already proven, and which models
are wanted next.

## Issue triage bot

The **Issue triage** workflow uses Copilot to suggest a type, area, priority
(P1 highest to P4 lowest), effort (XS to XL), and summary. It runs on opened,
edited, and reopened issues and updates one comment as `github-actions[bot]`.
It adds an existing `bug`, `enhancement`, `question`, or `documentation` label
only when no type label is already present. It never removes labels, closes
issues, or changes code. Area, priority, and effort remain comment-only suggestions.

The default model is `gpt-6-luna`, suitable for focused classification and summaries.
To use another model available to your Copilot account, set the repository Actions
variable `ISSUE_TRIAGE_MODEL` to its Copilot CLI model ID. Model access depends on
the token owner's subscription and model policies. Do not use `gpt-4.1`: GitHub
retired it from Copilot on June 1, 2026.

If classification fails with only a CLI exit code, enable Actions debug logging
for a diagnostic rerun to see the CLI stderr. A nonempty token passes the setup
check but does not prove the token is valid or has access to the selected model.

One-time setup after merging the workflow:

1. With an active Copilot subscription, create a
   [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new).
   Select your **personal account** as resource owner, **Public repositories**
   for repository access, and **Account permissions > Copilot Requests**.
   No repository write permission is needed for this token.
2. In this repository, open **Settings > Secrets and variables > Actions**.
   Add a repository secret named `COPILOT_GITHUB_TOKEN` containing that token.
   Do not paste it into an issue, commit, or chat. Renew the secret when it expires.
3. To triage an existing issue, open **Actions > Issue triage > Run workflow**
   and enter its issue number (for example, `5`).

Copilot usage is charged to the token owner's subscription. Public issue authors
can trigger runs, including by editing their issues. Disable the workflow from
Actions if you need to stop usage. Each run has a five-minute timeout, and
overlapping runs for the same issue are cancelled.

Issue text is treated as untrusted input. Copilot is granted no tools, receives
only the title and first 12,000 body characters, and does not inspect code or
attachments. Invalid model output fails the run without publishing it; stale
results are skipped if the issue changed or closed. Closed issues, pull requests,
and bot-authored issues are skipped. Missing credentials fail with a setup message.

Run the bot's dependency-free tests with:

```bash
node --test .github/scripts/issue-triage.test.cjs
```

## Translations

English and Swedish. `translations/sv.json` is generated from `strings.json`
so the two cannot drift apart structurally; a test asserts they have
identical keys and that every `{placeholder}` survives translation.

Other languages are welcome — copy `strings.json`, translate the leaf values
only, and leave the keys and placeholders alone.
