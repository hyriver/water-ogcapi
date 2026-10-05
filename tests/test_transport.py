"""Tests for the async HTTP transport."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx2
import pytest

from water_ogcapi._transport import (
    MAX_DELTA_SECONDS,
    MAX_SLEEP,
    Response,
    Transport,
    _is_transient,
    parse_count,
    parse_retry_after,
)
from water_ogcapi.exceptions import RateLimitError, ServiceError

URL = "https://example.com/collections"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff waits instead of performing them."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return waits


def responder(*responses: httpx2.Response) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    """Serve the given responses in order, repeating the last one."""
    seen: list[httpx2.Request] = []
    queue = list(responses)

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx2.MockTransport(handler), seen


def test_no_io_on_construction() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={})

    transport = Transport(transport=httpx2.MockTransport(handler))
    assert seen == []
    assert transport._client is None
    assert transport._sem is None


@pytest.mark.parametrize(
    "kwargs",
    [{"max_concurrent": 0}, {"max_concurrent": -1}, {"max_retries": -1}, {"backoff": -0.5}],
)
def test_invalid_settings_are_rejected_at_construction(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="must"):
        Transport(**kwargs)


@pytest.mark.parametrize("name", ["timeout", "max_concurrent", "max_retries", "backoff", "http2"])
def test_settings_are_read_only(name: str) -> None:
    """The client and semaphore capture settings on first use, so assignment would be ignored."""
    transport = Transport()
    with pytest.raises(AttributeError):
        setattr(transport, name, getattr(transport, name))


def test_get_returns_detached_response() -> None:
    mock, seen = responder(
        httpx2.Response(200, json={"type": "FeatureCollection"}, headers={"ETag": "W/abc"})
    )
    transport = Transport(transport=mock)
    resp = asyncio.run(transport.get(URL, params={"limit": "2"}))
    assert isinstance(resp, Response)
    assert resp.status == 200
    assert resp.json() == {"type": "FeatureCollection"}
    assert resp.headers["etag"] == "W/abc"
    assert str(seen[0].url) == f"{URL}?limit=2"


@pytest.mark.parametrize(
    ("query", "params", "expected"),
    [
        ("f=json&limit=10&offset=20", {"limit": "5"}, b"f=json&offset=20&limit=5"),
        ("f=json&limit=10&offset=20", {}, b"f=json&limit=10&offset=20"),
        ("f=json&limit=10&offset=20", None, b"f=json&limit=10&offset=20"),
        ("cursor=%FF;x=1&flag&a=b=c", {"q": "a b"}, b"cursor=%FF;x=1&flag&a=b=c&q=a+b"),
        ("&flag&&x=1&", {"q": "1"}, b"&flag&&x=1&&q=1"),
        ("", {"q": "1"}, b"q=1"),
    ],
)
def test_params_merge_into_the_url_query(
    query: str, params: dict[str, str] | None, expected: bytes
) -> None:
    """A next link keeps its paging state, byte for byte, when params come with it (L-07)."""
    mock, seen = responder(httpx2.Response(200, json={}))
    asyncio.run(Transport(transport=mock).get(f"{URL}?{query}", params=params))
    assert seen[0].url.query == expected


def test_request_headers_are_sent() -> None:
    """A dropped key header still succeeds, against the unkeyed quota (L-03)."""
    mock, seen = responder(httpx2.Response(200, json={}))
    asyncio.run(Transport(transport=mock).get(URL, headers={"X-Api-Key": "key"}))
    assert seen[0].headers["x-api-key"] == "key"


def test_client_uses_the_configured_timeout() -> None:
    mock, _ = responder(httpx2.Response(200, json={}))
    transport = Transport(transport=mock, timeout=7.0)
    asyncio.run(transport.get(URL))
    assert transport._client is not None
    assert transport._client.timeout == httpx2.Timeout(7.0)


def test_not_modified_body_is_not_json() -> None:
    mock, _ = responder(httpx2.Response(304))
    resp = asyncio.run(Transport(transport=mock).get(URL))
    with pytest.raises(json.JSONDecodeError):
        resp.json()


@pytest.mark.usefixtures("no_sleep")
def test_server_error_carries_a_long_retry_after() -> None:
    mock, seen = responder(httpx2.Response(503, headers={"Retry-After": "120"}))
    with pytest.raises(ServiceError) as excinfo:
        asyncio.run(Transport(transport=mock).get(URL))
    assert not isinstance(excinfo.value, RateLimitError)
    assert excinfo.value.retry_after == 120.0
    assert len(seen) == 1, "a wait above MAX_SLEEP goes to the caller"


def test_redirect_raises_instead_of_passing_through() -> None:
    mock, _ = responder(httpx2.Response(301, headers={"Location": "https://elsewhere.test/x"}))
    with pytest.raises(ServiceError, match=r"redirected to https://elsewhere\.test/x") as excinfo:
        asyncio.run(Transport(transport=mock).get(URL))
    assert excinfo.value.status == 301, "a bodyless 3xx must not read as success"


@pytest.mark.usefixtures("no_sleep")
def test_malformed_redirect_raises_once_with_its_status() -> None:
    mock, seen = responder(httpx2.Response(301, headers={"Location": "https://e.test:bad/x"}))
    with pytest.raises(ServiceError, match="redirected to") as excinfo:
        asyncio.run(Transport(transport=mock).get(URL))
    assert len(seen) == 1, "a bad Location is not a connection problem"
    assert excinfo.value.status == 301


def test_not_modified_is_returned_not_raised() -> None:
    mock, _ = responder(httpx2.Response(304))
    resp = asyncio.run(Transport(transport=mock).get(URL))
    assert resp.status == 304


@pytest.mark.usefixtures("no_sleep")
def test_undecodable_body_is_typed_and_not_retried() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        # Built inside the handler: httpx2 decodes eagerly at construction.
        return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not gzip")

    with pytest.raises(ServiceError, match="decompress"):
        asyncio.run(Transport(transport=httpx2.MockTransport(handler)).get(URL))
    assert len(seen) == 1, "a corrupt body is not a connection problem"


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (httpx2.ConnectError("x"), True),
        (httpx2.ReadTimeout("x"), True),
        (httpx2.UnsupportedProtocol("x"), False),
        (httpx2.LocalProtocolError("x"), False),
        (httpx2.ProxyError("x"), False),
        (httpx2.InvalidURL("x"), False),
        (httpx2.DecodingError("x"), False),
    ],
)
def test_only_connection_failures_are_retried(exc: Exception, transient: bool) -> None:
    assert _is_transient(exc) is transient


def test_client_error_raises() -> None:
    mock, seen = responder(httpx2.Response(404))
    with pytest.raises(ServiceError, match="HTTP 404") as excinfo:
        asyncio.run(Transport(transport=mock).get(URL))
    assert len(seen) == 1, "4xx must not be retried"
    assert excinfo.value.attempts == 1


def test_server_error_retries_then_succeeds(no_sleep: list[float]) -> None:
    mock, seen = responder(
        httpx2.Response(503), httpx2.Response(503), httpx2.Response(200, json={"ok": True})
    )
    resp = asyncio.run(Transport(transport=mock, backoff=0.5).get(URL))
    assert resp.json() == {"ok": True}
    assert len(seen) == 3
    assert no_sleep == [0.5, 1.0], "exponential backoff"


@pytest.mark.usefixtures("no_sleep")
def test_server_error_exhaustion_reports_attempts() -> None:
    mock, seen = responder(httpx2.Response(503))
    with pytest.raises(ServiceError) as excinfo:
        asyncio.run(Transport(transport=mock).get(URL))
    assert len(seen) == 4
    assert excinfo.value.status == 503
    assert excinfo.value.attempts == 4, "an outer retry budget needs to see exhaustion"
    assert "after 4 attempts" in str(excinfo.value)


@pytest.mark.parametrize(
    ("attempt", "expected"), [(0, 0.5), (3, 4.0), (20, MAX_SLEEP), (5000, MAX_SLEEP)]
)
def test_backoff_doubles_up_to_the_cap(attempt: int, expected: float) -> None:
    assert Transport(backoff=0.5)._delay(attempt) == expected


def test_retry_after_seconds_overrides_backoff(no_sleep: list[float]) -> None:
    mock, _ = responder(
        httpx2.Response(429, headers={"Retry-After": "7"}), httpx2.Response(200, json={})
    )
    asyncio.run(Transport(transport=mock).get(URL))
    assert no_sleep == [7.0]


def test_retry_after_zero_is_honored(no_sleep: list[float]) -> None:
    mock, _ = responder(
        httpx2.Response(429, headers={"Retry-After": "0"}), httpx2.Response(200, json={})
    )
    asyncio.run(Transport(transport=mock).get(URL))
    assert no_sleep == [0.0], "zero is a wait the server asked for, not a missing header"


def test_retry_after_above_cap_raises_instead_of_retrying_early(no_sleep: list[float]) -> None:
    mock, seen = responder(httpx2.Response(429, headers={"Retry-After": "9999"}))
    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(Transport(transport=mock).get(URL))
    assert len(seen) == 1, "retrying before the window reopens would burn every attempt"
    assert no_sleep == []
    assert excinfo.value.retry_after == 9999.0


@pytest.mark.usefixtures("no_sleep")
def test_huge_remaining_count_stays_typed() -> None:
    mock, _ = responder(httpx2.Response(429, headers={"X-RateLimit-Remaining": "9" * 5000}))
    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(Transport(transport=mock, max_retries=0).get(URL))
    assert excinfo.value.remaining is None, "int() refuses more than 4300 digits"


@pytest.mark.usefixtures("no_sleep")
def test_digit_shaped_remaining_does_not_crash() -> None:
    # Raw bytes, as a server sends them: httpx2 decodes headers as iso-8859-1.
    mock, _ = responder(httpx2.Response(429, headers=[(b"x-ratelimit-remaining", b"\xb2")]))
    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(Transport(transport=mock, max_retries=0).get(URL))
    assert excinfo.value.remaining is None, "int() rejects what isdigit() accepts"


def test_rate_limit_error_carries_parsed_headers(no_sleep: list[float]) -> None:
    mock, seen = responder(
        httpx2.Response(429, headers={"Retry-After": "3", "X-RateLimit-Remaining": "0"})
    )
    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(Transport(transport=mock, max_retries=1).get(URL))
    assert excinfo.value.retry_after == 3.0
    assert excinfo.value.remaining == 0
    assert excinfo.value.status == 429
    assert excinfo.value.attempts == 2
    assert len(seen) == 2
    assert no_sleep == [3.0]


@pytest.mark.usefixtures("no_sleep")
def test_transport_error_retries_then_raises() -> None:
    attempts: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(1)
        raise httpx2.ConnectError("boom", request=request)

    with pytest.raises(ServiceError, match="after 4 attempts") as excinfo:
        asyncio.run(Transport(transport=httpx2.MockTransport(handler)).get(URL))
    assert len(attempts) == 4, "initial attempt plus three retries"
    assert excinfo.value.attempts == 4


@pytest.mark.parametrize(
    "exc",
    [
        httpx2.LocalProtocolError("Illegal header value b'SECRET\\n'"),
        httpx2.ConnectError("boom"),
    ],
)
def test_transport_errors_keep_the_key_out(exc: httpx2.TransportError) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        exc.request = request
        raise exc

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    with pytest.raises(ServiceError) as excinfo:
        asyncio.run(transport.get(URL, headers={"X-Api-Key": "SECRET"}))
    error = excinfo.value
    assert "SECRET" not in str(error)
    assert "SECRET" not in repr(error)
    assert error.__cause__ is None, "the chained error keeps the unredacted request"
    assert error.__context__ is None


@pytest.mark.parametrize(
    ("url", "params"),
    [
        (URL, {"api_key": "SECRET"}),
        (URL, {"API_KEY": "SECRET"}),
        (f"{URL}?limit=2&api_key=SECRET", None),
        (f"{URL}?API%5FKEY=SECRET", None),
        (f"{URL}#api_key=SECRET", None),
        (f"{URL}#?api_key=SECRET", None),
        (f"{URL}#section?API%5Fkey=SECRET", None),
        ("https://[bad]/?api_key=SECRET", None),
    ],
)
def test_key_in_query_is_rejected_before_sending(url: str, params: dict[str, str] | None) -> None:
    mock, seen = responder(httpx2.Response(200, json={}))
    with pytest.raises(ValueError, match="X-Api-Key header") as excinfo:
        asyncio.run(Transport(transport=mock).get(url, params=params))
    assert seen == []
    assert "SECRET" not in str(excinfo.value)


def test_timeout_bounds_the_whole_attempt() -> None:
    calls: list[int] = []

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        await asyncio.sleep(5)
        return httpx2.Response(200, json={})

    transport = Transport(
        transport=httpx2.MockTransport(handler), timeout=0.05, max_retries=1, backoff=0
    )
    with pytest.raises(ServiceError, match="TimeoutError") as excinfo:
        asyncio.run(transport.get(URL))
    assert len(calls) == 2, "a timed-out attempt is retried"
    assert excinfo.value.attempts == 2


def test_concurrency_is_bounded() -> None:
    live = 0
    peak = 0

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1
        return httpx2.Response(200, json={})

    async def run() -> None:
        transport = Transport(transport=httpx2.MockTransport(handler), max_concurrent=2)
        await asyncio.gather(*(transport.get(URL) for _ in range(8)))

    asyncio.run(run())
    assert peak == 2, "the bound must be reached, not just respected"


def test_backoff_does_not_hold_a_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request waiting out a backoff must not starve queued work."""
    calls: list[str] = []
    real_sleep = asyncio.sleep

    async def backoff(_seconds: float) -> None:
        # The semaphore is FIFO-fair, so call order alone cannot tell whether the
        # permit was free during the wait. Check that the queued request ran meanwhile.
        for _ in range(100):
            if "/fast" in calls:
                return
            await real_sleep(0)
        pytest.fail("the queued request waited out another request's backoff")

    monkeypatch.setattr(asyncio, "sleep", backoff)

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        calls.append(path)
        if path == "/slow" and calls.count("/slow") == 1:
            return httpx2.Response(503)
        return httpx2.Response(200, json={})

    async def run() -> None:
        transport = Transport(transport=httpx2.MockTransport(handler), max_concurrent=1)
        await asyncio.gather(
            transport.get("https://example.com/slow"),
            transport.get("https://example.com/fast"),
        )

    asyncio.run(run())
    assert calls == ["/slow", "/fast", "/slow"], "the queued request must not wait out the backoff"


