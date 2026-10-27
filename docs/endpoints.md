# Tracking USGS endpoints

`water-ogcapi` talks to the three services the README lists with their base URLs. Each
service class reads the live `/collections` index, cached for an hour
([D-07](design.md#d-07-in-memory-success-only-metadata-cache)), and checks a query's
collection and parameter names against it before the first page request
([D-47](design.md#d-47-queries-are-checked-against-cached-metadata-before-the-first-page-request)).
A collection USGS adds to a service the package already covers therefore works with the
released package the day it appears. A new service, and a change to an existing one,
take the steps below.

## How a change is detected

The `Drift` workflow runs every Monday at 07:00 UTC, and from the Actions tab on request
([D-63](design.md#d-63-weekly-drift-run-against-the-live-services)). It has two jobs:

- `network-tests` runs the tests marked `network` against the live services: the Potomac
    gage and the Washington, DC bounding box on NWIS, the DC bounding box on FabricData,
    and the 22 two-digit HUCs on GeoConnex.
- `openapi` fetches each service's OpenAPI document and compares it with
    `tests/fixtures/<service>/openapi.json`: the whole document minus its prose
    (descriptions, summaries, titles, and examples), with `info` cut to the title and
    version. A wording edit passes, and a `$ref` is kept as the reference, so a change
    inside the referenced document is invisible. A service that does not answer fails
    the job and keeps its snapshot.

A failed `openapi` job prints a unified diff per service. Read it for:

| In the diff                                  | What changed upstream                                                                                                                                                                  |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Added `/collections/<id>` paths              | A new collection                                                                                                                                                                       |
| Removed `/collections/<id>` paths            | A collection withdrawn or renamed                                                                                                                                                      |
| A changed `maximum` on a `limit` parameter   | The page cap that `max_limit` in `_services.py` mirrors (D-30); a changed `default` changes nothing, since the client sends its own `limit` (D-41)                                     |
| A changed or added parameter                 | A query form the client may send                                                                                                                                                       |
| A changed `requestBody` media type or schema | The declared `POST` body; the client sends `application/query-cql-json` where the documents declare `application/json`, and the GeoConnex `ids` test guards that the servers accept it |
| A changed `info.version` and nothing else    | A release without an interface change the snapshot sees                                                                                                                                |

A failed `network-tests` job means a stable target moved or a service misbehaves; a
FabricData outage skips its test (D-14). Investigate following
`.agents/skills/live-api/SKILL.md`.

The diff cannot show a new service, since nothing holds a snapshot for it. Watch the
USGS Water Data APIs documentation at <https://api.waterdata.usgs.gov/docs/ogcapi/> for
a new base URL.

Between runs, the same check runs locally:

```bash
pixi r -e test315 drift
```

## Responding to drift

1. Decide what the change means for the client, using the table above. A new collection
    needs no code; add a fixture and an example when it earns one. A changed cap
    updates `max_limit` in `src/water_ogcapi/_services.py` and the D-30 table. A
    removed collection or parameter updates the tests, examples, and guides that name
    it.

1. Refresh the snapshots and commit them with their new `recorded` date:

    ```bash
    pixi r -e test315 drift --update
    ```

1. Open a PR for the change. When it changes what the client sends, release it, and name
    each snapshot's service version, the `info.version` field of its `openapi.json`, in
    the release notes (D-63).

## Adding a service

A new OGC API Features service gets:

1. A `ServiceProfile` and a service class in `src/water_ogcapi/_services.py`: the base
    URL with its trailing slash, the default and maximum `limit`, and `key_env` only
    for a USGS host that reads `X-Api-Key` (D-13, D-18). A blocking twin in `_sync.py`,
    and exports in `__init__.py` and `aio.py`.
1. An entry in `SERVICES` in `scripts/openapi_drift.py` and a row in
    `tests/fixtures/README.md`, then `pixi r -e test315 drift --update`.
1. Fixtures through `scripts/record_fixtures.py` and a network-marked test on a stable
    target (D-14).
1. A row in the README's endpoint table and a `D-` entry in the design record.
