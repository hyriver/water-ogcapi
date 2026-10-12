"""The base client for one OGC API Features service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self, cast

from water_ogcapi._metadata import Collections
from water_ogcapi._transport import Transport
from water_ogcapi.exceptions import ServiceError

if TYPE_CHECKING:
    from water_ogcapi._transport import Response

__all__ = ["OGCService"]


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

    async def aclose(self) -> None:
        """Close the transport if this service created it."""
        if self._owns_transport:
            await self._transport.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
