"""Service classes, each reading its base URL, page sizes, and key from a profile (D-41)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from water_ogcapi._client import OGCService, ServiceProfile

if TYPE_CHECKING:
    from water_ogcapi._transport import Transport

__all__ = ["NWIS", "FabricData", "GeoConnex"]

NWIS_PROFILE = ServiceProfile(
    # The landing page; without the slash it redirects to http:// with the key in its
    # query (L-11).
    base_url="https://api.waterdata.usgs.gov/ogcapi/v1/",
    default_limit=10_000,
    max_limit=50_000,
    rate_limit_headers=True,
    key_env="USGS_API_KEY",
)

FABRICDATA_PROFILE = ServiceProfile(
    base_url="https://api.water.usgs.gov/fabric/pygeoapi/",
    default_limit=1_000,
    max_limit=1_000,
    rate_limit_headers=True,
)

GEOCONNEX_PROFILE = ServiceProfile(
    base_url="https://reference.geoconnex.us/",
    default_limit=500,
    max_limit=10_000,
    rate_limit_headers=False,
)


class NWIS(OGCService):
    """Async client for the USGS Water Data OGC API, which serves NWIS data (D-42).

    Construction performs no HTTP. Pages hold 10,000 features unless ``params`` set a
    ``limit``, and 50,000 at most (D-41).

    Parameters
    ----------
    api_key : str, optional
        A USGS API key, sent in the ``X-Api-Key`` header of every request (D-18).
        Defaults to the ``USGS_API_KEY`` environment variable, read once here. An empty
        string sends no key.
    transport : Transport, optional
        Sends every request, as for `OGCService`.

    Raises
    ------
    ValueError
        If the key holds a character other than printable ASCII, such as a newline.
    """

    profile = NWIS_PROFILE

    def __init__(self, *, api_key: str | None = None, transport: Transport | None = None) -> None:
        super().__init__(transport=transport)
        if api_key is None and NWIS_PROFILE.key_env:
            api_key = os.environ.get(NWIS_PROFILE.key_env)
        # Transport.request() refuses such a key too, but only once a query runs (D-43).
        if api_key and not (api_key.isascii() and api_key.isprintable()):
            msg = "the USGS API key must be printable ASCII"
            raise ValueError(msg)
        self._api_key = api_key or None

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key} if self._api_key else {}


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
