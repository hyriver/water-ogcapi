"""`Transport.download`: streaming to a file, redirects, and what they keep out (D-69)."""

from __future__ import annotations

import asyncio
import logging
import os
import pickle
import stat
import sys
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from water_ogcapi import Client, __version__, _transport
from water_ogcapi._logging import logger
from water_ogcapi._transport import MAX_REDIRECTS, Transport
from water_ogcapi.exceptions import RateLimitError, ServiceError, ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

URL = "https://files.example.test/data/file.bin"
SIGNED = "https://storage.example.test/bucket/file.bin?X-Goog-Signature=SECRETSIG"


def serve(
    routes: dict[str, Callable[[httpx2.Request], httpx2.Response]],
) -> tuple[Transport, list[httpx2.Request]]:
    """A transport whose mock answers each URL, query excluded, from ``routes``."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return routes[str(request.url.copy_with(query=None))](request)

    return Transport(transport=httpx2.MockTransport(handler), max_retries=1, backoff=0), seen


def redirect(location: str, status: int = 302) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _: httpx2.Response(status, headers={"Location": location})


def body(content: bytes, **headers: str) -> Callable[[httpx2.Request], httpx2.Response]:
    return lambda _: httpx2.Response(200, content=content, headers=headers)


def fetch(transport: Transport, path: Path, **kwargs: Any) -> Any:
    return asyncio.run(transport.download(URL, path, **kwargs))


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_a_body_streams_to_the_file(tmp_path: Path) -> None:
    transport, seen = serve({URL: body(b"abc" * 1000, etag='"v1"')})
    path = tmp_path / "nested" / "file.bin"
    result = fetch(transport, path)
    assert path.read_bytes() == b"abc" * 1000
    assert list(path.parent.iterdir()) == [path], "no temporary file is left"
    assert (result.status, result.size, result.attempts, result.redirects) == (200, 3000, 1, 0)
    assert result.headers["etag"] == '"v1"'
    assert result.url == URL
    assert seen[0].headers["accept-encoding"] == "identity"


def test_a_hand_built_download_keeps_no_url_credential(tmp_path: Path) -> None:
    """D-91: the transport names a URL without these, and a caller's can hold them."""
    built = _transport.Download(
        path=tmp_path / "f",
        url="https://u:SECRET@example.test/f?api_key=SECRET&x=1",
        status=200,
        headers={},
        size=0,
        attempts=1,
        redirects=0,
    )
    assert built.url == "https://example.test/f?api_key=REDACTED&x=1"
    assert b"SECRET" not in pickle.dumps(built)


def test_a_caller_accept_encoding_is_sent_alone(tmp_path: Path) -> None:
    transport, seen = serve({URL: body(b"x")})
    fetch(transport, tmp_path / "f", headers={"accept-encoding": "gzip"})
    assert seen[0].headers.get_list("accept-encoding") == ["gzip"]


def test_a_cross_origin_redirect_drops_the_key_and_keeps_the_signature_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """HydroShare redirects within its host, then to a signed storage URL."""
    hop = "https://files.example.test/download/file.bin"
    transport, seen = serve(
        {
            URL: redirect("/download/file.bin", 301),
            hop: redirect(SIGNED),
            SIGNED.partition("?")[0]: body(b"payload"),
        }
    )
    records = _Records()
    logger.addHandler(records)
    caplog.set_level(logging.DEBUG)
    try:
        result = fetch(
            transport, tmp_path / "f", headers={"X-Api-Key": "KEY", "User-Agent": "hyriver"}
        )
    finally:
        logger.removeHandler(records)
    assert (tmp_path / "f").read_bytes() == b"payload"
    assert result.redirects == 2
    assert [r.headers.get("x-api-key") for r in seen] == ["KEY", "KEY", None]
    assert seen[-1].headers["user-agent"] == "hyriver"
    assert "SECRETSIG" not in repr(result)
    assert all("SECRETSIG" not in m for m in records.messages)
    assert "SECRETSIG" not in caplog.text
    assert any(
        "storage.example.test/bucket/file.bin (query omitted)" in m for m in records.messages
    )


def test_the_transport_user_agent_follows_a_cross_origin_redirect(tmp_path: Path) -> None:
    """D-78: it is no secret, so the hop to another origin keeps it."""
    routes = {URL: redirect(SIGNED), SIGNED.partition("?")[0]: body(b"x")}
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return routes[str(request.url.copy_with(query=None))](request)

    transport = Transport(transport=httpx2.MockTransport(handler), user_agent="hyriver/1.0")
    fetch(transport, tmp_path / "f")
    assert [r.headers["user-agent"] for r in seen] == ["hyriver/1.0", "hyriver/1.0"]


