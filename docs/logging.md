# Logging

`water-ogcapi` writes its log records to the `water_ogcapi` logger. That logger has its
own handler, which prints WARNING and above to stderr, and it does not propagate to the
root logger. A package that configures the root logger therefore neither receives these
records nor changes how they print, and a notebook shows each record once.
[D-20](design.md#d-20-package-logger-isolated-from-the-root-logger) records why.

## What gets logged

| Level   | Record                                                                |
| ------- | --------------------------------------------------------------------- |
| WARNING | A 429 response that the transport waits out and retries               |
| INFO    | A retry after a connection failure, a timeout, or a 5xx response      |
| DEBUG   | Each completed request, with its final URL, status, and attempt count |

A request that fails, on its first attempt or after its last retry, raises
`ServiceError` or `RateLimitError` without a log record, so log the exception where your
code handles it.

No record carries request headers. The API key travels only in the `X-Api-Key` header,
so it never reaches a log.

## Scripts and notebooks

`configure_logger()` sets where records go:

```python
from water_ogcapi import configure_logger

configure_logger(level="INFO")  # adds retries to the console
configure_logger(verbose=True)  # adds every request
configure_logger(file="logs/run.log")  # console at WARNING, file at DEBUG
configure_logger(file="logs/run.log", file_only=True, file_mode="w")
```

Each call replaces the previous configuration, and an argument left out returns to its
default. Calling `configure_logger(level="DEBUG")` after
`configure_logger(file="run.log")` turns file logging off, so pass `file=` again to keep
it. `configure_logger()` with no arguments restores the default. Log files are written
as UTF-8 on every platform.

Configure logging before starting queries. A record logged while a call replaces the
handlers can still land in the previous log file.

## Applications with their own logging setup

AWS Lambda, web frameworks, and JSON log pipelines attach their handlers to the root
logger, and `water_ogcapi` does not propagate there. In Lambda, the default stderr
output still reaches CloudWatch, as plain lines that lack the request ID and the
formatting Lambda's own handler adds.

To send records through the application's handlers, remove the package's stderr handler,
turn propagation on, and set a level on the `water_ogcapi` logger itself:

```python
import logging

log = logging.getLogger("water_ogcapi")
log.handlers.clear()  # without this, every record also prints to stderr
log.propagate = True
log.setLevel(logging.INFO)
```

Set the level on `water_ogcapi` because the root logger's level does not filter records
that propagate up from a child logger. Without `setLevel`, the root handlers receive
every DEBUG record. In Lambda, run this at module level, outside the handler function,
so it runs once per execution environment. Do not call `configure_logger()` afterwards,
since it adds the stderr handler back.

To keep the stderr output and also send records elsewhere, add your handler to the
`water_ogcapi` logger. `configure_logger()` leaves handlers it did not create in place.

## Tests

pytest's `caplog` captures through the root logger, so it sees no `water_ogcapi` records
until its handler is attached to that logger:

```python
import logging


def test_rate_limit_is_logged(caplog):
    log = logging.getLogger("water_ogcapi")
    log.addHandler(caplog.handler)
    try:
        ...  # run the query
    finally:
        log.removeHandler(caplog.handler)
    assert any(record.levelname == "WARNING" for record in caplog.records)
```

## The httpx2 logger

The HTTP library logs to its own `httpx2` logger, which propagates normally. At INFO it
writes one line per request:

```text
HTTP Request: GET https://api.waterdata.usgs.gov/ogcapi/v0/collections?f=json "HTTP/1.1 200 OK"
```

These lines show up wherever the root logger is at INFO or below. The URL never holds
the key ([D-18](design.md#d-18-the-api-key-travels-only-in-a-header)). To silence them,
raise that logger's level:

```python
logging.getLogger("httpx2").setLevel(logging.WARNING)
```
