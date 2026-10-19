"""A Python client for USGS OGC API Features services.

The service classes here block; `water_ogcapi.aio` holds their async twins (D-48).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from water_ogcapi._client import ServiceProfile
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
from water_ogcapi._results import (
    Check,
    Completeness,
    Crs,
    CrsSource,
    Link,
    Page,
    Pagination,
    QueryResult,
    StopReason,
)
from water_ogcapi._sync import NWIS, FabricData, GeoConnex, OGCService
from water_ogcapi._transport import Transport

try:
    __version__ = version("water-ogcapi")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "NWIS",
    "Check",
    "Collection",
    "Collections",
    "Completeness",
    "Crs",
    "CrsSource",
    "Extent",
    "FabricData",
    "GeoConnex",
    "Link",
    "OGCService",
    "Page",
    "Pagination",
    "Property",
    "QueryResult",
    "Schema",
    "ServiceProfile",
    "SpatialExtent",
    "StopReason",
    "TemporalExtent",
    "Transport",
    "__version__",
    "configure_logger",
]
