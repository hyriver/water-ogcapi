"""Tests for the sync API."""

from __future__ import annotations

import _thread
import asyncio
import inspect
import multiprocessing
import sys
import threading
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from water_ogcapi import NWIS, FabricData, GeoConnex, OGCService, Page, QueryResult, Transport, sync
from water_ogcapi.exceptions import PaginationError, ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"
FIRST = {
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "geometry": None, "properties": {"n": 1}}],
    "links": [{"rel": "next", "href": f"{BASE}/collections/c/items?offset=1"}],
}
LAST = {
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "geometry": None, "properties": {"n": 2}}],
    "links": [],
}


def serve() -> tuple[Transport, list[httpx2.Request]]:
    """A transport serving a two-page collection ``c``, recording each page request.

    The collection index the query check reads is served without a record.
    """
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/collections"):
            return httpx2.Response(200, json={"collections": [{"id": "c"}]})
        seen.append(request)
        return httpx2.Response(200, json=LAST if "offset" in str(request.url) else FIRST)

    return Transport(transport=httpx2.MockTransport(handler), max_retries=0), seen


def settled_tasks() -> int:
    """Tasks left on the background loop, besides this check, once they settle."""

    async def count() -> int:
        for _ in range(100):
            if len(asyncio.all_tasks()) == 1:
                break
            await asyncio.sleep(0.01)
        return len(asyncio.all_tasks()) - 1

    return sync._run(count())


def test_a_query_works_inside_a_running_loop() -> None:
    transport, _ = serve()

    async def notebook_cell() -> QueryResult:
        # A notebook cell runs while the kernel's loop is running.
        with sync.OGCService(BASE, transport=transport) as service:
            return service.collect("c")

    result = asyncio.run(notebook_cell())
    assert [f["properties"]["n"] for f in result.geojson["features"]] == [1, 2]
    assert result.pagination.completeness == "complete"


def test_calls_reuse_one_transport() -> None:
    transport, seen = serve()
    service = sync.OGCService(BASE, transport=transport)
    service.collect("c")
    client = transport._client
    assert client is not None
    assert transport._loop is sync._loop()
    service.collect("c")
    assert transport._client is client
    assert len(seen) == 4


def test_an_async_service_still_works_after_a_sync_call() -> None:
    sync_transport, _ = serve()
    async_transport, _ = serve()
    blocking = sync.OGCService(BASE, transport=sync_transport)
    service = OGCService(BASE, transport=async_transport)

    async def mixed() -> list[int]:
        counts = [len((await service.collect("c")).geojson["features"])]
        counts.append(len(blocking.collect("c").geojson["features"]))
        counts.append(len((await service.collect("c")).geojson["features"]))
        return counts

    assert asyncio.run(mixed()) == [2, 2, 2]


def test_each_step_sends_one_request() -> None:
    transport, seen = serve()
    pages = sync.OGCService(BASE, transport=transport).iter_pages("c")
    assert not seen
    assert next(pages).feature_count == 1
    assert len(seen) == 1
    assert [page.feature_count for page in pages] == [1]
    assert len(seen) == 2


def test_an_abandoned_iterator_leaves_no_request_running() -> None:
    transport, seen = serve()
    closed = threading.Event()

    class Recording(OGCService):
        async def iter_pages(self, collection: str, **kwargs: Any) -> AsyncIterator[Page]:
            try:
                async for page in super().iter_pages(collection, **kwargs):
                    yield page
            finally:
                closed.set()

    service = sync.OGCService(BASE, transport=transport)
    service._service = Recording(BASE, transport=transport)
    pages = service.iter_pages("c")
    next(pages)
    del pages
    assert closed.wait(5)
    assert settled_tasks() == 0
    assert len(seen) == 1


