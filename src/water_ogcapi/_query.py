"""Items requests and checkpoints: `Request`, `Checkpoint`, and the first request of a query.

Nothing here sends a request (D-39, D-44, D-52).
"""

from __future__ import annotations

import dataclasses
import json
import math
import numbers
from collections.abc import Collection, Iterator, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable
from urllib.parse import quote, urlsplit

from water_ogcapi._redact import redact_url
from water_ogcapi._results import ReadOnlyDict
from water_ogcapi._transport import query_strings, reject_credentials, reject_non_str, sent_url
from water_ogcapi.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "Checkpoint",
    "GeoInterface",
    "Held",
    "Request",
    "RequestId",
    "items_request",
    "path_segment",
]

CQL2_JSON = {"Content-Type": "application/query-cql-json"}
# The headers a Request may carry. The service adds its own, the API key included, at
# send time (D-40), so a stored Request never holds a credential header.
REQUEST_HEADERS = frozenset({"accept", "content-type"})
METHODS = frozenset({"GET", "POST"})

# A request's identity (D-38): its method, URL as sent with credentials masked, and body.
type RequestId = tuple[str, str, bytes | None]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    """One items request: its identity (D-38) and the headers it needs.

    A `Checkpoint` holds the requests a query has still to send (D-52). Construction
    refuses a credential, and loading a pickle checks again, so a stored or pickled
    request never carries one (D-65).

    Parameters
    ----------
    url : str
        The URL before ``params`` are added.
    method : str, optional
        ``GET``, the default, or ``POST``; every ``next`` link is requested the same way.
    params : mapping of str to str, optional
        Query parameters of this request only; a ``next`` link carries its own. An
        ``int``, ``float``, or ``bool`` value is stored as the text the transport sends
        (D-77).
    body : bytes, optional
        The body, sent to every ``next`` link as well.
    headers : mapping of str to str, optional
        ``Accept`` or ``Content-Type``, sent with every request beside the service's
        own headers.

    Raises
    ------
    ValidationError
        If ``method`` is not ``GET`` or ``POST``, ``url`` holds userinfo, ``url`` or
        ``params`` carry a credential such as ``api_key``, ``headers`` holds another
        header, or ``url`` or a name in ``params`` or ``headers`` is not exactly ``str``
        (D-65). A credential is refused, since masking it would change the request.

    Notes
    -----
    ``params`` and ``headers`` are read-only copies, so a change to the mappings passed
    in never reaches a stored request.
    """

    url: str
    method: str = "GET"
    params: Mapping[str, str] = dataclasses.field(default_factory=dict[str, str])
    body: bytes | None = dataclasses.field(default=None, repr=False)
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        method = self.method.upper()
        if method not in METHODS:
            msg = f"a request is a GET or a POST, got {self.method!r}"
            raise ValidationError(msg)
        # One reading of params, converted as the transport sends them (D-77), so the
        # stored copy is the one checked and the identity names the URL that goes out.
        params = query_strings(self.params) or {}
        reject_credentials(self.url, params)
        try:
            userinfo = urlsplit(self.url).netloc.rpartition("@")[0]
        except ValueError:
            userinfo = ""
        if userinfo:
            msg = "a request URL must not carry userinfo"
            raise ValidationError(msg)
        headers = dict(self.headers or {})
        reject_non_str(headers, "header names")
        if extra := sorted(name for name in headers if name.lower() not in REQUEST_HEADERS):
            msg = f"a request carries only Accept and Content-Type headers, got {', '.join(extra)}"
            raise ValidationError(msg)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "params", ReadOnlyDict(params))
        object.__setattr__(self, "headers", None if self.headers is None else ReadOnlyDict(headers))

    def identity(self) -> RequestId:
        """Return the method, URL as sent with credentials masked, and body (D-38)."""
        return self.method, redact_url(sent_url(self.url, self.params)), self.body

    def __setstate__(self, state: list[object]) -> None:
        for field, value in zip(dataclasses.fields(self), state, strict=True):
            object.__setattr__(self, field.name, value)
        # A pickle can hold what construction refuses, so a load checks again (D-65).
        self.__post_init__()


