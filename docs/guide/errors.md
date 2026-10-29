# Errors

A query's errors derive from `WaterOGCAPIError`, in `water_ogcapi.exceptions`. Other
exceptions keep their Python types: `to_arrow()` raises `ValueError`, a pyarrow error,
or a `ModuleNotFoundError` without the `arrow` extra, and a CQL2-JSON filter that JSON
cannot encode raises `TypeError`.

| Exception             | Raised when                                                      | Carries                                    |
| --------------------- | ---------------------------------------------------------------- | ------------------------------------------ |
| `ValidationError`     | Caller input is invalid, before any page request; a `ValueError` | A message naming the valid options         |
| `ServiceError`        | A request fails: an error status, a redirect, a malformed body   | `status`, `url`, `attempts`, `retry_after` |
| `RateLimitError`      | A 429 outlasts the retries, a `ServiceError`                     | `remaining`, plus the above                |
| `PaginationError`     | Page requests of a traversal fail                                | `failures`, `checkpoint`                   |
| `PartialResultsError` | `collect()` lost a page after others arrived                     | `result`, plus the above                   |

Every URL an error holds has its credential query parameters masked, and `NWIS` sends
the key only in a header.

## Input errors

`ValidationError` names what went wrong and, where the set is finite, the valid options:

```python
from water_ogcapi import NWIS
from water_ogcapi.exceptions import ServiceError, ValidationError

nwis = NWIS()
try:
    nwis.collect("monitoring-location")
except ValidationError as err:
    print(str(err)[:160])
```

## Service errors

A failed request raises `ServiceError` with the status and the URL as sent. `attempts`
above 1 means the transport retried first:

```python
try:
    nwis.item("monitoring-locations", "USGS-not-a-site")
except ServiceError as err:
    print(err.status, err.attempts, err.url)
```

## Failures during a traversal

A page request that fails inside `iter_pages()` raises `PaginationError` once every page
that arrived has been yielded. `failures` holds one `ServiceError` per failed request,
and `checkpoint`, when not `None`, resumes the query with `iter_pages(resume=...)`.

`collect()` raises `PartialResultsError` when a page fails after at least one arrived.
Its `result` is a `QueryResult` of the pages that arrived, with `completeness`
`limited`, and `collect(resume=err.result)` sends only the requests still pending. A
`PaginationError` from `collect()` means no page arrived, and its `checkpoint` resumes
the query. The pattern, with one retry:

```python
from water_ogcapi.exceptions import PaginationError, PartialResultsError

QUERY = {"monitoring_location_id": "USGS-01646500", "datetime": "2025-01-01/2025-01-31"}
try:
    result = nwis.collect("daily", params=QUERY)
except PartialResultsError as err:
    result = nwis.collect("daily", resume=err.result)
except PaginationError as err:
    if err.checkpoint is None:
        raise
    result = nwis.collect("daily", resume=err.checkpoint)
print(result.pagination.completeness)
nwis.close()
```

The [cache, quota, and resume notebook](../examples/cache_quota_resume.md) runs this
against a transport that fails one request on purpose.
