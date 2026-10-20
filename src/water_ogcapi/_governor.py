"""The quota governor, which spaces request starts as a service's quota runs down (D-06)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from water_ogcapi._logging import logger
from water_ogcapi._transport import MAX_SLEEP, parse_count, parse_retry_after
from water_ogcapi.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["RAMP", "STATIC_INTERVAL", "Governor"]

# Seconds between request starts while no recent response has carried usable counts. Neither
# GeoConnex nor unkeyed NWIS documents a limit, and this keeps either under 3,600 an hour.
STATIC_INTERVAL = 1.0
# The share of the quota left when pacing starts. The ramp settles near 89% of a rolling
# quota; starting at the whole quota would settle at 62% (D-49).
RAMP = 1 / 8


def _cached(headers: Mapping[str, str]) -> bool:
    """Whether a CDN served the response, so its counts are the cached response's (D-30)."""
    return "hit" in headers.get("x-cache", "").lower() or bool(parse_count(headers.get("age")))


class Governor:
    """Spaces the starts of one service's requests so its quota lasts (D-06).

    `Transport.request` calls `wait` before each attempt and `observe` on each response,
    retries included. Each ``X-RateLimit-Limit`` value keeps its own reading, since a
    default NWIS key counts ``/items`` and metadata separately, and the widest interval
    of the readings from the last ``window`` seconds applies; with none, the static
    ``interval`` does. With ``remaining`` of ``limit`` left, the interval is
    ``window / remaining - 8 * window / limit`` seconds, from 0 at an eighth of the quota
    to at most 60.

    Parameters
    ----------
    window : float or None
        Seconds over which the service's ``X-RateLimit-Limit`` counts. ``None`` ignores
        rate-limit headers, for a service that sends none.
    interval : float, optional
        Seconds between request starts while no reading from the last ``window`` seconds
        applies, defaults to 1. The first request never waits.

    Raises
    ------
    ValidationError
        If ``window`` is not positive or ``interval`` is negative.
    """

    def __init__(self, window: float | None, *, interval: float = STATIC_INTERVAL) -> None:
        if window is not None and not window > 0:
            msg = f"window must be positive, got {window}"
            raise ValidationError(msg)
        if not interval >= 0:
            msg = f"interval must not be negative, got {interval}"
            raise ValidationError(msg)
        self._window = window
        self._interval = interval
        self._last = float("-inf")
        self._hold = float("-inf")
        # X-RateLimit-Limit -> (interval, monotonic time of the reading)
        self._readings: dict[int, tuple[float, float]] = {}

    @property
    def window(self) -> float | None:
        """Seconds over which the service's ``X-RateLimit-Limit`` counts."""
        return self._window

    @property
    def interval(self) -> float:
        """Seconds between request starts while no recent reading applies."""
        return self._interval

    def _spacing(self, now: float) -> float:
        window = self._window or 0.0
        live = [pace for pace, seen in self._readings.values() if now - seen < window]
        return max(live) if live else self._interval

    async def wait(self) -> None:
        """Wait until the request may start: one interval after the previous start.

        A reading or a 429 that arrives during the wait applies to it, and a cancelled
        wait books nothing.
        """
        while True:
            now = time.monotonic()
            start = max(self._hold, self._last + self._spacing(now))
            if start <= now:
                # shortcut: a caller that then waits for a transport permit starts late,
                # so up to max_concurrent requests can start together; book at dispatch
                # if that matters.
                self._last = now
                return
            delay = start - now
            level = logging.INFO if delay > self._interval else logging.DEBUG
            logger.log(level, "waiting %.1f s for the service's rate limit", delay)
            await asyncio.sleep(delay)

    def observe(self, status: int, headers: Mapping[str, str]) -> None:
        """Read the counts on one response, and a 429's ``Retry-After``.

        A response a CDN served, or one without both counts, leaves the readings as they
        were. A 429's ``Retry-After`` holds back every later start for that long, up to
        60 seconds.

        Parameters
        ----------
        status : int
            HTTP status code.
        headers : mapping of str to str
            Response headers with lowercase keys.
        """
        now = time.monotonic()
        if status == 429:
            wait = parse_retry_after(headers.get("retry-after"), headers.get("date"))
            if wait is not None:
                # The server's own wait; its zero count would stretch it to 60 s.
                self._hold = max(self._hold, now + min(wait, MAX_SLEEP))
                return
        if self._window is None or _cached(headers):
            return
        limit = parse_count(headers.get("x-ratelimit-limit"))
        remaining = parse_count(headers.get("x-ratelimit-remaining"))
        if not limit or remaining is None:
            return
        pace = self._window / remaining - self._window / (limit * RAMP) if remaining else MAX_SLEEP
        self._readings[limit] = (min(max(pace, 0.0), MAX_SLEEP), now)
