"""Async HTTP transport.

The only module that imports ``httpx2``. Swapping the HTTP library is a
one-file edit as long as nothing else touches it.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import http.cookiejar
import inspect
import json
import logging
import math
import numbers
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, NoReturn, Self
from urllib.parse import parse_qsl, unquote, unquote_plus, urlencode, urlsplit

import httpx2

from water_ogcapi._logging import logger
from water_ogcapi._redact import CREDENTIAL_PARAMS, redact_url
from water_ogcapi.exceptions import RateLimitError, ServiceError, ValidationError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable, Mapping

    from water_ogcapi._governor import Governor

__all__ = ["Download", "Response", "Transport"]

try:
    __version__ = version("water-ogcapi")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
# The User-Agent a transport sends unless its caller names another (D-88).
USER_AGENT = f"water-ogcapi/{__version__}"

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
# RFC 9111 section 1.2.2: a delta-seconds value too large to represent reads as 2**31.
MAX_DELTA_SECONDS = 2**31
# Bytes of an error response an exception keeps: enough for a server's JSON or HTML
# reason, small enough that a failed page in a pickled checkpoint stays light (D-68).
MAX_ERROR_BODY = 64 * 1024
# Redirects a download follows before it gives up (D-69).
MAX_REDIRECTS = 5
# Headers a download keeps once a redirect leaves the first origin. Anything else, such
# as X-Api-Key or Authorization, stays with the origin it was meant for (D-69).
CROSS_ORIGIN_HEADERS = frozenset(
    {"accept", "accept-encoding", "if-modified-since", "if-none-match", "user-agent"}
)
# Response headers a Download keeps: what a later conditional download or a size check
# needs. Anything else, such as Set-Cookie or a Content-Location echoing a signed URL, goes.
DOWNLOAD_HEADERS = frozenset(
    {
        "age",
        "cache-control",
        "content-disposition",
        "content-length",
        "content-type",
        "date",
        "etag",
        "last-modified",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
    }
)


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


def reject_non_str(items: Iterable[object], what: str) -> None:
    """Refuse an item that is not exactly ``str``, naming only its type (D-65).

    A ``bytes`` name or a ``str`` subclass reads one way to a check and goes out
    another, so a credential check would miss it.
    """
    if odd := sorted({type(item).__name__ for item in items if type(item) is not str}):
        msg = f"{what} must be str, got {', '.join(odd)}"
        raise ValidationError(msg)


def reject_credentials(url: str, params: Mapping[str, str] | None) -> None:
    """Refuse a credential passed as a query parameter (docs/design.md D-18).

    Checks ``params`` and the query and fragment already in ``url``, with
    percent-encoding decoded. httpx2 keeps a fragment in logged and returned URLs even
    though it never sends it.
    """
    reject_non_str([url], "a URL")
    reject_non_str(params or {}, "query parameter names")
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
        raise ValidationError(msg)


def reject_userinfo(url: str, what: str) -> None:
    """Refuse a URL that holds userinfo or whose authority does not parse (D-86, D-90)."""
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        msg = f"{what} does not parse as a URL"
        raise ValidationError(msg) from None
    if "@" in netloc:
        msg = f"{what} must not carry userinfo"
        raise ValidationError(msg)


def query_strings(
    params: Mapping[str, str | int | float | bool] | None,
) -> dict[str, str] | None:
    """Copy ``params`` with each value as the text it goes out as (D-77).

    A ``bool`` goes out as ``true`` or ``false``, the spelling OGC API, ArcGIS, and NLDI
    servers take, and a number as Python writes it.
    """
    if not params:
        return None
    sent: dict[str, str] = {}
    for name, value in params.items():
        if isinstance(value, str):
            sent[name] = value
        elif isinstance(value, bool):
            sent[name] = "true" if value else "false"
        elif isinstance(value, numbers.Integral):
            sent[name] = str(int(value))
        elif isinstance(value, float) and math.isfinite(value):
            sent[name] = str(value)
        else:
            # Type only: a name has not passed the D-65 check yet and could hold a secret.
            kind = "non-finite float" if isinstance(value, float) else type(value).__name__
            msg = f"query parameter values must be str, int, float, or bool, got {kind}"
            raise ValidationError(msg)
    return sent


def _reject_unsendable_headers(headers: Mapping[str, str]) -> None:
    """Refuse a header value that is not printable ASCII, naming only the header (D-43).

    httpx2 raises ``UnicodeEncodeError`` for a non-ASCII value, and its ``repr`` holds
    the whole value, API key included.
    """
    reject_non_str(headers, "header names")
    reject_non_str(headers.values(), "header values")
    for name, value in headers.items():
        if not (value.isascii() and value.isprintable()):
            msg = f"header {name!r} must be printable ASCII"
            raise ValidationError(msg)


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


def sent_url(url: str, params: Mapping[str, str]) -> str:
    """Return the URL `Transport.request` sends for ``url`` and ``params``.

    An unparsable URL is returned as given, since the request would fail before sending.
    """
    try:
        return str(_merge_query(url, params) if params else httpx2.URL(url))
    except httpx2.InvalidURL:
        return url


def _describe(exc: Exception) -> str:
    """Name a failed request without echoing request data.

    h11 quotes an illegal header value back in ``LocalProtocolError``, so a key read
    with a trailing newline would land in the message verbatim.
    """
    if isinstance(exc, httpx2.LocalProtocolError):
        return "LocalProtocolError: malformed request, such as a header value with a line break"
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _describe_location(location: str) -> str:
    """Name a redirect target by scheme, host, and path only.

    A gateway can copy a header key into the redirect's query, so userinfo, query, and
    fragment never reach the message.
    """
    try:
        parts = urlsplit(location)
    except ValueError:
        return "an unparsable location"
    host = parts.netloc.rpartition("@")[2]
    target = f"{parts.scheme}://{host}{parts.path}" if parts.scheme else parts.path
    return f"{target} (query omitted)" if parts.query or parts.fragment else target


class _Unpaced:
    """The governor of a request sent without one: no waits, nothing read."""

    async def wait(self) -> None:
        return None

    def observe(self, _status: int, _headers: Mapping[str, str]) -> None:
        return None


_UNPACED = _Unpaced()


class _RedirectedError(Exception):
    """A redirect, caught before httpx2 parses its ``Location``."""

    def __init__(self, status: int, location: str, headers: Mapping[str, str]) -> None:
        super().__init__(status, location)
        self.status = status
        self.location = location
        self.headers = headers


async def _stop_at_redirect(response: httpx2.Response) -> None:
    """Response hook that turns a redirect into ``_RedirectedError``.

    httpx2 builds the follow-up request even with ``follow_redirects=False``, and a
    malformed ``Location`` then raises a ``RemoteProtocolError`` that reads as transient.
    """
    if response.has_redirect_location:
        raise _RedirectedError(response.status_code, response.headers["location"], response.headers)


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
        # OverflowError: older 3.13 and 3.14 releases let a huge-year date reach datetime (D-59).
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
        Final request URL as sent, query included with ``api_key`` masked, without
        userinfo or fragment (D-86).
    status : int
        HTTP status code. Always 2xx or 304; everything else raises instead.
    headers : mapping of str to str
        Response headers with lowercase keys. Repeated headers are comma-joined,
        as httpx2 returns them.
    content : bytes
        Raw response body.
    attempts : int, optional
        Requests sent to get this response, retries included.

    Raises
    ------
    ValidationError
        If ``url`` is not exactly ``str`` (D-65).
    """

    url: str
    status: int
    headers: Mapping[str, str]
    content: bytes
    attempts: int = 1

    def __post_init__(self) -> None:
        reject_non_str([self.url], "a URL")
        object.__setattr__(self, "url", redact_url(self.url))

    def json(self) -> Any:
        """Decode the body as JSON.

        Raises ``json.JSONDecodeError`` on an empty body, so branch on ``status``
        first: a 304 carries headers and nothing else.
        """
        return json.loads(self.content)

    def text(self) -> str:
        """Decode the body with the charset its ``Content-Type`` names (D-76).

        UTF-8 when the header names none, or names one Python does not know. Raises
        ``UnicodeDecodeError`` on bytes that charset cannot decode.
        """
        # The email parser keeps a ";" inside a quoted parameter in its parameter.
        header = Message()
        header["content-type"] = self.headers.get("content-type", "")
        charset = header.get_content_charset() or "utf-8"
        try:
            codecs.lookup(charset)
        except LookupError:
            charset = "utf-8"
        return self.content.decode(charset)


