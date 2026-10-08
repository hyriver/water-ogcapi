"""Result types: one response document per `Page`, the aggregate in `QueryResult`.

Each type redacts credentials in its constructor (docs/design.md D-04, D-29), so a
pickled, logged, or displayed result never carries the API key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from water_ogcapi._transport import CREDENTIAL_PARAMS

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Completeness", "Link", "Page", "Pagination", "QueryResult", "StopReason"]

Completeness = Literal["complete", "limited", "unknown"]
StopReason = Literal["limit", "failure"]

REDACTED = "REDACTED"
# Response headers a Page keeps. Anything unlisted is dropped, so a header nobody
# thought of cannot carry a credential into a result.
KEPT_HEADERS = frozenset(
    {
        "age",
        "cache-control",
        "content-crs",
        "content-type",
        "date",
        "etag",
        "last-modified",
        "retry-after",
        "x-api-umbrella-request-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
    }
)
_QUERY_ENTRY = re.compile(r"(?:^|(?<=[&;]))([^&;=]*)=([^&;]*)")


def _mask_entry(match: re.Match[str]) -> str:
    name = match[1]
    return f"{name}={REDACTED}" if unquote_plus(name).lower() in CREDENTIAL_PARAMS else match[0]


def redact_url(url: str) -> str:
    """Mask credential query values and drop userinfo and fragment from ``url``.

    Other query entries keep their bytes, so an opaque cursor still replays. A URL
    that needs no change is returned as given, and an unparsable one as ``REDACTED``.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    query = _QUERY_ENTRY.sub(_mask_entry, parts.query)
    netloc = parts.netloc.rpartition("@")[2]
    if (query, netloc, parts.fragment) == (parts.query, parts.netloc, ""):
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _redact_params(params: Mapping[str, str]) -> MappingProxyType[str, str]:
    return MappingProxyType(
        {k: REDACTED if k.lower() in CREDENTIAL_PARAMS else v for k, v in params.items()}
    )


def _keep_headers(headers: Mapping[str, str]) -> MappingProxyType[str, str]:
    return MappingProxyType({k.lower(): v for k, v in headers.items() if k.lower() in KEPT_HEADERS})


@dataclass(frozen=True, slots=True)
class Link:
    """A link from a response document, with credentials masked in ``href``.

    Parameters
    ----------
    href : str
        Target URL.
    rel : str, optional
        Relation type, such as ``next`` or ``self``.
    type : str, optional
        Media type of the target.
    title : str, optional
        Human-readable label.
    """

    href: str
    rel: str | None = None
    type: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "href", redact_url(self.href))


@dataclass(frozen=True, slots=True)
class Page:
    """One response document and the evidence of how it was fetched (D-22).

    Parameters
    ----------
    url : str
        The request as sent, query included. Credential values are masked, and
        userinfo and fragment dropped.
    params : mapping of str to str
        The caller's query parameters, with credential values masked.
    status : int
        HTTP status code.
    headers : mapping of str to str
        Response headers in ``KEPT_HEADERS``, with lowercase names.
    links : tuple of Link
        Links from the document.
    fetched_at : float
        When the response arrived, in seconds since the epoch.
    number_matched : int or None
        ``numberMatched`` as the server reported it, ``None`` when absent.
    number_returned : int or None
        ``numberReturned`` as the server reported it, ``None`` when absent.
    feature_count : int
        Features received in this document.
    body : dict, optional
        The decoded document, a Feature or a FeatureCollection. The one mutable
        field; the caller owns it.
    content : bytes, optional
        The response body after HTTP content decoding, before JSON decoding.

    Notes
    -----
    ``params`` and ``headers`` are read-only copies. ``body`` and ``content`` are the
    server's document as received, so redaction does not reach them; the key stays
    out of them because it never travels in a query (D-18).
    """

    url: str
    params: Mapping[str, str]
    status: int
    headers: Mapping[str, str]
    links: tuple[Link, ...]
    fetched_at: float
    number_matched: int | None
    number_returned: int | None
    feature_count: int
    body: dict[str, Any] | None = field(default=None, repr=False)
    content: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", redact_url(self.url))
        object.__setattr__(self, "params", _redact_params(self.params))
        object.__setattr__(self, "headers", _keep_headers(self.headers))
        object.__setattr__(self, "links", tuple(self.links))

    def json(self) -> Any:
        """Decode ``content`` into a new object on every call (D-23).

        Raises
        ------
        ValueError
            If the page kept no content.
        """
        if self.content is None:
            msg = "this page kept no content; collect(keep_pages=True) keeps it"
            raise ValueError(msg)
        return json.loads(self.content)

    def __getstate__(self) -> dict[str, Any]:
        # mappingproxy does not pickle.
        state = {f.name: getattr(self, f.name) for f in fields(self)}
        state["params"], state["headers"] = dict(self.params), dict(self.headers)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        for name, value in state.items():
            object.__setattr__(self, name, value)
        self.__post_init__()


@dataclass(frozen=True, slots=True)
class Pagination:
    """What a traversal fetched and whether it reached the end (D-22).

    Parameters
    ----------
    number_matched : int or None
        The ``numberMatched`` every reporting page agreed on, ``None`` when none
        reported it or pages disagreed.
    number_returned : int
        Features received across all pages.
    page_count : int
        Pages received.
    completeness : {"complete", "limited", "unknown"}
        ``complete`` when traversal reached the end and every check passed,
        ``limited`` when it stopped early, ``unknown`` otherwise.
    stop_reason : {"limit", "failure"}, optional
        Why a ``limited`` traversal stopped: a caller limit or a failed page.
    sort_by : str, optional
        The sort key the pages were requested with.

    Raises
    ------
    ValueError
        If ``stop_reason`` is set without ``limited``, or missing with it.
    """

    number_matched: int | None
    number_returned: int
    page_count: int
    completeness: Completeness
    stop_reason: StopReason | None = None
    sort_by: str | None = None

    def __post_init__(self) -> None:
        if (self.completeness == "limited") != (self.stop_reason is not None):
            msg = "stop_reason is set exactly when completeness is 'limited'"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class QueryResult:
    """An aggregated query result and the pages behind it (D-03).

    Parameters
    ----------
    geojson : dict
        The aggregated FeatureCollection, nothing coerced. Left out of ``repr``.
    pages : tuple of Page
        Every page received, in order. Bodies are ``None``; ``content`` holds the
        bytes only with ``keep_pages=True`` (D-23).
    pagination : Pagination
        Counts and completeness of the traversal.
    """

    geojson: dict[str, Any] = field(repr=False)
    pages: tuple[Page, ...]
    pagination: Pagination

    def __post_init__(self) -> None:
        object.__setattr__(self, "pages", tuple(self.pages))
