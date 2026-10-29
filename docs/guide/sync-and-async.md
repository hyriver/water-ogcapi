# Blocking and async APIs

## Blocking classes

`NWIS`, `FabricData`, `GeoConnex`, and `OGCService` at the package root block until each
call returns. Each one owns an async service of its own and runs it on one background
event-loop thread that the package starts on first use. A call from any thread, a
Jupyter cell included, waits for that loop and never calls `asyncio.run()`, so it works
where another event loop is already running.

`iter_pages()` returns a plain iterator. Each step fetches one page, and closing the
iterator, or dropping it, stops the query:

```python
from water_ogcapi import NWIS

DC = (-77.12, 38.80, -76.91, 38.99)

with NWIS() as nwis:
    pages = nwis.iter_pages("monitoring-locations", bbox=DC, params={"limit": "100"})
    first = next(pages)
    pages.close()
print(first.feature_count, "features in the first page")
```

## Async classes

`water_ogcapi.aio` holds the async twins, with the same names and arguments. Use them
from code that runs its own event loop, and to run several queries concurrently on one
service:

```python
import asyncio

from water_ogcapi.aio import NWIS as AsyncNWIS

SITES = ["USGS-01646500", "USGS-01594440", "USGS-01638500"]
QUERY = {"parameter_code": "00060", "statistic_id": "00003", "datetime": "2025-06-01/2025-06-30"}


async def main() -> None:
    async with AsyncNWIS() as nwis:
        results = await asyncio.gather(
            *(
                nwis.collect("daily", params={**QUERY, "monitoring_location_id": site})
                for site in SITES
            )
        )
        async for page in nwis.iter_pages("monitoring-locations", bbox=DC, params={"limit": "100"}):
            print(page.feature_count, "features")
            break
    for site, result in zip(SITES, results, strict=True):
        print(site, len(result.geojson["features"]), "days")


asyncio.run(main())
```

One service shares its transport, quota pacing, and metadata cache across every query it
runs, so the three queries above draw on one pacing schedule. The transport runs up to
`max_concurrent` requests at once, 4 by default.

## Transports

`Transport` holds the HTTP client and its settings: `timeout` per attempt (30 seconds),
`max_concurrent`, `max_retries`, and `backoff`. A service builds one unless given one. A
transport binds to the event loop of its first request, so share one only among blocking
services, which all run on the background loop, or only among async services on one
loop:

```python
from water_ogcapi import FabricData, Transport

transport = Transport(timeout=60, max_concurrent=2)
nwis = NWIS(transport=transport)
fabric = FabricData(transport=transport)
print(len(nwis.collect("monitoring-locations", bbox=DC).geojson["features"]))
print(len(fabric.collect("gagesii", bbox=DC).geojson["features"]))
nwis.close()
```

`close()` on either service closes the shared transport, and the other reconnects on its
next request.
