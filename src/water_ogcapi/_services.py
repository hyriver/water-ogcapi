"""Service classes, each reading its base URL, page sizes, and key from a profile (D-41)."""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from itertools import batched
from typing import TYPE_CHECKING, Any, Literal, cast

from water_ogcapi._client import OGCService, ServiceProfile
from water_ogcapi._transport import reject_non_str
from water_ogcapi.exceptions import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from water_ogcapi._query import Request
    from water_ogcapi._transport import Transport

__all__ = ["NWIS", "FabricData", "GeoConnex"]

PERIODS = frozenset({"year", "month"})
SITES = "monitoring_location_id"
# Pieces end a microsecond before the next begins: NWIS reads both ends of an interval as
# inclusive and compares to the microsecond, so pieces sharing a bound would both return
# a value at it (D-56).
GAP = timedelta(microseconds=1)

NWIS_PROFILE = ServiceProfile(
    # The landing page; without the slash it redirects to http:// with the key in its
    # query (L-11).
    base_url="https://api.waterdata.usgs.gov/ogcapi/v1/",
    default_limit=10_000,
    max_limit=50_000,
    key_env="USGS_API_KEY",
    # The hour api.data.gov documents.
    rate_limit_window=3_600.0,
)

FABRICDATA_PROFILE = ServiceProfile(
    base_url="https://api.water.usgs.gov/fabric/pygeoapi/",
    default_limit=1_000,
    max_limit=1_000,
    # One probe fits a rolling minute (D-30); USGS states no window.
    rate_limit_window=60.0,
)

GEOCONNEX_PROFILE = ServiceProfile(
    base_url="https://reference.geoconnex.us/",
    default_limit=500,
    max_limit=10_000,
)


def _instant(text: str) -> datetime:
    """Return one bound of a ``datetime`` interval in UTC; NWIS reads a date as its midnight.

    Raises
    ------
    ValidationError
        If ``text`` is neither an ISO 8601 date nor a timestamp with an offset.
    """
    try:
        return datetime.combine(date.fromisoformat(text), time(), UTC)
    except ValueError:
        pass
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        value = None
    if value is None or value.tzinfo is None:
        msg = f"split_datetime takes dates or timestamps with an offset, got {text!r}"
        raise ValidationError(msg)
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _period_end(value: datetime, period: str) -> datetime | None:
    """Return the start of the calendar year or month after ``value``'s, or ``None`` past 9999."""
    try:
        if period == "year":
            return datetime(value.year + 1, 1, 1, tzinfo=UTC)
        carry, month = divmod(value.month, 12)
        return datetime(value.year + carry, month + 1, 1, tzinfo=UTC)
    except ValueError:
        return None


def _periods(interval: str, period: str) -> list[str]:
    """Split a closed ``datetime`` interval into disjoint calendar periods, in UTC (D-56).

    Raises
    ------
    ValidationError
        If ``interval`` lacks either end or ends before it starts.
    """
    first, slash, last = interval.partition("/")
    if not slash or ".." in {first, last} or not first or not last:
        msg = f"split_datetime takes a datetime interval with both ends, got {interval!r}"
        raise ValidationError(msg)
    low, end = _instant(first), _instant(last)
    if end < low:
        msg = f"datetime interval {interval!r} ends before it starts"
        raise ValidationError(msg)
    bounds: list[str] = []
    while (high := _period_end(low, period)) is not None and high <= end:
        bounds.append(f"{_stamp(low)}/{_stamp(max(low, high - GAP))}")
        low = high
    return [*bounds, f"{_stamp(low)}/{_stamp(end)}"]


def _site_pieces(request: Request, size: int) -> list[Request] | None:
    """Split the site list of ``request`` into lists of ``size``, or ``None`` without one.

    The list is a comma-separated ``monitoring_location_id`` parameter or a CQL2-JSON
    ``in`` filter, as ``ids`` sends. A value listed twice goes in one piece only.
    """
    listed = request.params.get(SITES)
    if listed is not None:
        sites = list(dict.fromkeys(listed.split(",")))
        return [
            dataclasses.replace(request, params={**request.params, SITES: ",".join(chunk)})
            for chunk in batched(sites, size, strict=False)
        ]
    doc: Any = None if request.body is None else json.loads(request.body)
    match doc:
        case {"op": "in", "args": [{"property": str()} as prop, list() as values]} if len(doc) == 2:
            # Keyed by JSON text, so 1 and true stay apart.
            unique = list({json.dumps(v, sort_keys=True): v for v in values}.values())
            return [
                dataclasses.replace(
                    request,
                    body=json.dumps(
                        {"op": "in", "args": [prop, list(chunk)]}, separators=(",", ":")
                    ).encode(),
                )
                for chunk in batched(unique, size, strict=False)
            ]
        case _:
            return None


