# Data files from the STAC API

The USGS Water Data STAC API lists data files: each item describes one file, and its
`assets` link to that file. `WaterDataSTAC` queries it at
`api.waterdata.usgs.gov/stac/v0/` with the same methods and query forms as the other
services, one collection at a time. On 2026-11-13 it served three collections:
`continuous` and `daily`, Parquet files per monitoring location, and `ratings`, the
stage-discharge rating files.

## The API key

`WaterDataSTAC` sends the key in `USGS_STAC_API_KEY`, or in `USGS_API_KEY` when that is
unset, in the `X-Api-Key` header. A download that carries a key counts against that
key's hourly limit, one request per file and one per range request, so a reader such as
DuckDB that fetches a Parquet file in pieces spends several. USGS suggests a separate
key for this service, which keeps that traffic off the OGC API quota. A download without
a key reports no rate-limit headers, and USGS does not document the limit it gets.

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
`POST` to its items. The service refuses a `GET` URL past about 2 KB with a 403, so a
site list given as `ids` runs as concurrent pieces of 50 sites, or of `sites_per_piece`.
A CQL2-Text `IN` list is sent whole, so give a long site list as `ids`. A page holds up
to 1,000 items unless `params` set a `limit`, and 10,000 at most. The service has no
schema endpoint, so `schema()` raises `ServiceError`.

The items carry site metadata, so a CQL2-Text `LIKE` selects a basin by its hydrologic
unit code prefix:

```python
with WaterDataSTAC() as stac:
    basin = stac.collect("daily", filter="hydrologic_unit_code LIKE '02070008%'")
print(len(basin.geojson["features"]), "sites with daily values")
```

The items do not list parameters, periods, or approval status. To find the files of the
sites that measure discharge, ask the OGC API for their time series first, through
`combined-metadata`, then pass the sites to `WaterDataSTAC`:

```python
from water_ogcapi import WaterData

with WaterData() as wd:
    series = wd.collect(
        "combined-metadata",
        filter="hydrologic_unit_code LIKE '02070008%' AND parameter_code = '00060' "
        "AND data_type = 'Continuous values'",
    )
sites = sorted({f["properties"]["monitoring_location_id"] for f in series.geojson["features"]})
with WaterDataSTAC() as stac:
    files = stac.collect("continuous", ids=sites, id_property="monitoring_location_id")
print(len(sites), "sites,", len(files.geojson["features"]), "items")
```

## Downloading a file

`Client.download()` fetches a file to disk, creating its folder, as
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

Pass `headers={"X-Api-Key": ...}` to send a key with a download.

Reading a Parquet file from `daily` or `continuous` needs `pyarrow` or another Parquet
reader. A `continuous` item has two files: `current`, about the last 150 days, and
`historical`, everything older. If both hold the same `time_series_id` and `time`, keep
the row with the latest `last_modified`.

## Searching several collections

The service also answers `/search`, which queries several collections at once.
`WaterDataSTAC` leaves it to [pystac-client](https://pystac-client.readthedocs.io/),
whose `search()` posts the query and follows each `next` link with the body it carries:

<!-- not run -->

```python
import os

from pystac_client import Client as STACClient

catalog = STACClient.open(
    "https://api.waterdata.usgs.gov/stac/v0/",
    headers={"X-Api-Key": os.environ["USGS_STAC_API_KEY"]},
)
search = catalog.search(
    collections=["daily", "continuous"],
    filter={"op": "=", "args": [{"property": "monitoring_location_id"}, "USGS-01646500"]},
    filter_lang="cql2-json",
)
for item in search.items():
    print(item.collection_id, item.id, list(item.assets))
```

The service takes only `cql2-json` in a `POST` body.
