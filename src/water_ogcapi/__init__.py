"""An async-first Python client for USGS OGC API Features services."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

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
    Completeness,
    Link,
    Page,
    Pagination,
    QueryResult,
    StopReason,
)

try:
    __version__ = version("water-ogcapi")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "Collection",
    "Collections",
    "Completeness",
    "Extent",
    "Link",
    "Page",
    "Pagination",
    "Property",
    "QueryResult",
    "Schema",
    "SpatialExtent",
    "StopReason",
    "TemporalExtent",
    "__version__",
    "configure_logger",
]
