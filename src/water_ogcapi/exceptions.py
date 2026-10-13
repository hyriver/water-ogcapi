"""Exceptions for water_ogcapi."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from water_ogcapi._results import QueryResult

__all__ = [
    "PaginationError",
    "PartialResultsError",
    "RateLimitError",
    "ServiceError",
    "WaterOGCAPIError",
]


class WaterOGCAPIError(Exception):
    """Base exception for all water_ogcapi errors."""


class ServiceError(WaterOGCAPIError):
    """Raised when an OGC API service returns an error response.

    Parameters
    ----------
    message : str
        Description of the failure.
    url : str, optional
        The URL that triggered the error, included in the message when given.
    status : int, optional
        HTTP status code from the failed response, when available.
    attempts : int, optional
        Requests made before giving up, defaults to 1. Anything above 1 means the
        transport retried, so an outer retry budget can tell a single failure from
        an exhausted one.
    retry_after : float, optional
        Seconds the server asked the caller to wait, parsed from ``Retry-After``.
    method : str, optional
        The request's HTTP method, defaults to ``GET``.
    request_body : bytes, optional
        The request body as sent, ``None`` for a request without one (D-38).
    """

    def __init__(
        self,
        message: str,
        url: str | None = None,
        *,
        status: int | None = None,
        attempts: int = 1,
        retry_after: float | None = None,
        method: str = "GET",
        request_body: bytes | None = None,
    ) -> None:
        # args holds the bare message: pickle and copy rebuild the instance from args
        # and then restore the attributes, so a formatted args[0] gained its prefix twice.
        super().__init__(message)
        self.message = message
        self.url = url
        self.status = status
        self.attempts = attempts
        self.retry_after = retry_after
        self.method = method
        self.request_body = request_body

    def __str__(self) -> str:
        prefix = f"HTTP {self.status}: " if self.status is not None else ""
        full = f"{prefix}{self.message}"
        if self.attempts > 1:
            full = f"{full} (after {self.attempts} attempts)"
        if self.url is not None:
            full = f"{full}\nURL: {self.url}"
        if self.method != "GET":
            full = f"{full}\nMethod: {self.method}"
        return full


class RateLimitError(ServiceError):
    """Raised when an OGC API service returns HTTP 429 Too Many Requests.

    Parameters
    ----------
    message : str
        Description of the failure.
    url : str, optional
        The URL that triggered the error.
    retry_after : float, optional
        Seconds to wait before retrying, parsed from the ``Retry-After``
        header. Both integer-second and HTTP-date forms are supported.
    remaining : int, optional
        Value of the ``x-ratelimit-remaining`` header when present.
    attempts : int, optional
        Requests made before giving up, defaults to 1.
    method : str, optional
        The request's HTTP method, defaults to ``GET``.
    request_body : bytes, optional
        The request body as sent, ``None`` for a request without one (D-38).
    """

    def __init__(
        self,
        message: str,
        url: str | None = None,
        *,
        retry_after: float | None = None,
        remaining: int | None = None,
        attempts: int = 1,
        method: str = "GET",
        request_body: bytes | None = None,
    ) -> None:
        super().__init__(
            message,
            url,
            status=429,
            attempts=attempts,
            retry_after=retry_after,
            method=method,
            request_body=request_body,
        )
        self.remaining = remaining


class PaginationError(WaterOGCAPIError):
    """Raised when page requests of a traversal fail (docs/design.md D-24).

    Every page that succeeded has already been yielded, so the error holds no page.

    Parameters
    ----------
    failures : sequence of ServiceError
        One error per failed request, each naming the request by its URL as sent.
    """

    def __init__(self, failures: Sequence[ServiceError]) -> None:
        self.failures = tuple(failures)
        super().__init__(self.failures)

    def __str__(self) -> str:
        count = len(self.failures)
        head = f"{count} page request{'' if count == 1 else 's'} failed"
        return "\n".join([head, *map(str, self.failures)])


class PartialResultsError(PaginationError):
    """Raised by ``collect()`` when a page failed after at least one page succeeded.

    Parameters
    ----------
    failures : sequence of ServiceError
        One error per failed request, each naming the request by its URL as sent.
    result : QueryResult
        Every page that succeeded, with ``completeness`` ``limited`` and stop reason
        ``failure`` (docs/design.md D-24).
    """

    def __init__(self, failures: Sequence[ServiceError], result: QueryResult) -> None:
        super().__init__(failures)
        self.result = result
        # copy and pickle rebuild the instance as cls(*args).
        self.args = (self.failures, result)
