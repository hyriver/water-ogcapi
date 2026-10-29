"""Exceptions for water_ogcapi."""

from __future__ import annotations

from typing import TYPE_CHECKING

from water_ogcapi._redact import redact_url

if TYPE_CHECKING:
    from collections.abc import Sequence

    from water_ogcapi._query import Checkpoint
    from water_ogcapi._results import QueryResult

__all__ = [
    "PaginationError",
    "PartialResultsError",
    "RateLimitError",
    "ServiceError",
    "ValidationError",
    "WaterOGCAPIError",
]


class WaterOGCAPIError(Exception):
    """Base exception for all water_ogcapi errors."""


class ValidationError(WaterOGCAPIError, ValueError):
    """Raised before a request is sent when caller input is invalid (docs/design.md D-45).

    Its message names the valid options where a finite set exists, such as the
    collections of a service or the properties a query can filter on.
    """


class ServiceError(WaterOGCAPIError):
    """Raised when a request fails: an error status, a redirect, a body of the wrong shape,
    or a URL refused before sending (``attempts`` 0).

    Parameters
    ----------
    message : str
        Description of the failure.
    url : str, optional
        The URL that triggered the error, included in the message when given, with
        credential query values masked (D-04).
    status : int, optional
        HTTP status code from the failed response, when available.
    attempts : int, optional
        Requests made before giving up, defaults to 1; 0 when the request was refused
        before sending. Anything above 1 means the transport retried, so an outer
        retry budget can tell a single failure from an exhausted one.
    retry_after : float, optional
        Seconds the server asked the caller to wait, parsed from ``Retry-After``.
    method : str, optional
        The request's HTTP method, defaults to ``GET``.
    request_body : bytes, optional
        The request body as sent, ``None`` for a request without one (D-38).
    response_body : bytes, optional
        The body of the 4xx or 5xx response, its first 64 KiB as the server sent them
        (D-68), so a server's reason for refusing a request reaches the caller. ``None``
        when no error response arrived. ``str()`` leaves it out.
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
        response_body: bytes | None = None,
    ) -> None:
        # args holds the bare message: pickle and copy rebuild the instance from args
        # and then restore the attributes, so a formatted args[0] gained its prefix twice.
        super().__init__(message)
        self.message = message
        self.url = None if url is None else redact_url(url)
        self.status = status
        self.attempts = attempts
        self.retry_after = retry_after
        self.method = method
        self.request_body = request_body
        self.response_body = response_body

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
    response_body : bytes, optional
        The body of the 429 response, as for `ServiceError`.
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
        response_body: bytes | None = None,
    ) -> None:
        super().__init__(
            message,
            url,
            status=429,
            attempts=attempts,
            retry_after=retry_after,
            method=method,
            request_body=request_body,
            response_body=response_body,
        )
        self.remaining = remaining


class PaginationError(WaterOGCAPIError):
    """Raised when page requests of a traversal fail (docs/design.md D-24).

    Every page that succeeded has already been yielded, so the error holds no page.

    Parameters
    ----------
    failures : sequence of ServiceError
        One error per failed request, each naming the request by its URL as sent.
    checkpoint : Checkpoint, optional
        Where the query stands, the failed requests first, for ``resume`` (D-52).
        ``None`` when nothing but refused requests remains, since a resume would
        refuse them again.
    """

    def __init__(
        self, failures: Sequence[ServiceError], checkpoint: Checkpoint | None = None
    ) -> None:
        self.failures = tuple(failures)
        self.checkpoint = checkpoint
        super().__init__(self.failures)
        # copy and pickle rebuild the instance as cls(*args).
        self.args = (self.failures, checkpoint)

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
        ``failure`` (docs/design.md D-24). Its ``checkpoint``, also this error's, lets
        ``collect(resume=result)`` continue it (D-52).
    """

    def __init__(self, failures: Sequence[ServiceError], result: QueryResult) -> None:
        super().__init__(failures, result.checkpoint)
        self.result = result
        # copy and pickle rebuild the instance as cls(*args).
        self.args = (self.failures, result)
