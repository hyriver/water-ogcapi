"""Build the first request of an items query, with no I/O (D-39, D-44)."""

from __future__ import annotations

import dataclasses
import json
import math
import numbers
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable
from urllib.parse import quote

from water_ogcapi.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["GeoInterface", "Request", "items_request", "path_segment"]

CQL2_JSON = {"Content-Type": "application/query-cql-json"}


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    """The first request of a traversal: its identity (D-38) and the headers it needs.

    Parameters
    ----------
    url : str
        The URL before ``params`` are added.
    method : str, optional
        ``GET``, the default, or ``POST``; every ``next`` link is requested the same way.
    params : mapping of str to str, optional
        Query parameters of this request only; a ``next`` link carries its own.
    body : bytes, optional
        The body, sent to every ``next`` link as well.
    headers : mapping of str to str, optional
        Headers beyond the service's own, sent with every request.
    """

    url: str
    method: str = "GET"
    params: Mapping[str, str] = dataclasses.field(default_factory=dict[str, str])
    body: bytes | None = None
    headers: Mapping[str, str] | None = None


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


def _json_default(value: object) -> object:
    """Encode a mapping that ``json`` does not know, such as a ``MappingProxyType``."""
    if isinstance(value, Mapping):
        return dict(cast("Mapping[str, object]", value))
    msg = f"Object of type {type(value).__name__} is not JSON serializable"
    raise TypeError(msg)


def _items_query(
    params: Mapping[str, str] | None,
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
    query = {"f": "json", **(params or {})}
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
        body = json.dumps(cql2, separators=(",", ":"), allow_nan=False, default=_json_default)
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
    params: Mapping[str, str] | None,
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
