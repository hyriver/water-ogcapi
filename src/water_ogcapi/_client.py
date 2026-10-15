"""The base client for one OGC API Features service."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast
from urllib.parse import urlsplit

from water_ogcapi._metadata import Collections, parse_link
from water_ogcapi._query import Request, items_request, path_segment
from water_ogcapi._results import Page, Pagination, QueryResult, crs_of
from water_ogcapi._transport import Transport, parse_count, reject_credentials
from water_ogcapi.exceptions import PaginationError, PartialResultsError, ServiceError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from water_ogcapi._query import GeoInterface
    from water_ogcapi._results import Check, Completeness, Link
    from water_ogcapi._transport import Response

__all__ = ["OGCService", "ServiceProfile"]

DEFAULT_PORTS = {"http": 80, "https": 443}
JSON_TYPES = frozenset({"application/geo+json", "application/json"})


def json_object(
    resp: Response, *, method: str = "GET", request_body: bytes | None = None
) -> dict[str, Any]:
    """Decode a response body that must be a JSON object.

    ``method`` and ``request_body`` name the request in the error (D-38).

    Raises
    ------
    ServiceError
        If the body is not JSON, or is JSON of another type, such as a list.
    """
    try:
        doc = resp.json()
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        # Raised outside the except block: a JSONDecodeError holds the whole body.
        msg = "response body is not a JSON object"
        raise ServiceError(
            msg,
            resp.url,
            status=resp.status,
            attempts=resp.attempts,
            method=method,
            request_body=request_body,
        )
    return cast("dict[str, Any]", doc)


def _count(value: object) -> int | None:
    """Return a JSON count, or ``None`` for anything but a non-negative integer."""
    return value if type(value) is int and value >= 0 else None


def _malformed_counts(body: Mapping[str, Any]) -> bool:
    """Whether ``numberMatched`` or ``numberReturned`` is present but not a usable count.

    A null reads as absent.
    """
    members = ("numberMatched", "numberReturned")
    return any(body.get(key) is not None and _count(body[key]) is None for key in members)


def _broken_next(link: object) -> bool:
    """Whether ``link`` is a ``next`` link without a string ``href``."""
    if not isinstance(link, dict):
        return False
    member = cast("dict[str, Any]", link)
    return member.get("rel") == "next" and not isinstance(member.get("href"), str)


def _page(
    resp: Response, params: Mapping[str, str], method: str, request_body: bytes | None
) -> Page:
    """Build a `Page` from a response whose body is a FeatureCollection.

    Raises
    ------
    ServiceError
        If the body is not a FeatureCollection with a ``features`` list, or its
        ``links`` member is not a list or holds a ``next`` link without a string
        ``href``. Either would otherwise read as the last page (D-34).
    """
    body = json_object(resp, method=method, request_body=request_body)
    features = body.get("features")
    links = body.get("links", [])
    if body.get("type") != "FeatureCollection" or not isinstance(features, list):
        msg = "response is not a FeatureCollection with a features list"
        raise ServiceError(
            msg,
            resp.url,
            status=resp.status,
            attempts=resp.attempts,
            method=method,
            request_body=request_body,
        )
    if not isinstance(links, list) or any(map(_broken_next, links)):
        msg = "response has a malformed links member or next link"
        raise ServiceError(
            msg,
            resp.url,
            status=resp.status,
            attempts=resp.attempts,
            method=method,
            request_body=request_body,
        )
    return Page(
        url=resp.url,
        params=params,
        status=resp.status,
        headers=resp.headers,
        links=tuple(filter(None, map(parse_link, links))),
        fetched_at=time.time(),
        number_matched=_count(body.get("numberMatched")),
        number_returned=_count(body.get("numberReturned")),
        feature_count=len(features),
        body=body,
        content=resp.content,
        method=method,
        request_body=request_body,
    )


def _next_href(links: tuple[Link, ...]) -> str | None:
    """Return the ``next`` link to follow: the first of a JSON or unstated type, else the first."""
    nexts = [link for link in links if link.rel == "next"]
    for link in nexts:
        media_type = (link.type or "application/json").partition(";")[0].strip().lower()
        if media_type in JSON_TYPES:
            return link.href
    return nexts[0].href if nexts else None


def _origin(url: str) -> tuple[str, str | None, int | None] | None:
    """Return the scheme, host, and port of ``url``, the port defaulted by scheme."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, DEFAULT_PORTS.get(scheme) if port is None else port


