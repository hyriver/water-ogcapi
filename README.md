# Water OGC API: Water Data Client for the USGS Services

[![CI](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml/badge.svg)](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml)
[![Docs](https://github.com/hyriver/water-ogcapi/actions/workflows/docs.yml/badge.svg)](https://docs.hyriver.io/water-ogcapi/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/hyriver/water-ogcapi/blob/main/LICENSE)

An async-first Python client for USGS water data services over OGC API Features.

**Status:** early development. The async query API works on all three services; the sync
API, metadata cache, quota governor, and resume are in progress. The release candidate
on PyPI only reserves the name and has no query API, so install from this repository
until the first final release.

## Features

- Native async over HTTP/2, with bounded concurrency.
- Retries transient failures with exponential backoff and honors `Retry-After`.
- Two direct runtime dependencies, `httpx2` and `h2`. No pandas.

## Services

| Service    | Class        | Base URL                              |
| ---------- | ------------ | ------------------------------------- |
| NWIS       | `NWIS`       | `api.waterdata.usgs.gov/ogcapi/v1/`   |
| FabricData | `FabricData` | `api.water.usgs.gov/fabric/pygeoapi/` |
| GeoConnex  | `GeoConnex`  | `reference.geoconnex.us/`             |

`NWIS` sends the key in `USGS_API_KEY`, when set, in the `X-Api-Key` header. The other
two services never receive it.

## Installation

PyPI holds only a release candidate, which reserves the name and has no query API, and
conda-forge has no package yet. Install the development version from GitHub (Python 3.12
or later):

```bash
pip install git+https://github.com/hyriver/water-ogcapi
```

## Quick start

The monitoring locations in a small box around Washington, DC:

```python
import asyncio

from water_ogcapi import NWIS


async def main() -> None:
    async with NWIS() as nwis:
        result = await nwis.collect("monitoring-locations", bbox=(-77.12, 38.80, -76.91, 38.99))
    print(len(result.geojson["features"]), result.pagination.completeness)


asyncio.run(main())
```

`collect()` returns one `QueryResult` holding the aggregated GeoJSON and the evidence
for every page; `iter_pages()` streams each page as it arrives. The
[design decisions](https://docs.hyriver.io/water-ogcapi/design/) (D-02 and D-03)
describe both.

## Documentation

The [documentation](https://docs.hyriver.io/water-ogcapi/) has the API reference, a
[logging guide](https://docs.hyriver.io/water-ogcapi/logging/), and the design decisions
behind the library. Example notebooks for each service are in progress.

## Roadmap

The [roadmap](https://docs.hyriver.io/water-ogcapi/roadmap/) shows the progress on each
GitHub milestone, regenerated daily from the milestone issues.

## Contributing

Report bugs and ask questions in
[GitHub issues](https://github.com/hyriver/water-ogcapi/issues). The
[contributing guide](https://docs.hyriver.io/water-ogcapi/contributing/) covers the
development setup, the checks a change must pass, and the project conventions.

## License

MIT. See [LICENSE](https://github.com/hyriver/water-ogcapi/blob/main/LICENSE).
