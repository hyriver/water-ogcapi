# water-ogcapi

[![CI](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml/badge.svg)](https://github.com/hyriver/water-ogcapi/actions/workflows/ci.yml)
[![Docs](https://github.com/hyriver/water-ogcapi/actions/workflows/docs.yml/badge.svg)](https://docs.hyriver.io/water-ogcapi/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/hyriver/water-ogcapi/blob/main/LICENSE)

An async-first Python client for USGS water data services over OGC API Features.

**Status:** early development. The async transport exists, the query API is in progress,
and nothing is released yet.

## Features

- Native async over HTTP/2, with bounded concurrency.
- Retries transient failures with exponential backoff and honors `Retry-After`.
- Two direct runtime dependencies, `httpx2` and `h2`. No pandas.

## Services

| Service    | Base URL                             |
| ---------- | ------------------------------------ |
| NWIS       | `api.waterdata.usgs.gov/ogcapi/v0`   |
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

The [documentation](https://docs.hyriver.io/water-ogcapi/) has the API reference and the
design decisions behind the library. Example notebooks arrive with the query API.

## Roadmap

- [ ] Service classes for NWIS, FabricData, and GeoConnex.
- [ ] Queries by bbox, CQL2 spatial predicates, CQL2-JSON and CQL2-Text, identifier
    filters, and direct item fetch.
- [ ] Stream results page by page, or collect them into one result.
- [ ] Return raw GeoJSON together with the evidence of how it was fetched: request URLs,
    status codes, headers, links, and pagination state.
- [ ] Redact API keys from every result and error.
- [ ] A synchronous wrapper that works inside Jupyter.
- [ ] An in-memory metadata cache that revalidates with `ETag`.
- [ ] A quota governor that slows down before the rate limit runs out.
- [ ] Page-level checkpoint and resume.
- [x] A package logger with `configure_logger()`: console level, an optional log file
    with its own level and mode, and a file-only switch.
- [ ] Decide whether orjson decodes large responses, as a dependency or an optional
    extra (open question Q-09 in the design decisions).
- [ ] Release on PyPI and conda-forge.

## Contributing

Report bugs and ask questions in
[GitHub issues](https://github.com/hyriver/water-ogcapi/issues). The
[contributing guide](https://docs.hyriver.io/water-ogcapi/contributing/) covers the
development setup, the checks a change must pass, and the project conventions.

## License

MIT. See [LICENSE](https://github.com/hyriver/water-ogcapi/blob/main/LICENSE).
