# Data files from the STAC API

The USGS Water Data STAC API lists data files: each item describes one file, and its
`assets` link to that file. `WaterDataSTAC` queries it at
`api.waterdata.usgs.gov/stac/v0/` with the same methods and query forms as the other
services. On 2026-11-13 it served three collections: `continuous` and `daily`, one
Parquet file set per monitoring location, and `ratings`, the stage-discharge rating
files.

`WaterDataSTAC` sends the key in `USGS_API_KEY`, as `WaterData` does. USGS suggests a
separate key for this service; pass it as `WaterDataSTAC(api_key=...)`.

## Finding files

The rating files of the Potomac River gage:

```python
from water_ogcapi import WaterDataSTAC

SITE = ["USGS-01646500"]

with WaterDataSTAC() as stac:
    result = stac.collect("ratings", ids=SITE, id_property="monitoring_location_id")
for item in result.geojson["features"]:
    print(item["id"], item["properties"]["file_type"], item["assets"]["data"]["href"])
```

A CQL2-JSON `filter`, and `ids`, go in the query of a `GET`, since the service refuses a
`POST` to its items. A page holds up to 1,000 items unless `params` set a `limit`, and
10,000 at most. The service has no schema endpoint, so `schema()` raises `ServiceError`.

## Downloading a file

The asset files need no key. `Client.download()` fetches one to disk, as
`Transport.download()` does from async code:

```python
import tempfile
from pathlib import Path

from water_ogcapi import Client

base = next(f for f in result.geojson["features"] if f["properties"]["file_type"] == "base")
with Client() as client:
    path = Path(tempfile.mkdtemp()) / "USGS-01646500.base.rdb"
    download = client.download(base["assets"]["data"]["href"], path)
print(download.size, "bytes in", download.path.name)
```

Reading a Parquet asset from `daily` or `continuous` needs `pyarrow` or another Parquet
reader.

## Not covered

The service also answers `/search`, which queries several collections at once.
`WaterDataSTAC` queries one collection at a time through its items.