class Held(Collection[RequestId]):
    """The identities a checkpoint holds: the first ``count`` a run answered (D-52).

    Every checkpoint of one run shares the run's list and index, so a checkpoint per
    page costs no copy. The run only appends, so a checkpoint never sees a later
    identity. It pickles as a ``frozenset`` of its own identities.
    """

    __slots__ = ("_count", "_ids", "_index")

    def __init__(self, ids: list[RequestId], index: dict[RequestId, int], count: int) -> None:
        self._ids = ids
        self._index = index
        self._count = count

    @classmethod
    def start(cls, held: Collection[RequestId]) -> Held:
        """Return a growing record that begins with ``held``."""
        ids = list(dict.fromkeys(held))
        return cls(ids, {identity: n for n, identity in enumerate(ids)}, len(ids))

    def add(self, identity: RequestId) -> None:
        """Record ``identity`` as answered, unless it already is."""
        if identity not in self._index:
            self._index[identity] = len(self._ids)
            self._ids.append(identity)
            self._count = len(self._ids)

    def snapshot(self) -> Held:
        """Return the identities answered so far, shared with this record."""
        return Held(self._ids, self._index, self._count)

    def __iter__(self) -> Iterator[RequestId]:
        # A slice is one call, so a run appending on the loop thread cannot break it.
        return iter(self._ids[: self._count])

    def __contains__(self, item: object) -> bool:
        try:
            position = self._index.get(cast("RequestId", item))
        except TypeError:
            return False
        return position is not None and position < self._count

    def __len__(self) -> int:
        return self._count

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Collection):
            return NotImplemented
        return frozenset(self) == frozenset(cast("Collection[object]", other))

    def __hash__(self) -> int:
        return hash(frozenset(self))

    def __reduce__(self) -> tuple[type[frozenset[RequestId]], tuple[tuple[RequestId, ...]]]:
        return frozenset, (tuple(self),)

    def __repr__(self) -> str:
        return f"Held({self._count} requests)"


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Checkpoint:
    """Where an unfinished items query stands: what it holds and what it has left (D-52).

    Every yielded `Page` with more to fetch carries one, as do `PaginationError` and a
    `QueryResult` that stopped early. Pass it to ``iter_pages(resume=...)``, or a
    `QueryResult` to ``collect(resume=...)``. It pickles, holds no feature, and refuses
    to resume on another service or collection. Unpickle only a checkpoint you saved:
    loading a pickle runs code. A load runs the checks construction runs, its requests'
    included (D-65).

    Parameters
    ----------
    base_url : str
        The ``base_url`` of the service that ran the query.
    collection : str
        The collection the query reads.
    pending : tuple of Request
        The requests left to send, in order: the requests that failed, those in flight
        or fetched and not yet yielded, the ``next`` links not yet started, the pieces
        not yet started, then the refused ones (D-56).
    held : collection of tuple, optional
        The identity (method, URL as sent, body) of each request already answered, in
        the order answered. A resumed query refuses to send any of them again.
    malformed : bool, optional
        Whether an answered page held a ``numberMatched`` or ``numberReturned`` that is
        not a non-negative integer, so a resumed `QueryResult` still reads ``unknown``.

    Raises
    ------
    ValidationError
        If ``base_url`` or ``collection`` is not exactly ``str`` (D-65).
    TypeError
        If ``pending`` holds anything but `Request` objects.
    ValueError
        If ``pending`` is empty: a query with nothing left has no checkpoint.
    """

    base_url: str
    collection: str
    pending: tuple[Request, ...]
    held: Collection[RequestId] = dataclasses.field(
        default_factory=frozenset[RequestId], repr=False
    )
    malformed: bool = False

    def __post_init__(self) -> None:
        # Error messages echo both (D-65).
        reject_non_str([self.base_url], "base_url")
        reject_non_str([self.collection], "collection")
        pending = tuple(self.pending)
        # Runtime check for untyped callers, as Page.links has.
        if not all(isinstance(item, Request) for item in cast("tuple[object, ...]", pending)):
            msg = "pending must hold only Request objects"
            raise TypeError(msg)
        if not pending:
            msg = "a checkpoint needs a pending request"
            raise ValueError(msg)
        object.__setattr__(self, "pending", pending)
        if not isinstance(self.held, Held | frozenset):
            object.__setattr__(self, "held", frozenset(self.held))

    def __setstate__(self, state: list[object]) -> None:
        for field, value in zip(dataclasses.fields(self), state, strict=True):
            object.__setattr__(self, field.name, value)
        self.__post_init__()