def _pagination(pages: list[Page], *, failed: bool, malformed: bool) -> Pagination:
    """Summarize a traversal: counts, the checks that passed, and completeness (D-36)."""
    reported = {p.number_matched for p in pages if p.number_matched is not None}
    matched = next(iter(reported)) if len(reported) == 1 else None
    returned = sum(p.feature_count for p in pages)
    sort_by = pages[0].params.get("sortby") if pages else None
    if failed:
        return Pagination(matched, returned, len(pages), "limited", "failure", sort_by)
    # The traversal ended on a page without a next link. A malformed count, a count that
    # disagrees with numberMatched, a page whose numberReturned differs from its features,
    # or pages that disagree with each other, leave the result unknown.
    checks: tuple[Check, ...] = ("end_of_links",)
    if matched is not None and matched == returned:
        checks += ("count",)
    short = any(p.number_returned not in (None, p.feature_count) for p in pages)
    mismatch = matched is not None and matched != returned
    completeness: Completeness = (
        "unknown" if malformed or short or len(reported) > 1 or mismatch else "complete"
    )
    return Pagination(matched, returned, len(pages), completeness, sort_by=sort_by, checks=checks)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ServiceProfile:
    """What one service accepts and sends, read by its service class (D-13, D-41).

    Attributes
    ----------
    base_url : str
        The landing page.
    default_limit : int
        The ``limit`` an items query sends when its ``params`` hold none.
    max_limit : int
        The largest ``limit`` the service accepts.
    rate_limit_headers : bool
        Whether responses carry ``X-RateLimit-*`` headers (D-06).
    key_env : str or None, optional
        The environment variable that holds the service's API key, sent in the
        ``X-Api-Key`` header (D-18). ``None``, the default, for a service sent no key.
    """

    base_url: str
    default_limit: int
    max_limit: int
    rate_limit_headers: bool
    key_env: str | None = None


