"""Tests for the base service client."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import pickle
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx2
import pytest

from water_ogcapi import Page, QueryResult, Schema, ServiceProfile
from water_ogcapi._cache import TTL
from water_ogcapi._query import Request
from water_ogcapi._transport import Transport
from water_ogcapi.aio import OGCService
from water_ogcapi.exceptions import (
    PaginationError,
    PartialResultsError,
    ServiceError,
    ValidationError,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"


@pytest.fixture(autouse=True)
def unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send no metadata request before a query; tests/test_validation.py covers the check."""

    async def skip(*_: object) -> None:
        return None

    monkeypatch.setattr(OGCService, "_validate", skip)


def body(path: str) -> dict[str, Any]:
    return json.loads((FIXTURES / path).read_text())["response"]["body"]


def serve(*replies: dict[str, Any] | httpx2.Response) -> tuple[Transport, list[httpx2.Request]]:
    """A transport answering each request with the next reply, a dict as JSON."""
    seen: list[httpx2.Request] = []
    queue = list(replies)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        reply = queue.pop(0)
        return reply if isinstance(reply, httpx2.Response) else httpx2.Response(200, json=reply)

    return Transport(transport=httpx2.MockTransport(handler), max_retries=0), seen


def test_no_io_on_construction() -> None:
    transport, seen = serve()
    OGCService(BASE, transport=transport)
    assert seen == []


def test_collections_are_fetched_once() -> None:
    transport, seen = serve(body("nwis/collections.json"))
    service = OGCService(f"{BASE}/", transport=transport)

    async def run() -> tuple[Any, Any]:
        return await service.collections(), await service.collections()

    first, second = asyncio.run(run())
    assert first == second
    assert first.collections
    assert [str(r.url) for r in seen] == [f"{BASE}/collections?f=json"]


@pytest.mark.parametrize("content", [b"<html></html>", b"[]", b'{"truncated": '])
def test_body_must_be_a_json_object(content: bytes) -> None:
    transport, _ = serve(httpx2.Response(200, content=content))
    with pytest.raises(ServiceError, match="not a JSON object") as excinfo:
        asyncio.run(OGCService(BASE, transport=transport).collections())
    assert excinfo.value.status == 200
    assert excinfo.value.__context__ is None, "a JSONDecodeError holds the whole body"


def test_a_body_without_a_collections_list_is_not_cached() -> None:
    error = {"code": "InternalError", "description": "upstream timed out"}
    transport, seen = serve(error, body("nwis/collections.json"))
    service = OGCService(BASE, transport=transport)

    async def run() -> Any:
        with pytest.raises(ServiceError, match="not a collection index"):
            await service.collections()
        return await service.collections()

    assert asyncio.run(run()).collections
    assert len(seen) == 2


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The cache's clock, advanced by adding to its one element."""
    now = [1000.0]
    monkeypatch.setattr("water_ogcapi._cache.monotonic", lambda: now[0])
    return now


def test_an_expired_entry_revalidates_and_a_304_keeps_its_payload(clock: list[float]) -> None:
    transport, seen = serve(
        httpx2.Response(200, json=body("nwis/collections.json"), headers={"ETag": '"v1"'}),
        httpx2.Response(304, headers={"ETag": '"v2"'}),
        httpx2.Response(304),
        httpx2.Response(304),
    )
    service = OGCService(BASE, transport=transport)

    async def run() -> list[Any]:
        results = [await service.collections()]
        clock[0] += TTL
        results += [await service.collections(), await service.collections()]
        for _ in range(2):
            clock[0] += TTL
            results.append(await service.collections())
        return results

    results = asyncio.run(run())
    assert all(result == results[0] for result in results)
    # The third call is served from the entry the 304 renewed. A 304's ETag replaces
    # the stored one, and a 304 without one keeps it.
    assert [r.headers.get("if-none-match") for r in seen] == [None, '"v1"', '"v2"', '"v2"']


def test_last_modified_revalidates_with_if_modified_since(clock: list[float]) -> None:
    stamp = "Wed, 07 Oct 2026 12:00:00 GMT"
    transport, seen = serve(
        httpx2.Response(200, json=body("nwis/collections.json"), headers={"Last-Modified": stamp}),
        httpx2.Response(304),
    )
    service = OGCService(BASE, transport=transport)

    async def run() -> None:
        await service.collections()
        clock[0] += TTL
        await service.collections()

    asyncio.run(run())
    assert seen[1].headers.get("if-modified-since") == stamp
    assert "if-none-match" not in seen[1].headers


def test_an_expired_entry_without_validators_is_replaced(clock: list[float]) -> None:
    transport, seen = serve(body("nwis/collections.json"), body("geoconnex/collections.json"))
    service = OGCService(BASE, transport=transport)

    async def run() -> tuple[Any, Any]:
        first = await service.collections()
        clock[0] += TTL
        return first, await service.collections()

    first, second = asyncio.run(run())
    assert first != second
    assert not {"if-none-match", "if-modified-since"} & set(seen[1].headers)


