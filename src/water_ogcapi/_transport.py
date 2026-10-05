"""Async HTTP transport.

The only module that imports ``httpx2``. Swapping the HTTP library is a
one-file edit as long as nothing else touches it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qsl, unquote, unquote_plus, urlencode, urlsplit

import httpx2

from water_ogcapi._logging import logger
from water_ogcapi.exceptions import RateLimitError, ServiceError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Response", "Transport"]

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# TransportError subclasses that will not fix themselves, so retrying only burns
# attempts. A ProxyError is a proxy refusing the connection, often a 407 for missing
# credentials. InvalidURL is not even a TransportError, so it never reached the retry
# path to begin with.
FATAL_ERRORS = (
    httpx2.UnsupportedProtocol,
    httpx2.LocalProtocolError,
    httpx2.ProxyError,
    httpx2.InvalidURL,
)
# Longest wait this transport will sit through, in seconds. Caps our own backoff,
# and a `Retry-After` above it is raised to the caller instead of slept off.
MAX_SLEEP = 60.0
# Query parameters that carry a credential. A key in the URL reaches Response.url,
# error messages, and httpx2's INFO log, so it must travel in a header.
CREDENTIAL_PARAMS = frozenset({"api_key"})
# RFC 9111 section 1.2.2: a delta-seconds value too large to represent reads as 2**31.
MAX_DELTA_SECONDS = 2**31


def _is_transient(exc: Exception) -> bool:
    """Whether a failed request is worth retrying.

    Only connect, read, and write failures are. A decoding failure or a malformed
    URL is typed and surfaced immediately rather than retried.
    """
    return isinstance(exc, httpx2.TransportError) and not isinstance(exc, FATAL_ERRORS)


def _mentions_credential(text: str) -> bool:
    """Whether a credential name appears anywhere in ``text``, percent-decoded."""
    decoded = unquote(text).lower()
    return any(name in decoded for name in CREDENTIAL_PARAMS)


def _reject_credentials(url: str, params: Mapping[str, str] | None) -> None:
    """Refuse a credential passed as a query parameter (docs/design.md D-18).

    Checks ``params`` and the query and fragment already in ``url``, with
    percent-encoding decoded. httpx2 keeps a fragment in logged and returned URLs even
    though it never sends it.
    """
    names = {name.lower() for name in params or {}}
    try:
        parts = urlsplit(url)
    except ValueError:
        # Fail closed: the InvalidURL error that follows would echo the raw URL.
        leaked = _mentions_credential(url)
    else:
        # Some servers also split the query on ";", where parse_qsl splits only on "&".
        query = parse_qsl(parts.query.replace(";", "&"), keep_blank_values=True)
        names |= {name.lower() for name, _ in query}
        # A fragment has no key=value grammar ("#?api_key=", "#view?api_key="), so
        # match the name anywhere in it.
        leaked = _mentions_credential(parts.fragment)
    if leaked or CREDENTIAL_PARAMS & names:
        msg = "pass the API key in the X-Api-Key header, not as a query parameter"
        raise ValueError(msg)


def _merge_query(url: str, params: Mapping[str, str]) -> httpx2.URL:
    """Append ``params`` to the query in ``url``, keeping its other entries byte for byte.

    httpx2's ``params=`` replaces the whole query, which strips the paging state from a
    next link, and ``URL.copy_merge_params`` re-encodes it, which turns ``%FF`` in an
    opaque cursor into ``%EF%BF%BD``. An existing entry named in ``params`` is dropped.
    """
    parsed = httpx2.URL(url)
    query = parsed.query.decode("ascii")
    entries = query.split("&") if query else []
    kept = [e for e in entries if unquote_plus(e.partition("=")[0]) not in params]
    return parsed.copy_with(query="&".join([*kept, urlencode(params)]).encode("ascii"))


def _describe(exc: Exception) -> str:
    """Name a failed request without echoing request data.

    h11 quotes an illegal header value back in ``LocalProtocolError``, so a key read
    with a trailing newline would land in the message verbatim.
    """
    if isinstance(exc, httpx2.LocalProtocolError):
        return "LocalProtocolError: malformed request, such as a header value with a line break"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class _RedirectedError(Exception):
    """A redirect, caught before httpx2 parses its ``Location``."""

    def __init__(self, status: int, location: str) -> None:
        super().__init__(status, location)
        self.status = status
        self.location = location


async def _stop_at_redirect(response: httpx2.Response) -> None:
    """Response hook that turns a redirect into ``_RedirectedError``.

    httpx2 builds the follow-up request even with ``follow_redirects=False``, and a
    malformed ``Location`` then raises a ``RemoteProtocolError`` that reads as transient.
    """
    if response.has_redirect_location:
        raise _RedirectedError(response.status_code, response.headers["location"])


def _is_digits(value: str) -> bool:
    """Whether a header value is ASCII digits only; ``isdecimal()`` also takes other scripts."""
    return value.isascii() and value.isdecimal()


def parse_count(value: str | None) -> int | None:
    """Parse a non-negative integer header such as ``X-RateLimit-Remaining``.

    Returns ``None`` for anything other than up to 18 ASCII digits, which keeps
    ``int()`` clear of its digit limit.
    """
    return int(value) if value and len(value) <= 18 and _is_digits(value) else None


def _http_date(value: str | None) -> datetime | None:
    """Parse an HTTP-date, or return ``None``."""
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: Python 3.12 lets an HTTP-date with a huge year reach datetime.
        return None
    # RFC 7231 obliges us to accept zone-less asctime dates. Reading one as local
    # time puts the wait out by the UTC offset.
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


def parse_retry_after(value: str | None, date: str | None = None) -> float | None:
    """Parse a ``Retry-After`` header in either RFC 7231 form.

    Parameters
    ----------
    value : str or None
        Raw header value: delay in seconds, or an HTTP-date.
    date : str or None, optional
        The response's ``Date`` header. An HTTP-date wait is measured against it,
        falling back to the local clock, so client clock skew cannot shift the wait.

    Returns
    -------
    float or None
        Seconds to wait, or ``None`` if absent or unparsable. Never negative, never
        above ``MAX_DELTA_SECONDS``.
    """
    if not value:
        return None
    if _is_digits(value):
        # Only 1*DIGIT is valid: float() would also take "nan", "inf", and "1e400".
        digits = value.lstrip("0") or "0"
        if len(digits) > len(str(MAX_DELTA_SECONDS)):
            return float(MAX_DELTA_SECONDS)
        return float(min(int(digits), MAX_DELTA_SECONDS))
    when = _http_date(value)
    if when is None:
        return None
    now = _http_date(date) or datetime.now(tz=UTC)
    return max(0.0, min((when - now).total_seconds(), float(MAX_DELTA_SECONDS)))


@dataclass(frozen=True, slots=True)
class Response:
    """A single HTTP response, detached from the underlying HTTP library.

    Parameters
    ----------
    url : str
        Final request URL, query string included.
    status : int
        HTTP status code. Always 2xx or 304; everything else raises instead.
    headers : mapping of str to str
        Response headers with lowercase keys. Repeated headers are comma-joined,
        as httpx2 returns them.
    content : bytes
        Raw response body.
    """

    url: str
    status: int
    headers: Mapping[str, str]
    content: bytes

    def json(self) -> Any:
        """Decode the body as JSON.

        Raises ``json.JSONDecodeError`` on an empty body, so branch on ``status``
        first: a 304 carries headers and nothing else.
        """
        return json.loads(self.content)


def _finalize(resp: httpx2.Response, *, attempts: int) -> Response:
    """Convert a successful httpx2 response, or raise a typed error."""
    url = str(resp.url)
    headers = dict(resp.headers.items())
    if resp.status_code < 300 or resp.status_code == 304:
        logger.debug("GET %s returned HTTP %d after %d attempt(s)", url, resp.status_code, attempts)
        return Response(url=url, status=resp.status_code, headers=headers, content=resp.content)
    if resp.status_code < 400:
        # follow_redirects is off so the caller never leaks credentials to another
        # host. Returning the bodyless redirect as a success would surface later as
        # a JSON decode error instead.
        location = headers.get("location", "an unspecified location")
        msg = f"server redirected to {location}"
        raise ServiceError(msg, url, status=resp.status_code, attempts=attempts)
    retry_after = parse_retry_after(headers.get("retry-after"), headers.get("date"))
    if resp.status_code == 429:
        remaining = headers.get("x-ratelimit-remaining")
        msg = "rate limit exceeded"
        raise RateLimitError(
            msg,
            url,
            retry_after=retry_after,
            remaining=parse_count(remaining),
            attempts=attempts,
        )
    raise ServiceError(
        resp.reason_phrase or "request failed",
        url,
        status=resp.status_code,
        attempts=attempts,
        retry_after=retry_after,
    )


class Transport:
    """Bounded, retrying async HTTP client.

    The client and its semaphore are created on the first request, so construction
    performs no I/O. Both then belong to that event loop for the life of the
    instance: a later request from a different loop raises rather than failing
    later under contention. Use one transport per loop, or ``aclose()`` between
    loops. Retries cover transport errors and the status codes in ``RETRY_STATUS``.

    Parameters
    ----------
    timeout : float, optional
        Seconds each attempt may take, from sending the request to reading the last
        body byte, defaults to 30. httpx2 also applies it to each connect, read,
        write, and pool wait. A timed-out attempt is retried. Waiting for a
        concurrency permit is not bounded.
    max_concurrent : int, optional
        Requests in flight at once, defaults to 4. An explicit semaphore, not a
        connection-pool limit: over HTTP/2 a single connection multiplexes many
        streams, so pool size does not bound concurrency. Held only while a
        request is in flight, never across a backoff wait.
    max_retries : int, optional
        Retries after the first attempt, defaults to 3.
    backoff : float, optional
        Base of the exponential backoff in seconds, defaults to 0.5.
    http2 : bool, optional
        Negotiate HTTP/2, defaults to ``True``.
    transport : httpx2.AsyncBaseTransport, optional
        Injected transport, for tests.

    Raises
    ------
    ValueError
        If ``max_concurrent`` is below 1, or ``max_retries`` or ``backoff`` is
        negative. A zero-permit semaphore would block every request forever.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_concurrent: int = 4,
        max_retries: int = 3,
        backoff: float = 0.5,
        http2: bool = True,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if max_concurrent < 1:
            msg = f"max_concurrent must be at least 1, got {max_concurrent}"
            raise ValueError(msg)
        if max_retries < 0:
            msg = f"max_retries must not be negative, got {max_retries}"
            raise ValueError(msg)
        if backoff < 0:
            msg = f"backoff must not be negative, got {backoff}"
            raise ValueError(msg)
        # Read-only through properties: the client and semaphore capture these on the
        # first request, so a later assignment would silently change nothing.
        self._timeout = timeout
        self._max_concurrent = max_concurrent
        self._max_retries = max_retries
        self._backoff = backoff
        self._http2 = http2
        self._transport = transport
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sem: asyncio.Semaphore | None = None
        self._client: httpx2.AsyncClient | None = None

    @property
    def timeout(self) -> float:
        """Seconds each attempt may take."""
        return self._timeout

    @property
    def max_concurrent(self) -> int:
        """Requests in flight at once."""
        return self._max_concurrent

    @property
    def max_retries(self) -> int:
        """Retries after the first attempt."""
        return self._max_retries

    @property
    def backoff(self) -> float:
        """Base of the exponential backoff in seconds."""
        return self._backoff

    @property
    def http2(self) -> bool:
        """Whether HTTP/2 is negotiated."""
        return self._http2

    def _bind(self) -> tuple[httpx2.AsyncClient, asyncio.Semaphore]:
        """Return the client and semaphore, creating them on first use.

        Both are created together so the owning loop is fixed on the first call.
        Binding the semaphore lazily instead would defer the mismatch to whichever
        later request happens to contend, making the failure load-dependent.
        """
        loop = asyncio.get_running_loop()
        if self._client is None or self._sem is None:
            self._loop = loop
            self._sem = asyncio.Semaphore(self.max_concurrent)
            # follow_redirects stays off: httpx2 strips `Authorization` across
            # hosts but not our `api_key` header, so a redirect would leak it.
            self._client = httpx2.AsyncClient(
                timeout=self.timeout,
                http2=self.http2,
                follow_redirects=False,
                event_hooks={"response": [_stop_at_redirect]},
                transport=self._transport,
            )
        elif self._loop is not loop:
            msg = (
                "Transport is bound to another event loop. Use one Transport per "
                "loop, or await aclose() before reusing it."
            )
            raise RuntimeError(msg)
        return self._client, self._sem

    def _delay(self, attempt: int) -> float:
        """Exponential backoff for a zero-based attempt number."""
        # Clamp the exponent: 2**1024 overflows float conversion, while any exponent
        # this large already exceeds MAX_SLEEP.
        return min(self.backoff * 2 ** min(attempt, 1000), MAX_SLEEP)

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Fetch a URL, retrying transient failures.

        Parameters
        ----------
        url : str
            Absolute URL to fetch.
        params : mapping of str to str, optional
            Query parameters, merged into any query already in ``url``. A name in both
            takes its value from ``params``. Credentials are not allowed here.
        headers : mapping of str to str, optional
            Request headers, credentials included.

        Returns
        -------
        Response
            The first 2xx response. A 304 is returned, not raised, so conditional
            revalidation works.

        Raises
        ------
        RateLimitError
            On a final 429, or on a 429 whose ``Retry-After`` exceeds ``MAX_SLEEP``.
            Carries the parsed wait, the remaining count, and the attempt count.
        ServiceError
            On any other status of 400 or above, on a redirect, on a malformed URL
            or undecodable body, when retries are exhausted, or when ``aclose()``
            ran while the call waited for a permit or a backoff.
        RuntimeError
            If called from an event loop other than the one this transport bound to.
        ValueError
            If ``params`` carries a credential such as ``api_key``.
        """
        _reject_credentials(url, params)
        client, sem = self._bind()
        for attempt in range(self.max_retries + 1):
            final = attempt == self.max_retries
            try:
                async with sem, asyncio.timeout(self.timeout):
                    if self._client is not client or client.is_closed:
                        # aclose() ran while this call waited for a permit or a backoff,
                        # or is still closing; httpx2 would raise a bare RuntimeError.
                        msg = "transport closed while the request waited"
                        raise ServiceError(msg, url, attempts=attempt)
                    # Parsed inside the try so a malformed URL raises a typed ServiceError.
                    target = _merge_query(url, params) if params else url
                    # httpx2 times each operation, so a body trickling in under the
                    # read timeout would otherwise hold the permit indefinitely.
                    resp = await client.get(target, headers=headers)
            except TimeoutError:
                failure = f"TimeoutError: attempt exceeded {self.timeout} s"
                transient = True
            except _RedirectedError as redirect:
                msg = f"server redirected to {redirect.location}"
                raise ServiceError(msg, url, status=redirect.status, attempts=attempt + 1) from None
            except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
                failure, transient = _describe(exc), _is_transient(exc)
            else:
                if resp.status_code not in RETRY_STATUS or final:
                    return _finalize(resp, attempts=attempt + 1)
                wait = parse_retry_after(resp.headers.get("retry-after"), resp.headers.get("date"))
                if wait is not None and wait > MAX_SLEEP:
                    # Retrying earlier than asked would burn the remaining attempts
                    # against a window that has not reopened. Hand the wait to the caller.
                    return _finalize(resp, attempts=attempt + 1)
                delay = self._delay(attempt) if wait is None else wait
                logger.log(
                    logging.WARNING if resp.status_code == 429 else logging.INFO,
                    "GET %s returned HTTP %d; retry %d of %d in %.1f s",
                    url,
                    resp.status_code,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            if final or not transient:
                # Raised outside the except block: a chained httpx2 error keeps its
                # request, whose headers hold the API key unredacted.
                raise ServiceError(failure, url, attempts=attempt + 1)
            delay = self._delay(attempt)
            logger.info(
                "GET %s failed with %s; retry %d of %d in %.1f s",
                url,
                failure,
                attempt + 1,
                self.max_retries,
                delay,
            )
            # ponytail: no jitter, so concurrent retries stay in lockstep.
            # Add it if offset fan-out (docs/design.md D-05) ever retries in bulk.
            await asyncio.sleep(delay)
        # Unreachable: the last attempt returns or raises. Keeps the return type total.
        raise AssertionError  # pragma: no cover

    async def aclose(self) -> None:
        """Close the underlying client and release the loop binding.

        Call this from the loop that used the transport, before that loop exits.
        Closing pooled connections from a different loop is what fails, and the
        binding is released either way so a failed close cannot strand the instance.
        """
        try:
            if self._client is not None:
                await self._client.aclose()
        finally:
            self._client = None
            self._sem = None
            self._loop = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
