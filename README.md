# Water OGC API: Water Data Client for the USGS Services

[![CI](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml/badge.svg)](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml)
[![Docs](https://github.com/hyriver/water-ogcapi/actions/workflows/docs.yml/badge.svg)](https://docs.hyriver.io/water-ogcapi/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/hyriver/water-ogcapi/blob/main/LICENSE)

An async-first Python client for USGS water data services over OGC API Features.

**Status:** early development. The async transport exists and the query API is in
progress. The release candidate on PyPI only reserves the name and has no query API, so
install from this repository until the first final release.

## Features

- Native async over HTTP/2, with bounded concurrency.
- Retries transient failures with exponential backoff and honors `Retry-After`.
- Two direct runtime dependencies, `httpx2` and `h2`. No pandas.

## Services

| Service    | Base URL                             |
| ---------- | ------------------------------------ |
| NWIS       | `api.waterdata.usgs.gov/ogcapi/v1`   |
| FabricData | `api.water.usgs.gov/fabric/pygeoapi` |
| GeoConnex  | `reference.geoconnex.us`             |

## Installation

water-ogcapi is not on PyPI or conda-forge yet. Install the development version from
GitHub (Python 3.12 or later):

```bash
pip install git+https://github.com/hyriver/water-ogcapi
```

## Quick start

The query API is in progress, so there is no runnable example yet. In its planned shape,
`iter_pages()` streams each page as it arrives and `collect()` returns one result
holding the aggregated GeoJSON and the evidence for every page. The
[design decisions](https://docs.hyriver.io/water-ogcapi/design/) (D-02 and D-03)
describe it.

## Documentation

The [documentation](https://docs.hyriver.io/water-ogcapi/) has the API reference, a
[logging guide](https://docs.hyriver.io/water-ogcapi/logging/), and the design decisions
behind the library. Example notebooks arrive with the query API.

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
