"""The base client for one OGC API Features service."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import time
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self
from urllib.parse import urljoin, urlsplit

from water_ogcapi._cache import CacheEntry
from water_ogcapi._governor import Governor
from water_ogcapi._metadata import (
    Collections,
    Schema,
    collection_ids,
    json_object,
    parse_collections,
    parse_link,
    parse_schema,
    property_names,
)
from water_ogcapi._paging import (
    credential_link,
    malformed_counts,
    next_href,
    parse_page,
    summarize,
)
from water_ogcapi._query import Checkpoint, Held, Request, items_request, path_segment
from water_ogcapi._redact import REDACTED
from water_ogcapi._results import Page, QueryResult, crs_of
from water_ogcapi._transport import Transport, parse_count, reject_credentials, reject_non_str
from water_ogcapi.exceptions import (
    PaginationError,
    PartialResultsError,
    ServiceError,
    ValidationError,
)

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        AsyncIterator,
        Callable,
        Collection,
        Coroutine,
        Sequence,
    )

    from water_ogcapi._query import GeoInterface, RequestId
    from water_ogcapi._transport import Response

__all__ = ["OGCService", "ServiceProfile"]

DEFAULT_PORTS = {"http": 80, "https": 443}
REPEAT = "a next link repeats one already followed"
# The parameters pygeoapi reserves on an items request. Any other name filters on a
# property, and pygeoapi drops a name its provider does not know, widening the query.
QUERY_PARAMS = frozenset(
    {
        "bbox",
        "bbox-crs",
        "crs",
        "datetime",
        "f",
        "filter",
        "filter-crs",
        "filter-lang",
        "lang",
        "limit",
        "offset",
        "properties",
        "q",
        "resulttype",
        "skipGeometry",
        "sortby",
    }
)


def _origin(url: str) -> tuple[str, str | None, int | None] | None:
    """Return the scheme, host, and port of ``url``, the port defaulted by scheme."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, DEFAULT_PORTS.get(scheme) if port is None else port


def _merged(headers: Mapping[str, str] | None, own: Mapping[str, str]) -> dict[str, str]:
    """Return ``headers`` with ``own`` added last, dropping any name ``own`` sets."""
    # An unpickled Request skips its own check, and a name that is not a str has no lower().
    headers, own = dict(headers or {}), dict(own)
    reject_non_str([*headers, *own], "header names")
    names = {name.lower() for name in own}
    return {k: v for k, v in headers.items() if k.lower() not in names} | dict(own)


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ServiceProfile:
    """What one service accepts and sends, read by its service class (D-13, D-41).

    Parameters
    ----------
    base_url : str
        The landing page.
    default_limit : int
        The ``limit`` an items query sends when its ``params`` hold none.
    max_limit : int
        The largest ``limit`` the service accepts.
    key_env : str or None, optional
        The environment variable that holds the service's API key, sent in the
        ``X-Api-Key`` header (D-18). ``None``, the default, for a service sent no key.
    rate_limit_window : float or None, optional
        Seconds over which the service's ``X-RateLimit-Limit`` counts, read by its
        `Governor` (D-49). ``None``, the default, for a service that sends no
        ``X-RateLimit-*`` headers.
    """

    base_url: str
    default_limit: int
    max_limit: int
    key_env: str | None = None
    rate_limit_window: float | None = None


