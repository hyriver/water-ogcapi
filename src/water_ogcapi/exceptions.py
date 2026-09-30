"""Exceptions for water_ogcapi."""

from __future__ import annotations

__all__ = [
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
    """

    def __init__(
        self,
        message: str,
        url: str | None = None,
        *,
        status: int | None = None,
        attempts: int = 1,
        retry_after: float | None = None,
    ) -> None:
        # args holds the bare message: pickle and copy rebuild the instance from args
        # and then restore the attributes, so a formatted args[0] gained its prefix twice.
        super().__init__(message)
        self.message = message
        self.url = url
        self.status = status
        self.attempts = attempts
        self.retry_after = retry_after

    def __str__(self) -> str:
        prefix = f"HTTP {self.status}: " if self.status is not None else ""
        full = f"{prefix}{self.message}"
        if self.attempts > 1:
            full = f"{full} (after {self.attempts} attempts)"
        if self.url is not None:
            full = f"{full}\nURL: {self.url}"
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
    """

    def __init__(
        self,
        message: str,
        url: str | None = None,
        *,
        retry_after: float | None = None,
        remaining: int | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message, url, status=429, attempts=attempts, retry_after=retry_after)
        self.remaining = remaining
