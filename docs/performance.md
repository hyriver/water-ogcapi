# Performance

`scripts/bench.py` measures the five benchmarks that
[D-14](design.md#d-14-testing-strategy) plans. It runs offline against
`httpx2.MockTransport`: each items page repeats one NWIS daily-value feature from
`tests/fixtures/` (about 470 bytes of JSON) under distinct ids, and the mock builds each
page before any timer or trace starts and hands every response its own copy of the
bytes. The numbers therefore measure the client and leave out the network. Reproduce
them with:

```bash
pixi r -e dev bench
```

The run below used httpx2 2.16.0 and Python 3.15.1 on an Apple M2 Max under macOS. Timed
rows give the median and the range of 20 runs, or 5 for the 429 rows.

## Cold start

Each run starts a fresh interpreter, imports `water_ogcapi`, builds a blocking `NWIS`,
and takes the first page of a `daily` query with the default `validate=True`.

| Step                              | Median (range)   |
| --------------------------------- | ---------------- |
| `import water_ogcapi`             | 67 ms (65 to 78) |
| Construction to first feature     | 4 ms (4 to 5)    |
| TLS context, which the mock skips | 21 ms (20 to 40) |

A real first request also builds a TLS context, once per `Transport`, so the client's
share of a cold start is about 0.1 s before any network time. These rows move more
between sessions than within one: across six runs on this machine in one day, the import
median ranged from 67 to 197 ms and the TLS median from 21 to 111 ms, so read them as an
order of magnitude.

## Round trips for a one-page result

| Query                             | Requests |
| --------------------------------- | -------- |
| First query, `validate=True`      | 3        |
| Same query again, metadata cached | 1        |
| `validate=False`                  | 1        |

The check fetches the collection index and the collection's queryables
([D-47](design.md#d-47-queries-are-checked-against-cached-metadata-before-the-first-page-request)),
and the cache keeps both for an hour
([D-07](design.md#d-07-in-memory-success-only-metadata-cache)).

## Requests per 100,000 features

| Service    | Page size | Pages | Requests |
| ---------- | --------- | ----- | -------- |
| NWIS       | 10,000    | 10    | 12       |
| FabricData | 1,000     | 100   | 102      |
| GeoConnex  | 500       | 200   | 202      |

Each service uses its profile's default page size
([D-41](design.md#d-41-service-profiles-set-the-page-sizes)), and the two extra requests
are the metadata check. The governor's waits move only when requests start, so this run
skips them. GeoConnex sends no rate-limit headers, so its governor spaces requests one
second apart
([D-49](design.md#d-49-the-governor-paces-every-attempt-over-the-last-eighth-of-the-quota)),
and its 202 requests take at least 201 seconds. NWIS and FabricData with quota to spare
add no wait.

## Peak memory

`tracemalloc` traces the Python heap while `collect(validate=False)` fetches pages of
10,000 features. A request before the trace starts builds the HTTP client, so held, what
the trace still holds when `collect` returns, is nearly all the `QueryResult`.

| Features | `keep_pages` | Peak MiB | Held MiB | Peak KiB per feature |
| -------- | ------------ | -------- | -------- | -------------------- |
| 1,000    | False        | 2.2      | 1.3      | 2.27                 |
| 1,000    | True         | 2.2      | 1.8      | 2.27                 |
| 10,000   | False        | 22.3     | 13.6     | 2.28                 |
| 10,000   | True         | 22.3     | 18.0     | 2.28                 |
| 100,000  | False        | 151.9    | 134.8    | 1.56                 |
| 100,000  | True         | 191.6    | 178.9    | 1.96                 |

Memory grows linearly with the result, at about 1.4 KiB held per feature of this shape.
`keep_pages=True` adds each page's bytes, about 0.45 KiB per feature. Peak per feature
drops at 100,000 because one page in flight is a tenth of the result there and the whole
result at 10,000 or fewer.

## Recovery from a 429

A 5,000-feature query in pages of 1,000, where the third page first gets a 429 with
`Retry-After: 1` and `X-RateLimit-Remaining: 0`.

| Run           | Requests | Median (range)           | Result   |
| ------------- | -------- | ------------------------ | -------- |
| No 429        | 7        | 0.013 s (0.013 to 0.014) | complete |
| 429 on page 3 | 8        | 1.035 s (1.033 to 1.036) | complete |

The transport waits the second the server asked for and retries once, and the governor
holds later starts for the same second, so the 429 costs one request and the wait. Every
run returned all 5,000 features with no id repeated.

## Decoding

[Q-09](design.md#q-09-orjson-for-decoding-large-responses) asked whether orjson should
decode pages. `--fetch` saves three real pages per service at the profile's page size,
NWIS `continuous` for a year at the Potomac gage and FabricData `gagesii`, and records
how long `iter_pages` took to return each, its own `json.loads` included. It sends six
requests through the services, whose checks keep the key on NWIS's origin, and refuses
to write a page that holds the key. `--decode` times `json.loads` and `orjson.loads` on
each page (median of 20) and measures how much each grows a fresh process's peak RSS.
Save the pages outside the repository:

```bash
pixi r -e dev python scripts/bench.py --fetch /tmp/water-ogcapi-pages
pixi r -e dev python scripts/bench.py --decode /tmp/water-ogcapi-pages
```

Share is the `json.loads` time over the page's seconds, and saves is the difference
between the two decoders over the same seconds. A page's seconds include any wait the
governor added, which can only lower the share.

| Page         | MiB | Features | Page s | `json` ms | `orjson` ms | `json` share | `orjson` saves | `json` RSS MiB | `orjson` RSS MiB |
| ------------ | --- | -------- | ------ | --------- | ----------- | ------------ | -------------- | -------------- | ---------------- |
| nwis-1       | 6.8 | 10,000   | 3.64   | 19        | 9           | 0.5%         | 0.3%           | 23.9           | 31.0             |
| nwis-2       | 6.8 | 10,000   | 2.30   | 19        | 9           | 0.8%         | 0.5%           | 23.8           | 30.9             |
| nwis-3       | 6.8 | 10,000   | 1.54   | 19        | 9           | 1.3%         | 0.7%           | 23.6           | 31.0             |
| fabricdata-1 | 0.9 | 1,000    | 0.68   | 2         | 1           | 0.4%         | 0.2%           | 2.7            | 3.6              |
| fabricdata-2 | 0.9 | 1,000    | 0.50   | 2         | 1           | 0.5%         | 0.3%           | 2.6            | 3.6              |
| fabricdata-3 | 0.9 | 1,000    | 1.00   | 2         | 1           | 0.2%         | 0.2%           | 2.7            | 3.6              |

On these six pages orjson decoded twice as fast and grew peak RSS more, and every page
took at least 70 times as long to come back as `json.loads` took to decode it, so pages
keep `json.loads` ([D-61](design.md#d-61-pages-decode-with-jsonloads)).
