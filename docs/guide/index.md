# User guide

Each page covers one part of the package, and its examples run against the live
services. Read them in this order:

1. [Installation and the API key](setup.md): installing the package and its `arrow`
    extra, and how `NWIS` sends a USGS API key.
1. [Queries and results](queries.md): collections, the query forms, `QueryResult` and
    `Page`, and Arrow tables.
1. [Pagination and large queries](pagination.md): `iter_pages()` and `collect()`,
    splitting an NWIS query, converting page by page, and resuming a stopped query.
1. [Errors](errors.md): what each exception means and what it carries.
1. [Blocking and async APIs](sync-and-async.md): the blocking classes and their async
    twins in `water_ogcapi.aio`.
1. [Dask and multiprocessing workers](workers.md): sending a service to worker
    processes.

For packages that use the same services,
[`water-ogcapi` as the transport for `dataretrieval`](dataretrieval.md) shows where its
Water Data getters could send their requests through this package.

The [example notebooks](../examples/index.md) walk through each service, and the
[API reference](../api.md) lists every parameter.
