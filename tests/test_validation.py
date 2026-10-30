"""Tests for the query check against the collection index and queryables (D-45)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

import water_ogcapi
from water_ogcapi import Checkpoint, Request, Transport
from water_ogcapi.aio import OGCService
from water_ogcapi.exceptions import ServiceError, ValidationError

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"
PAGE = {"type": "FeatureCollection", "features": [], "links": []}
FEATURE = {"type": "Feature", "geometry": None, "properties": {}, "links": []}


def body(path: str) -> dict[str, Any]:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))["response"]["body"]


def serve(routes: dict[str, Any]) -> tuple[Transport, list[str]]:
    """A transport answering each path below ``BASE`` from ``routes``, else a page or a 404.

    Returns the transport and the path of every request it answered.
    """
    seen: list[str] = []
    prefix = httpx2.URL(BASE).path

    async def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix(prefix)
        seen.append(path)
        # Yield, so concurrent requests overlap as they would on a network.
        await asyncio.sleep(0.001)
        if path in routes:
            return httpx2.Response(200, json=routes[path])
        if path.endswith("/items"):
            return httpx2.Response(200, json=PAGE)
        if "/items/" in path:
            return httpx2.Response(200, json=FEATURE)
        return httpx2.Response(404)

    return Transport(transport=httpx2.MockTransport(handler), max_retries=0), seen


def nwis_routes() -> dict[str, Any]:
    return {
        "/collections": body("nwis/collections.json"),
        "/collections/daily/queryables": body("nwis/daily/queryables.json"),
    }


def test_an_unknown_collection_lists_the_collections_and_sends_no_query() -> None:
    transport, seen = serve(nwis_routes())
    with pytest.raises(ValidationError, match=r"unknown collection 'dayly'; use one of .*daily"):
        asyncio.run(OGCService(BASE, transport=transport).collect("dayly"))
    assert seen == ["/collections"]


def test_an_unknown_parameter_lists_the_queryables_and_the_query_parameters() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)
    params = {"monitoring_location_id": "USGS-01646500", "statecode": "24", "datetime": "P7D"}
    with pytest.raises(ValidationError, match="'statecode' for 'daily'") as excinfo:
        asyncio.run(service.collect("daily", params=params))
    assert "state_code" in str(excinfo.value)
    assert "skipGeometry" in str(excinfo.value)
    assert seen == ["/collections", "/collections/daily/queryables"]


def test_params_changed_during_the_check_are_checked_as_sent() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)
    params = {"typo": "x"}

    async def run() -> None:
        query = asyncio.create_task(service.collect("daily", params=params))
        # The query now waits on the index request, its params already copied.
        await asyncio.sleep(0)
        params.clear()
        params["parameter_code"] = "00060"
        await query

    with pytest.raises(ValidationError, match="'typo' for 'daily'"):
        asyncio.run(run())
    assert seen == ["/collections", "/collections/daily/queryables"]


def test_an_unknown_id_property_lists_the_queryables() -> None:
    transport, _ = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)
    with pytest.raises(ValidationError, match="id_property 'site' is not a queryable of 'daily'"):
        asyncio.run(service.collect("daily", ids=["USGS-01646500"], id_property="site"))


def test_a_known_query_is_sent() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)
    asyncio.run(
        service.collect(
            "daily",
            params={"parameter_code": "00060", "datetime": "P7D"},
            ids=["USGS-01646500"],
            id_property="monitoring_location_id",
        )
    )
    assert seen == ["/collections", "/collections/daily/queryables", "/collections/daily/items"]


def test_standard_parameters_alone_fetch_no_queryables() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)
    asyncio.run(service.collect("daily", params={"datetime": "P7D", "skipGeometry": "true"}))
    assert seen == ["/collections", "/collections/daily/items"]


def test_validate_false_sends_the_query_unchecked() -> None:
    transport, seen = serve({})
    service = OGCService(BASE, transport=transport)

    async def run() -> None:
        await service.collect("anything", params={"typo": "x"}, validate=False)
        await service.item("anything", "x", validate=False)

    asyncio.run(run())
    assert seen == ["/collections/anything/items", "/collections/anything/items/x"]


def test_item_checks_its_collection() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)
    with pytest.raises(ValidationError, match="unknown collection 'site'"):
        asyncio.run(service.item("site", "USGS-01646500"))
    assert seen == ["/collections"]


def test_a_credential_in_params_is_refused_before_the_check_sends() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)
    with pytest.raises(ValidationError, match="X-Api-Key header"):
        asyncio.run(service.collect("daily", params={"api_key": "secret"}))
    with pytest.raises(ValidationError, match="X-Api-Key header"):
        asyncio.run(service.item("daily", "x", params={"api_key": "secret"}))
    assert seen == []


def test_a_cached_index_is_revalidated_once_for_a_name_it_lacks() -> None:
    routes = {"/collections": {"collections": [{"id": "old"}]}}
    transport, seen = serve(routes)
    service = OGCService(BASE, transport=transport)

    async def run() -> None:
        await service.collect("old")
        routes["/collections"] = {"collections": [{"id": "old"}, {"id": "new"}]}
        await service.collect("new")
        with pytest.raises(ValidationError, match="use one of old, new"):
            await service.collect("gone")

    asyncio.run(run())
    index = [path for path in seen if path == "/collections"]
    # The first fetch, a revalidation that finds "new", and one that does not find "gone".
    assert len(index) == 3


def test_a_failed_revalidation_leaves_the_cached_copy_fresh() -> None:
    routes = {"/collections": {"collections": [{"id": "old"}]}}
    transport, seen = serve(routes)
    service = OGCService(BASE, transport=transport)

    async def run() -> None:
        await service.collect("old")
        del routes["/collections"]
        with pytest.raises(ServiceError, match="HTTP 404"):
            await service.collect("typo")
        await service.collect("old")

    asyncio.run(run())
    assert seen.count("/collections") == 2


def test_a_name_missing_from_a_fresh_index_is_not_fetched_again() -> None:
    transport, seen = serve(nwis_routes())
    with pytest.raises(ValidationError):
        asyncio.run(OGCService(BASE, transport=transport).collect("dayly"))
    assert seen == ["/collections"]


def test_concurrent_queries_fetch_the_metadata_once() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)

    async def run() -> None:
        params = {"parameter_code": "00060"}
        await asyncio.gather(*(service.collect("daily", params=params) for _ in range(5)))

    asyncio.run(run())
    assert seen.count("/collections") == 1
    assert seen.count("/collections/daily/queryables") == 1
    assert seen.count("/collections/daily/items") == 5


def test_metadata_fetches_work_on_a_second_event_loop() -> None:
    transport, seen = serve(nwis_routes())
    service = OGCService(BASE, transport=transport)

    async def burst() -> None:
        # Concurrent misses make the lock wait, which binds it to this loop.
        await asyncio.gather(*(service.collections() for _ in range(3)))
        service.refresh()
        await transport.aclose()

    asyncio.run(burst())
    asyncio.run(burst())
    assert seen.count("/collections") == 2


def test_a_property_that_is_not_an_object_still_names_a_queryable() -> None:
    queryables = {"type": "object", "properties": {"id": True, "site": {"type": "string"}}}
    transport, seen = serve(
        {"/collections": {"collections": [{"id": "c"}]}, "/collections/c/queryables": queryables}
    )
    asyncio.run(OGCService(BASE, transport=transport).collect("c", params={"site": "x"}))
    assert seen[-1] == "/collections/c/items"


def test_the_sync_api_raises_the_same_error() -> None:
    transport, _ = serve(nwis_routes())
    with pytest.raises(ValidationError, match="unknown collection"):
        water_ogcapi.OGCService(BASE, transport=transport).collect("dayly")


def test_a_resume_sends_no_metadata_request() -> None:
    """A checkpoint's requests passed the check when it was made (D-52)."""
    request = Request(url=f"{BASE}/collections/daily/items", params={"f": "json"})
    checkpoint = Checkpoint(base_url=BASE, collection="daily", pending=(request,))
    transport, seen = serve({})

    async def run() -> None:
        async for _ in OGCService(BASE, transport=transport).iter_pages("daily", resume=checkpoint):
            pass

    asyncio.run(run())
    assert seen == ["/collections/daily/items"]