class _Run:
    """The state of one `OGCService._run`: requests pending, in flight, and failed (D-56)."""

    def __init__(
        self,
        start: Checkpoint,
        *,
        fetch: Callable[[Request], Coroutine[Any, Any, Page]],
        refusal: Callable[[Request, Collection[RequestId]], str | None],
        following: Callable[
            [Page, Request, Collection[RequestId]], tuple[Request | None, ServiceError | None]
        ],
        width: int,
    ) -> None:
        self._start = start
        self._fetch = fetch
        self._refusal = refusal
        self._following = following
        self._width = width
        self._queue = deque(start.pending)
        self._held = Held.start(start.held)
        self._malformed = start.malformed
        # Insertion order is start order; a task stays here until its page is yielded.
        self.running: dict[asyncio.Task[Page], Request] = {}
        # Finished tasks in the order they finished, filled by a done callback.
        self.arrived: deque[asyncio.Task[Page]] = deque()
        self._chained: list[Request] = []
        self._failed: list[Request] = []
        self._refused: list[Request] = []
        self.failures: list[ServiceError] = []

    def checkpoint(self) -> Checkpoint | None:
        """Return where the query stands, or ``None`` with nothing left but refusals."""
        retry = [*self._failed, *self.running.values(), *self._chained, *self._queue]
        if not retry:
            return None
        return Checkpoint(
            base_url=self._start.base_url,
            collection=self._start.collection,
            pending=(*retry, *self._refused),
            held=self._held.snapshot(),
            malformed=self._malformed,
        )

    def _refused_as(self, request: Request) -> str | None:
        """Return why ``request`` must not be sent: held, off origin, or already in flight."""
        reason = self._refusal(request, self._held)
        if reason is None and request.identity() in {r.identity() for r in self.running.values()}:
            reason = REPEAT
        return reason

    def _refuse(self, request: Request, reason: str) -> None:
        url = request.identity()[1]
        self.failures.append(
            ServiceError(reason, url, attempts=0, method=request.method, request_body=request.body)
        )
        self._refused.append(request)

    def _send(self, request: Request) -> None:
        """Start ``request``, or record its refusal."""
        if (reason := self._refused_as(request)) is not None:
            self._refuse(request, reason)
            return
        task = asyncio.create_task(self._fetch(request))
        # Added before any waiter's callback, so a task is listed when a wait returns.
        task.add_done_callback(self.arrived.append)
        self.running[task] = request

    def _failing(self) -> bool:
        """Whether a request failed, received or not; a done task's callback may not have run."""
        return bool(self.failures) or any(
            t.done() and not t.cancelled() and isinstance(t.exception(), ServiceError)
            for t in self.running
        )

    def launch(self) -> None:
        """Start each waiting ``next`` link, then pending requests up to the width."""
        # A chain in flight runs to its end; after a failure no new piece starts, though
        # the failure may have arrived while the caller held a page.
        while self._chained:
            self._send(self._chained.pop(0))
        while self._queue and len(self.running) < self._width and not self._failing():
            self._send(self._queue.popleft())
        if self._failing():
            # Settle the queue's refusals now, so none reads as retryable in a checkpoint
            # and a resume loop ends.
            for request in list(self._queue):
                if (reason := self._refused_as(request)) is not None:
                    self._queue.remove(request)
                    self._refuse(request, reason)

    def receive(self) -> Page | None:
        """Record the request that finished first and return its page with the checkpoint.

        A failed request returns ``None`` and is retried first on a resume.
        """
        task = self.arrived.popleft()
        request = self.running.pop(task)
        try:
            page = task.result()
        except ServiceError as err:
            self.failures.append(err)
            self._failed.append(request)
            return None
        # The identity is the URL as sent, so a next link back to this page repeats it.
        self._held.add(request.identity())
        if page.body is not None and malformed_counts(page.body):
            self._malformed = True
        following, failure = self._following(page, request, self._held)
        if failure is not None and following is not None:
            self.failures.append(failure)
            self._refused.append(following)
        elif following is not None:
            self._chained.append(following)
        return dataclasses.replace(page, checkpoint=self.checkpoint())

    async def cancel(self) -> None:
        """Cancel the requests in flight and wait for them to end."""
        for task in self.running:
            task.cancel()
        if self.running:
            await asyncio.wait(self.running)


