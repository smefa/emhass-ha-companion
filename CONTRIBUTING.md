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

## Translations

English and Swedish. `translations/sv.json` is generated from `strings.json`
so the two cannot drift apart structurally; a test asserts they have
identical keys and that every `{placeholder}` survives translation.

Other languages are welcome — copy `strings.json`, translate the leaf values
only, and leave the keys and placeholders alone.
