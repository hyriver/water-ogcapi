"""Tests for the blocking classes the package root exports (D-46, D-48)."""

from __future__ import annotations

import _thread
import asyncio
import importlib
import inspect
import multiprocessing
import pickle
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

import water_ogcapi
from water_ogcapi import Page, QueryResult, Transport, _sync, aio
from water_ogcapi.aio import NWIS, FabricData, GeoConnex, OGCService
from water_ogcapi.exceptions import PaginationError, ServiceError, ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

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

    return _sync._run(count())


def test_a_query_works_inside_a_running_loop() -> None:
    transport, _ = serve()

    async def notebook_cell() -> QueryResult:
        # A notebook cell runs while the kernel's loop is running.
        with water_ogcapi.OGCService(BASE, transport=transport) as service:
            return service.collect("c")

    result = asyncio.run(notebook_cell())
    assert [f["properties"]["n"] for f in result.geojson["features"]] == [1, 2]
    assert result.pagination.completeness == "complete"


def test_calls_reuse_one_transport() -> None:
    transport, seen = serve()
    service = water_ogcapi.OGCService(BASE, transport=transport)
    service.collect("c")
    client = transport._client
    assert client is not None
    assert transport._loop is _sync._loop()[0]
    service.collect("c")
    assert transport._client is client
    assert len(seen) == 4


def test_an_async_service_still_works_after_a_sync_call() -> None:
    sync_transport, _ = serve()
    async_transport, _ = serve()
    blocking = water_ogcapi.OGCService(BASE, transport=sync_transport)
    service = OGCService(BASE, transport=async_transport)

    async def mixed() -> list[int]:
        counts = [len((await service.collect("c")).geojson["features"])]
        counts.append(len(blocking.collect("c").geojson["features"]))
        counts.append(len((await service.collect("c")).geojson["features"]))
        return counts

    assert asyncio.run(mixed()) == [2, 2, 2]


def test_each_step_sends_one_request() -> None:
    transport, seen = serve()
    pages = water_ogcapi.OGCService(BASE, transport=transport).iter_pages("c")
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

    service = water_ogcapi.OGCService(BASE, transport=transport)
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
    service = water_ogcapi.OGCService(BASE, transport=transport)
    threading.Thread(target=interrupt, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        service.collect("c")
    assert cancelled.wait(5)
    assert settled_tasks() == 0


def test_errors_propagate_with_their_type() -> None:
    transport = Transport(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(404)), max_retries=0
    )
    service = water_ogcapi.OGCService(BASE, transport=transport)
    with pytest.raises(PaginationError):
        service.collect("c", validate=False)
    with pytest.raises(ValidationError, match="would address another path"):
        next(service.iter_pages(".."))


@pytest.mark.parametrize("name", ["OGCService", "NWIS", "FabricData", "GeoConnex"])
def test_the_root_classes_block_and_the_aio_classes_await(name: str) -> None:
    assert not inspect.iscoroutinefunction(getattr(water_ogcapi, name).collect)
    assert inspect.iscoroutinefunction(getattr(aio, name).collect)


def test_a_plain_import_binds_aio_and_exceptions() -> None:
    # A fresh interpreter: this one imported both submodules already.
    code = "import water_ogcapi as w; w.aio.NWIS; w.exceptions.ServiceError"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_the_sync_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("water_ogcapi.sync")


PAIRS = [
    (water_ogcapi.OGCService, OGCService),
    (water_ogcapi.NWIS, NWIS),
    (water_ogcapi.FabricData, FabricData),
    (water_ogcapi.GeoConnex, GeoConnex),
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
    blocking: type[water_ogcapi.OGCService], service: type[OGCService]
) -> None:
    missing = [
        name for name in async_methods(service) if not hasattr(blocking, TWINS.get(name, name))
    ]
    assert not missing


@pytest.mark.parametrize("name", [n for n in async_methods(OGCService) if n not in TWINS])
def test_methods_copy_the_async_parameters_and_docstring(name: str) -> None:
    method, original = getattr(water_ogcapi.OGCService, name), getattr(OGCService, name)
    assert inspect.signature(method).parameters == inspect.signature(original).parameters
    assert method.__doc__ == original.__doc__


def test_iter_pages_names_the_iterator_it_returns() -> None:
    assert (
        inspect.signature(water_ogcapi.OGCService.iter_pages).return_annotation == "Iterator[Page]"
    )