def test_the_default_user_agent_follows_a_cross_origin_redirect(tmp_path: Path) -> None:
    """D-88: an omitted ``user_agent`` sends water-ogcapi's on every hop."""
    routes = {URL: redirect(SIGNED), SIGNED.partition("?")[0]: body(b"x")}
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return routes[str(request.url.copy_with(query=None))](request)

    fetch(Transport(transport=httpx2.MockTransport(handler)), tmp_path / "f")
    assert [r.headers["user-agent"] for r in seen] == [f"water-ogcapi/{__version__}"] * 2


def test_a_download_in_a_coding_httpx2_cannot_decode_raises(tmp_path: Path) -> None:
    """D-89: the file would hold bytes in a coding the caller cannot read."""
    transport, _ = serve({URL: body(b"x", **{"Content-Encoding": "compress"})})
    with pytest.raises(ServiceError, match="cannot decode Content-Encoding compress"):
        fetch(transport, tmp_path / "f", headers={"Accept-Encoding": "compress"})
    assert not list(tmp_path.iterdir())


def test_a_cross_origin_download_hides_the_coding_it_cannot_decode(tmp_path: Path) -> None:
    """D-89 under D-69: past the first origin, server text stays out of the error."""
    transport, _ = serve(
        {
            URL: redirect(SIGNED),
            SIGNED.partition("?")[0]: body(b"x", **{"Content-Encoding": "SECRETSIG"}),
        }
    )
    with pytest.raises(ServiceError, match="cannot decode the Content-Encoding from") as excinfo:
        fetch(transport, tmp_path / "f")
    assert "SECRETSIG" not in str(excinfo.value)
    assert not list(tmp_path.iterdir())


def test_a_call_s_user_agent_beats_the_transport_s_on_every_hop(tmp_path: Path) -> None:
    routes = {URL: redirect(SIGNED), SIGNED.partition("?")[0]: body(b"x")}
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return routes[str(request.url.copy_with(query=None))](request)

    transport = Transport(transport=httpx2.MockTransport(handler), user_agent="hyriver/1.0")
    fetch(transport, tmp_path / "f", headers={"User-Agent": "mine"})
    assert [r.headers["user-agent"] for r in seen] == ["mine", "mine"]


def test_a_relative_location_resolves_against_the_current_url(tmp_path: Path) -> None:
    transport, seen = serve(
        {URL: redirect("other.bin"), "https://files.example.test/data/other.bin": body(b"y")}
    )
    assert fetch(transport, tmp_path / "f").redirects == 1
    assert str(seen[-1].url) == "https://files.example.test/data/other.bin"


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("http://files.example.test/data/file.bin", "downgrade from https to http"),
        ("https://other.test/f?api_key=LEAKED", "carries a credential"),
        ("ftp://files.example.test/f", "unsupported scheme"),
        ("https://user:pass@other.test/f", "carries userinfo"),
        ("https://other.test/f?x=1;API_KEY=LEAKED", "carries a credential"),
    ],
)
def test_a_redirect_a_download_must_not_follow_raises(
    tmp_path: Path, location: str, reason: str
) -> None:
    transport, seen = serve({URL: redirect(location)})
    with pytest.raises(ServiceError, match=reason) as info:
        fetch(transport, tmp_path / "f")
    assert len(seen) == 1, "a refused redirect is not retried"
    assert info.value.url == URL
    assert "LEAKED" not in str(info.value)
    assert info.value.__context__ is None, "a chained error would keep the raw Location"
    assert "LEAKED" not in "".join(traceback.format_exception(info.value))
    assert not list(tmp_path.iterdir())


def test_more_than_the_redirect_limit_raises(tmp_path: Path) -> None:
    transport, seen = serve({URL: redirect(URL)})
    with pytest.raises(ServiceError, match=f"more than {MAX_REDIRECTS} redirects") as info:
        fetch(transport, tmp_path / "f")
    assert len(seen) == MAX_REDIRECTS + 1
    assert info.value.__context__ is None


