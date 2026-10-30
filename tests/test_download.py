"""`Transport.download`: streaming to a file, redirects, and what they keep out (D-69)."""

from __future__ import annotations

import asyncio
import logging
import os
import pickle
import stat
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest

from water_ogcapi import _transport
from water_ogcapi._logging import logger
from water_ogcapi._transport import MAX_REDIRECTS, Transport
from water_ogcapi.exceptions import RateLimitError, ServiceError

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
    transport, _ = serve({signed: body(b"z")})
    records = _Records()
    logger.addHandler(records)
    try:
        result = asyncio.run(transport.download(SIGNED, tmp_path / "f"))
        failing, _ = serve({signed: lambda _: httpx2.Response(404)})
        with pytest.raises(ServiceError) as info:
            asyncio.run(failing.download(SIGNED, tmp_path / "g"))
    finally:
        logger.removeHandler(records)
    assert result.url == signed
    assert "SECRETSIG" not in repr(result)
    assert "SECRETSIG" not in str(info.value)
    assert "SECRETSIG" not in pickle.dumps(info.value).decode("latin-1")
    assert all("SECRETSIG" not in m for m in records.messages)


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