@pytest.mark.parametrize("cached", [False, True])
def test_a_304_to_a_request_without_validators_raises(clock: list[float], cached: bool) -> None:
    replies = [body("nwis/collections.json")] if cached else []
    transport, _ = serve(*replies, httpx2.Response(304))
    service = OGCService(BASE, transport=transport)

    async def run() -> None:
        if cached:
            await service.collections()
            clock[0] += TTL
        await service.collections()

    with pytest.raises(ServiceError, match="sent no validators"):
        asyncio.run(run())


def test_a_caller_changing_a_result_leaves_the_cache_alone() -> None:
    transport, _ = serve(body("nwis/daily/queryables.json"))
    service = OGCService(BASE, transport=transport)

    async def run() -> tuple[Any, Any]:
        first = await service.queryables("daily")
        for prop in first.properties.values():
            for value in prop.extra.values():
                if isinstance(value, list | dict):
                    value.clear()
        return first, await service.queryables("daily")

    first, second = asyncio.run(run())
    assert first != second
    assert second == Schema.from_dict(body("nwis/daily/queryables.json"))


def test_refresh_drops_the_cache_without_a_request() -> None:
    transport, seen = serve(body("nwis/collections.json"), body("nwis/collections.json"))
    service = OGCService(BASE, transport=transport)

    async def run() -> int:
        await service.collections()
        service.refresh()
        sent = len(seen)
        await service.collections()
        return sent

    assert asyncio.run(run()) == 1
    assert len(seen) == 2


@pytest.mark.parametrize("name", ["queryables", "schema"])
def test_property_documents_are_cached_per_collection(name: str) -> None:
    doc = body(f"nwis/daily/{name}.json")
    transport, seen = serve(doc, doc)
    service = OGCService(BASE, transport=transport)
    fetch = getattr(service, name)

    async def run() -> tuple[Any, Any]:
        first, again = await fetch("daily"), await fetch("daily")
        await fetch("latest-daily")
        return first, again

    first, again = asyncio.run(run())
    assert first == again
    assert first.properties
    assert [str(r.url) for r in seen] == [
        f"{BASE}/collections/daily/{name}?f=json",
        f"{BASE}/collections/latest-daily/{name}?f=json",
    ]


def test_a_property_document_without_properties_is_not_cached() -> None:
    transport, seen = serve({"code": "InternalError"}, body("nwis/daily/queryables.json"))
    service = OGCService(BASE, transport=transport)

    async def run() -> Any:
        with pytest.raises(ServiceError, match="properties object"):
            await service.queryables("daily")
        return await service.queryables("daily")

    assert asyncio.run(run()).properties
    assert len(seen) == 2


def test_only_a_created_transport_is_closed() -> None:
    shared, _ = serve(body("nwis/collections.json"))

    async def run() -> OGCService:
        async with OGCService(BASE, transport=shared) as service:
            await service.collections()
        async with OGCService(BASE) as owner:
            owner._transport._bind()  # opens the client without sending a request
        return owner

    owner = asyncio.run(run())
    assert shared._client is not None, "a passed transport stays open for its other users"
    assert owner._transport._client is None


def record(path: str) -> dict[str, Any]:
    return json.loads((FIXTURES / path).read_text())