class OGCService:
    """Async client for one OGC API Features service.

    Construction performs no HTTP (docs/design.md D-10). The first request binds the
    transport to the running event loop.

    Parameters
    ----------
    base_url : str, optional
        The service's landing page, such as
        ``https://api.waterdata.usgs.gov/ogcapi/v1``. A trailing slash is ignored.
        Defaults to the profile's ``base_url``; required where ``profile`` is ``None``.
    transport : Transport, optional
        Sends every request. The service closes a transport it created and leaves a
        passed one open, so several services can share one.

    Attributes
    ----------
    profile : ServiceProfile or None
        The service's page sizes and key convention, set by each service class.
        ``None`` here: a query sends the server's default ``limit`` and any ``limit``
        goes unchecked.
    """

    profile: ClassVar[ServiceProfile | None] = None

    def __init__(self, base_url: str | None = None, *, transport: Transport | None = None) -> None:
        if base_url is None:
            if self.profile is None:
                msg = "base_url is required for a service without a profile"
                raise TypeError(msg)
            base_url = self.profile.base_url
        self._base_url = base_url.rstrip("/")
        self._owns_transport = transport is None
        self._transport = Transport() if transport is None else transport
        self._collections: Collections | None = None

    @property
    def base_url(self) -> str:
        """The landing page URL, without a trailing slash."""
        return self._base_url

    def _headers(self) -> dict[str, str]:
        """Return the headers every request to this service carries (D-40).

        The base service sends none. A service class overrides this to add its API key
        (D-18); `_send` keeps them to the origin of ``base_url``.
        """
        return {}

    def _limited(self, params: Mapping[str, str] | None) -> Mapping[str, str] | None:
        """Return ``params`` with the profile's ``default_limit`` added unless given.

        Raises
        ------
        ValueError
            If ``limit`` is not an integer from 1 to the profile's ``max_limit``.
        """
        if self.profile is None:
            return params
        query = {"limit": str(self.profile.default_limit), **(params or {})}
        # str(): httpx2 sends an int limit as given, so the check reads it the same way.
        count = parse_count(str(query["limit"]))
        if count is None or not 1 <= count <= self.profile.max_limit:
            msg = (
                f"limit takes an integer from 1 to {self.profile.max_limit}, got {query['limit']!r}"
            )
            raise ValueError(msg)
        return {**query, "limit": str(count)}

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Send one request with this service's headers added to ``headers`` (D-40).

        Raises
        ------
        ServiceError
            Before sending, if ``url`` leaves the scheme, host, and port of ``base_url``,
            since the service's headers would follow it there (D-34); otherwise as
            `Transport.request` raises.
        """
        if _origin(url) != _origin(self.base_url):
            msg = f"refused a URL outside {self.base_url}"
            raise ServiceError(msg, url, attempts=0, method=method.upper(), request_body=content)
        return await self._transport.request(
            method,
            url,
            params=params,
            headers={**self._headers(), **(headers or {})},
            content=content,
        )

    async def collections(self) -> Collections:
        """Return the collection index, fetching ``/collections`` on the first call.

        Raises
        ------
        ServiceError
            If the request fails or the body is not a JSON object with a
            ``collections`` list. Nothing is cached, so the next call asks again.
        ValueError
            If an entry of ``collections`` is not an object with a string ``id``.
        """
        if self._collections is None:
            url = f"{self.base_url}/collections"
            resp = await self._send("GET", url, params={"f": "json"})
            doc = json_object(resp)
            if not isinstance(doc.get("collections"), list):
                # A gateway's 200 error document would otherwise be cached as the index.
                msg = "response is not a collection index with a collections list"
                raise ServiceError(msg, resp.url, status=resp.status, attempts=resp.attempts)
            self._collections = Collections.from_dict(doc)
        return self._collections

    def _pieces(self, request: Request) -> Sequence[Request]:
        """Split an items request into independent pieces, traversed in order (D-44).

        The base service keeps it whole; a service that fans out overrides this (D-35).
        """
        return (request,)

    async def _traverse(self, request: Request) -> AsyncIterator[Page]:
        """Yield each page from ``request`` on, following ``next`` links (D-34, D-38).

        Every request, the ``next`` links included, sends the method, body, and headers
        of ``request``: a ``GET`` to the ``next`` link of a CQL2 ``POST`` drops the
        filter. Its ``params`` apply to the first request only.
        """
        url, query, method = request.url, request.params, request.method
        content, headers = request.body, request.headers
        followed: set[str] = set()
        while True:
            try:
                resp = await self._send(method, url, params=query, headers=headers, content=content)
                page = _page(resp, query, method.upper(), content)
            except ServiceError as err:
                raise PaginationError([err]) from err
            yield page
            # Holds the first request as sent too, so a next link back to it is a repeat.
            followed.add(page.url)
            href = _next_href(page.links)
            if href is None:
                return
            refusal = None
            if href in followed:
                # A caller collecting every page never stops, so a loop spends the quota.
                refusal = "a next link repeats one already followed"
            else:
                try:
                    reject_credentials(href, None)
                except ValueError:
                    # A gateway can copy the key into a URL (L-11). The transport's
                    # ValueError would escape collect() and drop the pages fetched so far.
                    refusal = "refused a next link that carries a credential in its query"
            if refusal is not None:
                err = ServiceError(
                    refusal, href, attempts=0, method=page.method, request_body=content
                )
                raise PaginationError([err])
            followed.add(href)
            url, query = href, {}

    async def iter_pages(
        self,
        collection: str,
        *,
        params: Mapping[str, str] | None = None,
        bbox: Sequence[float] | None = None,
        geometry: Mapping[str, Any] | GeoInterface | None = None,
        filter: str | Mapping[str, Any] | None = None,  # noqa: A002
        ids: Sequence[str | int] | None = None,
        id_property: str | None = None,
    ) -> AsyncIterator[Page]:
        """Yield each page of a collection's items, following ``next`` links (D-34).

        Only a page without a ``next`` link ends the traversal. A page shorter than
        requested, or one without ``numberMatched``, ends nothing.

        Parameters
        ----------
        collection : str
            The collection id.
        params : mapping of str to str, optional
            Query parameters of the first request, such as ``limit`` or ``datetime``,
            with ``f=json`` and the profile's ``default_limit`` added unless given.
            Later requests take their query from the ``next`` link.
        bbox : sequence of float, optional
            ``(west, south, east, north)`` in CRS84, or six values with the minimum and
            maximum heights, sent as the ``bbox`` parameter with every digit kept. West
            above east crosses the antimeridian.
        geometry : mapping or GeoInterface, optional
            A GeoJSON geometry, or an object with ``__geo_interface__`` such as a
            shapely polygon, in CRS84. Its bbox is sent in place of the geometry
            (D-11), so the result holds every feature intersecting that bbox; exact
            containment is the caller's post-filter.
        filter : str or mapping, optional
            A CQL2 filter, spatial predicates such as ``s_intersects`` included. A
            ``str`` is CQL2-Text, sent as the ``filter`` parameter of a ``GET``. A
            mapping is CQL2-JSON, sent as the body of a ``POST`` with ``Content-Type:
            application/query-cql-json``; every ``next`` link gets the same body, since
            a ``GET`` there drops the filter (D-38).
        ids : sequence of str or int, optional
            Values of ``id_property`` to match, sent as the CQL2-JSON filter
            ``{"op": "in", "args": [{"property": id_property}, ids]}``, as ``filter``
            sends a mapping. Each value goes out with its type, so ``"01646500"`` keeps
            its leading zero.
        id_property : str, optional
            The property ``ids`` matches, such as ``id`` for feature ids or
            ``monitoring_location_id`` for the sites of an NWIS time series.

        Yields
        ------
        Page
            One per response, the decoded document in ``body`` and its bytes in
            ``content``. The ``next`` link of each page is the resume point after it.

        Raises
        ------
        ValueError
            Before any request, when ``collection`` is empty, ``.``, or ``..``, both
            ``bbox`` and ``geometry``, or ``ids`` and ``filter``, are given, ``ids`` is a ``str`` or ``bytes``, empty, or lacks
            ``id_property``, ``bbox`` or ``geometry`` is malformed, a CQL2-JSON filter
            holds a NaN or infinite number, a filter is empty,
            ``params`` also holds a parameter a keyword sets (``bbox``, ``filter``,
            ``filter-lang``), or ``limit`` is not an integer from 1 to the profile's
            ``max_limit``.
        TypeError
            Before any request, when a CQL2-JSON filter holds a value JSON cannot
            encode, such as a set.
        PaginationError
            When a request fails, a body is not a FeatureCollection with well-formed
            links, or a ``next`` link repeats one already followed, leaves the scheme,
            host, and port of ``base_url``, or carries a credential in its query. Its one
            failure names that request or link by URL.
        """
        request = items_request(
            self.base_url,
            collection,
            params=self._limited(params),
            bbox=bbox,
            geometry=geometry,
            cql2=filter,
            ids=ids,
            id_property=id_property,
        )
        for piece in self._pieces(request):
            async for page in self._traverse(piece):
                yield page

    async def collect(
        self,
        collection: str,
        *,
        params: Mapping[str, str] | None = None,
        bbox: Sequence[float] | None = None,
        geometry: Mapping[str, Any] | GeoInterface | None = None,
        filter: str | Mapping[str, Any] | None = None,  # noqa: A002
        ids: Sequence[str | int] | None = None,
        id_property: str | None = None,
        keep_pages: bool = False,
    ) -> QueryResult:
        """Fetch every page of a collection's items into one `QueryResult` (D-22).

        Parameters
        ----------
        collection : str
            The collection id.
        params, bbox, geometry, filter, ids, id_property
            The query, as for `iter_pages`.
        keep_pages : bool, optional
            Keep each page's response bytes in ``Page.content`` (D-23). Page bodies
            are always dropped.

        Returns
        -------
        QueryResult
            ``geojson`` holds the features of all pages, in order.

        Raises
        ------
        ValueError
            Before any request, on a malformed query, as for `iter_pages`.
        TypeError
            Before any request, when a CQL2-JSON filter holds a value JSON cannot
            encode, as for `iter_pages`.
        PartialResultsError
            When a page fails after at least one succeeded. Its ``result`` holds the
            pages that succeeded.
        PaginationError
            When the first page fails.
        """
        pages: list[Page] = []
        features: list[Any] = []
        failure: PaginationError | None = None
        malformed = False
        try:
            async for page in self.iter_pages(
                collection,
                params=params,
                bbox=bbox,
                geometry=geometry,
                filter=filter,
                ids=ids,
                id_property=id_property,
            ):
                body = page.body or {}
                features.extend(body["features"])
                malformed = malformed or _malformed_counts(body)
                pages.append(
                    dataclasses.replace(
                        page, body=None, content=page.content if keep_pages else None
                    )
                )
        except PaginationError as err:
            failure = err
        result = QueryResult(
            geojson={"type": "FeatureCollection", "features": features},
            pages=tuple(pages),
            pagination=_pagination(pages, failed=failure is not None, malformed=malformed),
            crs=crs_of(pages),
        )
        if failure is None:
            return result
        if not pages:
            raise failure
        raise PartialResultsError(failure.failures, result) from failure

    async def item(
        self, collection: str, item_id: str | int, *, params: Mapping[str, str] | None = None
    ) -> Page:
        """Fetch one feature of a collection by its id (D-22).

        Parameters
        ----------
        collection : str
            The collection id.
        item_id : str or int
            The feature id, percent-encoded into the URL path.
        params : mapping of str to str, optional
            Query parameters, such as ``crs`` or ``skipGeometry``, with ``f=json`` added
            unless given.

        Returns
        -------
        Page
            ``body`` holds the decoded Feature and ``content`` its bytes.
            ``feature_count`` is 1, and ``number_matched`` and ``number_returned`` are
            ``None``.

        Raises
        ------
        ServiceError
            If the request fails, with status 404 for an unknown id, or the body is not
            a Feature whose ``links`` member, when present, is a list.
        ValueError
            Before any request, if ``collection`` or ``item_id`` is empty, ``.``, or
            ``..``, which would address another path, or ``params`` carries a credential
            such as ``api_key``.
        """
        url = (
            f"{self.base_url}/collections/{path_segment(collection, 'collection')}"
            f"/items/{path_segment(item_id, 'item_id')}"
        )
        query = {"f": "json", **(params or {})}
        resp = await self._send("GET", url, params=query)
        body = json_object(resp)
        links = body.get("links", [])
        if body.get("type") != "Feature" or not isinstance(links, list):
            msg = "response is not a Feature with a links list"
            raise ServiceError(msg, resp.url, status=resp.status, attempts=resp.attempts)
        return Page(
            url=resp.url,
            params=query,
            status=resp.status,
            headers=resp.headers,
            links=tuple(filter(None, map(parse_link, links))),
            fetched_at=time.time(),
            number_matched=None,
            number_returned=None,
            feature_count=1,
            body=body,
            content=resp.content,
        )

    async def aclose(self) -> None:
        """Close the transport if this service created it."""
        if self._owns_transport:
            await self._transport.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
