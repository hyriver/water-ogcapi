"""In-memory, success-only cache of parsed metadata documents (docs/design.md D-07)."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

__all__ = ["TTL", "CacheEntry"]

# Seconds an entry is served without asking the server.
TTL = 3600.0


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A parsed metadata document and when the response that carried it arrived.

    Parameters
    ----------
    payload : Any
        The parsed document.
    stored_at : float
        ``time.monotonic()`` when the response arrived.
    """

    payload: Any
    stored_at: float

    @classmethod
    def store(cls, payload: Any) -> CacheEntry:
        """Build an entry stamped now."""
        return cls(payload=payload, stored_at=monotonic())

    def expired(self) -> bool:
        """Whether ``TTL`` seconds have passed since the response arrived."""
        return monotonic() - self.stored_at >= TTL
