"""The base client for one OGC API Features service."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Self, cast
from urllib.parse import quote, urlsplit

from water_ogcapi._metadata import Collections, parse_link
from water_ogcapi._results import Page
from water_ogcapi._transport import Transport
from water_ogcapi.exceptions import PaginationError, ServiceError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from water_ogcapi._results import Link
    from water_ogcapi._transport import Response

__all__ = ["OGCService"]

DEFAULT_PORTS = {"http": 80, "https": 443}
JSON_TYPES = frozenset({"application/geo+json", "application/json"})


def json_object(resp: Response) -> dict[str, Any]:
    """Decode a response body that must be a JSON object.

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
        raise ServiceError(msg, resp.url, status=resp.status)
    return cast("dict[str, Any]", doc)


def _count(value: object) -> int | None:
    """Return a JSON count, or ``None`` for anything but a non-negative integer."""
    return value if type(value) is int and value >= 0 else None


def _broken_next(link: object) -> bool:
    """Whether ``link`` is a ``next`` link without a string ``href``."""
    if not isinstance(link, dict):
        return False
    member = cast("dict[str, Any]", link)
    return member.get("rel") == "next" and not isinstance(member.get("href"), str)


def _page(resp: Response, params: Mapping[str, str]) -> Page:
    """Build a `Page` from a response whose body is a FeatureCollection.

    Raises
    ------
    ServiceError
        If the body is not a FeatureCollection with a ``features`` list, or its
        ``links`` member is not a list or holds a ``next`` link without a string
        ``href``. Either would otherwise read as the last page (D-34).
    """
    body = json_object(resp)
    features = body.get("features")
    links = body.get("links", [])
    if body.get("type") != "FeatureCollection" or not isinstance(features, list):
        msg = "response is not a FeatureCollection with a features list"
        raise ServiceError(msg, resp.url, status=resp.status)
    if not isinstance(links, list) or any(map(_broken_next, links)):
        msg = "response has a malformed links member or next link"
        raise ServiceError(msg, resp.url, status=resp.status)
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


class OGCService:
    """Async client for one OGC API Features service.

    Construction performs no HTTP (docs/design.md D-10). The first request binds the
    transport to the running event loop.

    Parameters
    ----------
    base_url : str
        The service's landing page, such as
        ``https://api.waterdata.usgs.gov/ogcapi/v1``. A trailing slash is ignored.
    transport : Transport, optional
        Sends every request. The service closes a transport it created and leaves a
        passed one open, so several services can share one.
    """

    def __init__(self, base_url: str, *, transport: Transport | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_transport = transport is None
        self._transport = Transport() if transport is None else transport
        self._collections: Collections | None = None

    @property
    def base_url(self) -> str:
        """The landing page URL, without a trailing slash."""
        return self._base_url

    async def collections(self) -> Collections:
        """Return the collection index, fetching ``/collections`` on the first call.

        Raises
        ------
        ServiceError
            If the request fails or the body is not a JSON object.
        ValueError
            If an entry of ``collections`` is not an object with a string ``id``.
        """
        if self._collections is None:
            url = f"{self.base_url}/collections"
            resp = await self._transport.get(url, params={"f": "json"})
            self._collections = Collections.from_dict(json_object(resp))
        return self._collections

    async def iter_pages(
        self, collection: str, *, params: Mapping[str, str] | None = None
    ) -> AsyncIterator[Page]:
        """Yield each page of a collection's items, following ``next`` links (D-34).

        Only a page without a ``next`` link ends the traversal. A page shorter than
        requested, or one without ``numberMatched``, ends nothing.

        Parameters
        ----------
        collection : str
            The collection id.
        params : mapping of str to str, optional
            Query parameters of the first request, such as ``limit`` or ``bbox``, with
            ``f=json`` added unless given. Later requests take their query from the
            ``next`` link.

        Yields
        ------
        Page
            One per response, the decoded document in ``body`` and its bytes in
            ``content``. The ``next`` link of each page is the resume point after it.

        Raises
        ------
        PaginationError
            When a request fails, a body is not a FeatureCollection with well-formed
            links, or a ``next`` link repeats one already followed or leaves the scheme,
            host, and port of ``base_url``. Its one failure names that request or link
            by URL.
        """
        url = f"{self.base_url}/collections/{quote(collection, safe='')}/items"
        query: Mapping[str, str] = {"f": "json", **(params or {})}
        origin = _origin(self.base_url)
        followed: set[str] = set()
        while True:
            try:
                page = _page(await self._transport.get(url, params=query), query)
            except ServiceError as err:
                raise PaginationError([err]) from err
            yield page
            href = _next_href(page.links)
            if href is None:
                return
            if href in followed:
                # A caller collecting every page never stops, so a loop spends the quota.
                msg = "a next link repeats one already followed"
                raise PaginationError([ServiceError(msg, href)])
            if _origin(href) != origin:
                # The key header would follow the link to another host or to plain HTTP.
                msg = f"refused a next link outside {self.base_url}"
                raise PaginationError([ServiceError(msg, href)])
            followed.add(href)
            url, query = href, {}

    async def aclose(self) -> None:
        """Close the transport if this service created it."""
        if self._owns_transport:
            await self._transport.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
