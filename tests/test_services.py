"""Tests for the service classes."""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from water_ogcapi import NWIS, FabricData, GeoConnex, OGCService, QueryResult, Transport
from water_ogcapi.exceptions import PaginationError, ServiceError

FEATURE = {"type": "Feature", "geometry": None, "properties": {}, "links": []}
EMPTY_PAGE = {"type": "FeatureCollection", "features": [], "links": []}


def recording() -> tuple[Transport, list[httpx2.Request]]:
    """A transport answering every request as a one-page service would."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        path = request.url.path
        index = {"collections": []}
        doc = FEATURE if "/items/" in path else EMPTY_PAGE if path.endswith("/items") else index
        return httpx2.Response(200, json=doc)

    return Transport(transport=httpx2.MockTransport(handler), max_retries=0), seen


def exercise(*services: OGCService) -> None:
    """Send, from each service at once, the collection index, an item, a GET and a POST query."""

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
    assert [r.headers.get("x-api-key") for r in seen] == ["env-key"] * 4
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
    asyncio.run(NWIS(transport=transport).collect("c"))
    assert [(str(r.url), r.headers.get("x-api-key")) for r in seen][1] == (href, "env-key")
    assert seen[0].headers.get("x-api-key") == "env-key"


@pytest.mark.parametrize("key", ["SECRET\u00a0", "SECRET\n"])
def test_nwis_refuses_a_key_that_is_not_printable_ascii(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("USGS_API_KEY", key)
    with pytest.raises(ValueError, match="printable ASCII") as excinfo:
        NWIS()
    assert "SECRET" not in repr(excinfo.value)


def test_fabricdata_sends_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USGS_API_KEY", "env-key")
    transport, seen = recording()
    exercise(FabricData(transport=transport))
    assert len(seen) == 4
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
    assert keys == {"api.waterdata.usgs.gov": ["env-key"] * 4, "reference.geoconnex.us": [None] * 4}


@pytest.mark.parametrize(
    ("service", "default", "cap"),
    [(NWIS, 10_000, 50_000), (FabricData, 1_000, 1_000), (GeoConnex, 500, 10_000)],
)
def test_a_service_sends_its_profile_page_sizes(
    service: type[NWIS | FabricData | GeoConnex], default: int, cap: int
) -> None:
    transport, seen = recording()
    client = service(transport=transport)
    asyncio.run(client.collect("c"))
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
