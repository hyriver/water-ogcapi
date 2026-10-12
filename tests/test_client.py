"""Tests for the base service client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from water_ogcapi import OGCService, Page
from water_ogcapi._transport import Transport
from water_ogcapi.exceptions import PaginationError, ServiceError

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://api.waterdata.usgs.gov/ogcapi/v1"


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
    assert first is second
    assert first.collections
    assert [str(r.url) for r in seen] == [f"{BASE}/collections?f=json"]


@pytest.mark.parametrize("content", [b"<html></html>", b"[]", b'{"truncated": '])
def test_body_must_be_a_json_object(content: bytes) -> None:
    transport, _ = serve(httpx2.Response(200, content=content))
    with pytest.raises(ServiceError, match="not a JSON object") as excinfo:
        asyncio.run(OGCService(BASE, transport=transport).collections())
    assert excinfo.value.status == 200
    assert excinfo.value.__context__ is None, "a JSONDecodeError holds the whole body"


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
    service: OGCService, collection: str, params: dict[str, str] | None = None
) -> tuple[list[Page], PaginationError | None]:
    """Return the pages a traversal yielded and the error that ended it, if any."""
    got: list[Page] = []

    async def run() -> None:
        # A comprehension would lose the pages yielded before an error.
        async for page in service.iter_pages(collection, params=params):
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
    assert "refused a next link" in str(err)


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
