# Installation and the API key

## Installation

--8<-- "README.md:install"

The core installs two runtime dependencies, `httpx2` and `h2`, and imports no frame
library. pandas, geopandas, and polars read the tables that `to_arrow()` returns, and
none of them is a dependency.

## The USGS API key

`NWIS` works without a key. The
[USGS Water Data API keys page](https://api.waterdata.usgs.gov/docs/ogcapi/keys/)
explains the hourly limits a key raises and issues one. Set it in the environment:

```bash
export USGS_API_KEY="your key"
```

`NWIS()` reads `USGS_API_KEY` once, when it is built. `NWIS(api_key=...)` takes the key
from code, and `NWIS(api_key="")` sends none. The key travels only in the `X-Api-Key`
header of each request, never in a URL, so it stays out of the `next` links the server
writes and out of every `Page.url`. `FabricData` and `GeoConnex` never receive it.

Keyed NWIS responses report the key's quota in their `X-RateLimit-Limit` and
`X-RateLimit-Remaining` headers, which `Page.headers` keeps. Unkeyed NWIS responses
carry neither header, so both read `None` below without a key:

```python
import os

from water_ogcapi import NWIS

print("key set:", bool(os.environ.get("USGS_API_KEY")))
with NWIS() as nwis:
    page = nwis.item("monitoring-locations", "USGS-01646500")
print(page.headers.get("x-ratelimit-limit"), page.headers.get("x-ratelimit-remaining"))
```

A key passed in `params` raises before any request is sent, since a query parameter
would put it in the URL:

```python
from water_ogcapi.exceptions import ValidationError

with NWIS() as nwis:
    try:
        nwis.collect("monitoring-locations", params={"api_key": "not-a-key"})
    except ValidationError as err:
        print(err)
```

## Pacing

Each service paces its own requests. It spaces them further apart as
`X-RateLimit-Remaining` runs down, and one second apart when responses carry no
rate-limit headers, as GeoConnex's and unkeyed NWIS responses do. A timeout, a failed
connection, read, or write, or a 429, 500, 502, 503, or 504 response is retried up to
three times. A proxy refusal, an unsupported URL scheme, or a request httpx2 cannot
encode raises at once. A `Retry-After` of up to 60 seconds sets the wait. A longer one
raises at once, as a `RateLimitError` for a 429, with the wait in its `retry_after`.
`Transport(max_retries=...)` changes the retry count:

```python
from water_ogcapi import Transport

with NWIS(transport=Transport(max_retries=1)) as nwis:
    page = nwis.item("monitoring-locations", "USGS-01646500")
print(page.status)
```