def without_next(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop the next link, so a recorded page ends the traversal."""
    return {**doc, "links": [link for link in doc["links"] if link["rel"] != "next"]}


def traverse(
    service: OGCService, collection: str, params: dict[str, str] | None = None, **query: Any
) -> tuple[list[Page], PaginationError | None]:
    """Return the pages a traversal yielded and the error that ended it, if any."""
    got: list[Page] = []

    async def run() -> None:
        # A comprehension would lose the pages yielded before an error.
        async for page in service.iter_pages(collection, params=params, **query):
            got.append(page)  # noqa: PERF401

    try:
        asyncio.run(run())
    except PaginationError as err:
        return got, err
    return got, None


def recorded_pages(directory: str, count: int) -> list[dict[str, Any]]:
    return [record(f"{directory}/items-{n}.json") for n in range(1, count + 1)]


# NWIS reports no numberMatched, and FabricData names port 443 in its next links.
@pytest.mark.parametrize(
    ("directory", "count"), [("nwis/daily", 3), ("fabricdata/gagesii", 2), ("geoconnex/hu02", 3)]
)
def test_every_recorded_page_is_followed(directory: str, count: int) -> None:
    records = recorded_pages(directory, count)
    docs = [r["response"]["body"] for r in records]
    transport, seen = serve(*docs[:-1], without_next(docs[-1]))
    first = records[0]["request"]
    base, _, rest = first["url"].partition("/collections/")
    got, err = traverse(OGCService(base, transport=transport), rest.split("/")[0], first["params"])
    assert err is None
    assert [str(r.url) for r in seen] == [r["request"]["url"] for r in records]
    assert [p.feature_count for p in got] == [len(d["features"]) for d in docs]
    assert [p.number_matched for p in got] == [d.get("numberMatched") for d in docs]
    assert got[0].params == first["params"]
    assert [p.params for p in got[1:]] == [{}] * (count - 1), "the next link holds the query"
    assert got[1].body == docs[1]
    assert got[1].content is not None
    assert json.loads(got[1].content) == docs[1]


GEOCONNEX = "https://reference.geoconnex.us"


def hu02() -> list[dict[str, Any]]:
    return [r["response"]["body"] for r in recorded_pages("geoconnex/hu02", 3)]


def test_a_short_page_ends_nothing() -> None:
    docs = hu02()
    docs[0] = {**docs[0], "features": docs[0]["features"][:4], "numberReturned": 4}
    transport, seen = serve(*docs)
    got, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02", {"limit": "10"})
    assert err is None
    assert len(seen) == 3
    assert sum(p.feature_count for p in got) == 16


@pytest.mark.parametrize(
    ("reply", "status"),
    [(httpx2.Response(503), 503), (httpx2.Response(200, content=b"<html></html>"), 200)],
)
def test_a_failed_page_is_reported_by_its_url(reply: httpx2.Response, status: int) -> None:
    docs = hu02()
    transport, _ = serve(docs[0], reply)
    got, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02", {"limit": "10"})
    assert len(got) == 1, "the page before the failure reached the caller"
    assert err is not None
    [failure] = err.failures
    assert failure.status == status
    assert failure.url == record("geoconnex/hu02/items-2.json")["request"]["url"]


NEXT = f"{GEOCONNEX}/collections/hu02/items?offset=10"


@pytest.mark.parametrize(
    "doc",
    [
        {"code": "InvalidParameterValue", "description": "bad bbox"},
        {"type": "FeatureCollection"},
        {"type": "FeatureCollection", "features": {}},
        {"type": "Feature", "geometry": None, "properties": {}},
        {"type": "FeatureCollection", "features": [], "links": {"rel": "next", "href": NEXT}},
        {"type": "FeatureCollection", "features": [], "links": [{"rel": "next", "href": None}]},
        {"type": "FeatureCollection", "features": [], "links": [{"rel": "next"}]},
    ],
)
def test_a_page_that_could_read_as_the_last_one_fails(doc: dict[str, Any]) -> None:
    transport, _ = serve(doc)
    got, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02")
    assert got == []
    assert err is not None
    [failure] = err.failures
    assert failure.status == 200
    assert failure.url == f"{GEOCONNEX}/collections/hu02/items?f=json"


def test_a_page_without_links_is_the_last_one() -> None:
    transport, _ = serve({"type": "FeatureCollection", "features": []})
    got, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02")
    assert err is None
    assert [p.feature_count for p in got] == [0]


@pytest.mark.parametrize(
    "href",
    [
        "http://reference.geoconnex.us/collections/hu02/items?offset=10",
        "https://reference.geoconnex.us:8443/collections/hu02/items?offset=10",
        "https://elsewhere.test/collections/hu02/items?offset=10",
    ],
)
def test_a_next_link_off_the_base_origin_is_refused(href: str) -> None:
    doc = hu02()[0]
    doc["links"] = [{"rel": "next", "type": "application/geo+json", "href": href}]
    transport, seen = serve(doc)
    got, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02")
    assert len(got) == 1
    assert len(seen) == 1, "the key header would follow the link"
    assert err is not None
    assert err.failures[0].url == href
    assert "refused a URL outside" in str(err)
    assert err.failures[0].attempts == 0, "nothing was sent"


def test_a_relative_next_link_resolves_against_the_page() -> None:
    first = {**hu02()[0], "links": [{"rel": "next", "href": "?offset=10&f=json"}]}
    transport, seen = serve(first, without_next(hu02()[1]))
    pages, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02")
    assert err is None
    assert len(pages) == 2
    assert str(seen[1].url) == f"{GEOCONNEX}/collections/hu02/items?offset=10&f=json"


def test_an_unparsable_next_link_sends_nothing() -> None:
    first = {**hu02()[0], "links": [{"rel": "next", "href": "https://["}]}
    transport, seen = serve(first)
    pages, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02")
    assert len(pages) == 1
    assert err is not None
    assert len(seen) == 1


def test_a_repeated_next_link_stops_the_traversal() -> None:
    doc = hu02()[1]
    doc["links"] = [{"rel": "next", "type": "application/geo+json", "href": NEXT}]
    transport, seen = serve(doc, doc, doc)
    got, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02")
    assert len(got) == len(seen) == 2, "the repeat is caught before a third request"
    assert err is not None
    assert err.failures[0].url == NEXT
    assert "repeats one already followed" in str(err)


def test_a_json_next_link_wins_over_an_earlier_one() -> None:
    first, second, third = hu02()
    href = next(link["href"] for link in first["links"] if link["rel"] == "next")
    first["links"] = [
        {"rel": "next", "type": "text/html", "href": f"{GEOCONNEX}/html"},
        {"rel": "next", "type": "application/geo+json; charset=utf-8", "href": href},
    ]
    transport, seen = serve(first, second, third)
    _, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02")
    assert err is None
    assert str(seen[1].url) == href


def collected(
    service: OGCService, collection: str = "hu02", **kwargs: Any
) -> tuple[Any, Exception | None]:
    try:
        return asyncio.run(service.collect(collection, **kwargs)), None
    except PaginationError as err:
        return None, err


def test_collect_aggregates_every_feature_and_checks_the_count() -> None:
    docs = hu02()
    transport, _ = serve(*docs[:-1], without_next(docs[-1]))
    result, err = collected(OGCService(GEOCONNEX, transport=transport))
    assert err is None
    assert result.geojson["features"] == [f for d in docs for f in d["features"]]
    pagination = result.pagination
    assert pagination.completeness == "complete"
    assert pagination.checks == ("end_of_links", "count")
    assert (
        pagination.number_matched == pagination.number_returned == len(result.geojson["features"])
    )
    assert all(p.body is None and p.content is None for p in result.pages)


def test_collect_keeps_page_bytes_on_request() -> None:
    docs = hu02()
    transport, _ = serve(*docs[:-1], without_next(docs[-1]))
    result, _ = collected(OGCService(GEOCONNEX, transport=transport), keep_pages=True)
    assert all(p.body is None and p.json()["features"] for p in result.pages)


def test_collect_without_number_matched_checks_only_the_end_of_links() -> None:
    docs = [{k: v for k, v in d.items() if k != "numberMatched"} for d in hu02()]
    transport, _ = serve(*docs[:-1], without_next(docs[-1]))
    result, _ = collected(OGCService(GEOCONNEX, transport=transport))
    assert result.pagination.completeness == "complete"
    assert result.pagination.checks == ("end_of_links",)
    assert result.pagination.number_matched is None


def test_collect_reads_unknown_when_the_count_disagrees() -> None:
    docs = hu02()
    docs[0] = {**docs[0], "features": docs[0]["features"][:-1]}
    transport, _ = serve(*docs[:-1], without_next(docs[-1]))
    result, _ = collected(OGCService(GEOCONNEX, transport=transport))
    assert result.pagination.completeness == "unknown"
    assert result.pagination.checks == ("end_of_links",)


def test_collect_reads_unknown_when_pages_disagree_on_number_matched() -> None:
    docs = hu02()
    docs[1] = {**docs[1], "numberMatched": docs[1]["numberMatched"] + 1}
    transport, _ = serve(*docs[:-1], without_next(docs[-1]))
    result, _ = collected(OGCService(GEOCONNEX, transport=transport))
    assert result.pagination.completeness == "unknown"
    assert result.pagination.number_matched is None


def test_collect_reads_unknown_when_a_page_returns_fewer_features_than_it_reported() -> None:
    docs = [{k: v for k, v in d.items() if k != "numberMatched"} for d in hu02()]
    last = without_next(docs[-1])
    docs[-1] = {**last, "features": last["features"][:-1]}
    transport, _ = serve(*docs)
    result, _ = collected(OGCService(GEOCONNEX, transport=transport))
    assert result.pagination.number_matched is None
    assert result.pagination.completeness == "unknown"


def test_a_failed_later_page_raises_partial_results() -> None:
    docs = hu02()
    transport, _ = serve(docs[0], httpx2.Response(500))
    result, err = collected(OGCService(GEOCONNEX, transport=transport))
    assert result is None
    assert isinstance(err, PartialResultsError)
    assert err.result.pagination.completeness == "limited"
    assert err.result.pagination.stop_reason == "failure"
    assert err.result.geojson["features"] == docs[0]["features"]
    assert len(err.failures) == 1
    next_href = next(link["href"] for link in docs[0]["links"] if link["rel"] == "next")
    assert err.failures[0].url == next_href


@pytest.mark.parametrize(
    ("value", "completeness"),
    [(-1, "unknown"), ("4", "unknown"), (True, "unknown"), (4.0, "unknown"), (None, "complete")],
)
def test_collect_reads_a_malformed_count_as_unknown(value: object, completeness: str) -> None:
    docs = [{k: v for k, v in d.items() if k != "numberMatched"} for d in hu02()]
    docs[-1] = {**without_next(docs[-1]), "numberReturned": value}
    transport, _ = serve(*docs)
    result, _ = collected(OGCService(GEOCONNEX, transport=transport))
    assert result.pagination.completeness == completeness


def test_a_passing_count_check_is_listed_when_another_check_fails() -> None:
    docs = hu02()
    docs[-1] = {**without_next(docs[-1]), "numberReturned": "x"}
    transport, _ = serve(*docs)
    result, _ = collected(OGCService(GEOCONNEX, transport=transport))
    assert result.pagination.completeness == "unknown"
    assert result.pagination.checks == ("end_of_links", "count")


def test_a_next_link_back_to_the_first_request_is_a_repeat() -> None:
    doc = hu02()[0]
    first = f"{GEOCONNEX}/collections/hu02/items?f=json&limit=10"
    links = [{**link, "href": first} if link["rel"] == "next" else link for link in doc["links"]]
    transport, seen = serve({**doc, "links": links}, doc)
    _, err = collected(OGCService(GEOCONNEX, transport=transport), params={"limit": "10"})
    assert [str(r.url) for r in seen] == [first]
    assert isinstance(err, PartialResultsError)
    assert err.result.geojson["features"] == doc["features"]


def test_a_next_link_with_a_credential_is_a_page_failure() -> None:
    doc = hu02()[0]
    links = [
        {**link, "href": f"{link['href']}&api_key=SECRET"} if link["rel"] == "next" else link
        for link in doc["links"]
    ]
    transport, seen = serve({**doc, "links": links})
    _, err = collected(OGCService(GEOCONNEX, transport=transport))
    assert isinstance(err, PartialResultsError)
    assert len(seen) == 1
    assert err.result.geojson["features"] == doc["features"]
    assert err.failures[0].url is not None
    assert "SECRET" not in err.failures[0].url
    assert "api_key=REDACTED" in err.failures[0].url


@pytest.mark.parametrize("clone", [copy.copy, lambda e: pickle.loads(pickle.dumps(e))])
def test_partial_results_error_round_trip(clone: Any) -> None:
    """The error crosses process pools and Dask workers with its partial result."""
    transport, _ = serve(hu02()[0], httpx2.Response(500))
    _, err = collected(OGCService(GEOCONNEX, transport=transport))
    assert isinstance(err, PartialResultsError)
    restored = clone(err)
    assert type(restored) is PartialResultsError
    assert str(restored) == str(err)
    assert restored.result == err.result
    assert [vars(f) for f in restored.failures] == [vars(f) for f in err.failures]


def test_a_failed_first_page_raises_pagination_error() -> None:
    transport, _ = serve(httpx2.Response(500))
    _, err = collected(OGCService(GEOCONNEX, transport=transport))
    assert type(err) is PaginationError


def test_collect_records_the_crs_each_page_named() -> None:
    docs = hu02()
    docs[-1] = without_next(docs[-1])
    named = {"Content-Crs": "<http://www.opengis.net/def/crs/OGC/1.3/CRS84>"}
    transport, _ = serve(*(httpx2.Response(200, json=d, headers=named) for d in docs))
    result, _ = collected(OGCService(GEOCONNEX, transport=transport))
    assert result.crs.source == "content_crs"
    assert result.crs.page_crs == (result.crs.effective,) * 3
    assert result.crs.effective == "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
    assert result.crs.requested is None


FILTER = b'{"op": "=", "args": [{"property": "name"}, "x"]}'


def post_traversal(service: OGCService) -> tuple[list[Page], PaginationError | None]:
    got: list[Page] = []

    async def run() -> None:
        url = f"{GEOCONNEX}/collections/hu02/items"
        request = Request(url=url, method="POST", params={"f": "json"}, body=FILTER)
        async for page in service._traverse(request):
            got.append(page)  # noqa: PERF401

    try:
        asyncio.run(run())
    except PaginationError as err:
        return got, err
    return got, None


def test_a_post_traversal_sends_its_body_to_every_next_link() -> None:
    """A GET to the next link of a CQL2 POST drops the filter (D-38)."""
    docs = hu02()
    transport, seen = serve(*docs[:-1], without_next(docs[-1]))
    pages, err = post_traversal(OGCService(GEOCONNEX, transport=transport))
    assert err is None
    assert [(r.method, r.content) for r in seen] == [("POST", FILTER)] * 3
    nexts = [next(link["href"] for link in d["links"] if link["rel"] == "next") for d in docs[:-1]]
    assert [str(r.url) for r in seen[1:]] == nexts
    assert [(p.method, p.request_body) for p in pages] == [("POST", FILTER)] * 3


@pytest.mark.parametrize(
    "reply",
    [
        httpx2.Response(500),
        httpx2.Response(200, content=b"<html>gateway</html>"),
        httpx2.Response(200, json={"code": "InvalidParameterValue"}),
        httpx2.Response(200, json={"type": "FeatureCollection", "features": [], "links": {}}),
    ],
)
def test_a_failed_post_page_names_its_method_and_body(reply: httpx2.Response) -> None:
    transport, _ = serve(hu02()[0], reply)
    pages, err = post_traversal(OGCService(GEOCONNEX, transport=transport))
    assert len(pages) == 1
    assert err is not None
    assert (err.failures[0].method, err.failures[0].request_body) == ("POST", FILTER)


def first_request(**query: Any) -> httpx2.Request:
    """Run a one-page traversal with ``query`` and return its request."""
    transport, seen = serve(without_next(hu02()[0]))
    _, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02", **query)
    assert err is None
    (request,) = seen
    return request


@pytest.mark.parametrize(
    ("bbox", "sent"),
    [
        ((-77.123456789, 38.8, -76.9, 39), "-77.123456789,38.8,-76.9,39.0"),
        ((1e-7, -1e-7, 0.5, 0.5), "0.0000001,-0.0000001,0.5,0.5"),
        ((170, -10, -170, 10), "170.0,-10.0,-170.0,10.0"),
        ((-77, 38, 0, -76, 39, 100), "-77.0,38.0,0.0,-76.0,39.0,100.0"),
    ],
)
def test_a_bbox_is_sent_with_every_digit(bbox: tuple[float, ...], sent: str) -> None:
    request = first_request(bbox=bbox)
    assert (request.method, request.content) == ("GET", b"")
    assert dict(request.url.params) == {"f": "json", "bbox": sent}


class Shape:
    """A stand-in for a shapely geometry."""

    __geo_interface__: Any = {"type": "Point", "coordinates": (-77.5, 38.5)}


@pytest.mark.parametrize(
    ("geometry", "sent"),
    [
        (
            {"type": "Polygon", "coordinates": [[[-78, 38], [-77, 38], [-77.5, 39.25], [-78, 38]]]},
            "-78.0,38.0,-77.0,39.25",
        ),
        (
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [[[0, 0], [1, 0], [1, 1], [0, 0]]],
                    [[[5, -2, 9], [6, -2, 9], [6, -1, 9], [5, -2, 9]]],
                ],
            },
            "0.0,-2.0,6.0,1.0",
        ),
        (Shape(), "-77.5,38.5,-77.5,38.5"),
        (
            {
                "type": "GeometryCollection",
                "geometries": [
                    {"type": "Point", "coordinates": [-77, 38]},
                    {
                        "type": "GeometryCollection",
                        "geometries": [
                            {"type": "LineString", "coordinates": [[-76, 39], [-75.5, 38.5]]}
                        ],
                    },
                ],
            },
            "-77.0,38.0,-75.5,39.0",
        ),
    ],
)
def test_a_geometry_sends_its_bbox(geometry: object, sent: str) -> None:
    assert first_request(geometry=geometry).url.params["bbox"] == sent


@pytest.mark.parametrize(
    "query",
    [
        {"bbox": (0, 0, 1)},
        {"bbox": (0, float("nan"), 1, 1)},
        {"bbox": (True, 0, 1, 1)},
        {"bbox": (0, 2, 1, 1)},
        {"bbox": (0, 0, 100, 1, 1, 50)},
        {"bbox": "0,0,1,1"},
        {"bbox": (0, 0, 1, 1), "geometry": Shape()},
        {"bbox": (0, 0, 1, 1), "params": {"bbox": "0,0,1,1"}},
        {"geometry": {"type": "Polygon", "coordinates": []}},
        {"geometry": {"type": "GeometryCollection", "geometries": []}},
        {"geometry": {"type": "GeometryCollection", "geometries": [{"type": "Point"}]}},
        {"geometry": {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}}},
        {"geometry": {"type": "Point"}},
        {"geometry": {"type": "Point", "coordinates": [0]}},
        {"geometry": {"type": "Point", "coordinates": ["0", 1]}},
    ],
)
def test_a_malformed_spatial_query_sends_nothing(query: dict[str, Any]) -> None:
    transport, seen = serve()
    with pytest.raises(ValueError, match=r"bbox|coordinate|geometry|position"):
        traverse(OGCService(GEOCONNEX, transport=transport), "hu02", **query)
    assert seen == []


def test_collect_sends_the_bbox() -> None:
    transport, seen = serve(without_next(hu02()[0]))
    service = OGCService(GEOCONNEX, transport=transport)
    asyncio.run(service.collect("hu02", params={"limit": "5"}, bbox=(-78, 38, -77, 39)))
    assert dict(seen[0].url.params) == {"f": "json", "limit": "5", "bbox": "-78.0,38.0,-77.0,39.0"}


SITES = {"op": "in", "args": [{"property": "monitoring_location_id"}, ["USGS-1", "USGS-2"]]}
SITES_BODY = b'{"op":"in","args":[{"property":"monitoring_location_id"},["USGS-1","USGS-2"]]}'


def test_a_text_filter_goes_in_a_get() -> None:
    text = "parameter_code = '00060' AND S_INTERSECTS(geometry, POINT(-77 38))"
    request = first_request(filter=text)
    assert (request.method, request.content) == ("GET", b"")
    assert "content-type" not in request.headers
    assert dict(request.url.params) == {"f": "json", "filter": text}


def test_a_json_filter_is_posted_to_every_page() -> None:
    docs = hu02()
    transport, seen = serve(*docs[:-1], without_next(docs[-1]))
    pages, err = traverse(OGCService(GEOCONNEX, transport=transport), "hu02", filter=SITES)
    assert err is None
    assert [(r.method, r.content) for r in seen] == [("POST", SITES_BODY)] * 3
    assert {r.headers["content-type"] for r in seen} == {"application/query-cql-json"}
    assert dict(seen[0].url.params) == {"f": "json"}
    nexts = [next(link["href"] for link in d["links"] if link["rel"] == "next") for d in docs[:-1]]
    assert [str(r.url) for r in seen[1:]] == nexts
    assert [(p.method, p.request_body) for p in pages] == [("POST", SITES_BODY)] * 3


def test_a_spatial_predicate_and_a_bbox_combine_in_one_post() -> None:
    within = {
        "op": "s_intersects",
        "args": [{"property": "geometry"}, {"type": "Point", "coordinates": [-77.5, 38.5]}],
    }
    request = first_request(filter=within, bbox=(-78, 38, -77, 39), params={"limit": "5"})
    assert request.method == "POST"
    assert request.content == (
        b'{"op":"s_intersects","args":[{"property":"geometry"},'
        b'{"type":"Point","coordinates":[-77.5,38.5]}]}'
    )
    assert dict(request.url.params) == {"f": "json", "limit": "5", "bbox": "-78.0,38.0,-77.0,39.0"}


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ({"filter": ""}, ValidationError),
        ({"filter": {}}, ValidationError),
        ({"filter": "a = 1", "params": {"filter": "b = 2"}}, ValidationError),
        ({"filter": SITES, "params": {"filter-lang": "cql2-text"}}, ValidationError),
        ({"filter": {"op": "=", "args": [{"property": "x"}, float("nan")]}}, ValidationError),
        ({"filter": {"op": "<", "args": [{"property": "x"}, float("-inf")]}}, ValidationError),
        ({"filter": {"op": "in", "args": [{"property": "x"}, {1, 2}]}}, TypeError),
    ],
)
def test_a_malformed_filter_sends_nothing(query: dict[str, Any], error: type[Exception]) -> None:
    transport, seen = serve()
    with pytest.raises(error):
        traverse(OGCService(GEOCONNEX, transport=transport), "hu02", **query)
    assert seen == []


def test_a_read_only_mapping_filter_is_posted() -> None:
    sites = MappingProxyType(
        {"op": "in", "args": [MappingProxyType(SITES["args"][0]), SITES["args"][1]]}
    )
    assert first_request(filter=sites).content == SITES_BODY


def test_collect_posts_a_json_filter() -> None:
    transport, seen = serve(without_next(hu02()[0]))
    service = OGCService(GEOCONNEX, transport=transport)
    result = asyncio.run(service.collect("hu02", filter=SITES))
    assert (seen[0].method, seen[0].content) == ("POST", SITES_BODY)
    assert (result.pages[0].method, result.pages[0].request_body) == ("POST", SITES_BODY)


def test_ids_are_posted_as_an_in_filter_with_their_types() -> None:
    request = first_request(ids=["01646500", 7], id_property="staid")
    assert (request.method, request.headers["content-type"]) == (
        "POST",
        "application/query-cql-json",
    )
    assert request.content == b'{"op":"in","args":[{"property":"staid"},["01646500",7]]}'


@pytest.mark.parametrize(
    "query",
    [
        {"ids": ["USGS-1"]},
        {"id_property": "id"},
        {"ids": "USGS-1", "id_property": "id"},
        {"ids": [], "id_property": "id"},
        {"ids": b"01", "id_property": "id"},
        {"ids": (i for i in ()), "id_property": "id"},
        {"ids": ["USGS-1"], "id_property": ""},
        {"ids": ["USGS-1"], "id_property": "id", "filter": "a = 1"},
        {"ids": ["USGS-1"], "id_property": "id", "params": {"filter": "a = 1"}},
    ],
)
def test_a_malformed_identifier_query_sends_nothing(query: dict[str, Any]) -> None:
    transport, seen = serve()
    with pytest.raises(ValueError, match=r"ids|filter"):
        traverse(OGCService(GEOCONNEX, transport=transport), "hu02", **query)
    assert seen == []


def test_collect_posts_ids() -> None:
    transport, seen = serve(without_next(hu02()[0]))
    service = OGCService(GEOCONNEX, transport=transport)
    asyncio.run(service.collect("hu02", ids=["01"], id_property="id"))
    assert seen[0].content == b'{"op":"in","args":[{"property":"id"},["01"]]}'


def test_an_item_is_one_page() -> None:
    recorded = record("nwis/monitoring-locations/item-USGS-01646500.json")
    response = recorded["response"]
    transport, seen = serve(
        httpx2.Response(200, json=response["body"], headers=response["headers"])
    )
    page = asyncio.run(
        OGCService(BASE, transport=transport).item("monitoring-locations", "USGS-01646500")
    )
    assert [str(r.url) for r in seen] == [recorded["request"]["url"]]
    assert page.body == response["body"]
    assert page.content is not None
    assert json.loads(page.content) == response["body"]
    assert (page.feature_count, page.number_matched, page.number_returned) == (1, None, None)
    assert (page.method, page.request_body, page.params) == ("GET", None, {"f": "json"})
    assert [link.rel for link in page.links] == [link["rel"] for link in response["body"]["links"]]
    assert page.headers["content-crs"] == response["headers"]["content-crs"]


def test_an_item_sends_its_params() -> None:
    crs = "http://www.opengis.net/def/crs/EPSG/0/4326"
    transport, seen = serve({"type": "Feature", "geometry": None, "properties": {}})
    service = OGCService(BASE, transport=transport)
    page = asyncio.run(service.item("c", "x", params={"crs": crs, "skipGeometry": "true"}))
    assert dict(seen[0].url.params) == {"f": "json", "crs": crs, "skipGeometry": "true"}
    assert page.params == {"f": "json", "crs": crs, "skipGeometry": "true"}


@pytest.mark.parametrize("segment", ["", ".", ".."])
def test_a_path_segment_name_sends_nothing(segment: str) -> None:
    """httpx2 normalizes /collections/../items to the landing page's /items."""
    transport, seen = serve()
    service = OGCService(BASE, transport=transport)
    for call in (
        service.item("c", segment),
        service.item(segment, "x"),
        service.collect(segment),
    ):
        with pytest.raises(ValueError, match="would address another path"):
            asyncio.run(call)
    assert seen == []


def test_an_item_id_is_percent_encoded() -> None:
    transport, seen = serve({"type": "Feature", "geometry": None, "properties": {}})
    asyncio.run(OGCService(BASE, transport=transport).item("a b", "x/y?z"))
    assert seen[0].url.raw_path == b"/ogcapi/v1/collections/a%20b/items/x%2Fy%3Fz?f=json"


@pytest.mark.parametrize(
    ("reply", "status"),
    [
        (httpx2.Response(404, json={"code": "NotFound"}), 404),
        (httpx2.Response(200, json=hu02()[0]), 200),
        (httpx2.Response(200, json={"type": "Feature", "links": {}}), 200),
    ],
)
def test_an_item_that_is_not_a_feature_fails(reply: httpx2.Response, status: int) -> None:
    transport, _ = serve(reply)
    with pytest.raises(ServiceError) as excinfo:
        asyncio.run(OGCService(BASE, transport=transport).item("monitoring-locations", "x"))
    assert excinfo.value.status == status


class Keyed(OGCService):
    """A service class that adds a key to every request."""

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": "k"}


def test_service_headers_reach_every_request() -> None:
    feature = {"type": "Feature", "geometry": None, "properties": {}}
    page = without_next(hu02()[0])
    transport, seen = serve(body("nwis/collections.json"), feature, page, page)
    service = Keyed(BASE, transport=transport)

    async def run() -> None:
        await service.collections()
        await service.item("c", "x")
        await service.collect("c")
        await service.collect("c", filter=SITES)

    asyncio.run(run())
    assert [r.headers.get("x-api-key") for r in seen] == ["k"] * 4
    assert seen[3].headers["content-type"] == "application/query-cql-json"


@pytest.mark.parametrize(
    "url",
    [
        f"{GEOCONNEX}/collections/hu02/items",
        "http://api.waterdata.usgs.gov/ogcapi/v1/collections/c/items",
    ],
)
def test_a_first_url_off_the_base_origin_sends_nothing(url: str) -> None:
    """A resume replaying a stored URL must not carry the key to another host (D-40)."""
    transport, seen = serve()

    async def run() -> None:
        async for _ in Keyed(BASE, transport=transport)._traverse(Request(url=url)):
            pass

    with pytest.raises(PaginationError, match="refused a URL outside"):
        asyncio.run(run())
    assert seen == []


def retried(*replies: httpx2.Response) -> OGCService:
    """A service whose transport retries without waiting."""
    queue = list(replies)
    transport = Transport(transport=httpx2.MockTransport(lambda _: queue.pop(0)), backoff=0)
    return OGCService(BASE, transport=transport)


def test_a_body_that_fails_validation_reports_every_attempt() -> None:
    html = httpx2.Response(200, content=b"<html>gateway</html>")
    service = retried(httpx2.Response(503), httpx2.Response(503), html)
    _, err = traverse(service, "c")
    assert err is not None
    assert err.failures[0].attempts == 3
    service = retried(httpx2.Response(503), httpx2.Response(200, json=hu02()[0]))
    with pytest.raises(ServiceError) as excinfo:
        asyncio.run(service.item("c", "x"))
    assert excinfo.value.attempts == 2


def test_pagination_records_the_requested_sort_key() -> None:
    docs = hu02()
    transport, _ = serve(docs[0], without_next(docs[1]), docs[0], httpx2.Response(500))
    service = OGCService(GEOCONNEX, transport=transport)

    async def run() -> tuple[QueryResult, QueryResult]:
        result = await service.collect("hu02", params={"sortby": "huc2"})
        try:
            await service.collect("hu02", params={"sortby": "-huc2"})
        except PartialResultsError as err:
            return result, err.result
        raise AssertionError

    complete, partial = asyncio.run(run())
    assert (complete.pagination.sort_by, partial.pagination.sort_by) == ("huc2", "-huc2")


class Capped(OGCService):
    """A service whose profile sends 10 features a page by default and accepts 50."""

    profile = ServiceProfile(base_url=GEOCONNEX, default_limit=10, max_limit=50)


@pytest.mark.parametrize(
    ("params", "sent"), [(None, "10"), ({"limit": "50"}, "50"), ({"limit": 50}, "50")]
)
def test_a_profile_sets_the_default_limit(params: dict[str, Any] | None, sent: str) -> None:
    transport, seen = serve(without_next(hu02()[0]))
    _, err = traverse(Capped(GEOCONNEX, transport=transport), "hu02", params)
    assert err is None
    assert dict(seen[0].url.params) == {"f": "json", "limit": sent}


@pytest.mark.parametrize("limit", ["0", "51", "", "ten", "-1", "1e3", "\u0661", 51, True])
def test_a_limit_outside_the_profile_sends_nothing(limit: object) -> None:
    transport, seen = serve()
    with pytest.raises(ValueError, match="limit takes an integer from 1 to 50"):
        traverse(Capped(GEOCONNEX, transport=transport), "hu02", {"limit": limit})
    assert seen == []


def test_a_profile_supplies_the_base_url() -> None:
    assert Capped().base_url == GEOCONNEX
    with pytest.raises(TypeError, match="base_url is required"):
        OGCService()


class Split(OGCService):
    """A service that splits every items query into two pieces by year."""

    def _pieces(self, request: Request) -> list[Request]:
        return [
            dataclasses.replace(request, params={**request.params, "datetime": year})
            for year in ("2020", "2021")
        ]


def test_a_split_query_traverses_each_piece_in_order() -> None:
    page = without_next(hu02()[0])
    transport, seen = serve(page, page)
    pages, err = traverse(Split(GEOCONNEX, transport=transport), "hu02", bbox=(-78, 38, -77, 39))
    assert err is None
    assert len(pages) == 2
    assert [dict(r.url.params) for r in seen] == [
        {"f": "json", "bbox": "-78.0,38.0,-77.0,39.0", "datetime": year}
        for year in ("2020", "2021")
    ]