def test_an_interrupt_cancels_the_request() -> None:
    started = threading.Event()
    cancelled = threading.Event()

    async def handler(_: httpx2.Request) -> httpx2.Response:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return httpx2.Response(200, json=LAST)

    def interrupt() -> None:
        # Sets the interrupt flag without a signal, as ipykernel does on Windows.
        if started.wait(5):
            _thread.interrupt_main()

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    service = sync.OGCService(BASE, transport=transport)
    threading.Thread(target=interrupt, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        service.collect("c")
    assert cancelled.wait(5)
    assert settled_tasks() == 0


def test_errors_propagate_with_their_type() -> None:
    transport = Transport(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(404)), max_retries=0
    )
    service = sync.OGCService(BASE, transport=transport)
    with pytest.raises(PaginationError):
        service.collect("c", validate=False)
    with pytest.raises(ValidationError, match="would address another path"):
        next(service.iter_pages(".."))


PAIRS = [
    (sync.OGCService, OGCService),
    (sync.NWIS, NWIS),
    (sync.FabricData, FabricData),
    (sync.GeoConnex, GeoConnex),
]
TWINS = {"aclose": "close"}


def async_methods(service: type[OGCService]) -> list[str]:
    """The public coroutine and async generator methods of an async service class."""
    return sorted(
        name
        for name, value in inspect.getmembers(service)
        if not name.startswith("_")
        and (inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value))
    )


@pytest.mark.parametrize(("blocking", "service"), PAIRS)
def test_every_async_method_has_a_blocking_twin(
    blocking: type[sync.OGCService], service: type[OGCService]
) -> None:
    missing = [
        name for name in async_methods(service) if not hasattr(blocking, TWINS.get(name, name))
    ]
    assert not missing


@pytest.mark.parametrize("name", [n for n in async_methods(OGCService) if n not in TWINS])
def test_methods_copy_the_async_parameters_and_docstring(name: str) -> None:
    method, original = getattr(sync.OGCService, name), getattr(OGCService, name)
    assert inspect.signature(method).parameters == inspect.signature(original).parameters
    assert method.__doc__ == original.__doc__


def test_iter_pages_names_the_iterator_it_returns() -> None:
    assert inspect.signature(sync.OGCService.iter_pages).return_annotation == "Iterator[Page]"


@pytest.mark.parametrize(
    ("blocking", "service"),
    [(sync.NWIS, NWIS), (sync.FabricData, FabricData), (sync.GeoConnex, GeoConnex)],
)
def test_each_class_queries_its_own_service(
    blocking: type[sync.NWIS | sync.FabricData | sync.GeoConnex],
    service: type[NWIS | FabricData | GeoConnex],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USGS_API_KEY", "k")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"collections": []})

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    blocking(transport=transport).collections()
    assert service.profile is not None
    assert str(seen[0].url).startswith(service.profile.base_url)
    # The key goes only to NWIS (D-13).
    assert seen[0].headers.get("X-Api-Key") == ("k" if service is NWIS else None)


def test_close_closes_a_passed_transport() -> None:
    transport, _ = serve()
    with sync.OGCService(BASE, transport=transport) as service:
        service.collect("c")
        assert transport._client is not None
    assert transport._client is None


def test_a_service_sharing_a_closed_transport_reconnects() -> None:
    transport, seen = serve()
    first, second = (sync.OGCService(BASE, transport=transport) for _ in range(2))
    first.collect("c")
    first.close()
    assert len(second.collect("c").geojson["features"]) == 2
    assert len(seen) == 4


def collect_in_child() -> None:
    transport, _ = serve()
    sync.OGCService(BASE, transport=transport).collect("c")


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_forked_child_starts_its_own_loop() -> None:
    sync._loop()
    held = threading.Event()

    def hold_the_lock() -> None:
        with sync._LOCK:
            held.set()
            threading.Event().wait(0.2)

    threading.Thread(target=hold_the_lock, daemon=True).start()
    held.wait(5)
    child = multiprocessing.get_context("fork").Process(target=collect_in_child)
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
    assert child.exitcode == 0