def _raise_for(
    resp: httpx2.Response,
    content: bytes | None,
    *,
    url: str,
    attempts: int,
    body: bytes | None,
    note: str = "",
    phrase: bool = True,
) -> NoReturn:
    """Raise the typed error for a 3xx, 4xx, or 5xx response.

    ``url`` names the request in the error; ``note`` is appended to the message. A
    ``content`` of ``None`` keeps no body, and ``phrase=False`` leaves out the server's
    reason phrase, which a download's signed host can fill.
    """
    method = resp.request.method
    headers = dict(resp.headers.items())
    if resp.status_code < 400:
        # follow_redirects is off so the caller never leaks credentials to another
        # host. Returning the bodyless redirect as a success would surface later as
        # a JSON decode error instead.
        location = headers.get("location")
        target = _describe_location(location) if location else "an unspecified location"
        msg = f"server redirected to {target}{note}"
        raise ServiceError(
            msg, url, status=resp.status_code, attempts=attempts, method=method, request_body=body
        )
    retry_after = parse_retry_after(headers.get("retry-after"), headers.get("date"))
    if resp.status_code == 429:
        remaining = headers.get("x-ratelimit-remaining")
        msg = f"rate limit exceeded{note}"
        raise RateLimitError(
            msg,
            url,
            retry_after=retry_after,
            remaining=parse_count(remaining),
            attempts=attempts,
            method=method,
            request_body=body,
            response_body=None if content is None else content[:MAX_ERROR_BODY],
        )
    msg_0 = f"{(resp.reason_phrase if phrase else '') or 'request failed'}{note}"
    raise ServiceError(
        msg_0,
        url,
        status=resp.status_code,
        attempts=attempts,
        retry_after=retry_after,
        method=method,
        request_body=body,
        response_body=None if content is None else content[:MAX_ERROR_BODY],
    )


