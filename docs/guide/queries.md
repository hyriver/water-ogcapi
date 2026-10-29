# Queries and results

## Services and collections

Three classes cover the three services: `NWIS` for the USGS Water Data API, `FabricData`
for the USGS FabricData service, and `GeoConnex` for the GeoConnex reference features.
Building one sends no request. Each holds its HTTP connections until `close()`, which a
`with` block calls:

```python
from water_ogcapi import NWIS

nwis = NWIS()
collections = nwis.collections()
print([c.id for c in collections.collections or ()][:5])
queryables = nwis.queryables("monitoring-locations")
print(sorted(queryables.properties or {})[:5])
```

`collections()` and `queryables()` return typed documents, `Collections` and `Schema`,
and each service caches them for an hour. `refresh()` drops the cache.

## Query forms

`collect()` and `iter_pages()` take the query as keyword arguments, and each form can
combine with `params`:

| Keyword    | Sends                                                                   |
| ---------- | ----------------------------------------------------------------------- |
| `params`   | Query parameters as given, such as `limit`, `datetime`, or a property   |
| `bbox`     | `(west, south, east, north)` in CRS84                                   |
| `geometry` | The bbox of a GeoJSON geometry or a shapely shape, in CRS84             |
| `filter`   | A CQL2 filter: a `str` as CQL2-Text, a mapping as CQL2-JSON in a `POST` |
| `ids`      | Values of `id_property` to match, as a CQL2-JSON `in` filter            |

A property in `params` matches its value:

```python
DC = (-77.12, 38.80, -76.91, 38.99)

streams = nwis.collect("monitoring-locations", bbox=DC, params={"site_type_code": "ST"})
print(len(streams.geojson["features"]), "stream sites")
```

CQL2-Text compares, combines, and tests geometry:

```python
large = nwis.collect(
    "monitoring-locations",
    bbox=DC,
    filter="site_type_code = 'ST' AND drainage_area > 100",
)
print("stream sites draining over 100 square miles:", len(large.geojson["features"]))
```

`ids` matches a list of values of one property. Here the sites' daily mean discharge for
one week:

```python
week = nwis.collect(
    "daily",
    ids=["USGS-01646500", "USGS-01594440"],
    id_property="monitoring_location_id",
    params={
        "parameter_code": "00060",
        "statistic_id": "00003",
        "datetime": "2025-06-01/2025-06-07",
    },
)
print(len(week.geojson["features"]), "daily values")
```

`geometry` sends the bbox of the shape, so the result holds every feature that
intersects that bbox. Filter the result with the shape for exact containment.

`item()` fetches one feature by its id and returns a `Page` whose `body` is the Feature:

```python
site = nwis.item("monitoring-locations", "USGS-01646500")
print(site.body["properties"]["monitoring_location_name"])
```

## Checked names

Before the first page request, a query checks its collection and each name in `params`
against the service's metadata. The OGC API server behind these services ignores a
parameter it does not know, so an unchecked typo returns the whole collection:

```python
from water_ogcapi.exceptions import ValidationError

try:
    nwis.collect("monitoring-locations", params={"site_typ_code": "ST"})
except ValidationError as err:
    print(str(err)[:120])
```

`validate=False` skips the check, for a name the server accepts but its queryables leave
out.

## Results

`collect()` returns a `QueryResult`:

- `geojson`: the FeatureCollection of every page, nothing coerced.
- `pagination`: `number_matched` as the server reported it, `number_returned`,
    `page_count`, `completeness` (`complete`, `limited`, or `unknown`), and the `checks`
    behind it.
- `crs`: the coordinate reference system of the geometries and the evidence for it, the
    `Content-Crs` header or the CRS84 default.
- `pages`: one `Page` per response, with its URL as sent, status, kept headers, links,
    and feature counts.

```python
print(streams.pagination.completeness, streams.pagination.checks)
print(streams.crs.effective, streams.crs.source)
print(streams.pages[0].url)
```

`complete` means the traversal reached a page without a `next` link and every check that
could run passed. A `Page` masks credential query parameters in its URL and `params`,
and `NWIS` sends the key only in a header. The body is kept as the server sent it.

## Arrow tables

With the `arrow` extra, `QueryResult.to_arrow()` returns a `pyarrow.Table` with one row
per feature and a column per property. An `id` column comes first when any feature has
an id, and a GeoArrow `geometry` column that carries the CRS comes last when any feature
has a geometry. A result without features gives a table with no columns. geopandas,
polars, and pandas read it:

```python
import geopandas as gpd
import polars as pl

table = streams.to_arrow()
sites = gpd.GeoDataFrame.from_arrow(table)
print(sites.crs.to_string(), len(sites))
print(pl.from_arrow(table).select("id", "monitoring_location_name").head(3))
print(table.drop_columns("geometry").to_pandas().shape)
nwis.close()
```

A large result holds every feature in Python beside the table, so
[Pagination and large queries](pagination.md#converting-page-by-page) converts one page
at a time.
