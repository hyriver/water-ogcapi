# Reviewing water-ogcapi

For every reviewer of a change here, a person or an agent. Read `AGENTS.md` first, and
the `docs/design.md` entries the change touches.

## What failure looks like here

1. **A credential leaks.** The USGS key reaches a log record, an error message, a URL, a
    pickle, a redirect target, or a host other than the NWIS profile's (D-04, D-13,
    D-18, D-20, D-40, L-09, L-11).
1. **The evidence lies.** A `QueryResult` reads `complete` after a page was skipped,
    cut, or failed, or a `Page` or an error misstates the request that produced it
    (D-03, D-24, D-36, D-38).
1. **Quota burns.** A loop, a retry, or a repeated `next` link sends requests the
    governor never paced (D-34, D-49).
1. **A boundary breaks.** `httpx2` imported outside `_transport.py` (D-01), HTTP at
    construction (D-10), or a redirect followed (D-12).
1. **The public surface moves silently.** A public name removed without D-21's
    deprecation release, or a pickled type's module path changed without the alias D-51
    asks for.
1. **A claim outruns its test.** A docstring or design-record sentence states behavior
    that no test pins.

## Out of scope

- `pixi.lock`, `tests/fixtures/**/*.json`, notebook outputs in `docs/examples/*.ipynb`,
    and the generated `docs/roadmap.md`.
- Anything the gate already enforces: ruff with `select = ["ALL"]`, pyright strict on
    `src/`, mdformat, codespell.
- Accepted entries in `docs/design.md`. Challenge one only with evidence or a
    consequence the entry does not account for.

## How to report

- Each finding names the file and line, the input or state that triggers it, and what
    goes wrong. A guess from a name is no finding.
- Mark each finding verified (you read the code or ran a probe) or inferred.
- Tag each one important, nit, or pre-existing (present before this change). Report at
    most three nits.
- Report the class of a bug: when one site has it, list every site that does, so one fix
    round covers them.
- Open with the count, "N findings (I important, P pre-existing)", and say "No findings"
    when there are none.
- On a re-review, give a fixed or still-open verdict on each earlier finding first,
    review only the new diff, and repeat no finding already answered.

## Depth

Match the depth to what a missed bug would cost:

| Change touches                                                                                                                                                                      | Review                                                                                                                       |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Only docs, `pixi.lock`, pre-commit revisions, or tests                                                                                                                              | CI only                                                                                                                      |
| Other code in `src/` or `scripts/`                                                                                                                                                  | A review of the diff                                                                                                         |
| `_transport.py`, `_governor.py`, `_services.py`, `_redact.py`, `_sync.py`, `OGCService._send()` or `_traverse()`, the exports in `__init__.py` or `aio.py`, or `.github/workflows/` | A review of the whole project at the change, and `/security-review` when the change touches the key, redaction, or redirects |

After three review rounds on one change, stop and reconsider the approach before a
fourth.
