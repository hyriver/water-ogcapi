"""In-memory, success-only cache of parsed metadata documents (docs/design.md D-07)."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["TTL", "CacheEntry"]

# Seconds an entry is served without asking the server.
TTL = 3600.0


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A parsed metadata document and the validators of the response that carried it.

    Parameters
    ----------
    payload : Any
        The parsed document.
    etag : str or None
        The response's ``ETag``, sent back as ``If-None-Match``.
    last_modified : str or None
        The response's ``Last-Modified``, sent back as ``If-Modified-Since``.
    stored_at : float
        ``time.monotonic()`` when the response arrived.
    """

    payload: Any
    etag: str | None
    last_modified: str | None
    stored_at: float

    @classmethod
    def store(
        cls, payload: Any, headers: Mapping[str, str], previous: CacheEntry | None = None
    ) -> CacheEntry:
        """Build an entry from response headers, keeping ``previous`` validators they omit.

        Pass ``previous`` only for a 304, which may leave its validators out.
        """
        return cls(
            payload=payload,
            etag=headers.get("etag") or (previous.etag if previous else None),
            last_modified=headers.get("last-modified")
            or (previous.last_modified if previous else None),
            stored_at=monotonic(),
        )

    def expired(self) -> bool:
        """Whether ``TTL`` seconds have passed since the response arrived."""
        return monotonic() - self.stored_at >= TTL

    def validators(self) -> dict[str, str]:
        """Return the conditional request headers that revalidate this entry."""
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers
