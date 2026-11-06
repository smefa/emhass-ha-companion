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

```bash
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
```

Tests are split in two. `tests/` needs no running Home Assistant and runs on
any platform. `tests/integration/` needs one, via
`pytest-homeassistant-custom-component`, which does not load on Windows —
run those on Linux or in a container.

Run the bare `pytest` before pushing, not the two directories separately:
collection problems only appear when both are collected together.

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