@pytest.mark.parametrize(
    ("blocking", "service"),
    [
        (water_ogcapi.NWIS, NWIS),
        (water_ogcapi.FabricData, FabricData),
        (water_ogcapi.GeoConnex, GeoConnex),
    ],
)
def test_each_class_queries_its_own_service(
    blocking: type[water_ogcapi.NWIS | water_ogcapi.FabricData | water_ogcapi.GeoConnex],
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
    with water_ogcapi.OGCService(BASE, transport=transport) as service:
        service.collect("c")
        assert transport._client is not None
    assert transport._client is None


def test_a_service_sharing_a_closed_transport_reconnects() -> None:
    transport, seen = serve()
    first, second = (water_ogcapi.OGCService(BASE, transport=transport) for _ in range(2))
    first.collect("c")
    first.close()
    assert len(second.collect("c").geojson["features"]) == 2
    assert len(seen) == 4


def exitcode_in_fork(target: Callable[[], None]) -> int | None:
    """Run ``target`` in a forked child, which starts its own loop, killed after 10 s.

    A loop that hangs or stops ends with the child, so later tests keep theirs.
    """
    child = multiprocessing.get_context("fork").Process(target=target)
    child.start()
    child.join(10)
    if child.is_alive():
        child.kill()
        child.join()
    return child.exitcode


def collect_in_child() -> None:
    transport, _ = serve()
    water_ogcapi.OGCService(BASE, transport=transport).collect("c")


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_forked_child_starts_its_own_loop() -> None:
    _sync._loop()
    held = threading.Event()

    def hold_the_lock() -> None:
        with _sync._LOCK:
            held.set()
            threading.Event().wait(0.2)

    threading.Thread(target=hold_the_lock, daemon=True).start()
    held.wait(5)
    assert exitcode_in_fork(collect_in_child) == 0


def block_on_the_loop() -> None:
    transport, seen = serve()
    service = water_ogcapi.OGCService(BASE, transport=transport)

    async def on_the_loop() -> None:
        # Stands in for a logging handler or custom transport, which the loop runs.
        service.collect("c")

    with pytest.raises(RuntimeError, match="would wait on itself"):
        _sync._run(on_the_loop())
    assert not seen


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_blocking_call_on_the_loop_raises() -> None:
    assert exitcode_in_fork(block_on_the_loop) == 0


def stop_the_loop() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        raise SystemExit

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    service = water_ogcapi.OGCService(BASE, transport=transport)
    for _ in range(2):  # the second call finds the loop already stopped
        with pytest.raises(RuntimeError, match="event loop has stopped"):
            service.collect("c", validate=False)
    coro = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="event loop has stopped"):
        _sync._run(coro)
    # Closed unsent, so the dead loop's queue keeps no call's arguments.
    assert inspect.getcoroutinestate(coro) == "CORO_CLOSED"


@pytest.mark.skipif(sys.platform == "win32", reason="no fork on Windows")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_call_raises_once_the_loop_has_stopped() -> None:
    assert exitcode_in_fork(stop_the_loop) == 0


def _echo(request: httpx2.Request) -> httpx2.Response:
    """Answer with the method and body; module level, so a transport holding it pickles."""
    return httpx2.Response(200, json={"method": request.method, "body": request.content.decode()})


def test_a_client_reuses_one_transport_across_calls() -> None:
    """Two asyncio.run() calls on one Transport raise; a Client sends both (D-70)."""
    transport = Transport(transport=httpx2.MockTransport(_echo))
    with water_ogcapi.Client(transport=transport) as client:
        assert client.get(f"{BASE}/a").json()["method"] == "GET"
        bound = transport._client
        posted = client.request("POST", f"{BASE}/b", content=b"q", headers={"x": "1"})
        assert posted.json() == {"method": "POST", "body": "q"}
        assert transport._client is bound
        assert transport._loop is _sync._loop()[0]
    assert transport._client is None, "close() closes the transport"


def test_a_client_downloads_from_sync_code(tmp_path: Any) -> None:
    transport = Transport(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=b"x"))
    )
    path = tmp_path / "f.bin"
    with water_ogcapi.Client(transport=transport) as client:
        result = client.download(f"{BASE}/f.bin", path)
    assert (result.size, path.read_bytes()) == (1, b"x")


def test_a_client_works_inside_a_running_loop() -> None:
    transport = Transport(transport=httpx2.MockTransport(_echo))

    async def notebook_cell() -> Any:
        with water_ogcapi.Client(transport=transport) as client:
            return client.get(f"{BASE}/a").json()

    assert asyncio.run(notebook_cell())["method"] == "GET"


def test_a_used_client_pickles_and_sends_again() -> None:
    client = water_ogcapi.Client(transport=Transport(transport=httpx2.MockTransport(_echo)))
    client.get(f"{BASE}/a")
    restored = pickle.loads(pickle.dumps(client))
    assert restored.transport._client is None, "the loop-bound state stays behind (D-64)"
    assert restored.get(f"{BASE}/b").json()["method"] == "GET"
    client.close()
    restored.close()


