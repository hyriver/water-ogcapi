"""The module paths a pickle names stay importable across releases (D-51)."""

from __future__ import annotations

import io
import json
import pickle
from pathlib import Path
from typing import Any

from water_ogcapi import (
    CRS,
    Checkpoint,
    Collections,
    Link,
    Page,
    Pagination,
    QueryResult,
    Request,
    Schema,
    ServiceProfile,
)
from water_ogcapi._results import CRS84
from water_ogcapi.exceptions import (
    PaginationError,
    PartialResultsError,
    RateLimitError,
    ServiceError,
    ValidationError,
    WaterOGCAPIError,
)

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://example.com/collections/c/items"

# A pickle loads in a later release only while each of these paths resolves. A module
# move leaves an alias at the old path; a new pickled type joins this set.
PICKLED_PATHS = {
    ("water_ogcapi._client", "ServiceProfile"),
    ("water_ogcapi._metadata", "Collection"),
    ("water_ogcapi._metadata", "Collections"),
    ("water_ogcapi._metadata", "Extent"),
    ("water_ogcapi._metadata", "Property"),
    ("water_ogcapi._metadata", "Schema"),
    ("water_ogcapi._metadata", "SpatialExtent"),
    ("water_ogcapi._metadata", "TemporalExtent"),
    ("water_ogcapi._query", "Checkpoint"),
    ("water_ogcapi._query", "Request"),
    ("water_ogcapi._results", "CRS"),
    ("water_ogcapi._results", "Link"),
    ("water_ogcapi._results", "Page"),
    ("water_ogcapi._results", "Pagination"),
    ("water_ogcapi._results", "QueryResult"),
    ("water_ogcapi._results", "ReadOnlyDict"),
    ("water_ogcapi.exceptions", "PaginationError"),
    ("water_ogcapi.exceptions", "PartialResultsError"),
    ("water_ogcapi.exceptions", "RateLimitError"),
    ("water_ogcapi.exceptions", "ServiceError"),
    ("water_ogcapi.exceptions", "ValidationError"),
    ("water_ogcapi.exceptions", "WaterOGCAPIError"),
}


class GlobalRecorder(pickle.Unpickler):
    """An unpickler that records every global a pickle names."""

    def __init__(self, data: bytes) -> None:
        super().__init__(io.BytesIO(data))
        self.names: set[tuple[str, str]] = set()

    def find_class(self, module: str, name: str, /) -> Any:
        self.names.add((module, name))
        return super().find_class(module, name)


def body(path: str) -> dict[str, Any]:
    return json.loads((FIXTURES / path).read_text())["response"]["body"]


def everything_pickled() -> list[object]:
    page = Page(
        url=URL,
        params={"f": "json"},
        status=200,
        headers={"content-type": "application/geo+json"},
        links=(Link(href=URL, rel="next", extra={"x": 1}),),
        fetched_at=1.0,
        number_matched=None,
        number_returned=0,
        feature_count=0,
    )
    result = QueryResult(
        geojson={"type": "FeatureCollection", "features": []},
        pages=(page,),
        pagination=Pagination(None, 0, 1, "complete"),
        crs=CRS(None, (), CRS84, "default"),
        checkpoint=Checkpoint(
            base_url=URL,
            collection="c",
            pending=(Request(url=URL, params={"f": "json"}),),
            held=frozenset({("GET", URL, None)}),
        ),
    )
    failure = RateLimitError("rate limit exceeded", URL, retry_after=7.0, remaining=0)
    return [
        result,
        Collections.from_dict(body("nwis/collections.json")),
        Schema.from_dict(body("nwis/monitoring-locations/queryables.json")),
        PartialResultsError([failure], result),
        PaginationError([ServiceError("service unavailable", URL, status=503)]),
        ValidationError("limit takes an integer from 1 to 50000"),
        WaterOGCAPIError("failed"),
        ServiceProfile(base_url=URL, default_limit=10, max_limit=50, rate_limit_window=60.0),
    ]


def test_pickles_name_only_the_pinned_paths() -> None:
    seen: set[tuple[str, str]] = set()
    for obj in everything_pickled():
        recorder = GlobalRecorder(pickle.dumps(obj))
        clone = recorder.load()
        assert type(clone) is type(obj)
        seen |= {(m, n) for m, n in recorder.names if m.startswith("water_ogcapi")}
    assert seen == PICKLED_PATHS