@runtime_checkable
class GeoInterface(Protocol):
    """An object exposing a GeoJSON geometry, such as a shapely geometry."""

    @property
    def __geo_interface__(self) -> Mapping[str, Any]: ...


def _coordinate(value: object) -> float:
    """Return ``value`` as a float, raising for anything but a finite real number."""
    if type(value) is bool or not isinstance(value, numbers.Real) or not math.isfinite(value):
        msg = f"a coordinate must be a finite number, got {value!r}"
        raise ValidationError(msg)
    return float(value)


def _bbox_param(bbox: Sequence[float]) -> str:
    """Serialize a bbox unrounded and without exponents: rounding can move an edge inward."""
    coords = [_coordinate(v) for v in bbox]
    half = len(coords) // 2
    if len(coords) not in (4, 6) or any(coords[i] > coords[i + half] for i in range(1, half)):
        msg = (
            "bbox takes (west, south, east, north) or (west, south, low, east, north, high), "
            "with south <= north and low <= high"
        )
        raise ValidationError(msg)
    return ",".join(format(Decimal(repr(v)), "f") for v in coords)


def _coordinate_arrays(geo: object) -> list[object]:
    """Return a GeoJSON geometry's ``coordinates``, or those of each in a GeometryCollection."""
    member = cast("Mapping[str, object]", geo) if isinstance(geo, Mapping) else {}
    geometries = member.get("geometries")
    if member.get("type") == "GeometryCollection" and isinstance(geometries, list | tuple):
        parts = cast("list[object] | tuple[object, ...]", geometries)
        return [coords for part in parts for coords in _coordinate_arrays(part)]
    return [member.get("coordinates")]


def _geometry_bbox(geometry: object) -> tuple[float, ...]:
    """Return the 2D bbox of every position in a GeoJSON geometry."""
    geo = geometry.__geo_interface__ if isinstance(geometry, GeoInterface) else geometry
    xs: list[float] = []
    ys: list[float] = []
    stack = _coordinate_arrays(geo)
    while stack:
        node = stack.pop()
        if not isinstance(node, list | tuple):
            msg = "geometry must be a GeoJSON geometry with nested coordinate arrays"
            raise ValidationError(msg)
        items = cast("list[object] | tuple[object, ...]", node)
        if items and not isinstance(items[0], list | tuple):
            if len(items) < 2:
                msg = f"a position needs two coordinates, got {list(items)!r}"
                raise ValidationError(msg)
            xs.append(_coordinate(items[0]))
            ys.append(_coordinate(items[1]))
        else:
            stack.extend(items)
    if not xs:
        msg = "geometry has no positions"
        raise ValidationError(msg)
    return min(xs), min(ys), max(xs), max(ys)


