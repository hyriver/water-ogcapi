# Migrating from `pynhd.ogcapi`

`pynhd.ogcapi`, in pynhd 0.20.0, has three classes, `GeoConnex`, `FabricData`, and
`NWIS`, that query the same services and return GeoDataFrames. `water-ogcapi` has a
class for each service under the same name, returns the GeoJSON with the evidence of how
each page was fetched, and converts it to a GeoDataFrame through `to_arrow()`. The
`pynhd` code on this page is shown, not run.

## The calls side by side

A `pynhd` service is built for one collection, its `item`, and each method is one query
form. A `water-ogcapi` service covers every collection, and `collect()` takes the
collection and the query form as arguments:

| `pynhd.ogcapi`                               | `water-ogcapi`                                                |
| -------------------------------------------- | ------------------------------------------------------------- |
| `GeoConnex("hu02").bybox(bbox)`              | `GeoConnex().collect("hu02", bbox=bbox)`                      |
| `GeoConnex("hu02").byid("huc2", ["02"])`     | `GeoConnex().collect("hu02", ids=["02"], id_property="huc2")` |
| `GeoConnex("hu02").byfilter("huc2 <= '05'")` | `GeoConnex().collect("hu02", filter="huc2 <= '05'")`          |
| `GeoConnex("hu02").bycql(cql2_json)`         | `GeoConnex().collect("hu02", filter=cql2_json)`               |
| `GeoConnex("hu02").byitem("02")`             | `GeoConnex().item("hu02", "02")`                              |
| `GeoConnex("gages").bygeometry(polygon)`     | `GeoConnex().collect("gages", geometry=polygon)`, then filter |
| `skip_geometry=True`                         | `params={"skipGeometry": "true"}`                             |
| `max_nfeatures=1000`                         | `params={"limit": "1000"}`                                    |

Two regions by their HUC2 code:

<!-- not run -->

```python title="pynhd.ogcapi"
from pynhd.ogcapi import GeoConnex

regions = GeoConnex("hu02").byid("huc2", ["02", "05"])
```

```python title="water-ogcapi"
import geopandas as gpd

from water_ogcapi import GeoConnex

with GeoConnex() as gc:
    result = gc.collect("hu02", ids=["02", "05"], id_property="huc2")
regions = gpd.GeoDataFrame.from_arrow(result.to_arrow())
print(regions[["id", "name"]])
```

The NWIS monitoring locations in a box around Washington, DC:

<!-- not run -->

```python title="pynhd.ogcapi"
from pynhd.ogcapi import NWIS

sites = NWIS("monitoring-locations").bybox((-77.12, 38.80, -76.91, 38.99))
```

```python title="water-ogcapi"
from water_ogcapi import NWIS

DC = (-77.12, 38.80, -76.91, 38.99)

with NWIS() as nwis:
    result = nwis.collect("monitoring-locations", bbox=DC)
sites = gpd.GeoDataFrame.from_arrow(result.to_arrow())
print(len(sites), result.pagination.completeness)
```

In pynhd 0.20.0, building `NWIS` raises `ServiceError` ("The service doesn't have a
valid queryable link") against the current API: `pynhd` reads every collection's
queryables when built, and the API's `edr/daily` collection lists no queryables link.
`water-ogcapi` reads the queryables of the queried collection only.

## Geometry predicates

`bygeometry()` sends the bbox of the geometry, then keeps the features that meet its
`predicate` with geopandas. `geometry=` sends the same bbox and returns every feature
that intersects it, so the predicate becomes one line on the frame:

<!-- not run -->

```python title="pynhd.ogcapi"
from shapely import Polygon

area = Polygon([(-77.12, 38.80), (-76.91, 38.80), (-77.0, 38.99)])
gages = GeoConnex("gages").bygeometry(area, predicate="within")
```

```python title="water-ogcapi"
from shapely import Polygon

area = Polygon([(-77.12, 38.80), (-76.91, 38.80), (-77.0, 38.99)])
with GeoConnex() as gc:
    result = gc.collect("gages", geometry=area)
gages = gpd.GeoDataFrame.from_arrow(result.to_arrow())
inside = gages[gages.within(area)]
print(len(gages), "in the bbox,", len(inside), "within the triangle")
```

`bygeometry()` also takes a `crs` and reprojects the geometry to EPSG:4326 first.
`geometry=` takes CRS84 coordinates, longitude first, so reproject a shape before
passing it.

## What behaves differently

- **Construction.** Building a `pynhd` service fetches the service's collection list and
    every collection's queryables. A `water-ogcapi` service sends no request until a
    query, which fetches only the metadata it checks and caches it for an hour.
- **Pagination.** `pynhd` requests pages by `offset`, sorted by the id property, in
    parallel once the first page reports `numberMatched`. `water-ogcapi` follows each
    page's `next` link, which the server builds, and reports whether the traversal
    reached the end in `pagination.completeness`. The
    [design notes](../design.md#l-01-offset-pagination-silently-skips-or-truncates) say
    why offsets can skip or truncate features.
- **No matches.** `pynhd` raises `ZeroMatchedError`. `collect()` returns a result whose
    `geojson["features"]` is empty.
- **The API key.** `pynhd`'s `NWIS` and `FabricData` read `USGS_API_KEY`, and its
    `FabricData` sends the key in an `api_key` header. `water-ogcapi` sends the key only
    to NWIS, in the `X-Api-Key` header.
- **Development endpoints.** `pynhd` takes `dev=True`. The `water-ogcapi` service
    classes have fixed base URLs, and `OGCService(base_url)` queries any other OGC API
    Features service, with no key.
- **Errors.** `pynhd` raises its own `ServiceError` and `InputValueError`.
    `water-ogcapi` raises a `ValidationError` before any page request for an unknown
    collection or property, with the valid names in the message, and a `ServiceError`
    with the status and URL for a failed request. The [Errors](errors.md) page lists
    them.

A query that matches nothing:

```python
with NWIS() as nwis:
    empty = nwis.collect("monitoring-locations", bbox=DC, params={"site_type_code": "XX"})
print(len(empty.geojson["features"]), empty.pagination.completeness)
```
