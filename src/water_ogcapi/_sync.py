"""The blocking classes that `water_ogcapi` exports (D-08, D-46, D-48, D-70).

Each class has the name, constructor, and methods of its async twin in
`water_ogcapi.aio`. A method blocks until the async method of the same name returns, and
`iter_pages` returns an iterator that fetches one page per step. Every call runs on one
background event loop, so it works where a loop is already running, as in Jupyter.

```python
from water_ogcapi import NWIS

with NWIS() as nwis:
    result = nwis.collect("daily", params={"monitoring_location_id": "USGS-01646500"})
```
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import inspect
import os
import sys
import threading
from typing import TYPE_CHECKING, Any, Concatenate, Self

from water_ogcapi import _client, _services
from water_ogcapi._transport import Transport
from water_ogcapi.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterator

__all__ = ["NWIS", "Client", "FabricData", "GeoConnex", "OGCService"]

# Seconds between interrupt checks while a call waits; a call still returns once its result
# is ready. A blocked wait misses an interrupt that only sets the flag, as ipykernel's on
# Windows does.
_POLL = 0.1

_LOCK = threading.Lock()

_STOPPED = "the water-ogcapi event loop has stopped; restart the process"


@functools.cache
def _start_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="water-ogcapi", daemon=True)
    thread.start()
    return loop, thread


if sys.platform != "win32":

    def _reset_in_child() -> None:
        # The child has the parent's loop without its thread, so it starts its own.
        _LOCK.release()
        _start_loop.cache_clear()

    # Held across the fork, so no other thread's hold leaves the child's copy locked.
    os.register_at_fork(
        before=_LOCK.acquire, after_in_parent=_LOCK.release, after_in_child=_reset_in_child
    )


def _loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Return the background event loop and its thread, starting both on the first call."""
    # functools.cache alone can run _start_loop twice when two threads race.
    with _LOCK:
        return _start_loop()


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` on the background loop and return its result.

    An exception raised while waiting, such as the ``KeyboardInterrupt`` of a notebook
    interrupt, requests cancellation of ``coro`` and its request before it propagates;
    the cancellation finishes on the loop afterward.

    Raises
    ------
    RuntimeError
        If called on the background loop's thread, as from a logging handler or custom
        transport the loop runs, since the call would wait on itself. Also if the loop's
        thread has stopped, which a ``SystemExit`` raised in code the loop runs does.
    """
    loop, thread = _loop()
    if threading.current_thread() is thread:
        coro.close()
        msg = "a blocking call on the water-ogcapi event loop would wait on itself"
        raise RuntimeError(msg)
    # A dead loop never drains its queue, so a coroutine submitted to it is kept forever.
    if not thread.is_alive():
        coro.close()
        raise RuntimeError(_STOPPED)
    future = None
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        while not future.done():
            concurrent.futures.wait((future,), timeout=_POLL)
            if not future.done() and not thread.is_alive():
                raise RuntimeError(_STOPPED)
    except BaseException:
        if future is not None:
            future.cancel()
        raise
    return future.result()


async def _anext[T](pages: AsyncIterator[T]) -> T:
    return await anext(pages)


def _blocking[**P, T](
    method: Callable[Concatenate[Any, P], Coroutine[Any, Any, T]],
) -> Callable[Concatenate[OGCService | Client, P], T]:
    """Return a method that blocks on the async ``method`` of the wrapped service."""

    @functools.wraps(method)
    def call(self: OGCService | Client, /, *args: P.args, **kwargs: P.kwargs) -> T:
        # By name, so a service class that overrides the method gets its own.
        service = self._service  # pyright: ignore[reportPrivateUsage]
        return _run(getattr(service, method.__name__)(*args, **kwargs))

    return call


def _iterating[**P, T](
    method: Callable[Concatenate[Any, P], AsyncIterator[T]],
) -> Callable[Concatenate[OGCService, P], Iterator[T]]:
    """Return a method that steps through the async iterator ``method`` returns.

    Each step fetches one item, so an unsplit query sends no request between steps; the
    other pieces of a split query stay in flight (D-56). Closing or dropping the iterator
    drops the async one, and the loop's async generator finalizer closes it.
    """

    @functools.wraps(method)
    def call(self: OGCService, /, *args: P.args, **kwargs: P.kwargs) -> Iterator[T]:
        service = self._service  # pyright: ignore[reportPrivateUsage]
        items = getattr(service, method.__name__)(*args, **kwargs)
        while True:
            try:
                item = _run(_anext(items))
            except StopAsyncIteration:
                return
            yield item

    # wraps points inspect at the async method, whose return type names an async iterator.
    signature = inspect.signature(method)
    vars(call)["__signature__"] = signature.replace(
        return_annotation=str(signature.return_annotation).removeprefix("Async")
    )
    return call


class OGCService:
    """Blocking client for one OGC API Features service.

    Runs a `water_ogcapi.aio.OGCService` of its own on the background loop. Its methods
    `collections`, `queryables`, `schema`, `iter_pages`, `collect`, and `item` block on
    the async methods of the same name. A `Transport` passed here binds to that loop, so
    share it only among sync services (D-10); `close()` closes it, and a service sharing
    it reconnects on its next request. It pickles as `water_ogcapi.aio.OGCService` does
    (D-64).

    Parameters
    ----------
    base_url : str
        The service's landing page, as for `water_ogcapi.aio.OGCService`.
    transport : Transport, optional
        Sends every request, as for `water_ogcapi.aio.OGCService`.
    """

    _service: _client.OGCService
    _transport: Transport

    collections = _blocking(_client.OGCService.collections)
    queryables = _blocking(_client.OGCService.queryables)
    schema = _blocking(_client.OGCService.schema)
    iter_pages = _iterating(_client.OGCService.iter_pages)
    collect = _blocking(_client.OGCService.collect)
    item = _blocking(_client.OGCService.item)

    def __init__(self, base_url: str, *, transport: Transport | None = None) -> None:
        self._transport = Transport() if transport is None else transport
        self._service = _client.OGCService(base_url, transport=self._transport)

    @property
    def base_url(self) -> str:
        """The landing page URL, without a trailing slash."""
        return self._service.base_url

    def refresh(self) -> None:
        """Drop every cached metadata document, so the next call fetches it again."""
        self._service.refresh()

    def close(self) -> None:
        """Close the transport, whether this service created it or it was passed."""
        # A passed transport is bound to the background loop, where the caller cannot close it.
        _run(self._transport.aclose())

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class NWIS(OGCService):
    """Blocking client for the USGS Water Data OGC API, as `water_ogcapi.aio.NWIS`.

    Parameters
    ----------
    api_key : str, optional
        A USGS API key, as for `water_ogcapi.aio.NWIS`.
    transport : Transport, optional
        Sends every request, as for `OGCService`.

    Raises
    ------
    ValidationError
        If the key holds a character other than printable ASCII, or is not exactly
        ``str``.
    """

    def __init__(self, *, api_key: str | None = None, transport: Transport | None = None) -> None:
        self._transport = Transport() if transport is None else transport
        self._service = _services.NWIS(api_key=api_key, transport=self._transport)


class FabricData(OGCService):
    """Blocking client for the USGS FabricData service, as `water_ogcapi.aio.FabricData`.

    Parameters
    ----------
    transport : Transport, optional
        Sends every request, as for `OGCService`.
    """

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = Transport() if transport is None else transport
        self._service = _services.FabricData(transport=self._transport)


class GeoConnex(OGCService):
    """Blocking client for the GeoConnex reference features, as `water_ogcapi.aio.GeoConnex`.

    Parameters
    ----------
    transport : Transport, optional
        Sends every request, as for `OGCService`.
    """

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = Transport() if transport is None else transport
        self._service = _services.GeoConnex(transport=self._transport)


class Client:
    """Blocking HTTP client: one `Transport` run on the background loop (D-70).

    Its methods `request`, `get`, and `download` block on the `Transport` methods of the
    same name and take their arguments, so sync code sends any request and reuses one
    transport's connections across calls; `run` runs a coroutine that sends many at once
    (D-73). A `Transport` used this way binds to the background loop, so share it only
    with the blocking classes (D-10). The client pickles as its transport does (D-64).

    ```python
    from water_ogcapi import Client

    with Client() as client:
        page = client.get("https://api.water.usgs.gov/nldi/linked-data/comid/1722317")
        client.download("https://example.org/file.zip", "data/file.zip")
    ```

    Parameters
    ----------
    transport : Transport, optional
        Sends every request, a new default `Transport` when omitted. ``close()`` closes
        it, and a blocking service sharing it reconnects on its next request.
    """

    # _blocking calls the method of the same name on _service, here the transport.
    _service: Transport

    request = _blocking(Transport.request)
    get = _blocking(Transport.get)
    download = _blocking(Transport.download)

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._service = Transport() if transport is None else transport

    @property
    def transport(self) -> Transport:
        """The transport every call goes through."""
        return self._service

    def run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` on the background loop and return its result (D-73).

        The coroutine sends its requests through `transport`, or through another
        transport only blocking classes use, so one blocking call can await many
        requests. An interrupt requests cancellation of the coroutine and the tasks it
        awaits, then propagates; the cancellation finishes on the loop afterward.

        ```python
        async def fetch_all():
            return await asyncio.gather(*(client.transport.get(url) for url in urls))

        responses = client.run(fetch_all())
        ```

        Parameters
        ----------
        coro : coroutine
            A coroutine object, such as ``fetch_all()``. Call ``asyncio.gather()`` inside
            it, since the future it returns belongs to the loop that made it.

        Returns
        -------
        T
            What the coroutine returns.

        Raises
        ------
        ValidationError
            If ``coro`` is not a coroutine object: a coroutine function, a future, or a
            task.
        RuntimeError
            If called inside a coroutine that `run` or another blocking call runs, which
            would wait on itself, or once the loop's thread has stopped.
        """
        if not inspect.iscoroutine(coro):
            msg = f"run() takes a coroutine object, got {type(coro).__name__}"
            raise ValidationError(msg)
        return _run(coro)

    def close(self) -> None:
        """Close the transport, whether this client created it or it was passed."""
        _run(self._service.aclose())

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