def _undecodable(resp: httpx2.Response, client: httpx2.AsyncClient) -> str:
    """Return the codings of ``resp`` that httpx2 leaves encoded, comma-joined."""
    # httpx2 asks for exactly the codings it decodes and passes any other through.
    decodable = {c.strip() for c in client.headers.get("accept-encoding", "").split(",")}
    values = resp.headers.get_list("content-encoding", split_commas=True)
    codings = (c.strip().lower() for c in values)
    return ", ".join(c for c in codings if c not in {*decodable, "", "identity"})


def _finalize(
    resp: httpx2.Response, client: httpx2.AsyncClient, *, attempts: int, body: bytes | None
) -> Response:
    """Convert a successful httpx2 response, or raise a typed error."""
    url = str(resp.url)
    if resp.status_code < 300 and (coding := _undecodable(resp, client)):
        msg = f"cannot decode Content-Encoding {coding}"
        raise ServiceError(
            msg,
            url,
            status=resp.status_code,
            attempts=attempts,
            method=resp.request.method,
            request_body=body,
        )
    if resp.status_code < 300 or resp.status_code == 304:
        logger.debug(
            "%s %s returned HTTP %d after %d attempt(s)",
            resp.request.method,
            redact_url(url),
            resp.status_code,
            attempts,
        )
        return Response(
            url=url,
            status=resp.status_code,
            headers=dict(resp.headers.items()),
            content=resp.content,
            attempts=attempts,
        )
    try:
        content = resp.content
    except httpx2.ResponseNotRead:
        # An error whose body failed to read (D-49).
        content = None
    _raise_for(resp, content, url=url, attempts=attempts, body=body)


