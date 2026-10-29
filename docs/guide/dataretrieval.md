# `water-ogcapi` as the transport for `dataretrieval`

`dataretrieval`'s Water Data getters, such as `waterdata.get_daily()`, return pandas
frames typed for analysis. Under each getter, its OGC engine walks the pages of a
request with httpx: it follows the `next` links, retries, splits long queries, and sends
the API key. `water-ogcapi` does that walk and returns each page with the evidence of
how it was fetched, leaving column types and frames to the caller. This page shows how
the getters could hand the walk to `water-ogcapi` and keep their own shaping. The
`dataretrieval` code on this page is from release 1.4.0 and is shown, not run.

## The same query in both

Daily mean discharge at the Potomac River near Washington, DC, for 2025:

<!-- not run -->

```python title="dataretrieval"
from dataretrieval import waterdata

df, md = waterdata.get_daily(
    monitoring_location_id="USGS-01646500",
    parameter_code="00060",
    statistic_id="00003",
    time="2025-01-01/2025-12-31",
)
```

```python title="water-ogcapi"
from water_ogcapi import NWIS

QUERY = {
    "monitoring_location_id": "USGS-01646500",
    "parameter_code": "00060",
    "statistic_id": "00003",
    "datetime": "2025-01-01/2025-12-31",
}
with NWIS() as nwis:
    result = nwis.collect("daily", params=QUERY)
frame = result.to_arrow().to_pandas()
print(frame.shape, result.pagination.completeness)
```

Both return the same 365 days. `dataretrieval` sends the interval as a filter on the
`time` property, in pages of 50,000, and `water-ogcapi` sends the query as given, here
the OGC `datetime` parameter, in pages of 10,000. `dataretrieval` then renames the
feature `id` to `daily_id`, converts `time` to datetimes and `value` to floats, and
builds a GeoDataFrame when geopandas is installed. `water-ogcapi` keeps each property as
the server sent it, `value` included, and the geometry as GeoArrow WKB.

## Where the transport plugs in

`dataretrieval.ogc.engine._walk_pages(geopd, req, client)` sends one prepared
`httpx.Request`, follows its `next` links, parses each page into a frame with
`_get_resp_data()`, and returns the frames joined with one aggregated `httpx.Response`
for the call's metadata. A version on `water-ogcapi` takes the collection and query in
place of a prepared request, and keeps the pages as the metadata:

```python
import asyncio
from typing import Any

import pandas as pd

from water_ogcapi import Page
from water_ogcapi.aio import NWIS as AsyncNWIS


async def walk_pages(
    nwis: AsyncNWIS,
    collection: str,
    params: dict[str, str],
    cql2: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, list[Page]]:
    """Fetch every page of one query and join their features into one frame."""
    frames: list[pd.DataFrame] = []
    pages: list[Page] = []
    async for page in nwis.iter_pages(collection, params=params, filter=cql2):
        frames.append(page.to_arrow().to_pandas())
        pages.append(page)
    return pd.concat(frames, ignore_index=True), pages


async def main() -> None:
    async with AsyncNWIS() as nwis:
        frame, pages = await walk_pages(nwis, "daily", QUERY)
    print(len(frame), "rows from", len(pages), "pages")
    print(pages[0].url)
    print(pages[-1].headers.get("x-ratelimit-remaining"))


asyncio.run(main())
```

`_get_resp_data()` reads only its `body` argument when one is given, and each page holds
the decoded body in `page.body`, so `dataretrieval` can keep its own parsing in place of
`to_arrow()`. The first page's `url` is the request as sent, with the key never in it,
and the last page's headers carry the rate-limit state that `dataretrieval`'s metadata
reads from the aggregated response.

## What each concern maps to

| `dataretrieval` 1.4.0                                          | `water-ogcapi`                                                                                                |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `transport.pagination.paginate()` follows the `next` links     | `iter_pages()` follows them within the scheme, host, and port of the base URL, and refuses a repeated request |
| `RetryPolicy`, set by `retries` and `stall_timeout`            | `Transport(max_retries=..., timeout=...)`, retrying transport errors and 429, 500, 502, 503, and 504          |
| `FanOut` runs the chunks of a query split to fit a URL's bytes | `sites_per_piece` splits a site list, `split_datetime` an interval, and `ids` sends a list in a `POST` body   |
| `concurrency` caps the chunks in flight                        | `Transport(max_concurrent=...)` caps the requests in flight, 4 by default                                     |
| `ChunkInterrupted` and its `.call.resume()`                    | `PartialResultsError` and `collect(resume=...)`, or a page's `checkpoint`                                     |
| `API_USGS_PAT`, sent only to the host that accepts it          | `USGS_API_KEY`, sent only by `NWIS`, in the `X-Api-Key` header                                                |

`dataretrieval` keeps its key in `API_USGS_PAT`, and `NWIS(api_key=...)` takes it from
there. A long site list fits either way: `dataretrieval` splits the URL, and `ids` sends
the list as a CQL2-JSON body that no URL limit reaches:

```python
SITES = ["USGS-01646500", "USGS-01594440", "USGS-01638500"]
DAILY = {"parameter_code": "00060", "statistic_id": "00003", "datetime": "2025-06-01/2025-06-30"}

with NWIS() as nwis:
    result = nwis.collect("daily", ids=SITES, id_property="monitoring_location_id", params=DAILY)
print(len(result.geojson["features"]), "daily values for", len(SITES), "sites")
```
