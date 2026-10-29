# Pagination and large queries

## Pages and `next` links

A server answers a query one page at a time, and each page but the last links to the
next. `iter_pages()` yields one `Page` per response as it arrives, and `collect()` reads
every page into one `QueryResult`. Both follow the `next` links to the end, and only a
page without one ends the traversal.

An NWIS page asks for up to 10,000 features unless `params` set a `limit`, which NWIS
caps at 50,000. FabricData pages ask for up to 1,000 and GeoConnex pages 500 by default.
The last page, and any page of a small query, holds fewer.

```python
from water_ogcapi import NWIS

DC = (-77.12, 38.80, -76.91, 38.99)

nwis = NWIS()
for page in nwis.iter_pages("monitoring-locations", bbox=DC, params={"limit": "200"}):
    print(page.feature_count, "features")
```

A page from `iter_pages()` holds the decoded document in `body` and its bytes in
`content`. `collect()` keeps neither, unless `keep_pages=True` keeps the bytes.

## Splitting an NWIS query

NWIS can split one query into pieces that run concurrently, as the USGS efficiency
guidance recommends for long records and long site lists. `split_datetime="year"` or
`"month"` splits a closed `datetime` interval at calendar boundaries in UTC, and
`sites_per_piece` splits a site list. The transport runs up to `max_concurrent` pieces
at once, 4 by default, and pages arrive in the order they finish:

```python
daily = nwis.collect(
    "daily",
    params={
        "monitoring_location_id": "USGS-01646500",
        "parameter_code": "00060",
        "statistic_id": "00003",
        "datetime": "2021-01-01/2025-12-31",
    },
    split_datetime="year",
)
print(len(daily.geojson["features"]), "days in", daily.pagination.page_count, "pages")
```

Each piece costs at least one request, so size pieces near one page: a year of daily
values for one site is 365 features, well under one page.

## Converting page by page

`QueryResult.to_arrow()` needs every feature in Python beside the table it builds.
`Page.to_arrow()` converts one page, so a loop over `iter_pages()` holds a few pages at
a time: the page it last yielded and the next one, or for a split query up to
`max_concurrent` pages in flight plus the last yielded. Each page types its columns on
its own, and `promote_options="permissive"` joins pages where a column is missing or
null throughout. A column whose types cannot combine, such as an integer on one page and
a string on another, still raises:

```python
import pyarrow as pa

tables = [
    page.to_arrow()
    for page in nwis.iter_pages(
        "daily",
        params={
            "monitoring_location_id": "USGS-01646500",
            "parameter_code": "00060",
            "statistic_id": "00003",
            "datetime": "2021-01-01/2025-12-31",
        },
        split_datetime="year",
    )
]
first = tables[0].schema.field("geometry").metadata
assert all(t.schema.field("geometry").metadata == first for t in tables), "CRS differs"
table = pa.concat_tables(tables, promote_options="permissive")
print(table.num_rows, "rows from", len(tables), "pages")
```

The join keeps the first table's `geometry` field metadata, which names the CRS, and
raises nothing when another table's names a different one. Each page reads its CRS from
its own `Content-Crs` header, so the example compares the metadata before joining. To
keep memory flat, write each table to its own file, such as one Parquet file per page.

## Resuming a stopped query

A query that stops on a failure can continue where it stopped. Each page from
`iter_pages()`, except the last, carries a `checkpoint` of the requests still to send,
and `iter_pages(resume=checkpoint)` sends only those, never one whose page the caller
already holds. A split query's checkpoint also lists the pieces still in flight, so a
piece whose page had arrived but was not yet yielded is sent again. A `Checkpoint`
pickles, so a stopped process can save it and another resume from it:

```python
import pickle

pages = nwis.iter_pages("monitoring-locations", bbox=DC, params={"limit": "200"})
first = next(pages)
pages.close()
saved = pickle.dumps(first.checkpoint)

rest = list(nwis.iter_pages("monitoring-locations", resume=pickle.loads(saved)))
print(first.feature_count + sum(page.feature_count for page in rest), "features in all")
nwis.close()
```

`collect()` raises `PartialResultsError` when a page fails after others arrived. Its
`result` holds the pages that arrived and the checkpoint, and `collect(resume=result)`
returns the whole result. The [Errors](errors.md) page shows both, and the
[cache, quota, and resume notebook](../examples/cache_quota_resume.md) walks through a
failure.
