"""Tests for the exception types."""

from __future__ import annotations

import copy
import pickle
from typing import TYPE_CHECKING

import pytest

from water_ogcapi.exceptions import RateLimitError, ServiceError

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "https://example.com/collections"


@pytest.mark.parametrize(
    "error",
    [
        ServiceError("service unavailable", URL, status=503, attempts=4, retry_after=2.0),
        RateLimitError("rate limit exceeded", URL, retry_after=3.0, remaining=0, attempts=2),
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
