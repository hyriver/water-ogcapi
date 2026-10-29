"""Conversion of features to an Arrow table, for the ``arrow`` extra (D-58, D-60, D-66).

Only `QueryResult.to_arrow` and `Page.to_arrow` import this module, so pyarrow, shapely, and
pyproj stay off the core import path (D-09).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyproj
import shapely
from pyproj.exceptions import CRSError

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from water_ogcapi._results import CRS

__all__ = ["to_table"]

# A first axis GeoArrow must read as y. Its name decides: a polar stereographic easting
# (EPSG:3031) points north.
Y_FIRST = ("latitude", "northing", "southing")


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


def _swap_xy(coords: NDArray[np.float64]) -> NDArray[np.float64]:
    """Exchange the first two columns of a coordinate array, keeping any third."""
    swapped = coords.copy()
    swapped[:, [0, 1]] = coords[:, [1, 0]]
    return swapped


def to_table(features: list[Any], crs: CRS) -> pa.Table:
    """Return ``features`` as a table; `QueryResult.to_arrow` documents it."""
    if not features:
        return pa.table({})
    table = pa.Table.from_struct_array(pa.array(_rows(features)))
    geometries = [feature.get("geometry") for feature in features]
    if all(geometry is None for geometry in geometries):
        return table
    if crs.effective is None:
        msg = (
            f"the CRS of the geometries is {crs.source}: requested "
            f"{crs.requested!r}, pages named {crs.page_crs!r}"
        )
        raise ValueError(msg)
    try:
        proj = pyproj.CRS.from_user_input(crs.effective)
    except CRSError as err:
        msg = f"pyproj cannot read the geometries' CRS {crs.effective!r}"
        raise ValueError(msg) from err
    if "geometry" in table.column_names:
        msg = "a property named 'geometry' would share a name with the geometry column"
        raise ValueError(msg)
    texts = [None if geometry is None else json.dumps(geometry) for geometry in geometries]
    shapes = shapely.from_geojson(texts)
    # An OGC server sends coordinates in the CRS's axis order (latitude first for
    # EPSG:4326), and GeoArrow reads them as x/y whatever the CRS says.
    first = proj.axis_info[0].name.lower() if proj.axis_info else ""
    if any(word in first for word in Y_FIRST):
        # include_z=None keeps each geometry's dimensions; shapely's stubs type it bool.
        shapes = shapely.transform(shapes, _swap_xy, include_z=None)  # pyright: ignore[reportCallIssue, reportArgumentType]
    wkb = shapely.to_wkb(shapes, flavor="iso")
    metadata = {
        "ARROW:extension:name": "geoarrow.wkb",
        "ARROW:extension:metadata": json.dumps({"crs": proj.to_json_dict()}),
    }
    field = pa.field("geometry", pa.binary(), metadata=metadata)
    return table.append_column(field, pa.array(wkb, pa.binary()))