def _key(api_key: str | None) -> str | None:
    """Return the key NWIS sends: ``api_key``, else ``USGS_API_KEY``, ``None`` for none."""
    if api_key is None and NWIS_PROFILE.key_env:
        api_key = os.environ.get(NWIS_PROFILE.key_env)
    # Transport.request() refuses such a key too, but only once a query runs (D-43, D-65).
    if api_key is not None:
        reject_non_str([api_key], "the USGS API key")
    if api_key and not (api_key.isascii() and api_key.isprintable()):
        msg = "the USGS API key must be printable ASCII"
        raise ValidationError(msg)
    return api_key or None


class NWIS(OGCService):
    """Async client for the USGS Water Data OGC API, which serves NWIS data (D-42).

    Construction performs no HTTP. Pages hold 10,000 features unless ``params`` set a
    ``limit``, and 50,000 at most (D-41).

    Parameters
    ----------
    api_key : str, optional
        A USGS API key, sent in the ``X-Api-Key`` header of every request (D-18).
        Defaults to the ``USGS_API_KEY`` environment variable, read once here. An empty
        string sends no key. A pickle or deep copy leaves the key out, the empty string
        included, and reads ``USGS_API_KEY`` again.
    transport : Transport, optional
        Sends every request, as for `OGCService`.

    Raises
    ------
    ValidationError
        If the key holds a character other than printable ASCII, such as a newline,
        or is not exactly ``str`` (D-65).
    """

    profile = NWIS_PROFILE

    def __init__(self, *, api_key: str | None = None, transport: Transport | None = None) -> None:
        super().__init__(transport=transport)
        self._api_key = _key(api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key} if self._api_key else {}

    def _pieces(
        self,
        request: Request,
        split_datetime: Literal["year", "month"] | None,
        sites_per_piece: int | None,
    ) -> Sequence[Request]:
        """Split a query by calendar period or by site list (D-35, D-56)."""
        if split_datetime is not None and sites_per_piece is not None:
            msg = "pass split_datetime or sites_per_piece, not both"
            raise ValidationError(msg)
        if split_datetime is not None:
            if split_datetime not in PERIODS:
                msg = f"split_datetime takes 'year' or 'month', got {split_datetime!r}"
                raise ValidationError(msg)
            interval = request.params.get("datetime")
            if interval is None:
                msg = "split_datetime takes a datetime interval in params"
                raise ValidationError(msg)
            periods = _periods(interval, split_datetime)
            if len(periods) == 1:
                return (request,)
            return [
                dataclasses.replace(request, params={**request.params, "datetime": period})
                for period in periods
            ]
        if sites_per_piece is None:
            return (request,)
        # Runtime check for untyped callers: True is an int, and 2.5 slips past the hint.
        count = cast("object", sites_per_piece)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            msg = f"sites_per_piece takes a positive integer, got {sites_per_piece!r}"
            raise ValidationError(msg)
        pieces = _site_pieces(request, sites_per_piece)
        if pieces is None:
            msg = f"sites_per_piece takes a site list: ids, or a comma-separated {SITES} in params"
            raise ValidationError(msg)
        return pieces

    def __getstate__(self) -> dict[str, Any]:
        return {**super().__getstate__(), "_api_key": None}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self._api_key = _key(None)


class FabricData(OGCService):
    """Async client for the USGS FabricData service, which is sent no key (D-30).

    Construction performs no HTTP. Pages hold 1,000 features unless ``params`` set a
    smaller ``limit`` (D-41).

    Parameters
    ----------
    transport : Transport, optional
        Sends every request, as for `OGCService`. Pass
        ``Transport(timeout=..., max_retries=...)`` to tune the timeout and retry count.
    """

    profile = FABRICDATA_PROFILE

    def __init__(self, *, transport: Transport | None = None) -> None:
        super().__init__(transport=transport)


class GeoConnex(OGCService):
    """Async client for the GeoConnex reference features, which never get the USGS key.

    GeoConnex is not a USGS host, so the key never goes there (D-13). Construction
    performs no HTTP. Pages hold 500 features unless ``params`` set a ``limit``, and
    10,000 at most (D-41).

    Parameters
    ----------
    transport : Transport, optional
        Sends every request, as for `OGCService`. Sharing one with an `NWIS` service
        is safe, since the key travels with each NWIS request.
    """

    profile = GEOCONNEX_PROFILE

    def __init__(self, *, transport: Transport | None = None) -> None:
        super().__init__(transport=transport)
