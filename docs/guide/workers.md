# Dask and multiprocessing workers

A service pickles, whether or not it has sent requests, so it can go to worker processes
as a task argument. Each worker rebuilds what belongs to its own process: the HTTP
client, the quota pacing, and the metadata cache.

- Pass the service as a task argument, to `Pool.map`, `ProcessPoolExecutor.map`, or a
    Dask task, where it is pickled.
- Set `USGS_API_KEY` in each worker's environment. A pickled `NWIS` leaves its key out,
    and the worker reads `USGS_API_KEY` when it loads the service.
- Expect each worker to fetch its own metadata the first time it checks a query, and to
    pace its requests on its own. Several workers share one hourly quota but not the
    pacing, so keep the worker count small.

## multiprocessing

The function a worker runs must be importable, so define it at module level and start
the pool under `if __name__ == "__main__":`. The parent runs one query first, so the
service it sends holds a client, cached metadata, and pacing readings:

```python
from concurrent.futures import ProcessPoolExecutor
from itertools import repeat

from water_ogcapi import NWIS

QUERY = {"parameter_code": "00060", "statistic_id": "00003", "datetime": "2025-01-01/2025-12-31"}
SITES = ["USGS-01646500", "USGS-01594440", "USGS-01638500"]


def days(nwis: NWIS, site: str) -> int:
    """Count the daily mean discharge values of one site in 2025."""
    result = nwis.collect("daily", params={**QUERY, "monitoring_location_id": site})
    return len(result.geojson["features"])


if __name__ == "__main__":
    with NWIS() as nwis, ProcessPoolExecutor(max_workers=2) as pool:
        print(SITES[0], days(nwis, SITES[0]), "days, in this process")
        for site, count in zip(SITES, pool.map(days, repeat(nwis), SITES), strict=True):
            print(site, count, "days")
```

A `multiprocessing.Process` given a used service under the `fork` start method, the
default on Linux before Python 3.14, inherits it without a pickle and raises
`RuntimeError` on its first request. Pass the service through a pool or executor, as
above, or build it in the worker.

## Dask

A Dask task takes the service the same way. With `dask.distributed`, each worker process
loads its own copy:

```python
from dask.distributed import Client, LocalCluster

if __name__ == "__main__":
    with LocalCluster(n_workers=2, threads_per_worker=1) as cluster, Client(cluster) as client:
        nwis = NWIS()
        print(SITES[0], days(nwis, SITES[0]), "days, in this process")
        futures = client.map(days, [nwis] * len(SITES), SITES)
        for site, count in zip(SITES, client.gather(futures), strict=True):
            print(site, count, "days")
        nwis.close()
```

A worker on another machine needs `USGS_API_KEY` in its own environment, set where the
cluster starts its workers.
