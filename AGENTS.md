# AGENTS.md

Instructions for AI coding agents working in this repository.

## Project

`water-ogcapi` is an async-first Python client for USGS water data services (NWIS,
FabricData, GeoConnex) over OGC API Features. `README.md` lists the endpoints.

USGS operates these APIs. Before any request to a live endpoint, read
`.agents/skills/live-api/SKILL.md`.

## Design record

`docs/design.md` holds every design decision (`D-`), the lessons from past mistakes
(`L-`), and the open questions (`Q-`), each with its rationale and rejected
alternatives.

- Before designing a component or proposing an alternative to a rule below, search it.
    Do not reopen an accepted decision without evidence it does not already account for.
- When a change makes or reverses a decision, answers a question, or exposes a mistake
    worth not repeating, update `docs/design.md` in the same change, following the rules
    at its top.

## Design rules

One-line summaries of the decisions any code change can break. The IDs point into
`docs/design.md`.

- `src/water_ogcapi/_transport.py` is the only module that imports `httpx2` (D-01).
- `iter_pages()` is the primitive and `collect()` builds on it (D-02).
- Query methods return payload plus evidence (`Page`, `QueryResult`), never a bare
    GeoJSON dict (D-03).
- Any type that carries request headers or params redacts credentials at construction,
    using an allowlist (D-04).
- The sync wrapper runs the async core on one background event-loop thread, never
    `asyncio.run()` or `nest_asyncio` (D-08).
- No pandas, geopandas, xarray, Arrow, shapely, or STAC on the core import path (D-09).
- Construction performs no HTTP (D-10), and redirects are never followed (D-12).
- The USGS API key never goes to a non-USGS host such as GeoConnex (D-13).
- The API key travels only in the `X-Api-Key` header, never as a query parameter (D-18).

## Commands

Run everything through pixi. Never call bare `pytest`, `python`, or `pyright`.

| Task                       | Command                             |
| -------------------------- | ----------------------------------- |
| Lint (pre-commit)          | `pixi r lint`                       |
| Type check                 | `pixi r typecheck`                  |
| Test, offline (3.12)       | `pixi r -e test312 test`            |
| Test, offline (3.15)       | `pixi r -e test315 test`            |
| Test, network-marked only  | `pixi r -e test315 test-network`    |
| Test, everything           | `pixi r -e test315 test-all`        |
| Coverage report / HTML     | `pixi r -e test315 report` / `html` |
| Run Python                 | `pixi r -e dev python ...`          |
| Spell check (writes fixes) | `pixi r spell`                      |
| Changelog preview          | `pixi r changelog`                  |
| Docs, strict build         | `pixi r docs`                       |
| Docs, live preview         | `pixi r docs-serve`                 |

Environments: `dev`, `test312`, `test315`, `typecheck`, `lint`, `docs`.

A change is done when lint, typecheck, and the offline tests on both test312 and test315
pass, plus `pixi r docs` when it touches `docs/`, `README.md`, or `zensical.toml`.
`.github/workflows/ci.yml` runs the first three, and `.github/workflows/docs.yml` builds
the site on every PR and deploys `main` to GitHub Pages.

## Code style

- Python >= 3.12, with `from __future__ import annotations` in every file.
- ruff with 100-character lines and `select = ["ALL"]`; pyright in strict mode. Both are
    configured in `pyproject.toml`.
- NumPy-convention docstrings.
- Source in `src/water_ogcapi/`, tests in `tests/`.
- Offline tests mock HTTP with `httpx2.MockTransport`. Tests that reach a real server
    carry `@pytest.mark.network`, and CI never requires them.

## Gotchas

- `addopts` sets `--cov-append` and coverage runs in parallel mode, so coverage
    accumulates across runs. Delete `.coverage` and any `.coverage.*` files before
    trusting a number.
- `report` and `html` depend on `test-all`, so they also run the network-marked tests.
- pyright checks only `src/`, so a green `typecheck` says nothing about `tests/`.
- `pixi r lint` runs vulture at 80% confidence, which catches only unused imports,
    unused function arguments, and unreachable code. Unused functions, classes, and
    local variables score 60% and pass.
- ruff auto-inserts `from __future__ import annotations`.
- mdformat rewraps markdown to 88 columns during `pixi r lint`, but pre-commit sees only
    git-tracked files, so an untracked doc is skipped.
- `docs/index.md` only includes `README.md`. Edit the README to change the site's home
    page.
- Skills live in `.agents/skills/`. `.claude/skills` is a symlink to it for Claude Code,
    which does not read `.agents/`.

## Git

- Conventional Commits: `<type>[optional scope]: <description>`, with type one of
    `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`.
- Subject under 72 characters, imperative mood.
- PRs target `main`.
- git-cliff generates the changelog from commit messages. Do not edit `CHANGELOG.md` by
    hand.
- `pixi r release <version>` tags and pushes to the remote. Run it only when a
    maintainer asks.

## Writing style

These rules cover code comments, docstrings, commit messages, PR descriptions, issues,
and docs.

- American spelling. Fix British spellings on sight.
- Active voice, present tense, concise.
- Use an em or en dash only when no comma, colon, semicolon, parentheses, or new
    sentence works.
- Be concrete. A number, name, or mechanism beats an abstraction: "handles errors
    robustly" becomes "retries 429 and 5xx three times, then raises".
- No contrastive framing. Say what a thing is and stop, without a negated counterpart
    added for rhythm or emphasis: "the launch is checked rather than trusted" reads "the
    launch is checked". The ban covers "rather than", "not X but Y", "instead of", and
    "X, not Y" in any position. A real either/or, where both options are live and the
    reader needs to know which was picked, stays.
- Keep terms consistent. Once something has a name, reuse it, because a reader takes a
    new word for a new thing. If the text calls it a `Page`, later sentences do not
    switch to "chunk" or "batch".
- Cut filler ("it's worth noting", "in order to", "when it comes to"), puffery ("plays a
    vital role", "stands as a testament"), and closing summaries that restate what came
    before.
- Banned words: delve, foster, empower, streamline, facilitate, cutting-edge, seamless,
    multifaceted, nuanced, intricate, meticulous, paramount, transformative, elevate,
    embark, supercharge.

Comments and commits:

- Comment only what the code does not say: a decision a reader would not guess, or
    behavior a reader would not see from reading it.
- Keep a comment or commit body to a line or two. Cut the rationale to the sentence that
    stops the next reader from undoing the change.
- Comments describe the code as it stands. History ("used to", "the old code", the bug
    that prompted the change) goes in the commit message.

Reviews and reports, including API findings:

- Say which claims you checked against the source and which you infer.
- Reserve prose for what changes the conclusion. Minor issues go in a numbered list at
    the end.
