"""An async-first Python client for USGS OGC API Features services."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from water_ogcapi._logging import configure_logger
from water_ogcapi._results import Link, Page, Pagination, QueryResult

try:
    __version__ = version("water-ogcapi")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["Link", "Page", "Pagination", "QueryResult", "__version__", "configure_logger"]