def test_a_304_leaves_the_file_untouched(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"old")
    transport, seen = serve({URL: lambda _: httpx2.Response(304, headers={"etag": '"v1"'})})
    result = fetch(transport, path, headers={"If-None-Match": '"v1"'})
    assert (result.status, result.size) == (304, 0)
    assert path.read_bytes() == b"old"
    assert seen[0].headers["if-none-match"] == '"v1"'


def test_an_error_after_a_redirect_names_the_first_url(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"old")
    transport, _ = serve(
        {
            URL: redirect(SIGNED),
            SIGNED.partition("?")[0]: lambda _: httpx2.Response(
                403, content=b"<Error>expired</Error>"
            ),
        }
    )
    with pytest.raises(ServiceError, match="HTTP 403") as info:
        fetch(transport, path)
    assert info.value.url == URL
    assert "storage.example.test/bucket/file.bin (query omitted)" in str(info.value)
    assert "SECRETSIG" not in str(info.value)
    assert info.value.response_body is None, "a signed host can echo its signature"
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


class _CutShort(httpx2.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial"
        raise httpx2.ReadError("connection reset")


def test_a_download_error_whose_body_fails_keeps_its_status(tmp_path: Path) -> None:
    """D-49: a 429's wait survives, with no ``response_body``."""
    transport, seen = serve(
        {URL: lambda _: httpx2.Response(429, headers={"Retry-After": "120"}, stream=_CutShort())}
    )
    with pytest.raises(RateLimitError) as excinfo:
        fetch(transport, tmp_path / "f")
    assert (excinfo.value.retry_after, excinfo.value.response_body) == (120.0, None)
    assert len(seen) == 1
    assert not list(tmp_path.iterdir())


def test_a_download_4xx_whose_body_fails_is_not_sent_again(tmp_path: Path) -> None:
    transport, seen = serve({URL: lambda _: httpx2.Response(400, stream=_CutShort())})
    with pytest.raises(ServiceError) as excinfo:
        fetch(transport, tmp_path / "f")
    assert (excinfo.value.status, excinfo.value.response_body) == (400, None)
    assert len(seen) == 1


def test_a_retry_starts_the_file_over(tmp_path: Path) -> None:
    replies = iter(
        [httpx2.Response(200, stream=_CutShort()), httpx2.Response(200, content=b"whole")]
    )
    transport, _ = serve({URL: lambda _: next(replies)})
    result = fetch(transport, tmp_path / "f")
    assert (tmp_path / "f").read_bytes() == b"whole"
    assert result.attempts == 2


class _Trickle(httpx2.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield b"."
            await asyncio.sleep(0.01)


def test_a_download_can_skip_retries(tmp_path: Path) -> None:
    """D-74: ``max_retries=0`` sends one attempt even on a retrying transport."""
    transport, seen = serve({URL: lambda _: httpx2.Response(503)})
    with pytest.raises(ServiceError) as excinfo:
        fetch(transport, tmp_path / "f", max_retries=0)
    assert (len(seen), excinfo.value.attempts) == (1, 1)
    assert not list(tmp_path.iterdir())


def test_the_total_budget_bounds_a_trickle(tmp_path: Path) -> None:
    transport, _ = serve({URL: lambda _: httpx2.Response(200, stream=_Trickle())})
    with pytest.raises(ServiceError, match=r"total budget of 0\.1 s"):
        fetch(transport, tmp_path / "f", total=0.1)
    assert not list(tmp_path.iterdir())


def test_httpx2_request_records_stay_out_of_the_root_logger() -> None:
    """httpx2 logs full URLs at INFO and httpcore2 response headers at DEBUG."""
    assert logging.getLogger("httpx2").level == logging.WARNING
    assert logging.getLogger("httpcore2").level == logging.INFO


def test_a_signed_url_passed_in_stays_out_of_the_result_errors_and_logs(tmp_path: Path) -> None:
    signed = SIGNED.partition("?")[0]
    replies = iter([httpx2.Response(429, headers={"Retry-After": "0"}), httpx2.Response(200)])
    transport, _ = serve({signed: lambda _: next(replies)})
    records = _Records()
    logger.addHandler(records)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        result = asyncio.run(transport.download(SIGNED, tmp_path / "f"))
        failing, _ = serve({signed: lambda _: httpx2.Response(404)})
        with pytest.raises(ServiceError) as info:
            asyncio.run(failing.download(SIGNED, tmp_path / "g"))
    finally:
        logger.removeHandler(records)
        logger.setLevel(level)
    assert result.url == signed
    assert "SECRETSIG" not in repr(result)
    assert "SECRETSIG" not in str(info.value)
    assert "SECRETSIG" not in pickle.dumps(info.value).decode("latin-1")
    # The retry and the completion each log, so the check below reads real records.
    assert len([m for m in records.messages if "storage.example.test" in m]) >= 2
    assert all("SECRETSIG" not in m for m in records.messages)


def test_a_download_logs_a_429_retry_as_a_warning(tmp_path: Path) -> None:
    """docs/guide/logging.md: a 429 the transport waits out logs at WARNING, as request() does."""
    replies = iter([httpx2.Response(429, headers={"Retry-After": "0"}), httpx2.Response(200)])
    transport, _ = serve({URL: lambda _: next(replies)})
    levels: list[int] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = lambda record: levels.append(record.levelno)  # type: ignore[method-assign]
    logger.addHandler(handler)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        fetch(transport, tmp_path / "f")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert levels == [logging.WARNING, logging.DEBUG]


def test_the_result_keeps_only_the_listed_headers(tmp_path: Path) -> None:
    transport, _ = serve(
        {
            URL: lambda _: httpx2.Response(
                200,
                content=b"x",
                headers={
                    "etag": '"v2"',
                    "set-cookie": "session=SECRET",
                    "content-location": SIGNED,
                },
            )
        }
    )
    headers = fetch(transport, tmp_path / "f").headers
    assert headers["etag"] == '"v2"'
    assert "set-cookie" not in headers
    assert "content-location" not in headers


def test_no_cookie_follows_a_redirect(tmp_path: Path) -> None:
    hop = "https://files.example.test/download/file.bin"
    transport, seen = serve(
        {
            URL: lambda _: httpx2.Response(
                302, headers={"Location": hop, "Set-Cookie": "session=SECRET; Path=/"}
            ),
            hop: body(b"x"),
        }
    )
    fetch(transport, tmp_path / "f")
    assert seen[-1].headers.get("cookie") is None


def test_files_beside_the_target_are_left_alone(tmp_path: Path) -> None:
    """A temporary name only the call knows: an old .part file or a planted symlink stays."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    path = tmp_path / "f"
    (tmp_path / "f.part").symlink_to(victim)
    ok, _ = serve({URL: body(b"new")})
    fetch(ok, path)
    failing, _ = serve({URL: lambda _: httpx2.Response(404)})
    with pytest.raises(ServiceError):
        fetch(failing, tmp_path / "g")
    assert victim.read_bytes() == b"keep"
    assert (tmp_path / "f.part").is_symlink()
    assert path.read_bytes() == b"new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f", "f.part", "victim"]


class _CountingGovernor:
    def __init__(self) -> None:
        self.waits = 0

    async def wait(self) -> None:
        self.waits += 1

    def observe(self, _status: int, _headers: Any) -> None:
        return None


def test_each_redirect_hop_waits_for_the_governor(tmp_path: Path) -> None:
    hop = "https://files.example.test/download/file.bin"
    transport, _ = serve(
        {URL: redirect(hop), hop: redirect(SIGNED), SIGNED.partition("?")[0]: body(b"x")}
    )
    governor = _CountingGovernor()
    fetch(transport, tmp_path / "f", governor=governor)
    assert governor.waits == 3


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot unlink an open file")
def test_a_symlink_swapped_in_mid_download_is_not_written_through(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")

    def swap(_: httpx2.Request) -> httpx2.Response:
        (part,) = tmp_path.glob(".f.*.part")
        part.unlink()
        part.symlink_to(victim)
        return httpx2.Response(200, content=b"payload")

    transport, _ = serve({URL: swap})
    fetch(transport, tmp_path / "f")
    assert victim.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "reply",
    [
        httpx2.RemoteProtocolError("illegal status line: NOTHTTP ?X-Goog-Signature=SECRETSIG"),
        httpx2.Response(403, extensions={"reason_phrase": b"Expired SECRETSIG"}),
    ],
)
def test_server_text_stays_out_of_a_signed_url_s_error(
    tmp_path: Path, reply: httpx2.Response | Exception
) -> None:
    """A status line or reason phrase can echo the query, so neither reaches the error."""

    def answer(_: httpx2.Request) -> httpx2.Response:
        if isinstance(reply, Exception):
            raise reply
        return reply

    transport, _ = serve({SIGNED.partition("?")[0]: answer})
    with pytest.raises(ServiceError) as info:
        asyncio.run(transport.download(SIGNED, tmp_path / "f"))
    assert "SECRETSIG" not in str(info.value)
    assert "SECRETSIG" not in pickle.dumps(info.value).decode("latin-1")


def test_each_read_gets_the_transport_timeout(tmp_path: Path) -> None:
    transport, seen = serve({URL: body(b"x")})
    fetch(transport, tmp_path / "f")
    assert seen[0].extensions["timeout"]["read"] == transport.timeout


def test_no_total_budget_still_downloads(tmp_path: Path) -> None:
    transport, _ = serve({URL: body(b"x")})
    assert fetch(transport, tmp_path / "f", total=None).size == 1


def test_a_retried_status_waits_as_asked_and_starts_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(_transport.asyncio, "sleep", record)
    replies = iter(
        [
            httpx2.Response(503, headers={"Retry-After": "7"}, content=b"busy"),
            httpx2.Response(200, content=b"ok"),
        ]
    )
    transport, _ = serve({URL: lambda _: next(replies)})
    result = fetch(transport, tmp_path / "f")
    assert (result.attempts, (tmp_path / "f").read_bytes()) == (2, b"ok")
    assert waits == [7.0]


def test_a_long_retry_after_raises_at_once(tmp_path: Path) -> None:
    transport, seen = serve({URL: lambda _: httpx2.Response(429, headers={"Retry-After": "9999"})})
    with pytest.raises(RateLimitError) as info:
        fetch(transport, tmp_path / "f")
    assert (len(seen), info.value.retry_after) == (1, 9999)
    assert not list(tmp_path.iterdir())


def test_a_cancelled_download_removes_its_file_and_frees_its_permit(tmp_path: Path) -> None:
    transport, _ = serve({URL: lambda _: httpx2.Response(200, stream=_Trickle())})

    async def run() -> None:
        task = asyncio.create_task(transport.download(URL, tmp_path / "f", total=None))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert transport._sem is not None
        assert transport._sem._value == transport.max_concurrent

    asyncio.run(run())
    assert not list(tmp_path.iterdir())


def test_a_same_origin_hop_to_a_signed_url_keeps_its_text_out(tmp_path: Path) -> None:
    signed = "https://files.example.test/signed/file.bin"
    transport, _ = serve(
        {
            URL: redirect("/signed/file.bin?X-Goog-Signature=SECRETSIG"),
            signed: lambda _: httpx2.Response(
                403, content=b"<Sig>SECRETSIG</Sig>", extensions={"reason_phrase": b"No SECRETSIG"}
            ),
        }
    )
    with pytest.raises(ServiceError) as info:
        fetch(transport, tmp_path / "f")
    assert "SECRETSIG" not in str(info.value)
    assert info.value.response_body is None


def test_a_signed_url_s_own_error_keeps_no_body(tmp_path: Path) -> None:
    """An S3 SignatureDoesNotMatch body echoes the signature it was sent."""
    transport, _ = serve(
        {SIGNED.partition("?")[0]: lambda _: httpx2.Response(403, content=b"<S>SECRETSIG</S>")}
    )
    with pytest.raises(ServiceError) as info:
        asyncio.run(transport.download(SIGNED, tmp_path / "f"))
    assert info.value.response_body is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_file_gets_the_mode_open_would_give_it(tmp_path: Path) -> None:
    transport, _ = serve({URL: body(b"x")})
    fetch(transport, tmp_path / "f")
    (tmp_path / "g").write_bytes(b"x")
    assert stat.S_IMODE((tmp_path / "f").stat().st_mode) == stat.S_IMODE(
        (tmp_path / "g").stat().st_mode
    )


def test_the_file_is_closed_before_it_is_renamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to rename a file that is still open."""
    opened: list[Any] = []
    closed_at_rename: list[bool] = []
    real_fdopen, real_replace = os.fdopen, Path.replace

    def fdopen(*args: Any, **kwargs: Any) -> Any:
        opened.append(real_fdopen(*args, **kwargs))
        return opened[-1]

    def replace(self: Path, target: Any) -> Path:
        closed_at_rename.append(opened[-1].closed)
        return real_replace(self, target)

    monkeypatch.setattr(_transport.os, "fdopen", fdopen)
    monkeypatch.setattr(_transport.Path, "replace", replace)
    transport, _ = serve({URL: body(b"x")})
    fetch(transport, tmp_path / "f")
    assert closed_at_rename == [True]


def test_a_failed_close_still_removes_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fdopen = os.fdopen

    class FullDisk:
        def __init__(self, inner: Any) -> None:
            self.inner, self.failed = inner, False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        def close(self) -> None:
            self.inner.close()
            if not self.failed:
                self.failed = True
                raise OSError(28, "No space left on device")

    monkeypatch.setattr(_transport.os, "fdopen", lambda *a, **k: FullDisk(real_fdopen(*a, **k)))
    transport, _ = serve({URL: body(b"x")})
    with pytest.raises(OSError, match="No space left"):
        fetch(transport, tmp_path / "f")
    assert not list(tmp_path.iterdir())


def test_a_call_timeout_reaches_every_hop_of_a_download(tmp_path: Path) -> None:
    """D-19: httpx2's limits follow the redirects, the cross-origin one included."""
    transport, seen = serve(
        {URL: redirect("https://elsewhere.test/f"), "https://elsewhere.test/f": body(b"x")}
    )

    async def both() -> None:
        await transport.download(URL, tmp_path / "f", timeout=500.0)
        await transport.download(URL, tmp_path / "g")

    asyncio.run(both())
    limits = [request.extensions["timeout"]["read"] for request in seen]
    assert limits == [500.0, 500.0, transport.timeout, transport.timeout]


def test_a_call_timeout_leaves_the_total_budget_alone(tmp_path: Path) -> None:
    transport, _ = serve({URL: lambda _: httpx2.Response(200, stream=_Trickle())})
    with pytest.raises(ServiceError, match=r"total budget of 0\.1 s"):
        fetch(transport, tmp_path / "f", total=0.1, timeout=500.0)


def test_a_download_refuses_a_timeout_that_is_not_positive(tmp_path: Path) -> None:
    transport, seen = serve({URL: body(b"x")})
    with pytest.raises(ValidationError, match="timeout must be"):
        fetch(transport, tmp_path / "f", timeout=0)
    assert seen == []
    assert not list(tmp_path.iterdir())


def test_the_client_passes_a_download_timeout_through(tmp_path: Path) -> None:
    transport, seen = serve({URL: body(b"x")})
    with Client(transport=transport) as client:
        client.download(URL, tmp_path / "f", timeout=500.0)
    assert seen[0].extensions["timeout"]["read"] == 500.0


class _Gated(httpx2.AsyncByteStream):
    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self.gate.wait()
        yield b"x"


def test_long_downloads_do_not_delay_a_request_to_another_origin(tmp_path: Path) -> None:
    """D-92: four downloads fill their origin's bound and leave the total room."""

    async def run() -> None:
        gate = asyncio.Event()
        seen: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.url.host)
            if request.url.host == "files.example.test":
                return httpx2.Response(200, stream=_Gated(gate))
            return httpx2.Response(200, json={})

        transport = Transport(transport=httpx2.MockTransport(handler))
        files = [asyncio.create_task(transport.download(URL, tmp_path / str(n))) for n in range(5)]
        async with asyncio.timeout(5):
            while seen.count("files.example.test") < 4:  # noqa: ASYNC110
                await asyncio.sleep(0)
        await asyncio.wait_for(transport.get("https://api.example.test/x"), 5)
        assert seen.count("files.example.test") == 4, "the fifth waits for its origin"
        gate.set()
        assert [f.size for f in await asyncio.gather(*files)] == [1] * 5
        await transport.aclose()

    asyncio.run(run())


def test_a_redirect_chain_across_origins_completes_at_a_bound_of_one(tmp_path: Path) -> None:
    """D-92: each hop gives its permits back first, so A to B to A cannot deadlock."""
    other = "https://elsewhere.example.test/f"
    back = "https://files.example.test/final"
    routes = {URL: redirect(other), other: redirect(back), back: body(b"x")}
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return routes[str(request.url)](request)

    transport = Transport(
        transport=httpx2.MockTransport(handler), max_concurrent=1, max_per_origin=1
    )

    async def run() -> None:
        await asyncio.wait_for(transport.download(URL, tmp_path / "f"), 5)
        assert transport._sem is not None
        assert transport._sem._value == 1
        assert all(sem._value == 1 for sem in transport._origins.values())

    asyncio.run(run())
    assert (tmp_path / "f").read_bytes() == b"x"
    assert len(seen) == 3


def test_a_cancelled_download_returns_both_permits(tmp_path: Path) -> None:
    async def run() -> None:
        gate = asyncio.Event()
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, stream=_Gated(gate))

        transport = Transport(transport=httpx2.MockTransport(handler))
        task = asyncio.create_task(transport.download(URL, tmp_path / "f"))
        async with asyncio.timeout(5):
            while not seen:  # noqa: ASYNC110
                await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert transport._sem is not None
        assert transport._sem._value == transport.max_concurrent
        assert all(sem._value == transport.max_per_origin for sem in transport._origins.values())
        await transport.aclose()

    asyncio.run(run())
    assert not list(tmp_path.iterdir())