def _filter(
    cql2: str | Mapping[str, Any] | None,
    ids: Sequence[str | int] | None,
    id_property: str | None,
) -> str | Mapping[str, Any] | None:
    """Return ``cql2``, or the CQL2-JSON ``in`` filter that ``ids`` and ``id_property`` set.

    Raises
    ------
    ValidationError
        If ``ids`` comes with a filter, is a ``str`` or ``bytes``, is empty, or lacks
        ``id_property``.
    """
    if ids is None and id_property is None:
        return cql2
    if cql2 is not None:
        msg = "pass ids or filter, not both; combine them in one CQL2 filter"
        raise ValidationError(msg)
    values = [] if ids is None or isinstance(ids, str | bytes | bytearray) else list(ids)
    if not values or not id_property:
        msg = "ids takes a non-empty sequence of identifiers, and id_property their property"
        raise ValidationError(msg)
    return {"op": "in", "args": [{"property": id_property}, values]}


def _items_query(
    params: Mapping[str, str | int | float | bool] | None,
    bbox: Sequence[float] | None,
    geometry: Mapping[str, Any] | GeoInterface | None,
    cql2: str | Mapping[str, Any] | None,
) -> tuple[dict[str, str], bytes | None]:
    """Build the first request's query, with ``f=json`` added unless given, and its body.

    A CQL2-Text filter goes in the query; a CQL2-JSON filter becomes the body (D-39).

    Raises
    ------
    ValidationError
        If both ``bbox`` and ``geometry`` are given, either is malformed, a CQL2-JSON
        filter holds a NaN or infinite number, a filter is empty, or ``params`` already
        holds a parameter a keyword sets.
    TypeError
        If a CQL2-JSON filter holds a value JSON cannot encode, such as a set.
    """
    query = {"f": "json", **(query_strings(params) or {})}
    if cql2 is not None and not cql2:
        msg = "filter is empty; pass None for no filter"
        raise ValidationError(msg)
    if bbox is not None and geometry is not None:
        msg = "pass bbox or geometry, not both"
        raise ValidationError(msg)
    box = _geometry_bbox(geometry) if geometry is not None else bbox
    claimed = ({"bbox"} if box is not None else set()) | (
        {"filter", "filter-lang"} if cql2 is not None else set()
    )
    if clash := sorted(claimed & query.keys()):
        msg = f"{', '.join(clash)} given both as a keyword and in params"
        raise ValidationError(msg)
    if box is not None:
        query["bbox"] = _bbox_param(box)
    if isinstance(cql2, str):
        query["filter"] = cql2
        return query, None
    if cql2 is None:
        return query, None
    try:
        # The top level may be any mapping, as the filter's type says; inside it, JSON types.
        body = json.dumps(dict(cql2), separators=(",", ":"), allow_nan=False)
    except ValueError as exc:
        # A NaN or infinite number, or a mapping that contains itself.
        msg = f"filter cannot be sent as JSON: {exc}"
        raise ValidationError(msg) from None
    return query, body.encode()


def path_segment(value: str | int, name: str) -> str:
    """Return ``value`` percent-encoded as one URL path segment.

    Raises
    ------
    ValidationError
        If ``value`` is empty, ``.``, or ``..``, which would address another path.
    """
    text = str(value)
    if text in {"", ".", ".."}:
        msg = f"{name} {value!r} would address another path"
        raise ValidationError(msg)
    return quote(text, safe="")


def items_request(
    base_url: str,
    collection: str,
    *,
    params: Mapping[str, str | int | float | bool] | None,
    bbox: Sequence[float] | None,
    geometry: Mapping[str, Any] | GeoInterface | None,
    cql2: str | Mapping[str, Any] | None,
    ids: Sequence[str | int] | None,
    id_property: str | None,
) -> Request:
    """Return the first request of an items query, as `OGCService.iter_pages` takes it.

    A CQL2-JSON filter, or ``ids``, makes it a ``POST`` with the filter as its body.

    Raises
    ------
    ValidationError, TypeError
        On a malformed query, as `OGCService.iter_pages` documents.
    """
    url = f"{base_url}/collections/{path_segment(collection, 'collection')}/items"
    query, body = _items_query(params, bbox, geometry, _filter(cql2, ids, id_property))
    if body is None:
        return Request(url=url, params=query)
    return Request(url=url, method="POST", params=query, body=body, headers=CQL2_JSON)
