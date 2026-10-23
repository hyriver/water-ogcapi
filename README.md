# Water OGC API: Water Data Client for the USGS Services

[![CI](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml/badge.svg)](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml)
[![Docs](https://github.com/hyriver/water-ogcapi/actions/workflows/docs.yml/badge.svg)](https://docs.hyriver.io/water-ogcapi/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/hyriver/water-ogcapi/blob/main/LICENSE)

A Python client for USGS water data services over OGC API Features. Its service classes
block, for scripts and notebooks, and `water_ogcapi.aio` holds their async twins.

**Status:** early development. The async and sync query APIs and the metadata cache work
on all three services, with the quota governor, page-level resume, and concurrent NWIS
pieces. The release candidate on PyPI only reserves the name and has no query API, so
install from this repository until the first final release.

## Features

- Blocking service classes for scripts and notebooks, Jupyter included.
- Async twins in `water_ogcapi.aio`, over HTTP/2 with bounded concurrency.
- Retries transient failures with exponential backoff and honors `Retry-After`.
- Splits an NWIS query by year, month, or site list and runs the pieces concurrently, as
    USGS's efficiency guidance recommends.
- Spaces requests further apart as a service's `X-RateLimit-Remaining` runs down, and
    one second apart on a service that sends no rate-limit headers.
- Checks a query's collection and parameter names against the service's metadata, so a
    typo raises an error listing the valid names before it can return the whole
    collection.
- Two direct runtime dependencies, `httpx2` and `h2`. No pandas.
- With the `arrow` extra, `QueryResult.to_arrow()` returns a pyarrow Table with a
    GeoArrow geometry column that geopandas, polars, and pandas read.

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
conda-forge has no package yet. Install the development version from GitHub (Python 3.13
or later):

```bash
pip install git+https://github.com/hyriver/water-ogcapi
```

The `arrow` extra adds pyarrow and shapely for `QueryResult.to_arrow()`:

```bash
pip install "water-ogcapi[arrow] @ git+https://github.com/hyriver/water-ogcapi"
```

## Quick start

The monitoring locations in a small box around Washington, DC:

```python
from water_ogcapi import NWIS

with NWIS() as nwis:
    result = nwis.collect("monitoring-locations", bbox=(-77.12, 38.80, -76.91, 38.99))
print(len(result.geojson["features"]), result.pagination.completeness)
```

`collect()` returns one `QueryResult` holding the aggregated GeoJSON and the evidence
for every page; `iter_pages()` streams each page as it arrives. The
[design decisions](https://docs.hyriver.io/water-ogcapi/design/) (D-02 and D-03)
describe both.

Code that runs its own event loop can use the async classes in `water_ogcapi.aio`, which
take the same arguments:

```python
import asyncio

from water_ogcapi.aio import NWIS


async def main() -> None:
    async with NWIS() as nwis:
        result = await nwis.collect("monitoring-locations", bbox=(-77.12, 38.80, -76.91, 38.99))


asyncio.run(main())
```

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