def _origin(url: httpx2.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


def _bare(url: httpx2.URL) -> str:
    """Name ``url`` by scheme, host, port, and path: a query can hold a signature."""
    host = url.netloc.decode("ascii").rpartition("@")[2]
    return f"{url.scheme}://{host}{url.raw_path.partition(b'?')[0].decode('ascii')}"


def _refusal(current: httpx2.URL, target: httpx2.URL) -> str | None:
    """Return why a download must not follow a redirect to ``target``, or ``None``."""
    checks = (
        (target.scheme not in {"http", "https"}, f"unsupported scheme {target.scheme!r}"),
        (current.scheme == "https" and target.scheme == "http", "a downgrade from https to http"),
        # httpx2 would turn userinfo into an Authorization header for the next host.
        (bool(target.userinfo), "the location carries userinfo"),
    )
    reason = next((why for refused, why in checks if refused), None)
    if reason is None:
        try:
            reject_credentials(str(target), None)
        except ValidationError:
            reason = "the location carries a credential"
    return reason


def _create_part(target: Path) -> tuple[BinaryIO, Path]:
    """Create the temporary file a download writes, beside ``target`` (D-69).

    ``O_EXCL`` under a random name refuses any entry already there, a planted symlink
    included, and the mode is the one ``open()`` gives, the umask applied by the system.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    while True:
        part = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
        try:
            handle = os.open(part, flags, 0o666)
        except FileExistsError:
            continue
        return os.fdopen(handle, "wb"), part


def _set_remote_time(part: Path, last_modified: str | None) -> None:
    """Set the times of ``part`` from ``last_modified``, when it parses (D-69)."""
    when = _http_date(last_modified)
    if when is None:
        return
    try:
        stamp = when.timestamp()
        os.utime(part, (stamp, stamp))
    except (OSError, OverflowError, ValueError) as exc:
        # The server's text picks the time, and the whole file is already here.
        logger.debug("left the local file time: %s", type(exc).__name__)


async def _finish_part(
    part: Path,
    kept: Mapping[str, str],
    check: Callable[[Path, Mapping[str, str]], object] | None,
    *,
    remote_time: bool,
) -> None:
    """Run the caller's check on the finished ``part``, then set its time (D-69)."""
    checked = None if check is None else check(part, kept)
    if inspect.isawaitable(checked):
        await checked
    if remote_time:
        _set_remote_time(part, kept.get("last-modified"))


@dataclass(frozen=True, slots=True)
class Download:
    """A file `Transport.download` wrote, and the response that filled it (D-69).

    Parameters
    ----------
    path : Path
        The file. After a 304 it is the existing file, untouched.
    url : str
        The request as first sent, by scheme, host, port, and path: a query can hold a
        signature, so none is kept, and neither is a redirect target.
    status : int
        The status of the last response, 2xx or 304.
    headers : mapping of str to str
        The last response's headers, with lowercase names, such as ``etag`` and
        ``last-modified`` for a later conditional download.
    size : int
        Bytes written, 0 after a 304.
    attempts : int
        Attempts made, retries included.
    redirects : int
        Redirects followed on the attempt that succeeded.

    Raises
    ------
    ValidationError
        If ``url`` is not exactly ``str`` (D-65).

    Notes
    -----
    A hand-built ``url`` loses its userinfo and fragment and has ``api_key`` masked, as
    `Response` does (D-91).
    """

    path: Path
    url: str
    status: int
    headers: Mapping[str, str]
    size: int
    attempts: int
    redirects: int

    def __post_init__(self) -> None:
        reject_non_str([self.url], "a URL")
        object.__setattr__(self, "url", redact_url(self.url))


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
        Requests in flight at once across every host, defaults to 16 (D-92). An explicit
        semaphore, not a connection-pool limit: over HTTP/2 a single connection
        multiplexes many streams, so pool size does not bound concurrency. Held only
        while a request is in flight, never across a backoff wait.
    max_per_origin : int, optional
        Requests in flight at once to one scheme, host, and port, defaults to 4, so long
        downloads from one host leave room for the others. Both bounds apply, so one
        host gets the smaller of the two.
    max_retries : int, optional
        Retries after the first attempt, defaults to 3.
    backoff : float, optional
        Base of the exponential backoff in seconds, defaults to 0.5.
    http2 : bool, optional
        Negotiate HTTP/2, defaults to ``True``.
    transport : httpx2.AsyncBaseTransport, optional
        Injected transport, for tests.
    user_agent : str, optional
        The ``User-Agent`` of every request, redirect hops included, so a server can
        tell which package called it (D-78). ``water-ogcapi/<version>`` when omitted
        (D-88). A ``User-Agent`` in a call's ``headers`` takes precedence.

    Raises
    ------
    ValidationError
        If ``max_concurrent`` or ``max_per_origin`` is below 1, ``max_retries`` or
        ``backoff`` is negative,
        or ``user_agent`` is not a printable ASCII ``str``. A zero-permit semaphore
        would block every request forever.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_concurrent: int = 16,
        max_per_origin: int = 4,
        max_retries: int = 3,
        backoff: float = 0.5,
        http2: bool = True,
        transport: httpx2.AsyncBaseTransport | None = None,
        user_agent: str | None = None,
    ) -> None:
        if user_agent is not None:
            reject_non_str([user_agent], "user_agent")
            _reject_unsendable_headers({"User-Agent": user_agent})
        if max_concurrent < 1:
            msg = f"max_concurrent must be at least 1, got {max_concurrent}"
            raise ValidationError(msg)
        if max_per_origin < 1:
            msg = f"max_per_origin must be at least 1, got {max_per_origin}"
            raise ValidationError(msg)
        if max_retries < 0:
            msg = f"max_retries must not be negative, got {max_retries}"
            raise ValidationError(msg)
        if backoff < 0:
            msg = f"backoff must not be negative, got {backoff}"
            raise ValidationError(msg)
        # Read-only through properties: the client and semaphore capture these on the
        # first request, so a later assignment would silently change nothing.
        self._timeout = timeout
        self._max_concurrent = max_concurrent
        self._max_per_origin = max_per_origin
        self._max_retries = max_retries
        self._backoff = backoff
        self._http2 = http2
        self._transport = transport
        self._user_agent = USER_AGENT if user_agent is None else user_agent
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sem: asyncio.Semaphore | None = None
        self._origins: dict[tuple[str, str, int | None], asyncio.Semaphore] = {}
        self._client: httpx2.AsyncClient | None = None

    @property
    def timeout(self) -> float:
        """Seconds each attempt may take."""
        return self._timeout

    @property
    def max_concurrent(self) -> int:
        """Requests in flight at once across every host."""
        return self._max_concurrent

    @property
    def max_per_origin(self) -> int:
        """Requests in flight at once to one scheme, host, and port."""
        return self._max_per_origin

    @property
    def max_retries(self) -> int:
        """Retries after the first attempt."""
        return self._max_retries

    @property
    def backoff(self) -> float:
        """Base of the exponential backoff in seconds."""
        return self._backoff

    @property
    def user_agent(self) -> str:
        """The ``User-Agent`` sent with every request."""
        return self._user_agent

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
            self._origins = {}
            # follow_redirects stays off: httpx2 strips `Authorization` across
            # hosts but not our `X-Api-Key` header, so a redirect would leak it.
            self._client = httpx2.AsyncClient(
                timeout=self.timeout,
                http2=self.http2,
                headers={"User-Agent": self.user_agent},
                follow_redirects=False,
                # A jar that keeps nothing: a cookie would cross the services that share
                # this transport, and a download's redirects, unchecked (D-69).
                cookies=http.cookiejar.CookieJar(
                    policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
                ),
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

    def _origin_permit(self, url: httpx2.URL) -> asyncio.Semaphore:
        """Return the bound of ``url``'s origin, taken before the total's (D-92).

        Taking the total first would let requests queued for one busy origin hold every
        total permit.
        """
        # shortcut: an origin's entry stays until aclose(); evict idle ones if a transport
        # ever meets origins without bound.
        return self._origins.setdefault(_origin(url), asyncio.Semaphore(self.max_per_origin))

    @contextlib.asynccontextmanager
    async def _permits(
        self, url: httpx2.URL, sem: asyncio.Semaphore, pacer: Governor | _Unpaced
    ) -> AsyncGenerator[None]:
        """Hold ``url``'s origin permit and a total permit, then book a slot (D-49, D-92).

        Booked before either queue, every waiting call would clear the governor at once,
        and no reading or 429 would reach it.
        """
        async with self._origin_permit(url), sem:
            await pacer.wait()
            yield

    def _delay(self, attempt: int) -> float:
        """Exponential backoff for a zero-based attempt number."""
        # Clamp the exponent: 2**1024 overflows float conversion, while any exponent
        # this large already exceeds MAX_SLEEP.
        return min(self.backoff * 2 ** min(attempt, 1000), MAX_SLEEP)

    def _retry_delay(self, resp: httpx2.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying ``resp``, or ``None`` to return or raise it now."""
        if resp.status_code not in RETRY_STATUS:
            return None
        wait = parse_retry_after(resp.headers.get("retry-after"), resp.headers.get("date"))
        if wait is not None and wait > MAX_SLEEP:
            # Retrying earlier than asked would burn the remaining attempts against a
            # window that has not reopened. Hand the wait to the caller.
            return None
        return self._delay(attempt) if wait is None else wait

    def _retries(self, override: int | None) -> int:
        """Return the retries one call may make: ``override``, or the transport's."""
        if override is None:
            return self.max_retries
        if override < 0:
            msg = f"max_retries must not be negative, got {override}"
            raise ValidationError(msg)
        return override

    def _timeout_of(self, override: float | None) -> float:
        """Return the timeout of one call: ``override``, or the transport's."""
        if override is None:
            return self.timeout
        if not (override > 0 and math.isfinite(override)):
            msg = f"timeout must be a positive, finite number of seconds, got {override}"
            raise ValidationError(msg)
        return override

    async def _attempt(
        self,
        client: httpx2.AsyncClient,
        sem: asyncio.Semaphore,
        target: httpx2.URL,
        *,
        pacer: Governor | _Unpaced,
        method: str,
        headers: dict[str, str],
        content: bytes | None,
        made: int,
        seconds: float,
    ) -> httpx2.Response:
        """Send one attempt under a permit and read its body.

        A 4xx or 5xx comes back even if its body failed to read, since its status and
        headers stand without it (D-49); any other failure raises.
        """
        resp: httpx2.Response | None = None
        try:
            async with self._permits(target, sem, pacer), asyncio.timeout(seconds):
                if self._client is not client or client.is_closed:
                    # aclose() ran while this call waited for a permit or a backoff, or is
                    # still closing; httpx2 would raise a bare RuntimeError.
                    msg = "transport closed while the request waited"
                    raise ServiceError(
                        msg, str(target), attempts=made, method=method, request_body=content
                    )
                # httpx2 times each operation, so a body trickling in under the read timeout
                # would otherwise hold the permit indefinitely.
                resp = await client.send(
                    client.build_request(
                        method, target, headers=headers, content=content, timeout=seconds
                    ),
                    stream=True,
                )
                # Read on arrival, so a 429's hold applies while its body is still coming.
                pacer.observe(resp.status_code, resp.headers)
                try:
                    # Unread when httpx2 cannot decode it, so it raises unretried (D-89).
                    if resp.status_code >= 300 or not _undecodable(resp, client):
                        await resp.aread()
                except httpx2.HTTPError:
                    if resp.status_code < 400:
                        raise
                finally:
                    await resp.aclose()
        except TimeoutError:
            # The deadline can also fall while an error's body is read.
            if resp is None or resp.status_code < 400:
                raise
        return resp

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool] | None = None,
        headers: Mapping[str, str] | None = None,
        governor: Governor | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Response:
        """Send a ``GET`` through `request`, which documents the parameters and errors."""
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            governor=governor,
            max_retries=max_retries,
            timeout=timeout,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int | float | bool] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        governor: Governor | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Response:
        """Send a request, retrying transient failures.

        A ``POST`` is retried like a ``GET``: this package posts only queries, which
        change nothing on the server (docs/design.md D-38).

        Parameters
        ----------
        method : str
            HTTP method, such as ``GET`` or ``POST``.
        url : str
            Absolute URL to fetch. Its query stays in the result, errors, and log
            records, so a credential goes in ``headers``, and a signed URL through
            `download` (D-86).
        params : mapping of str to str, int, float, or bool, optional
            Query parameters, merged into any query already in ``url``. A name in both
            takes its value from ``params``. A ``bool`` goes out as ``true`` or
            ``false`` and a number as Python writes it (D-77). Credentials are not
            allowed here.
        headers : mapping of str to str, optional
            Request headers, credentials included. Each value must be printable ASCII.
        content : bytes, optional
            Request body, sent unchanged on every attempt.
        governor : Governor, optional
            Paces every attempt, retries included, and reads the rate-limit headers of
            every response (D-06). Each service class passes its own.
        max_retries : int, optional
            Retries after the first attempt for this call, the transport's
            ``max_retries`` when omitted (D-74). ``0`` sends one attempt, for a request
            that must not be replayed or a failure the caller handles itself.
        timeout : float, optional
            Seconds each attempt of this call may take, and httpx2's limit on each
            connect, read, write, and pool wait, the transport's ``timeout`` when omitted
            (D-19). A call cannot turn the timeout off.

        Returns
        -------
        Response
            The first 2xx response. A 304 is returned, not raised, so conditional
            revalidation works.

        Raises
        ------
        RateLimitError
            On a final 429, or on a 429 whose ``Retry-After`` exceeds ``MAX_SLEEP``.
            Carries the parsed wait, the remaining count, and the attempt count. Every
            error carries the method and body of the request (D-38).
        ServiceError
            On any other status of 400 or above, on a redirect, on a malformed URL
            or undecodable body, on a 2xx whose ``Content-Encoding`` httpx2 cannot
            decode (D-89), when retries are exhausted, or when ``aclose()`` ran while
            the call waited for a permit or a backoff.
        RuntimeError
            If called from an event loop other than the one this transport bound to.
        ValidationError
            If ``params`` carries a credential such as ``api_key``, ``url`` or a
            parameter or header name or header value is not exactly ``str`` (D-65), a
            header value is not printable ASCII, ``max_retries`` is negative, or
            ``timeout`` is not a positive, finite number. The message names the header,
            never its value. Also if a ``params`` value is of another type, or a
            non-finite float.
        """
        retries = self._retries(max_retries)
        seconds = self._timeout_of(timeout)
        # Copies, so every attempt sends the params and headers that passed the checks.
        query = query_strings(params)
        reject_credentials(url, query)
        headers = dict(headers or {})
        _reject_unsendable_headers(headers)
        method = method.upper()
        client, sem = self._bind()
        pacer = _UNPACED if governor is None else governor
        failure = ""
        try:
            target = _merge_query(url, query) if query else httpx2.URL(url)
        except httpx2.InvalidURL as exc:
            target, failure = None, _describe(exc)
        if target is None:
            raise ServiceError(failure, url, attempts=1, method=method, request_body=content)
        # Every error names the request as sent, query included (docs/design.md D-24).
        sent = str(target)
        for attempt in range(retries + 1):
            final = attempt == retries
            status: int | None = None
            try:
                resp = await self._attempt(
                    client,
                    sem,
                    target,
                    pacer=pacer,
                    method=method,
                    headers=headers,
                    content=content,
                    made=attempt,
                    seconds=seconds,
                )
            except TimeoutError:
                failure = f"TimeoutError: attempt exceeded {seconds} s"
                transient = True
            except _RedirectedError as redirect:
                pacer.observe(redirect.status, redirect.headers)
                failure = f"server redirected to {_describe_location(redirect.location)}"
                transient, status = False, redirect.status
            except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
                failure, transient = _describe(exc), _is_transient(exc)
            else:
                delay = None if final else self._retry_delay(resp, attempt)
                if delay is None:
                    return _finalize(resp, client, attempts=attempt + 1, body=content)
                logger.log(
                    logging.WARNING if resp.status_code == 429 else logging.INFO,
                    "%s %s returned HTTP %d; retry %d of %d in %.1f s",
                    method,
                    redact_url(sent),
                    resp.status_code,
                    attempt + 1,
                    retries,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            if final or not transient:
                # Raised outside the except block: a chained httpx2 error keeps its
                # request, whose headers hold the API key, and a redirect keeps its raw
                # Location.
                raise ServiceError(
                    failure,
                    sent,
                    status=status,
                    attempts=attempt + 1,
                    method=method,
                    request_body=content,
                )
            delay = self._delay(attempt)
            logger.info(
                "%s %s failed with %s; retry %d of %d in %.1f s",
                method,
                redact_url(sent),
                failure,
                attempt + 1,
                retries,
                delay,
            )
            # shortcut: no jitter, so the retries of NWIS pieces (D-56) that fail together
            # stay in lockstep, at most max_per_origin of them; add it if that load matters.
            await asyncio.sleep(delay)
        # Unreachable: the last attempt returns or raises. Keeps the return type total.
        raise AssertionError  # pragma: no cover

    async def _receive(
        self,
        resp: httpx2.Response,
        out: BinaryIO,
        *,
        named: str,
        where: str,
        quiet: bool,
        attempt: int,
        retries: int,
        redirects: int,
    ) -> tuple[int, dict[str, str], int, int] | float:
        """Write a 2xx body to ``out``; for an error, return the retry delay or raise.

        A ``quiet`` error keeps neither the body nor the reason phrase, since a signed
        URL's host can echo the signature in either.
        """
        if resp.status_code < 300 or resp.status_code == 304:
            size = 0
            if resp.status_code != 304:
                out.seek(0)
                out.truncate()
                async for chunk in resp.aiter_bytes():
                    out.write(chunk)
                    size += len(chunk)
                out.flush()
            kept = {k: v for k, v in resp.headers.items() if k in DOWNLOAD_HEADERS}
            logger.debug(
                "GET %s returned HTTP %d%s after %d attempt(s)",
                named,
                resp.status_code,
                where,
                attempt + 1,
            )
            return resp.status_code, kept, size, redirects
        content: bytes | None = None
        if not quiet:
            content = b""
            try:
                async for chunk in resp.aiter_bytes():
                    content += chunk
                    if len(content) >= MAX_ERROR_BODY:
                        break
            except httpx2.HTTPError:
                # An error's status and headers stand without its body (D-49).
                content = None
        delay = None if attempt == retries else self._retry_delay(resp, attempt)
        if delay is None:
            _raise_for(
                resp,
                content,
                url=named,
                attempts=attempt + 1,
                body=None,
                note=where,
                phrase=not quiet,
            )
        logger.log(
            logging.WARNING if resp.status_code == 429 else logging.INFO,
            "GET %s returned HTTP %d%s; retry %d of %d in %.1f s",
            named,
            resp.status_code,
            where,
            attempt + 1,
            retries,
            delay,
        )
        return delay

    @staticmethod
    def _next_hop(current: httpx2.URL, location: str, redirects: int) -> httpx2.URL | str:
        """Return the URL a redirect points to, or why a download must not follow it."""
        if redirects >= MAX_REDIRECTS:
            return f"more than {MAX_REDIRECTS} redirects"
        try:
            target = current.join(location)
        except (httpx2.InvalidURL, ValueError):
            return "the location does not parse"
        return _refusal(current, target) or target

    async def _stream_to(
        self,
        client: httpx2.AsyncClient,
        sem: asyncio.Semaphore,
        first: httpx2.URL,
        *,
        headers: dict[str, str],
        out: BinaryIO,
        pacer: Governor | _Unpaced,
        attempt: int,
        retries: int,
        seconds: float,
        public_query: bool,
    ) -> tuple[int, dict[str, str], int, int] | float:
        """Run one download attempt; return the result, or the delay before a retry.

        Raises the attempt's error when it is final or not worth retrying. Every error is
        raised outside an ``except`` block, so none chains a raw ``Location``.
        """
        final = attempt == retries
        # Quiet once any URL of the chain has a query or the chain has left the first
        # origin: from then on a server's text can echo a signature. public_query speaks
        # for the first URL's query only.
        named, quiet = _bare(first), bool(first.query) and not public_query
        target, hop_headers, redirects = first, headers, 0
        try:
            while True:
                redirect: _RedirectedError | None = None
                try:
                    async with self._permits(target, sem, pacer):
                        if self._client is not client or client.is_closed:
                            msg = "transport closed while the request waited"
                            raise ServiceError(msg, named, attempts=attempt, method="GET")
                        async with client.stream(
                            "GET", target, headers=hop_headers, timeout=seconds
                        ) as resp:
                            pacer.observe(resp.status_code, resp.headers)
                            where = (
                                ""
                                if target == first
                                else f" from {_describe_location(str(target))}"
                            )
                            if resp.status_code < 300 and (coding := _undecodable(resp, client)):
                                # The coding is server text, which a signed host can fill.
                                named_coding = (
                                    "the Content-Encoding"
                                    if quiet
                                    else f"Content-Encoding {coding}"
                                )
                                msg = f"cannot decode {named_coding}{where}"
                                raise ServiceError(
                                    msg,
                                    named,
                                    status=resp.status_code,
                                    attempts=attempt + 1,
                                    method="GET",
                                )
                            return await self._receive(
                                resp,
                                out,
                                named=named,
                                where=where,
                                quiet=quiet,
                                attempt=attempt,
                                retries=retries,
                                redirects=redirects,
                            )
                except _RedirectedError as caught:
                    redirect = caught
                pacer.observe(redirect.status, redirect.headers)
                status, location = redirect.status, redirect.location
                del redirect
                hop = self._next_hop(target, location, redirects)
                if isinstance(hop, str):
                    msg = f"server redirected to {_describe_location(location)}: {hop}"
                    raise ServiceError(msg, named, status=status, attempts=attempt + 1)
                target, redirects = hop, redirects + 1
                quiet = quiet or bool(target.query)
                if _origin(target) != _origin(first):
                    quiet = True
                    hop_headers = {
                        k: v for k, v in headers.items() if k.lower() in CROSS_ORIGIN_HEADERS
                    }
                logger.info(
                    "GET %s redirected (%d) to %s", named, status, _describe_location(str(target))
                )
        except (httpx2.HTTPError, httpx2.InvalidURL) as exc:
            # A library message can quote the URL or status line it failed on, so a URL
            # with a query, or a later hop, is named by the exception type only.
            if target == first:
                failure = type(exc).__name__ if quiet else _describe(exc)
            else:
                failure = f"{type(exc).__name__} from {_describe_location(str(target))}"
            transient = _is_transient(exc)
        if final or not transient:
            raise ServiceError(failure, named, attempts=attempt + 1, method="GET")
        delay = self._delay(attempt)
        logger.info(
            "GET %s failed with %s; retry %d of %d in %.1f s",
            named,
            failure,
            attempt + 1,
            retries,
            delay,
        )
        return delay

    async def download(
        self,
        url: str,
        path: str | os.PathLike[str],
        *,
        params: Mapping[str, str | int | float | bool] | None = None,
        headers: Mapping[str, str] | None = None,
        governor: Governor | None = None,
        total: float | None = 3600.0,
        max_retries: int | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
        public_query: bool = False,
        check: Callable[[Path, Mapping[str, str]], object] | None = None,
        remote_time: bool = False,
    ) -> Download:
        """Stream a ``GET`` response to a file, following redirects (D-69).

        The body goes to a temporary ``.part`` file beside ``path``, created under a name
        only this call knows, renamed to ``path`` once complete and removed on any
        failure, so ``path`` holds either the old file or the whole new one. Parent
        folders are created. Every attempt starts the file over, and a 304, the answer
        to an ``If-None-Match`` or ``If-Modified-Since`` in ``headers``, leaves ``path``
        as it was. ``Accept-Encoding: identity`` is sent unless ``headers`` names an
        encoding, so the file holds the bytes the server stores. The call's ``timeout``
        applies to each connect and each read of the body, on every redirect hop.

        Up to ``MAX_REDIRECTS`` redirects are followed, each paced by ``governor`` and
        sent under its own concurrency permit. Once one leaves the scheme, host, and
        port of ``url``, only the headers in ``CROSS_ORIGIN_HEADERS`` go along, so a key
        header stays with its own host. A redirect from ``https`` to ``http``, to a URL
        with userinfo, or to a URL whose query carries a credential such as ``api_key``,
        is refused. A query can hold a signature, so the result, errors, and log records
        name each URL by scheme, host, port, and path only.

        Parameters
        ----------
        url : str
            Absolute URL of the file.
        path : str or path-like
            Where to write it.
        params, headers, governor, max_retries
            As for `request`. ``params`` go on the first request only.
        total : float or None, optional
            Seconds the whole call may take, retries, redirects, and waits included,
            defaults to 3600. ``None`` bounds only each read.
        timeout : float, optional
            httpx2's limit on each connect, read, write, and pool wait of this call, the
            transport's ``timeout`` when omitted (D-19). It sets no deadline on an attempt:
            ``total`` bounds the call.
        public_query : bool, optional
            States that the query of ``url`` and ``params`` holds no credential, so an
            error from the first origin keeps its body, reason phrase, and library message
            as `request` errors do (D-68), defaults to ``False``. Leave it off for a signed
            URL: a server's text can echo the signature into the error, its pickle, and
            log records. A redirect to a URL with a query, or to another origin, goes quiet
            whatever this says. The result and errors still name the URL without its query.
        check : callable, optional
            Called with the path of the finished ``.part`` file and the headers
            `Download` keeps, before the rename, and awaited if it returns an awaitable. A
            plain function runs on the event loop, so keep it short. An exception it raises
            propagates as raised, unretried, with the ``.part`` removed and ``path`` keeping
            its old file, which stops a 200 holding an error document from replacing a good
            file. ``total`` does not bound it. A 304 skips it.
        remote_time : bool, optional
            Give the file the time in the response's ``Last-Modified`` before the rename,
            as ``curl -R`` does, so it never exists with the local write time, defaults to
            ``False``. A missing or unparsable header, or a time the file system refuses,
            leaves the local time.

        Returns
        -------
        Download
            The path, status, kept headers, size, and attempt and redirect counts.

        Raises
        ------
        ServiceError
            As `request` does, and also for a refused redirect, more than
            ``MAX_REDIRECTS`` redirects, or a call that outlasted ``total``.
        ValidationError
            As for `request`.
        """
        retries = self._retries(max_retries)
        seconds = self._timeout_of(timeout)
        query = query_strings(params)
        reject_credentials(url, query)
        headers = dict(headers or {})
        _reject_unsendable_headers(headers)
        if not any(name.lower() == "accept-encoding" for name in headers):
            headers["Accept-Encoding"] = "identity"
        try:
            first = _merge_query(url, query) if query else httpx2.URL(url)
        except httpx2.InvalidURL:
            first = None
        if first is None:
            msg = "malformed URL"
            raise ServiceError(msg, _describe_location(url), attempts=0, method="GET")
        named = _bare(first)
        target = Path(path)
        client, sem = self._bind()
        pacer = _UNPACED if governor is None else governor
        target.parent.mkdir(parents=True, exist_ok=True)
        out, part = _create_part(target)
        made = 0
        try:
            try:
                async with asyncio.timeout(total):
                    for attempt in range(retries + 1):
                        made = attempt + 1
                        outcome = await self._stream_to(
                            client,
                            sem,
                            first,
                            headers=headers,
                            out=out,
                            pacer=pacer,
                            attempt=attempt,
                            retries=retries,
                            seconds=seconds,
                            public_query=public_query,
                        )
                        if isinstance(outcome, tuple):
                            break
                        await asyncio.sleep(outcome)
                    else:
                        # Unreachable: the last attempt returns or raises.
                        raise AssertionError  # pragma: no cover
            except TimeoutError:
                msg = f"download exceeded its total budget of {total} s"
                raise ServiceError(msg, named, attempts=made) from None
            status, kept, size, redirects = outcome
            # Closed first: Windows does not rename a file that is still open.
            out.close()
            if status != 304:
                # Outside the budget's scope, so a check's TimeoutError stays its own.
                await _finish_part(part, kept, check, remote_time=remote_time)
                part.replace(target)
            return Download(
                path=target,
                url=named,
                status=status,
                headers=kept,
                size=size,
                attempts=made,
                redirects=redirects,
            )
        finally:
            # Synchronous, so the cleanup runs even while the task is being cancelled, and
            # even when closing fails on a full disk.
            try:
                out.close()
            finally:
                part.unlink(missing_ok=True)

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
            self._origins = {}
            self._loop = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    def __getstate__(self) -> dict[str, Any]:
        # The client and semaphores belong to this process's loop; a load binds anew (D-64).
        return {**self.__dict__, "_loop": None, "_sem": None, "_origins": {}, "_client": None}
