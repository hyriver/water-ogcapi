"""Tests for the base service client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from water_ogcapi import OGCService
from water_ogcapi._transport import Transport
from water_ogcapi.exceptions import ServiceError

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
