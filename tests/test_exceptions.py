"""Tests for the exception types."""

from __future__ import annotations

import asyncio
import copy
import pickle
from typing import TYPE_CHECKING, Any

import pytest

from water_ogcapi import Transport, configure_logger
from water_ogcapi.aio import NWIS, OGCService
from water_ogcapi.exceptions import (
    PaginationError,
    RateLimitError,
    ServiceError,
    ValidationError,
    WaterOGCAPIError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "https://example.com/collections"


@pytest.mark.parametrize(
    "error",
    [
        ServiceError("service unavailable", URL, status=503, attempts=4, retry_after=2.0),
        RateLimitError("rate limit exceeded", URL, retry_after=3.0, remaining=0, attempts=2),
        ServiceError("bad filter", URL, status=400, method="POST", request_body=b"{}"),
        ServiceError("bad where", URL, status=400, response_body=b'{"error": {"code": 400}}'),
        RateLimitError("rate limit exceeded", URL, response_body=b"slow down"),
    ],
)
@pytest.mark.parametrize("clone", [copy.copy, lambda e: pickle.loads(pickle.dumps(e))])
def test_round_trip_keeps_message_and_attributes(
    error: ServiceError, clone: Callable[[ServiceError], ServiceError]
) -> None:
    """Exceptions cross process pools and Dask workers by pickling."""
    restored = clone(error)
    assert type(restored) is type(error)
    assert str(restored) == str(error)
    assert vars(restored) == vars(error)


def test_message_carries_status_attempts_and_url() -> None:
    error = RateLimitError("rate limit exceeded", URL, attempts=2)
    assert str(error) == f"HTTP 429: rate limit exceeded (after 2 attempts)\nURL: {URL}"


@pytest.mark.parametrize("clone", [copy.copy, lambda e: pickle.loads(pickle.dumps(e))])
def test_pagination_error_round_trip(clone: Callable[[PaginationError], PaginationError]) -> None:
    error = PaginationError([ServiceError("service unavailable", URL, status=503)])
    restored = clone(error)
    assert str(restored) == f"1 page request failed\nHTTP 503: service unavailable\nURL: {URL}"
    assert [vars(f) for f in restored.failures] == [vars(f) for f in error.failures]


def test_a_service_error_masks_a_credential_in_its_url() -> None:
    """A resume replaying a stored URL reaches ServiceError before the transport's check."""
    err = PaginationError([ServiceError("refused", f"{URL}?api_key=SECRET&f=json", attempts=0)])
    for text in (str(err), repr(err.failures[0]), pickle.dumps(err).decode("latin-1")):
        assert "SECRET" not in text
    assert err.failures[0].url == f"{URL}?api_key=REDACTED&f=json"


@pytest.mark.parametrize(
    "check",
    [
        lambda: OGCService("https://example.com").iter_pages("c", bbox=[1, 2, 3]).__anext__(),
        lambda: OGCService("https://example.com").item("..", "x"),
        lambda: NWIS(api_key="", transport=None).collect("c", params={"limit": "0"}),
        lambda: Transport(max_concurrent=0),
        lambda: NWIS(api_key="key\n"),
        lambda: configure_logger(level="LOUD"),
    ],
    ids=["bbox", "path-segment", "limit", "transport", "key", "log-level"],
)
def test_input_errors_are_validation_errors(check: Any) -> None:
    async def run() -> None:
        result = check()
        if asyncio.iscoroutine(result):
            await result

    with pytest.raises(ValidationError) as excinfo:
        asyncio.run(run())
    assert isinstance(excinfo.value, WaterOGCAPIError)
    assert isinstance(excinfo.value, ValueError)
