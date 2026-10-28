"""Tests for the service classes."""

from __future__ import annotations

import asyncio
import copy
import os
import pickle
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest

import water_ogcapi
from water_ogcapi import QueryResult, Transport
from water_ogcapi.aio import NWIS, FabricData, GeoConnex, OGCService
from water_ogcapi.exceptions import PaginationError, ServiceError, ValidationError

FEATURE = {"type": "Feature", "geometry": None, "properties": {}, "links": []}
EMPTY_PAGE = {"type": "FeatureCollection", "features": [], "links": []}
INDEX = {"collections": [{"id": "c"}]}
QUERYABLES = {"type": "object", "properties": {"id": {"type": "string"}}}


def recording() -> tuple[Transport, list[httpx2.Request]]:
    """A transport answering every request as a one-page service would."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        path = request.url.path
        doc = (
            FEATURE
            if "/items/" in path
            else EMPTY_PAGE
            if path.endswith("/items")
            else QUERYABLES
            if path.endswith("/queryables")
            else INDEX
        )
        return httpx2.Response(200, json=doc)

    return Transport(transport=httpx2.MockTransport(handler), max_retries=0), seen


def exercise(*services: OGCService) -> None:
    """Send, from each service at once, the collection index, an item, a GET and a POST query.

    The POST query's check also fetches the queryables, so each service sends five requests.
    """

    async def requests(service: OGCService) -> None:
        await service.collections()
        await service.item("c", "x")
        await service.collect("c")
        await service.collect("c", ids=["x"], id_property="id")

    async def run() -> None:
        await asyncio.gather(*map(requests, services))

    asyncio.run(run())


def test_nwis_sends_the_key_read_at_construction_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USGS_API_KEY", "env-key")
    transport, seen = recording()
    service = NWIS(transport=transport)
    monkeypatch.setenv("USGS_API_KEY", "changed")
    exercise(service)
    assert [r.headers.get("x-api-key") for r in seen] == ["env-key"] * 5
    assert {r.url.host for r in seen} == {"api.waterdata.usgs.gov"}


@pytest.mark.parametrize(
    ("env", "api_key", "sent"),
    [("env-key", "arg-key", "arg-key"), ("env-key", "", None), (None, None, None)],
)
def test_nwis_takes_the_key_argument_over_the_environment(
    monkeypatch: pytest.MonkeyPatch, env: str | None, api_key: str | None, sent: str | None
) -> None:
    if env is None:
        monkeypatch.delenv("USGS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("USGS_API_KEY", env)
    transport, seen = recording()
    exercise(NWIS(api_key=api_key, transport=transport))
    assert {r.headers.get("x-api-key") for r in seen} == {sent}


def test_nwis_sends_the_key_to_a_next_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USGS_API_KEY", "env-key")
    href = f"{NWIS().base_url}/collections/c/items?cursor=2"
    first = {**EMPTY_PAGE, "links": [{"rel": "next", "href": href}]}
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=first if len(seen) == 1 else EMPTY_PAGE)

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    asyncio.run(NWIS(transport=transport).collect("c", validate=False))
    assert [(str(r.url), r.headers.get("x-api-key")) for r in seen][1] == (href, "env-key")
    assert seen[0].headers.get("x-api-key") == "env-key"


@pytest.mark.parametrize("key", ["SECRET\u00a0", "SECRET\n"])
def test_nwis_refuses_a_key_that_is_not_printable_ascii(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    pickled = pickle.dumps(NWIS(api_key="arg-key"))
    monkeypatch.setenv("USGS_API_KEY", key)
    with pytest.raises(ValueError, match="printable ASCII") as excinfo:
        NWIS()
    assert "SECRET" not in repr(excinfo.value)
    with pytest.raises(ValueError, match="printable ASCII"):
        pickle.loads(pickled)


class LooksASCII(str):
    """A key that claims to be ASCII whatever it holds."""

    __slots__ = ()

    def isascii(self) -> bool:
        return True


def test_nwis_refuses_a_key_that_is_not_a_str() -> None:
    with pytest.raises(ValidationError, match=r"key must be str, got LooksASCII$") as excinfo:
        NWIS(api_key=LooksASCII("SECRET\u00e9"))
    assert "SECRET" not in repr(excinfo.value)


def roundtrip(obj: object) -> object:
    return pickle.loads(pickle.dumps(obj))


@pytest.mark.parametrize("clone", [roundtrip, copy.deepcopy])
@pytest.mark.parametrize("cls", [NWIS, water_ogcapi.NWIS])
@pytest.mark.parametrize(("env", "sent"), [("env-key", {"X-Api-Key": "env-key"}), (None, {})])
def test_a_pickled_nwis_leaves_its_key_out_and_reads_the_environment_on_load(
    monkeypatch: pytest.MonkeyPatch,
    clone: Callable[[object], object],
    cls: type,
    env: str | None,
    sent: dict[str, str],
) -> None:
    service = cls(api_key="arg-key")
    assert b"arg-key" not in pickle.dumps(service)
    if env is None:
        monkeypatch.delenv("USGS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("USGS_API_KEY", env)
    loaded = clone(service)
    assert getattr(loaded, "_service", loaded)._headers() == sent


PAGE = {"type": "FeatureCollection", "features": [FEATURE], "links": []}
# Module level, so a pickled MockTransport names it and another process imports it.
SEEN: list[str] = []


def answer(request: httpx2.Request) -> httpx2.Response:
    SEEN.append(request.url.path.rsplit("/", 1)[-1])
    return httpx2.Response(200, json=INDEX if request.url.path.endswith("/collections") else PAGE)


CHILD = """
import asyncio, pickle, sys
service = pickle.loads(sys.stdin.buffer.read())
query = service.collect("c")
result = asyncio.run(query) if asyncio.iscoroutine(query) else query
sys.stdout.buffer.write(pickle.dumps((result, sys.modules[sys.argv[1]].SEEN)))
"""


def pickled_after_a_query(service: NWIS | water_ogcapi.NWIS) -> bytes:
    if isinstance(service, water_ogcapi.NWIS):
        with service:
            service.collect("c")
            return pickle.dumps(service)

    async def run() -> bytes:
        async with service._transport:
            await service.collect("c")
            return pickle.dumps(service)

    return asyncio.run(run())


async def answer_later(request: httpx2.Request) -> httpx2.Response:
    await asyncio.sleep(0.01)
    return answer(request)


def test_a_service_pickles_while_a_metadata_request_waits() -> None:
    transport = Transport(transport=httpx2.MockTransport(answer_later), max_retries=0)
    service = GeoConnex(transport=transport)

    async def run() -> bytes:
        async with transport:
            # The first call holds the index lock, and the second waits on it.
            calls = [asyncio.create_task(service.collections()) for _ in range(2)]
            await asyncio.sleep(0)
            pickled = pickle.dumps(service)
            await asyncio.gather(*calls)
            return pickled

    loaded = pickle.loads(asyncio.run(run()))

    async def again() -> None:
        async with loaded._transport:
            await loaded.collections()

    asyncio.run(again())


@pytest.mark.parametrize("cls", [NWIS, water_ogcapi.NWIS])
def test_a_used_service_pickles_and_queries_in_another_process(cls: type) -> None:
    transport = Transport(transport=httpx2.MockTransport(answer), max_retries=0)
    data = pickled_after_a_query(cls(api_key="arg-key", transport=transport))
    assert b"arg-key" not in data
    env = {k: v for k, v in os.environ.items() if k != "USGS_API_KEY"}
    child = subprocess.run(
        [sys.executable, "-c", CHILD, __name__],
        input=data,
        capture_output=True,
        check=True,
        cwd=Path(__file__).parent.parent,
        env=env,
        timeout=60,
    )
    result, seen = pickle.loads(child.stdout)
    assert result.geojson["features"] == PAGE["features"]
    assert seen == ["collections", "items"], "the child fetches its own index"


def test_services_pickled_together_keep_sharing_their_transport() -> None:
    transport = Transport()
    services = [water_ogcapi.NWIS(transport=transport), water_ogcapi.GeoConnex(transport=transport)]
    loaded = pickle.loads(pickle.dumps(services))
    shared = {id(s._transport) for s in loaded} | {id(s._service._transport) for s in loaded}
    assert len(shared) == 1


def test_fabricdata_sends_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USGS_API_KEY", "env-key")
    transport, seen = recording()
    exercise(FabricData(transport=transport))
    assert len(seen) == 5
    assert not any("x-api-key" in r.headers for r in seen)
    assert {r.url.host for r in seen} == {"api.water.usgs.gov"}


def test_the_key_never_reaches_geoconnex_on_a_shared_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USGS_API_KEY", "env-key")
    transport, seen = recording()
    exercise(NWIS(transport=transport), GeoConnex(transport=transport))
    keys = {
        host: [r.headers.get("x-api-key") for r in seen if r.url.host == host]
        for host in ("api.waterdata.usgs.gov", "reference.geoconnex.us")
    }
    assert keys == {"api.waterdata.usgs.gov": ["env-key"] * 5, "reference.geoconnex.us": [None] * 5}


@pytest.mark.parametrize(
    ("service", "default", "cap"),
    [(NWIS, 10_000, 50_000), (FabricData, 1_000, 1_000), (GeoConnex, 500, 10_000)],
)
def test_a_service_sends_its_profile_page_sizes(
    service: type[NWIS | FabricData | GeoConnex], default: int, cap: int
) -> None:
    transport, seen = recording()
    client = service(transport=transport)
    asyncio.run(client.collect("c", validate=False))
    assert seen[0].url.params["limit"] == str(default)
    assert str(seen[0].url).startswith(f"{client.base_url}/collections/c/items?")
    with pytest.raises(ValueError, match=f"from 1 to {cap},"):
        asyncio.run(client.collect("c", params={"limit": str(cap + 1)}))
    assert len(seen) == 1


@pytest.mark.network
def test_nwis_fetches_the_potomac_gage() -> None:
    async def run() -> object:
        async with Transport(max_retries=0) as transport, NWIS(transport=transport) as service:
            page = await service.item("monitoring-locations", "USGS-01646500")
            return (page.body or {}).get("id")

    assert asyncio.run(run()) == "USGS-01646500"


@pytest.mark.network
def test_nwis_finds_every_site_in_the_dc_bbox() -> None:
    bbox = (-77.12, 38.80, -76.91, 38.99)

    async def run() -> QueryResult:
        async with Transport(max_retries=0) as transport, NWIS(transport=transport) as svc:
            return await svc.collect("monitoring-locations", bbox=bbox)

    result = asyncio.run(run())
    assert result.pagination.completeness == "complete"
    assert "end_of_links" in result.pagination.checks
    west, south, east, north = bbox
    points = [f["geometry"]["coordinates"] for f in result.geojson["features"]]
    assert points
    assert all(west <= x <= east and south <= y <= north for x, y in points)


def did_not_respond(err: PaginationError) -> bool:
    """Whether a sent request timed out, lost its connection, or got a 5xx (D-14)."""
    failure = err.failures[0]
    return failure.attempts > 0 and (failure.status is None or failure.status >= 500)


@pytest.mark.parametrize(
    ("failure", "skips"),
    [
        (ServiceError("timed out", "u"), True),
        (ServiceError("bad gateway", "u", status=502), True),
        (ServiceError("not found", "u", status=404), False),
        (ServiceError("a next link repeats one already followed", "u", attempts=0), False),
    ],
)
def test_only_a_missing_response_skips_the_fabricdata_test(
    failure: ServiceError, skips: bool
) -> None:
    assert did_not_respond(PaginationError([failure])) is skips


@pytest.mark.network
def test_fabricdata_finds_every_gage_in_the_dc_bbox() -> None:
    async def run() -> QueryResult:
        async with Transport(max_retries=0) as transport, FabricData(transport=transport) as svc:
            return await svc.collect("gagesii", bbox=(-77.12, 38.80, -76.91, 38.99))

    try:
        result = asyncio.run(run())
    except PaginationError as err:
        if did_not_respond(err):
            pytest.skip(f"FabricData did not respond: {err.failures[0]}")
        raise
    assert result.pagination.completeness == "complete"
    assert "count" in result.pagination.checks
    assert result.geojson["features"]


@pytest.mark.network
def test_geoconnex_returns_the_22_two_digit_hucs() -> None:
    async def run() -> QueryResult:
        async with Transport(max_retries=0) as transport, GeoConnex(transport=transport) as svc:
            return await svc.collect("hu02", params={"skipGeometry": "true"})

    result = asyncio.run(run())
    assert sorted(f["id"] for f in result.geojson["features"]) == [f"{n:02d}" for n in range(1, 23)]
    assert result.pagination.completeness == "complete"
    assert "count" in result.pagination.checks


@pytest.mark.network
def test_geoconnex_answers_an_ids_query_sent_as_cql2_json() -> None:
    """The documents declare the POST body as application/json; the client sends CQL2-JSON."""

    async def run() -> QueryResult:
        async with Transport(max_retries=0) as transport, GeoConnex(transport=transport) as svc:
            return await svc.collect(
                "hu02", ids=["01", "02"], id_property="huc2", params={"skipGeometry": "true"}
            )

    result = asyncio.run(run())
    assert sorted(f["id"] for f in result.geojson["features"]) == ["01", "02"]
    assert result.pagination.completeness == "complete"