def _echoing(request: httpx2.Request) -> httpx2.Response:
    """A 400 whose body and reason phrase echo the query it was sent."""
    query = request.url.query
    return httpx2.Response(
        400, content=b"bad parameter in " + query, extensions={"reason_phrase": b"Bad " + query}
    )


def test_a_public_query_keeps_the_server_s_message(tmp_path: Path) -> None:
    """D-69: a caller who built the query reads which parameter was wrong."""
    transport, _ = serve({URL: _echoing})
    with pytest.raises(ServiceError) as excinfo:
        fetch(transport, tmp_path / "f", params={"var": "tmax"}, public_query=True)
    err = excinfo.value
    assert err.response_body == b"bad parameter in var=tmax"
    assert "var=tmax" in str(err)
    assert (err.status, err.url) == (400, URL), "the URL is still named without its query"


def test_a_public_query_keeps_a_library_message(tmp_path: Path) -> None:
    def refuse(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused by proxy")

    for kept, message in ((True, "refused by proxy"), (False, "ConnectError")):
        transport, _ = serve({URL: refuse})
        with pytest.raises(ServiceError, match=message) as excinfo:
            fetch(transport, tmp_path / "f", params={"var": "tmax"}, public_query=kept)
        assert ("refused by proxy" in str(excinfo.value)) is kept


def test_a_signature_in_params_stays_out_of_the_error_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """D-69: only ``api_key`` is refused by name, so quiet stays the default."""
    records = _Records()
    logger.addHandler(records)
    caplog.set_level(logging.DEBUG)
    try:
        transport, _ = serve({URL: _echoing})
        with pytest.raises(ServiceError) as excinfo:
            fetch(transport, tmp_path / "f", params={"X-Amz-Signature": "SECRETSIG"})
    finally:
        logger.removeHandler(records)
    err = excinfo.value
    assert err.response_body is None
    assert err.status == 400
    assert "SECRETSIG" not in str(err)
    assert b"SECRETSIG" not in pickle.dumps(err)
    assert not any("SECRETSIG" in message for message in records.messages)


@pytest.mark.parametrize(
    "hop",
    ["https://files.example.test/next?X-Amz-Signature=SECRETSIG", "https://other.test/next"],
    ids=["same-origin-with-query", "cross-origin"],
)
def test_a_public_query_goes_quiet_after_a_redirect(tmp_path: Path, hop: str) -> None:
    """D-69: the caller spoke for the first URL's query, and nothing a server sent."""
    transport, seen = serve({URL: redirect(hop), hop.partition("?")[0]: _echoing})
    with pytest.raises(ServiceError) as excinfo:
        fetch(transport, tmp_path / "f", params={"var": "tmax"}, public_query=True)
    err = excinfo.value
    assert len(seen) == 2
    assert err.response_body is None
    assert "SECRETSIG" not in str(err)
    assert b"SECRETSIG" not in pickle.dumps(err)


def test_a_public_query_stays_kept_after_a_redirect_with_no_query(tmp_path: Path) -> None:
    hop = "https://files.example.test/next"

    def reply(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, content=b"nope")

    transport, _ = serve({URL: redirect(hop), hop: reply})
    with pytest.raises(ServiceError) as excinfo:
        fetch(transport, tmp_path / "f", params={"var": "tmax"}, public_query=True)
    assert excinfo.value.response_body == b"nope"


def test_the_client_passes_public_query_through(tmp_path: Path) -> None:
    transport, _ = serve({URL: _echoing})
    with Client(transport=transport) as client, pytest.raises(ServiceError) as excinfo:
        client.download(URL, tmp_path / "f", params={"var": "tmax"}, public_query=True)
    assert excinfo.value.response_body == b"bad parameter in var=tmax"


def test_a_failing_check_keeps_the_existing_file(tmp_path: Path) -> None:
    """D-69: a 200 holding an error document does not replace a good file."""
    path = tmp_path / "f"
    path.write_bytes(b"good")
    transport, seen = serve({URL: body(b'{"error": "busy"}')})

    def refuse(part: Path, _: Any) -> None:
        raise ValueError(part.read_text())

    with pytest.raises(ValueError, match="busy"):
        fetch(transport, path, check=refuse)
    assert path.read_bytes() == b"good"
    assert list(tmp_path.iterdir()) == [path], "the .part is gone"
    assert len(seen) == 1, "a check's failure is not retried"


def test_a_passing_check_reads_the_part_before_the_rename(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"old")
    transport, _ = serve({URL: body(b"new", **{"Content-Type": "image/tiff"})})
    read: list[tuple[bytes, bytes, str]] = []

    def record(part: Path, headers: Any) -> None:
        read.append((part.read_bytes(), path.read_bytes(), headers["content-type"]))

    result = fetch(transport, path, check=record)
    assert read == [(b"new", b"old", "image/tiff")]
    assert path.read_bytes() == b"new"
    assert (result.status, result.size) == (200, 3)


def test_a_304_skips_the_check(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"old")
    transport, _ = serve({URL: lambda _: httpx2.Response(304)})
    checked: list[Path] = []
    result = fetch(transport, path, check=lambda part, _: checked.append(part))
    assert checked == []
    assert result.status == 304
    assert path.read_bytes() == b"old"


def test_a_check_s_timeout_error_stays_its_own(tmp_path: Path) -> None:
    transport, _ = serve({URL: body(b"x")})

    def refuse(_: Path, __: Any) -> None:
        raise TimeoutError("the check gave up")

    with pytest.raises(TimeoutError, match="the check gave up"):
        fetch(transport, tmp_path / "f", check=refuse)
    assert not list(tmp_path.iterdir())


def test_the_client_passes_a_check_through(tmp_path: Path) -> None:
    transport, _ = serve({URL: body(b"x")})
    checked: list[bytes] = []
    with Client(transport=transport) as client:
        client.download(
            URL, tmp_path / "f", check=lambda part, _: checked.append(part.read_bytes())
        )
    assert checked == [b"x"]


STAMP = 1445412480.0
LAST_MODIFIED = {"Last-Modified": "Wed, 21 Oct 2015 07:28:00 GMT"}


def test_remote_time_sets_the_file_time_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-69: the file never exists with the local write time, even if the process stops."""
    transport, _ = serve({URL: body(b"x", **LAST_MODIFIED)})
    at_rename: list[float] = []
    rename = Path.replace

    def spy(self: Path, target: Path) -> Path:
        at_rename.append(self.stat().st_mtime)
        return rename(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    fetch(transport, tmp_path / "f", remote_time=True)
    assert at_rename == [STAMP]
    assert (tmp_path / "f").stat().st_mtime == STAMP


@pytest.mark.parametrize(
    ("headers", "kwargs"),
    [
        ({}, {"remote_time": True}),
        ({"Last-Modified": "not a date"}, {"remote_time": True}),
        (LAST_MODIFIED, {}),
    ],
    ids=["missing", "unparsable", "off"],
)
def test_the_file_keeps_the_local_time(
    tmp_path: Path, headers: dict[str, str], kwargs: dict[str, Any]
) -> None:
    transport, _ = serve({URL: body(b"x", **headers)})
    fetch(transport, tmp_path / "f", **kwargs)
    assert abs((tmp_path / "f").stat().st_mtime - time.time()) < 60


def test_a_time_the_file_system_refuses_still_delivers_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(_transport.os, "utime", refuse)
    transport, _ = serve({URL: body(b"x", **LAST_MODIFIED)})
    assert fetch(transport, tmp_path / "f", remote_time=True).status == 200
    assert (tmp_path / "f").read_bytes() == b"x"


def test_a_304_leaves_the_file_time_untouched(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"old")
    os.utime(path, (1000.0, 1000.0))
    transport, _ = serve({URL: lambda _: httpx2.Response(304, headers=LAST_MODIFIED)})
    assert fetch(transport, path, remote_time=True).status == 304
    assert path.stat().st_mtime == 1000.0
    assert path.read_bytes() == b"old"


def test_the_client_passes_remote_time_through(tmp_path: Path) -> None:
    transport, _ = serve({URL: body(b"x", **LAST_MODIFIED)})
    with Client(transport=transport) as client:
        client.download(URL, tmp_path / "f", remote_time=True)
    assert (tmp_path / "f").stat().st_mtime == STAMP