def test_aclose_during_backoff_raises_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock, seen = responder(httpx2.Response(503))
    transport = Transport(transport=mock)

    async def closing_sleep(_seconds: float) -> None:
        await transport.aclose()

    monkeypatch.setattr(asyncio, "sleep", closing_sleep)
    with pytest.raises(ServiceError, match="transport closed") as excinfo:
        asyncio.run(transport.get(URL))
    assert len(seen) == 1
    assert excinfo.value.attempts == 1


def test_aclose_while_queued_raises_service_error() -> None:
    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/hold":
                entered.set()
                await release.wait()
            return httpx2.Response(200, json={})

        transport = Transport(transport=httpx2.MockTransport(handler), max_concurrent=1)
        holder = asyncio.create_task(transport.get("https://example.com/hold"))
        await entered.wait()
        queued = asyncio.create_task(transport.get("https://example.com/queued"))
        await asyncio.sleep(0)
        await transport.aclose()
        release.set()
        await asyncio.gather(holder, return_exceptions=True)
        with pytest.raises(ServiceError, match="transport closed") as excinfo:
            await queued
        assert excinfo.value.attempts == 0

    asyncio.run(run())


class _SlowClose(httpx2.MockTransport):
    """A transport whose close suspends until released."""

    def __init__(self, handler: Any, gate: asyncio.Event) -> None:
        super().__init__(handler)
        self.gate = gate

    async def aclose(self) -> None:
        await self.gate.wait()


