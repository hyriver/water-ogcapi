"""Tests for the quota governor (D-06)."""

from __future__ import annotations

import asyncio
import collections
import itertools
import logging
import types
from typing import Any

import httpx2
import pytest

from water_ogcapi import Governor, Transport, _governor
from water_ogcapi.aio import NWIS, GeoConnex, OGCService
from water_ogcapi.exceptions import PartialResultsError, ServiceError, ValidationError

URL = "https://example.com/collections"

pytestmark = pytest.mark.paced


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A fake monotonic clock that every ``asyncio.sleep`` advances, starting at 0."""
    now = [0.0]

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(_governor, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


def counts(limit: int, remaining: int, **extra: str) -> dict[str, str]:
    return {"x-ratelimit-limit": str(limit), "x-ratelimit-remaining": str(remaining), **extra}


def starts(governor: Governor, clock: list[float], n: int) -> list[float]:
    """Return the clock reading at each of ``n`` successive request starts."""

    async def run() -> list[float]:
        seen = []
        for _ in range(n):
            await governor.wait()
            seen.append(clock[0])
        return seen

    return asyncio.run(run())


def spacing(governor: Governor, clock: list[float]) -> float:
    """Return the wait between two back-to-back starts after the readings so far."""
    first, second = starts(governor, clock, 2)
    return second - first


def test_without_counts_requests_start_one_static_interval_apart(clock: list[float]) -> None:
    assert starts(Governor(60.0), clock, 3) == [0.0, 1.0, 2.0], "the first never waits"


@pytest.mark.parametrize(
    ("remaining", "wait"), [(1000, 0.0), (125, 0.0), (100, 7.2), (50, 43.2), (10, 60.0), (0, 60.0)]
)
def test_the_spacing_widens_as_the_quota_runs_down(
    clock: list[float], remaining: int, wait: float
) -> None:
    governor = Governor(3600.0)
    governor.observe(200, counts(1000, remaining))
    assert spacing(governor, clock) == pytest.approx(wait), "capped at 60 s"


@pytest.mark.parametrize("cdn", [{"x-cache": "Hit from cloudfront"}, {"age": "6425"}])
def test_a_cached_response_leaves_the_readings(clock: list[float], cdn: dict[str, str]) -> None:
    governor = Governor(3600.0)
    governor.observe(200, counts(1000, 100))
    governor.observe(200, counts(1000, 999, **cdn))
    assert spacing(governor, clock) == pytest.approx(7.2)


def test_the_most_restrictive_limit_wins(clock: list[float]) -> None:
    governor = Governor(3600.0)
    governor.observe(200, counts(1000, 100))
    governor.observe(200, counts(4000, 3990))
    assert spacing(governor, clock) == pytest.approx(7.2)


def test_a_reading_expires_after_a_full_window(clock: list[float]) -> None:
    governor = Governor(60.0)
    governor.observe(200, counts(10, 1))
    clock[0] = 60.0
    assert spacing(governor, clock) == 1.0, "the window has refilled; the static default is back"


@pytest.mark.parametrize("headers", [{}, {"x-ratelimit-remaining": "5"}, counts(0, 0)])
def test_unusable_counts_keep_the_readings(clock: list[float], headers: dict[str, str]) -> None:
    governor = Governor(3600.0)
    governor.observe(200, counts(1000, 100))
    governor.observe(200, headers)
    assert spacing(governor, clock) == pytest.approx(7.2)


def test_without_a_window_counts_are_ignored(clock: list[float]) -> None:
    governor = Governor(None)
    governor.observe(200, counts(10, 1))
    assert spacing(governor, clock) == 1.0


@pytest.mark.parametrize(("retry_after", "held"), [("7", 7.0), ("600", 60.0)])
def test_a_429_holds_back_the_next_start(clock: list[float], retry_after: str, held: float) -> None:
    governor = Governor(None, interval=0.0)
    governor.observe(429, {"retry-after": retry_after})
    assert starts(governor, clock, 1) == [held]


def test_a_429_with_retry_after_skips_its_counts(clock: list[float]) -> None:
    governor = Governor(3600.0, interval=0.0)
    governor.observe(429, {"retry-after": "7", **counts(1000, 0)})
    assert starts(governor, clock, 2) == [7.0, 7.0], "no 60 s reading from the zero count"


@pytest.mark.parametrize(("window", "interval"), [(0.0, 1.0), (-1.0, 1.0), (60.0, -1.0)])
def test_invalid_settings_raise(window: float, interval: float) -> None:
    with pytest.raises(ValidationError):
        Governor(window, interval=interval)


def test_a_wait_set_by_counts_is_logged_at_info(
    clock: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    governor = Governor(3600.0)
    governor.observe(200, counts(1000, 100))
    with caplog.at_level(logging.DEBUG, logger="water_ogcapi"):
        starts(governor, clock, 2)
    assert [r.levelno for r in caplog.records] == [logging.INFO]


def test_a_reading_during_a_wait_widens_it() -> None:
    # Real time with short intervals: the fake clock serializes concurrent sleeps.
    governor = Governor(0.2, interval=0.02)

    async def run() -> list[float]:
        loop = asyncio.get_running_loop()
        await governor.wait()
        t0 = loop.time()
        queued = [asyncio.create_task(governor.wait()) for _ in range(2)]
        await asyncio.sleep(0.005)
        governor.observe(200, counts(100, 1))  # widens the spacing to 0.18 s
        done = []
        for task in asyncio.as_completed(queued):
            await task
            done.append(loop.time() - t0)
        return done

    first, second = asyncio.run(run())
    assert first > 0.15, "the queued wait took up the reading that arrived during it"
    assert second - first > 0.15


def test_a_cancelled_wait_books_no_slot() -> None:
    governor = Governor(None, interval=0.1)

    async def run() -> float:
        loop = asyncio.get_running_loop()
        await governor.wait()
        t0 = loop.time()
        abandoned = [asyncio.create_task(governor.wait()) for _ in range(3)]
        await asyncio.sleep(0.01)
        for task in abandoned:
            task.cancel()
        await asyncio.gather(*abandoned, return_exceptions=True)
        await governor.wait()
        return loop.time() - t0

    assert asyncio.run(run()) < 0.15, "one interval, not four"


class Recorder(Governor):
    """A governor that records each call."""

    def __init__(self) -> None:
        super().__init__(None, interval=0.0)
        self.calls: list[Any] = []

    async def wait(self) -> None:
        self.calls.append("wait")

    def observe(self, status: int, _headers: Any) -> None:
        self.calls.append(status)


@pytest.mark.usefixtures("clock")
def test_the_transport_paces_and_reads_every_attempt() -> None:
    replies = [httpx2.Response(503), httpx2.Response(200, json={})]
    transport = Transport(transport=httpx2.MockTransport(lambda _: replies.pop(0)))
    governor = Recorder()
    asyncio.run(transport.get(URL, governor=governor))
    assert governor.calls == ["wait", 503, "wait", 200]


def test_the_transport_reads_a_refused_redirect() -> None:
    redirect = httpx2.Response(302, headers={"location": "https://other.example/"})
    transport = Transport(transport=httpx2.MockTransport(lambda _: redirect))
    governor = Recorder()
    with pytest.raises(ServiceError):
        asyncio.run(transport.get(URL, governor=governor))
    assert governor.calls == ["wait", 302]


def rolling_quota(clock: list[float], limit: int, window: float) -> tuple[Transport, list[Any]]:
    """An NWIS mock that counts requests over a rolling window and pages forever."""
    base = NWIS(api_key="").base_url
    sent: collections.deque[float] = collections.deque()
    log: list[tuple[float, int]] = []

    def handler(_: httpx2.Request) -> httpx2.Response:
        now = clock[0]
        while sent and now - sent[0] >= window:
            sent.popleft()
        if len(sent) >= limit:
            log.append((now, 429))
            return httpx2.Response(429, headers=counts(limit, 0))
        sent.append(now)
        log.append((now, 200))
        page = f"{base}/collections/c/items?page={len(log)}"
        doc = {
            "type": "FeatureCollection",
            "features": [],
            "links": [{"rel": "next", "href": page}],
        }
        return httpx2.Response(200, json=doc, headers=counts(limit, limit - len(sent)))

    return Transport(transport=httpx2.MockTransport(handler), max_retries=0), log


def first_pages(service: OGCService, n: int) -> None:
    async def run() -> None:
        pages = service.iter_pages("c", validate=False)
        for _ in range(n):
            await anext(pages)
        await pages.aclose()

    asyncio.run(run())


def test_a_running_down_quota_widens_the_spacing_before_any_429(clock: list[float]) -> None:
    transport, log = rolling_quota(clock, limit=1000, window=3600.0)
    first_pages(NWIS(api_key="", transport=transport), 2500)
    times = [t for t, _ in log]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert {status for _, status in log} == {200}, "no 429 over more than a window"
    assert times[-1] > 3600.0
    paced = next(i for i, gap in enumerate(gaps) if gap > 0)
    ramp = gaps[paced : paced + 90]
    assert ramp == sorted(ramp), "each request waits longer as the count falls"
    assert ramp[0] < 1.0
    assert ramp[-1] == 60.0, "the ramp reaches the cap"


def test_without_the_governor_the_same_quota_runs_out(clock: list[float]) -> None:
    transport, log = rolling_quota(clock, limit=1000, window=3600.0)
    service = NWIS(api_key="", transport=transport)
    service._governor = None  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(PartialResultsError):
        asyncio.run(service.collect("c", validate=False))
    assert len(log) == 1001
    assert log[-1][1] == 429, "the control arm shows the mock quota bites"


@pytest.mark.parametrize("bare", [False, True])
def test_a_service_without_a_window_keeps_its_static_spacing(
    clock: list[float], *, bare: bool
) -> None:
    base = GeoConnex().base_url
    seen: list[float] = []

    def handler(_: httpx2.Request) -> httpx2.Response:
        seen.append(clock[0])
        doc = {"type": "FeatureCollection", "features": [], "links": []}
        if len(seen) < 3:
            doc["links"] = [{"rel": "next", "href": f"{base}/collections/c/items?p={len(seen)}"}]
        return httpx2.Response(200, json=doc, headers=counts(10, 1))

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    service = OGCService(base, transport=transport) if bare else GeoConnex(transport=transport)
    asyncio.run(service.collect("c", validate=False))
    assert seen == [0.0, 1.0, 2.0], "counts ignored, starts 1 s apart"