class OGCService:
    """Async client for one OGC API Features service.

    Construction performs no HTTP (docs/design.md D-10). The first request binds the
    transport to the running event loop. A pickle leaves out the HTTP client, the
    metadata cache, and the governor's readings, rebuilt on first use, so a service that
    has run queries loads in another process (D-64).

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
        The service's page sizes, key convention, and rate-limit window, set by each
        service class. ``None`` here: a query sends the server's default ``limit``, any
        ``limit`` goes unchecked, and requests start 1 s apart (D-49).

    Raises
    ------
    TypeError
        If ``base_url`` is missing for a service without a profile.
    ValidationError
        If ``base_url`` is not exactly ``str`` (D-65).
    """

    profile: ClassVar[ServiceProfile | None] = None

    def __init__(self, base_url: str | None = None, *, transport: Transport | None = None) -> None:
        if base_url is None:
            if self.profile is None:
                msg = "base_url is required for a service without a profile"
                raise TypeError(msg)
            base_url = self.profile.base_url
        # Error messages echo it, and a subclass can read another URL to urlsplit() (D-65).
        reject_non_str([base_url], "base_url")
        self._base_url = base_url.rstrip("/")
        self._owns_transport = transport is None
        self._transport = Transport() if transport is None else transport
        self._governor = Governor(None if self.profile is None else self.profile.rate_limit_window)
        self._cache: dict[str, CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_loop: asyncio.AbstractEventLoop | None = None

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
        ValidationError
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
            raise ValidationError(msg)
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

        A header of the service, such as its key, replaces one of the same name in
        ``headers`` whatever its case (D-53).

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
            headers=_merged(headers, self._headers()),
            content=content,
            governor=self._governor,
        )

    async def _metadata[T](
        self, url: str, parse: Callable[[Response], T], *, stale: CacheEntry | None = None
    ) -> T:
        """Return the document at ``url`` as ``parse`` reads it, cached for an hour (D-07).

        An expired entry is revalidated with its ``ETag`` and ``Last-Modified``, and a
        304 keeps its payload. Only a response that ``parse`` accepts is stored. The
        payload is the cached object itself, so a public method returns a copy. Concurrent
        calls for one URL send one request. An entry that is still ``stale`` is
        revalidated however fresh, and stays cached as it was if that fails.
        """
        loop = asyncio.get_running_loop()
        if loop is not self._locks_loop:
            # A lock that has made a caller wait is bound to that loop.
            self._locks, self._locks_loop = {}, loop
        async with self._locks.setdefault(url, asyncio.Lock()):
            entry = self._cache.get(url)
            if entry is not None and entry is not stale and not entry.expired():
                return entry.payload
            headers = entry.validators() if entry is not None else {}
            resp = await self._send("GET", url, params={"f": "json"}, headers=headers)
            if resp.status != 304:
                self._cache[url] = CacheEntry.store(parse(resp), resp.headers)
            elif entry is not None and headers:
                self._cache[url] = CacheEntry.store(entry.payload, resp.headers, entry)
            else:
                msg = "HTTP 304 to a request that sent no validators"
                raise ServiceError(msg, resp.url, status=resp.status, attempts=resp.attempts)
            return self._cache[url].payload

    async def _listed[T](
        self,
        url: str,
        parse: Callable[[Response], T],
        names: Callable[[T], list[str]],
        wanted: Collection[str],
    ) -> list[str]:
        """Return the names the document at ``url`` lists, as ``names`` reads them.

        When a copy cached before this call lacks a name in ``wanted``, the copy is
        revalidated once, since the server may have added the name since.
        """
        before = self._cache.get(url)
        found = names(await self._metadata(url, parse))
        entry = self._cache[url]
        if entry is before and not set(wanted) <= set(found):
            found = names(await self._metadata(url, parse, stale=entry))
        return found

    async def _validate(
        self, collection: str, params: Mapping[str, str] | None, id_property: str | None
    ) -> None:
        """Check a query against the cached collection index and queryables (D-47).

        Raises
        ------
        ValidationError
            If the index lacks ``collection``, a name in ``params`` is neither in
            ``QUERY_PARAMS`` nor a queryable, or ``id_property`` is not a queryable.
        ServiceError
            If a metadata request fails.
        """
        index = f"{self.base_url}/collections"
        ids = await self._listed(index, parse_collections, collection_ids, [collection])
        if collection not in ids:
            msg = f"unknown collection {collection!r}; use one of {', '.join(ids)}"
            raise ValidationError(msg)
        filters = sorted(set(params or {}) - QUERY_PARAMS)
        wanted = filters + ([id_property] if id_property else [])
        if not wanted:
            return
        url = f"{self.base_url}/collections/{path_segment(collection, 'collection')}/queryables"
        queryables = await self._listed(url, parse_schema, property_names, wanted)
        listing = ", ".join(queryables)
        if unknown := [name for name in filters if name not in queryables]:
            msg = (
                f"unknown query parameter {', '.join(map(repr, unknown))} for {collection!r}; "
                f"use a queryable ({listing}) or one of {', '.join(sorted(QUERY_PARAMS))}"
            )
            raise ValidationError(msg)
        if id_property and id_property not in queryables:
            msg = (
                f"id_property {id_property!r} is not a queryable of {collection!r}; "
                f"use one of {listing}"
            )
            raise ValidationError(msg)

    async def collections(self) -> Collections:
        """Return the collection index from ``/collections``.

        The index is cached for an hour, then revalidated (D-07); `refresh` drops it.

        Raises
        ------
        ServiceError
            If the request fails or the body is not a JSON object with a
            ``collections`` list. Nothing is cached, so the next call asks again.
        ValueError
            If an entry of ``collections`` is not an object with a string ``id``.
        """
        index = await self._metadata(f"{self.base_url}/collections", parse_collections)
        # A deep copy: nested values in ``extra`` are plain lists and dicts (D-33).
        return copy.deepcopy(index)

    async def queryables(self, collection: str) -> Schema:
        """Return the properties a query on ``collection`` can filter on.

        The document is cached for an hour, then revalidated (D-07); `refresh` drops it.

        Parameters
        ----------
        collection : str
            The collection id.

        Raises
        ------
        ServiceError
            If the request fails, with status 404 for an unknown collection, or the
            body is not a JSON object with a ``properties`` object.
        ValidationError
            Before any request, if ``collection`` is empty, ``.``, or ``..``.
        """
        url = f"{self.base_url}/collections/{path_segment(collection, 'collection')}/queryables"
        return copy.deepcopy(await self._metadata(url, parse_schema))

    async def schema(self, collection: str) -> Schema:
        """Return the properties each feature of ``collection`` carries.

        The document is cached for an hour, then revalidated (D-07); `refresh` drops it.

        Parameters
        ----------
        collection : str
            The collection id.

        Raises
        ------
        ServiceError
            If the request fails, with status 404 for an unknown collection, or the
            body is not a JSON object with a ``properties`` object.
        ValidationError
            Before any request, if ``collection`` is empty, ``.``, or ``..``.
        """
        url = f"{self.base_url}/collections/{path_segment(collection, 'collection')}/schema"
        return copy.deepcopy(await self._metadata(url, parse_schema))

    def refresh(self) -> None:
        """Drop every cached metadata document, so the next call fetches it again."""
        self._cache.clear()

    def _pieces(
        self,
        request: Request,
        split_datetime: Literal["year", "month"] | None,
        sites_per_piece: int | None,
    ) -> Sequence[Request]:
        """Split an items request into independent pieces, run concurrently (D-44, D-56).

        The base service keeps it whole and refuses a split; NWIS overrides this (D-35).

        Raises
        ------
        ValidationError
            If ``split_datetime`` or ``sites_per_piece`` is given.
        """
        if split_datetime is not None or sites_per_piece is not None:
            msg = f"{type(self).__name__} does not split queries; only NWIS does"
            raise ValidationError(msg)
        return (request,)

    def _resumable(
        self, collection: str, checkpoint: Checkpoint, given: Mapping[str, object]
    ) -> None:
        """Check that ``checkpoint`` belongs to this service and ``collection`` (D-52).

        Raises
        ------
        ValidationError
            If a query keyword in ``given`` is set, or the checkpoint names another
            ``base_url`` or collection.
        """
        if used := [name for name, value in given.items() if value is not None]:
            msg = f"resume carries its own query; drop {', '.join(used)}"
            raise ValidationError(msg)
        if (checkpoint.base_url, checkpoint.collection) != (self.base_url, collection):
            msg = (
                f"resume is a checkpoint of {checkpoint.collection!r} on "
                f"{checkpoint.base_url}, not of {collection!r} on {self.base_url}"
            )
            raise ValidationError(msg)

    def _refusal(self, request: Request, held: Collection[RequestId]) -> str | None:
        """Return why ``request`` must not be sent, or ``None`` (D-34, D-52)."""
        if _origin(request.url) != _origin(self.base_url):
            return f"refused a URL outside {self.base_url}"
        if request.identity() in held:
            # A caller collecting every page never stops, so a loop spends the quota.
            return REPEAT
        return None

    def _next_request(
        self, page: Page, request: Request, held: Collection[RequestId]
    ) -> tuple[Request | None, ServiceError | None]:
        """Return the request for the ``next`` link of ``page``, and why it is refused.

        The request keeps the method, body, and headers of ``request`` (D-38). A link
        that leaves the origin of ``base_url`` (D-34) or repeats a request already
        answered is refused before it is sent, and the request comes back beside the
        refusal. A link with a credential never gets here: its page fails (D-53).
        """
        href = next_href(page.links)
        if href is None:
            return None, None
        # RFC 8288 allows a relative reference; the origin check needs it absolute.
        # Link redacts an unparsable href to REDACTED, which must not resolve to a path.
        if href != REDACTED:
            href = urljoin(page.url, href)
        following = Request(
            url=href, method=request.method, body=request.body, headers=request.headers
        )
        refusal = self._refusal(following, held)
        if refusal is None:
            return following, None
        err = ServiceError(refusal, href, attempts=0, method=page.method, request_body=request.body)
        return following, err

    async def _page(self, request: Request) -> Page:
        """Send ``request`` and parse its response as one page of items."""
        resp = await self._send(
            request.method,
            request.url,
            params=request.params,
            headers=request.headers,
            content=request.body,
        )
        return parse_page(resp, request.params, request.method, request.body)

    async def _run(self, start: Checkpoint) -> AsyncGenerator[Page]:
        """Yield each page of the pending requests, following ``next`` links (D-34, D-52).

        Pending requests start in order, up to the transport's ``max_concurrent`` at
        once, and each follows its ``next`` links to the end; pages come in the order
        their responses arrive (D-56). A ``next`` link starts once the caller asks for
        the page after the one that carried it, so an unsplit query sends nothing
        between steps (D-46). Every request, the ``next`` links included, sends the
        method, body, and headers of the request that began its chain: a ``GET`` to the
        ``next`` link of a CQL2 ``POST`` drops the filter (D-38). No request in
        ``start.held``, or one already in flight, is sent. After a failure no pending
        request starts, the chains in flight run to their ends, and then
        `PaginationError` names every failure. Each yielded page carries the checkpoint
        after it: the failed requests, those in flight or not yet yielded, the ``next``
        links not yet started, the requests never started, then the refused ones, which
        a resume refuses again; refused requests alone leave no checkpoint. Closing the
        iterator cancels the requests in flight.
        """
        run = _Run(
            start,
            fetch=self._page,
            refusal=self._refusal,
            following=self._next_request,
            width=self._transport.max_concurrent,
        )
        try:
            run.launch()
            while run.running:
                while not run.arrived:
                    await asyncio.wait(run.running, return_when=asyncio.FIRST_COMPLETED)
                if (page := run.receive()) is not None:
                    yield page
                run.launch()
            if run.failures:
                raise PaginationError(run.failures, run.checkpoint())
        finally:
            await run.cancel()

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
        split_datetime: Literal["year", "month"] | None = None,
        sites_per_piece: int | None = None,
        validate: bool = True,
        resume: Checkpoint | None = None,
    ) -> AsyncIterator[Page]:
        """Yield each page of a collection's items, following ``next`` links (D-34).

        Only a page without a ``next`` link ends the traversal. A page shorter than
        requested, or one without ``numberMatched``, ends nothing. A split query runs
        its pieces concurrently and yields pages as they arrive (D-56).

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
        split_datetime : {'year', 'month'}, optional
            NWIS only (D-35, D-56): split the ``datetime`` interval in ``params`` at
            calendar boundaries in UTC into pieces that run concurrently, up to the
            transport's ``max_concurrent`` at once, each following its own ``next``
            links. The interval needs both ends, as dates or timestamps with an
            offset, and each piece ends one microsecond before the next begins. Every
            piece costs at least one request, so size pieces near one page.
        sites_per_piece : int, optional
            NWIS only (D-35, D-56): split the site list, ``ids`` or a comma-separated
            ``monitoring_location_id`` in ``params``, into lists of this many sites
            that run concurrently as ``split_datetime`` pieces do. A site listed twice
            is sent once. Not with ``split_datetime``.
        validate : bool, optional
            Before the first page request, check ``collection`` against the collection
            index, and each name in ``params`` outside ``QUERY_PARAMS``, and
            ``id_property``, against the collection's queryables, from metadata cached
            for an hour (D-07). pygeoapi drops a name it does not know, so an unchecked
            typo returns the whole collection. Defaults to ``True``; ``False`` skips the
            check, for a server that accepts a name its queryables leave out.
        resume : Checkpoint, optional
            Continue a query from a `Checkpoint`: a page's ``checkpoint`` or a
            `PaginationError`'s (D-52). Its pending requests are sent as stored, so no
            query keyword may come with it, ``validate`` is ignored, and no request it
            holds is sent again.

        Yields
        ------
        Page
            One per response, the decoded document in ``body`` and its bytes in
            ``content``. Its ``checkpoint`` is where the query stands once the caller
            holds it.

        Raises
        ------
        ValidationError
            Before any request, when ``collection`` is empty, ``.``, or ``..``, both
            ``bbox`` and ``geometry``, or ``ids`` and ``filter``, are given, ``ids`` is a
            ``str`` or ``bytes``, empty, or lacks ``id_property``, ``bbox`` or
            ``geometry`` is malformed, a CQL2-JSON filter holds a NaN or infinite
            number, a filter is empty, ``params`` also holds a parameter a keyword sets
            (``bbox``, ``filter``, ``filter-lang``) or a credential such as ``api_key``,
            or ``limit`` is not an integer from 1 to the profile's ``max_limit``, or
            a split is given to a service other than NWIS, both splits are given, or
            the query has no closed ``datetime`` interval or site list to split.
            With ``validate``, before the first page request, when the collection, a
            name in ``params``, or ``id_property`` is unknown; the message lists the
            valid ones.
            With ``resume``, when a query keyword comes with it or it is a checkpoint
            of another service or collection.
        TypeError
            Before any request, when a CQL2-JSON filter holds a value JSON cannot
            encode, such as a set.
        ServiceError
            With ``validate``, when a metadata request fails.
        PaginationError
            When a request fails, a body is not a FeatureCollection with well-formed
            links, or a ``next`` link repeats a request already answered, leaves the
            scheme, host, and port of ``base_url``, or a page has a link that carries a
            credential (D-53). It is raised once every page that arrived has been
            yielded; its failures name each such request or link by URL, and its
            ``checkpoint`` is where the query stands.
        """
        if resume is not None:
            given = {
                "params": params,
                "bbox": bbox,
                "geometry": geometry,
                "filter": filter,
                "ids": ids,
                "id_property": id_property,
                "split_datetime": split_datetime,
                "sites_per_piece": sites_per_piece,
            }
            self._resumable(collection, resume, given)
            start = resume
        else:
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
            pieces = tuple(self._pieces(request, split_datetime, sites_per_piece))
            if validate:
                # The request's copy: the caller's mapping may change during the await.
                await self._validate(collection, request.params, id_property)
            start = Checkpoint(base_url=self.base_url, collection=collection, pending=pieces)
        # Closed with this iterator, so the pieces in flight are cancelled at once.
        async with contextlib.aclosing(self._run(start)) as pages:
            async for page in pages:
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
        split_datetime: Literal["year", "month"] | None = None,
        sites_per_piece: int | None = None,
        validate: bool = True,
        keep_pages: bool = False,
        resume: QueryResult | Checkpoint | None = None,
    ) -> QueryResult:
        """Fetch every page of a collection's items into one `QueryResult` (D-22).

        Parameters
        ----------
        collection : str
            The collection id.
        params, bbox, geometry, filter, ids, id_property, split_datetime, sites_per_piece
            The query and its pieces, as for `iter_pages`.
        validate : bool, optional
            Check the query against cached metadata first, as for `iter_pages`.
        keep_pages : bool, optional
            Keep each page's response bytes in ``Page.content`` (D-23). Page bodies
            are always dropped. With ``resume``, it applies to the new pages; held
            pages keep what the earlier call kept.
        resume : QueryResult or Checkpoint, optional
            Continue a query that stopped on a failure (D-52): the ``result`` of a
            `PartialResultsError`, or the ``checkpoint`` of a `PaginationError` raised
            before any page arrived. The result's pages and features come first in the
            new result, and no request for them is sent. No query keyword may come
            with it.

        Returns
        -------
        QueryResult
            ``geojson`` holds the features of all pages in the order they arrived,
            those of ``resume`` first.

        Raises
        ------
        ValidationError
            On a malformed query or, with ``validate``, an unknown name, as for
            `iter_pages`. With ``resume``, before any request, when a query keyword
            comes with it, it is from another service or collection, a `QueryResult`
            has no ``checkpoint``, or a `Checkpoint` holds pages, which only the
            `QueryResult` that holds them can carry.
        TypeError
            Before any request, when a CQL2-JSON filter holds a value JSON cannot
            encode, as for `iter_pages`.
        ServiceError
            With ``validate``, when a metadata request fails.
        PartialResultsError
            When a page fails after at least one succeeded, ``resume``'s included. Its
            ``result`` holds the pages that succeeded and the checkpoint to resume.
        PaginationError
            When no page arrives, as when the first page of an unsplit query fails.
        """
        pages: list[Page] = []
        features: list[Any] = []
        failure: PaginationError | None = None
        checkpoint = resume.checkpoint if isinstance(resume, QueryResult) else resume
        malformed = False
        if resume is not None:
            if checkpoint is None:
                msg = "resume has nothing left to send: the query ended or a request was refused"
                raise ValidationError(msg)
            given = {
                "params": params,
                "bbox": bbox,
                "geometry": geometry,
                "filter": filter,
                "ids": ids,
                "id_property": id_property,
                "split_datetime": split_datetime,
                "sites_per_piece": sites_per_piece,
            }
            self._resumable(collection, checkpoint, given)
            if isinstance(resume, Checkpoint) and resume.held:
                msg = "this checkpoint holds pages; resume the QueryResult that holds them"
                raise ValidationError(msg)
            if isinstance(resume, QueryResult):
                # Copied, so the two results share no feature object (D-23).
                pages = list(resume.pages)
                features = copy.deepcopy(resume.geojson["features"])
            malformed = checkpoint.malformed
        try:
            async for page in self.iter_pages(
                collection,
                params=params,
                bbox=bbox,
                geometry=geometry,
                filter=filter,
                ids=ids,
                id_property=id_property,
                split_datetime=split_datetime,
                sites_per_piece=sites_per_piece,
                validate=validate,
                resume=checkpoint,
            ):
                if page.body is None:
                    # Unreachable: parse_page always sets the body. Keeps the type narrow.
                    raise AssertionError  # pragma: no cover
                features.extend(page.body["features"])
                malformed = malformed or malformed_counts(page.body)
                pages.append(
                    dataclasses.replace(
                        page,
                        body=None,
                        content=page.content if keep_pages else None,
                        checkpoint=None,
                    )
                )
        except PaginationError as err:
            failure = err
        result = QueryResult(
            geojson={"type": "FeatureCollection", "features": features},
            pages=tuple(pages),
            pagination=summarize(pages, features, failed=failure is not None, malformed=malformed),
            crs=crs_of(pages),
            checkpoint=None if failure is None else failure.checkpoint,
        )
        if failure is None:
            return result
        if not pages:
            raise failure
        raise PartialResultsError(failure.failures, result) from failure

    async def item(
        self,
        collection: str,
        item_id: str | int,
        *,
        params: Mapping[str, str] | None = None,
        validate: bool = True,
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
        validate : bool, optional
            Before the request, check ``collection`` against the cached collection
            index, as `iter_pages` does. Defaults to ``True``.

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
            a Feature whose ``links`` member, when present, is a list. With
            ``validate``, also when the metadata request fails.
        ValidationError
            Before any request, if ``collection`` or ``item_id`` is empty, ``.``, or
            ``..``, which would address another path, or ``params`` carries a credential
            such as ``api_key``. With ``validate``, if the collection is unknown.
        """
        url = (
            f"{self.base_url}/collections/{path_segment(collection, 'collection')}"
            f"/items/{path_segment(item_id, 'item_id')}"
        )
        query = {"f": "json", **(params or {})}
        reject_credentials(url, query)
        if validate:
            await self._validate(collection, None, None)
        resp = await self._send("GET", url, params=query)
        body = json_object(resp)
        links = body.get("links", [])
        if body.get("type") != "Feature" or not isinstance(links, list):
            msg = "response is not a Feature with a links list"
            raise ServiceError(msg, resp.url, status=resp.status, attempts=resp.attempts)
        if credential_link(links):
            # body and content would keep it (D-29).
            msg = "response has a link that carries a credential"
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

    def __getstate__(self) -> dict[str, Any]:
        # Loop-bound locks, and cache entries stamped by this process's clock (D-64).
        return {**self.__dict__, "_cache": {}, "_locks": {}, "_locks_loop": None}