def test_retry_during_close_raises_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx2 marks the client closed before its cleanup finishes."""
    real_sleep = asyncio.sleep

    async def run() -> None:
        in_backoff, resume, gate = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def backoff(_seconds: float) -> None:
            in_backoff.set()
            await resume.wait()

        monkeypatch.setattr(asyncio, "sleep", backoff)
        transport = Transport(transport=_SlowClose(lambda _r: httpx2.Response(503), gate))
        call = asyncio.create_task(transport.get(URL))
        await in_backoff.wait()
        closing = asyncio.create_task(transport.aclose())
        await real_sleep(0)
        resume.set()
        with pytest.raises(ServiceError, match="transport closed") as excinfo:
            await call
        assert excinfo.value.attempts == 1
        gate.set()
        await closing

    asyncio.run(run())


@pytest.mark.parametrize("first_status", [None, 503], ids=["mid-request", "mid-backoff"])
def test_cancellation_releases_the_permit(first_status: int | None) -> None:
    async def run() -> None:
        entered = asyncio.Event()

        async def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/stuck":
                entered.set()
                if first_status is None:
                    await asyncio.sleep(10)
                else:
                    return httpx2.Response(first_status)
            return httpx2.Response(200, json={})

        transport = Transport(transport=httpx2.MockTransport(handler), max_concurrent=1, backoff=10)
        task = asyncio.create_task(transport.get("https://example.com/stuck"))
        await entered.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        resp = await asyncio.wait_for(transport.get("https://example.com/ok"), timeout=2)
        assert resp.status == 200, "a cancelled call must give its permit back"

    asyncio.run(run())


def test_reuse_across_event_loops_raises() -> None:
    mock, _ = responder(httpx2.Response(200, json={}))
    transport = Transport(transport=mock)
    asyncio.run(transport.get(URL))
    with pytest.raises(RuntimeError, match="another event loop"):
        asyncio.run(transport.get(URL))


def test_aclose_allows_a_new_loop() -> None:
    mock, seen = responder(httpx2.Response(200, json={}))
    transport = Transport(transport=mock)

    closed_client: httpx2.AsyncClient

    async def first() -> None:
        nonlocal closed_client
        await transport.get(URL)
        assert transport._client is not None
        closed_client = transport._client
        await transport.aclose()

    asyncio.run(first())
    assert closed_client.is_closed
    assert transport._client is None
    assert transport._sem is None
    asyncio.run(transport.get(URL))
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("5", 5.0),
        ("0", 0.0),
        ("not-a-date", None),
        # RFC 9110 allows only 1*DIGIT; float() would accept all of these.
        ("-1", None),
        ("+5", None),
        ("7_000", None),
        ("NaN", None),
        ("inf", None),
        ("1e400", None),
        ("\u0663", None),
        ("9" * 5000, float(MAX_DELTA_SECONDS)),
        ("0" * 20 + "5", 5.0),
        ("Wed, 21 Oct 999999999999999999999 07:28:00 GMT", None),
    ],
)
def test_parse_retry_after_scalars(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("0", 0), ("42", 42), ("-1", None), ("\u0663", None), ("9" * 5000, None)],
)
def test_parse_count(value: str | None, expected: int | None) -> None:
    assert parse_count(value) == expected


SERVER_DATE = "Sun, 21 Oct 2001 07:28:00 GMT"


def test_parse_retry_after_http_date_uses_server_clock() -> None:
    """Measured against the local clock, a 2001 date would read as zero wait."""
    assert parse_retry_after("Sun, 21 Oct 2001 07:28:30 GMT", SERVER_DATE) == 30.0


def test_parse_retry_after_zoneless_date_is_read_as_utc() -> None:
    """RFC 7231 asctime dates carry no zone; local time would skew the wait."""
    assert parse_retry_after("Sun Oct 21 07:28:30 2001", SERVER_DATE) == 30.0


def test_parse_retry_after_http_date_falls_back_to_local_clock() -> None:
    when = datetime.now(tz=UTC) + timedelta(seconds=30)
    parsed = parse_retry_after(format_datetime(when, usegmt=True), "not a date")
    assert parsed is not None
    assert 20.0 <= parsed <= 30.0


def test_http_date_retry_after_drives_the_retry_loop(no_sleep: list[float]) -> None:
    mock, _ = responder(
        httpx2.Response(
            503,
            headers={"Retry-After": "Sun, 21 Oct 2001 07:28:05 GMT", "Date": SERVER_DATE},
        ),
        httpx2.Response(200, json={}),
    )
    asyncio.run(Transport(transport=mock).get(URL))
    assert no_sleep == [5.0]