def test_run_gathers_requests_on_the_clients_transport() -> None:
    """D-73: one blocking call awaits many requests on the shared transport."""
    transport = Transport(transport=httpx2.MockTransport(_echo))
    with water_ogcapi.Client(transport=transport) as client:

        async def fetch_all() -> list[Any]:
            return await asyncio.gather(*(client.transport.get(f"{BASE}/{n}") for n in "abc"))

        responses = client.run(fetch_all())
        assert [r.url.rsplit("/", 1)[-1] for r in responses] == ["a", "b", "c"]
        assert transport._loop is _sync._loop()[0]
        assert client.get(f"{BASE}/d").json()["method"] == "GET", "the same transport sends on"


def test_run_shares_one_transport_with_a_blocking_service() -> None:
    """A dependent package's blocking twin runs its gather where NWIS sends (D-73)."""
    transport, seen = serve()
    nwis = water_ogcapi.OGCService(BASE, transport=transport)
    client = water_ogcapi.Client(transport=transport)
    nwis.collect("c")
    pool = transport._client

    async def fetch_all() -> list[Any]:
        url = f"{BASE}/collections/c/items"
        return await asyncio.gather(transport.get(url), transport.get(f"{url}?offset=1"))

    first, last = client.run(fetch_all())
    assert (first.json(), last.json()) == (FIRST, LAST)
    assert transport._client is pool, "one connection pool for both"
    assert len(seen) == 4
    client.close()


def _unstarted_future() -> asyncio.Future[None]:
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    loop.close()
    return future


def _done_task() -> asyncio.Task[None]:
    loop = asyncio.new_event_loop()
    task = loop.create_task(asyncio.sleep(0))
    loop.run_until_complete(task)
    loop.close()
    return task


@pytest.mark.parametrize(
    "given",
    [lambda: asyncio.sleep, lambda: 1, _unstarted_future, _done_task],
    ids=["coroutine function", "value", "future of another loop", "task"],
)
def test_run_refuses_what_is_not_a_coroutine(given: Callable[[], Any]) -> None:
    with (
        water_ogcapi.Client() as client,
        pytest.raises(ValidationError, match=r"run\(\) takes a coroutine object"),
    ):
        client.run(given())


def test_run_inside_a_coroutine_it_runs_raises() -> None:
    client = water_ogcapi.Client()

    async def nested() -> None:
        client.run(asyncio.sleep(0))

    with pytest.raises(RuntimeError, match="would wait on itself"):
        client.run(nested())


def test_an_interrupt_cancels_every_gathered_request() -> None:
    started: list[str] = []
    cancelled: list[str] = []
    both = threading.Event()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        started.append(request.url.path)
        if len(started) == 2:
            both.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(request.url.path)
            raise
        return httpx2.Response(200)

    def interrupt() -> None:
        if both.wait(5):
            _thread.interrupt_main()

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    client = water_ogcapi.Client(transport=transport)

    async def fetch_all() -> list[Any]:
        return await asyncio.gather(transport.get(f"{BASE}/a"), transport.get(f"{BASE}/b"))

    threading.Thread(target=interrupt, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        client.run(fetch_all())
    assert settled_tasks() == 0
    assert sorted(cancelled) == sorted(started) == ["/ogcapi/v1/a", "/ogcapi/v1/b"]
    client.close()


def test_the_root_names_the_async_engine_and_the_blocking_client() -> None:
    """D-70 keeps Transport the async engine; the blocking HTTP class is Client."""
    assert inspect.iscoroutinefunction(water_ogcapi.Transport.request)
    assert not inspect.iscoroutinefunction(water_ogcapi.Client.request)
    assert water_ogcapi.Client.download.__name__ == "download"


def test_the_root_exports_the_types_the_transport_returns(tmp_path: Any) -> None:
    """D-75: return types a dependent package can annotate with a public name."""
    transport = Transport(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=b"x"))
    )
    with water_ogcapi.Client(transport=transport) as client:
        assert isinstance(client.get(f"{BASE}/a"), water_ogcapi.Response)
        assert isinstance(client.download(f"{BASE}/f", tmp_path / "f"), water_ogcapi.Download)
    assert {"Response", "Download"} <= set(water_ogcapi.__all__)


def test_a_client_call_takes_its_own_retries_and_typed_params() -> None:
    """D-74 and D-77 reach the blocking wrappers, which copy the async signatures."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(503)

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=3, backoff=0)
    with water_ogcapi.Client(transport=transport) as client, pytest.raises(ServiceError):
        client.get(f"{BASE}/a", max_retries=0, params={"x": True})
    assert [r.url.query for r in seen] == [b"x=true"]
