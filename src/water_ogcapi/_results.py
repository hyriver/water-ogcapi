"""Result types: one response document per `Page`, the aggregate in `QueryResult`.

Each type redacts credentials in its constructor (docs/design.md D-04, D-29), so a
pickled, logged, or displayed result never carries the API key.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn, Self, cast
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from water_ogcapi._transport import CREDENTIAL_PARAMS

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    # A JSON member name, the field that carries it, and the parser that returns the
    # field's value or None when the member does not have the expected shape.
    type Members = Mapping[str, tuple[str, Callable[[Any], Any]]]

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


class ReadOnlyDict[V](dict[str, V]):
    """A ``dict`` that raises ``TypeError`` on any change.

    It pickles, copies, and works with ``dataclasses.asdict`` and ``json.dumps``.
    """

    __slots__ = ()

    def _refuse(self: object, *_args: object, **_kwargs: object) -> NoReturn:
        msg = "this mapping is read-only"
        raise TypeError(msg)

    clear = pop = popitem = setdefault = update = _refuse

    __setitem__ = __delitem__ = __ior__ = _refuse

    def __reduce__(self) -> tuple[type[Self], tuple[dict[str, V]]]:
        # The default reduction refills the copy through __setitem__.
        return (type(self), (dict(self),))


def _redact_params(params: Mapping[str, str]) -> ReadOnlyDict[str]:
    return ReadOnlyDict(
        {k: REDACTED if k.lower() in CREDENTIAL_PARAMS else v for k, v in params.items()}
    )


def _keep_headers(headers: Mapping[str, str]) -> ReadOnlyDict[str]:
    return ReadOnlyDict({k.lower(): v for k, v in headers.items() if k.lower() in KEPT_HEADERS})


def _all_of[T](items: tuple[object, ...], kind: type[T], name: str) -> tuple[T, ...]:
    # Runtime check for untyped callers: a raw link dict would skip href redaction.
    if not all(isinstance(item, kind) for item in items):
        msg = f"{name} must hold only {kind.__name__} objects"
        raise TypeError(msg)
    return cast("tuple[T, ...]", items)


def empty_extra() -> ReadOnlyDict[Any]:
    return ReadOnlyDict()


def _reject(_value: object) -> None:
    return None


def as_str(value: object) -> str | None:
    """Return ``value`` if it is a string, else ``None``."""
    return value if isinstance(value, str) else None


def from_members(
    doc: Mapping[str, Any], members: Members, required: str | None = None
) -> dict[str, Any]:
    """Build field values from ``doc``, with every member ``members`` does not carry in ``extra``.

    A member whose parser returns ``None``, such as a null or a value of the wrong JSON
    type, stays in ``extra`` as sent, so no member is lost.

    Raises
    ------
    ValueError
        If ``required`` names a field that no member filled.
    """
    fields: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in doc.items():
        name, parse = members.get(key, ("", _reject))
        parsed = parse(value)
        if parsed is None:
            extra[key] = copy.deepcopy(value)
        else:
            fields[name] = parsed
    if required is not None and required not in fields:
        msg = f"the document has no string {required!r} member"
        raise ValueError(msg)
    fields["extra"] = ReadOnlyDict(extra)
    return fields


def _to_json(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_to_json(item) for item in cast("tuple[Any, ...]", value)]
    if isinstance(value, dict):
        return {key: _to_json(item) for key, item in cast("dict[str, Any]", value).items()}
    to_dict = getattr(value, "to_dict", None)
    return value if to_dict is None else to_dict()


def to_members(obj: object, members: Members, extra: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the document ``from_members`` parsed: the set fields, then ``extra``."""
    doc = {
        key: _to_json(getattr(obj, name))
        for key, (name, _) in members.items()
        if getattr(obj, name) is not None
    }
    doc.update(copy.deepcopy(dict(extra)))
    return doc


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
    hreflang : str, optional
        Language of the target.
    extra : mapping of str to Any, optional
        Every other member of the link object, read-only and left out of the hash.
    """

    href: str
    rel: str | None = None
    type: str | None = None
    title: str | None = None
    hreflang: str | None = None
    extra: Mapping[str, Any] = field(default_factory=empty_extra, hash=False)

    _MEMBERS: ClassVar[Members] = {
        key: (key, as_str) for key in ("href", "rel", "type", "title", "hreflang")
    }

    def __post_init__(self) -> None:
        object.__setattr__(self, "href", redact_url(self.href))

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Self:
        """Parse a link object.

        Raises
        ------
        ValueError
            If ``doc`` has no string ``href``.
        """
        return cls(**from_members(doc, cls._MEMBERS, required="href"))

    def to_dict(self) -> dict[str, Any]:
        """Return the link object, ``href`` redacted."""
        return to_members(self, self._MEMBERS, self.extra)


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
        object.__setattr__(self, "links", _all_of(tuple(self.links), Link, "links"))

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
        object.__setattr__(self, "pages", _all_of(tuple(self.pages), Page, "pages"))
