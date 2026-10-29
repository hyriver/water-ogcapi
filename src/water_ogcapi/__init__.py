"""A Python client for USGS OGC API Features services.

The service classes here block, as does `Client` for plain HTTP; `water_ogcapi.aio` holds
the async service classes (D-48, D-70).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from water_ogcapi import aio, exceptions
from water_ogcapi._client import ServiceProfile
from water_ogcapi._governor import Governor
from water_ogcapi._logging import configure_logger
from water_ogcapi._metadata import (
    Collection,
    Collections,
    Extent,
    Property,
    Schema,
    SpatialExtent,
    TemporalExtent,
)
from water_ogcapi._query import Checkpoint, Request
from water_ogcapi._results import (
    CRS,
    Check,
    Completeness,
    CRSSource,
    Link,
    Page,
    Pagination,
    QueryResult,
    StopReason,
)
from water_ogcapi._sync import NWIS, Client, FabricData, GeoConnex, OGCService
from water_ogcapi._transport import Transport

try:
    __version__ = version("water-ogcapi")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "CRS",
    "NWIS",
    "CRSSource",
    "Check",
    "Checkpoint",
    "Client",
    "Collection",
    "Collections",
    "Completeness",
    "Extent",
    "FabricData",
    "GeoConnex",
    "Governor",
    "Link",
    "OGCService",
    "Page",
    "Pagination",
    "Property",
    "QueryResult",
    "Request",
    "Schema",
    "ServiceProfile",
    "SpatialExtent",
    "StopReason",
    "TemporalExtent",
    "Transport",
    "__version__",
    "aio",
    "configure_logger",
    "exceptions",
]
