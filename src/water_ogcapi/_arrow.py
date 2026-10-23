"""Conversion of a `QueryResult` to an Arrow table, for the ``arrow`` extra (D-58).

Only `QueryResult.to_arrow` imports this module, so pyarrow and shapely stay off the core
import path (D-09).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import shapely

from water_ogcapi._results import CRS84

if TYPE_CHECKING:
    from water_ogcapi._results import QueryResult

__all__ = ["to_table"]


def _rows(features: list[Any]) -> list[dict[str, Any]]:
    """Return each feature's properties, with its ``id`` first when any feature has one."""
    if all(feature.get("id") is None for feature in features):
        return [dict(feature.get("properties") or {}) for feature in features]
    rows: list[dict[str, Any]] = []
    for feature in features:
        fid = feature.get("id")
        properties = feature.get("properties") or {}
        own = properties.get("id")
        if own is not None and own != fid:
            msg = f"feature {fid!r} has a property 'id' of {own!r}; the id column holds one"
            raise ValueError(msg)
        rows.append({"id": fid} | {k: v for k, v in properties.items() if k != "id"})
    return rows


def to_table(result: QueryResult) -> pa.Table:
    """Return ``result``'s features as a table; `QueryResult.to_arrow` documents it."""
    features: list[Any] = result.geojson["features"]
    if not features:
        return pa.table({})
    table = pa.Table.from_struct_array(pa.array(_rows(features)))
    geometries = [feature.get("geometry") for feature in features]
    if all(geometry is None for geometry in geometries):
        return table
    # GeoArrow reads coordinates as x/y, and an OGC server answering a request for a
    # latitude-first CRS such as EPSG:4326 sends latitude first; only CRS84 is known x/y.
    if result.crs.effective != CRS84:
        msg = (
            f"to_arrow() takes CRS84 geometries; this result's CRS is "
            f"{result.crs.effective!r} ({result.crs.source}): requested "
            f"{result.crs.requested!r}, pages named {result.crs.page_crs!r}"
        )
        raise ValueError(msg)
    if "geometry" in table.column_names:
        msg = "a property named 'geometry' would share a name with the geometry column"
        raise ValueError(msg)
    texts = [None if geometry is None else json.dumps(geometry) for geometry in geometries]
    wkb = shapely.to_wkb(shapely.from_geojson(texts))
    metadata = {
        "ARROW:extension:name": "geoarrow.wkb",
        "ARROW:extension:metadata": json.dumps({"crs": CRS84}),
    }
    field = pa.field("geometry", pa.binary(), metadata=metadata)
    return table.append_column(field, pa.array(wkb, pa.binary()))
