"""Tests for `QueryResult.to_arrow` (D-58)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx2
import polars as pl
import pyarrow as pa
import pytest
import shapely

from water_ogcapi import CRS, Pagination, QueryResult
from water_ogcapi._results import CRS84
from water_ogcapi._transport import Transport
from water_ogcapi.aio import OGCService

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send no metadata request before a query."""

    async def skip(*_: object) -> None:
        return None

    monkeypatch.setattr(OGCService, "_validate", skip)


def collected(directory: str, count: int) -> QueryResult:
    """Collect the recorded pages of ``directory``, served with their recorded headers."""
    records = [
        json.loads((FIXTURES / directory / f"items-{n}.json").read_text())
        for n in range(1, count + 1)
    ]
    last = records[-1]["response"]["body"]
    last["links"] = [link for link in last["links"] if link["rel"] != "next"]
    queue = [r["response"] for r in records]

    def handler(_: httpx2.Request) -> httpx2.Response:
        reply = queue.pop(0)
        return httpx2.Response(reply["status"], headers=reply["headers"], json=reply["body"])

    transport = Transport(transport=httpx2.MockTransport(handler), max_retries=0)
    first = records[0]["request"]
    base, _, rest = first["url"].partition("/collections/")
    service = OGCService(base, transport=transport)
    return asyncio.run(service.collect(rest.split("/")[0], params=first["params"]))


EPSG4326 = "http://www.opengis.net/def/crs/EPSG/0/4326"
DEFAULT = CRS(None, (), CRS84, "default")


def result_of(features: list[dict[str, Any]], crs: CRS = DEFAULT) -> QueryResult:
    return QueryResult(
        geojson={"type": "FeatureCollection", "features": features},
        pages=(),
        pagination=Pagination(None, len(features), 0, "complete"),
        crs=crs,
    )


def point(fid: object, x: float, *z: float, **properties: Any) -> dict[str, Any]:
    geometry = {"type": "Point", "coordinates": [x, 38.9, *z]}
    return {"type": "Feature", "id": fid, "geometry": geometry, "properties": properties}


def test_a_result_becomes_a_table_with_a_geoarrow_column() -> None:
    result = collected("nwis/monitoring-locations", 3)
    features = result.geojson["features"]
    table = result.to_arrow()
    assert result.crs.source == "content_crs"
    assert table.num_rows == len(features)
    assert table.column_names[0] == "id"
    assert table.column_names.count("id") == 1, "the equal property id is dropped"
    assert table.column("id").to_pylist() == [f["id"] for f in features]
    assert table.column("monitoring_location_name").to_pylist() == [
        f["properties"]["monitoring_location_name"] for f in features
    ]
    field = table.schema.field("geometry")
    assert field.metadata == {
        b"ARROW:extension:name": b"geoarrow.wkb",
        b"ARROW:extension:metadata": json.dumps({"crs": CRS84}).encode(),
    }
    expected = [shapely.geometry.shape(f["geometry"]) for f in features]
    assert list(shapely.from_wkb(table.column("geometry").to_pylist())) == expected


def test_geopandas_polars_and_pandas_read_the_table() -> None:
    table = collected("nwis/monitoring-locations", 3).to_arrow()
    frame = gpd.GeoDataFrame.from_arrow(table)
    assert frame.crs is not None
    assert frame.crs.to_string() == "OGC:CRS84"
    assert (
        frame.geometry.x.tolist()
        == shapely.get_x(shapely.from_wkb(table.column("geometry").to_pylist())).tolist()
    )
    ids = table.column("id").to_pylist()
    frame = pl.from_arrow(table)
    assert isinstance(frame, pl.DataFrame)
    assert frame.shape == (table.num_rows, table.num_columns)
    assert frame["id"].to_list() == ids
    assert "geoarrow.wkb" in str(frame.schema["geometry"]), "kept as an extension type"
    assert table.to_pandas()["id"].tolist() == ids


def test_a_result_without_geometry_has_no_geometry_column() -> None:
    result = collected("geoconnex/hu02", 3)
    table = result.to_arrow()
    assert "geometry" not in table.column_names
    assert table.column("id").to_pylist() == [f"{n:02}" for n in range(1, 23)]


def test_the_feature_id_fills_a_null_id_property() -> None:
    """FabricData's properties carry an id of null beside the feature id."""
    result = collected("fabricdata/gagesii", 2)
    table = result.to_arrow()
    assert table.column("id").to_pylist() == [f["id"] for f in result.geojson["features"]]
    assert table.column_names.count("id") == 1


def test_a_property_seen_only_in_a_later_feature_is_kept() -> None:
    table = result_of([point(1, -77.0, a=1), point(2, -77.1, a=2.5, b="x")]).to_arrow()
    assert table.column("a").to_pylist() == [1.0, 2.5]
    assert table.column("b").to_pylist() == [None, "x"]


def test_a_feature_without_geometry_reads_null() -> None:
    features = [point("a", -77.0), {**point("b", 0.0), "geometry": None}]
    table = result_of(features).to_arrow()
    assert table.column("geometry").to_pylist()[1] is None


def test_without_ids_the_properties_are_the_columns() -> None:
    features = [{k: v for k, v in point(None, -77.0, id="own").items() if k != "id"}]
    table = result_of(features).to_arrow()
    assert table.column_names == ["id", "geometry"]
    assert table.column("id").to_pylist() == ["own"]


def test_a_feature_without_an_id_reads_null() -> None:
    features = [point(1, -77.0), {k: v for k, v in point(None, -77.1).items() if k != "id"}]
    assert result_of(features).to_arrow().column("id").to_pylist() == [1, None]


def test_a_third_coordinate_is_kept() -> None:
    table = result_of([point(1, -77.0, 12.5)]).to_arrow()
    [geometry] = shapely.from_wkb(table.column("geometry").to_pylist())
    assert shapely.get_z(geometry) == 12.5


def test_an_empty_result_is_an_empty_table() -> None:
    assert result_of([]).to_arrow().shape == (0, 0)


@pytest.mark.parametrize(
    ("features", "crs", "message"),
    [
        ([point(1, -77.0)], CRS("EPSG:3857", (), None, "unconfirmed"), "unconfirmed"),
        # An OGC server sends EPSG:4326 coordinates latitude first; GeoArrow reads x/y.
        ([point(1, 38.9)], CRS(EPSG4326, (EPSG4326,), EPSG4326, "content_crs"), "CRS84"),
        ([point(1, -77.0, id=2)], DEFAULT, "property 'id'"),
        ([point(1, -77.0, geometry="x")], DEFAULT, "named 'geometry'"),
    ],
)
def test_a_table_that_would_mislead_raises(
    features: list[dict[str, Any]], crs: CRS, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        result_of(features, crs).to_arrow()


def test_without_geometry_the_crs_and_a_geometry_property_pass() -> None:
    features = [{**point(1, 0.0, geometry="x"), "geometry": None}]
    crs = CRS(EPSG4326, (), None, "unconfirmed")
    assert result_of(features, crs).to_arrow().column_names == ["id", "geometry"]


@pytest.mark.parametrize(
    "features",
    [[point(1, -77.0, a=1), point(2, -77.1, a="x")], [point(1, -77.0), point("b", -77.1)]],
)
def test_a_column_of_mixed_types_raises(features: list[dict[str, Any]]) -> None:
    with pytest.raises(pa.ArrowException):
        result_of(features).to_arrow()


@pytest.mark.parametrize("module", ["pyarrow", "shapely"])
def test_without_the_extra_the_error_names_it(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    monkeypatch.delitem(sys.modules, "water_ogcapi._arrow", raising=False)
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(ModuleNotFoundError, match=r"water-ogcapi\[arrow\]") as info:
        result_of([point(1, -77.0)]).to_arrow()
    assert info.value.name == module


def test_another_missing_module_is_raised_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "water_ogcapi._arrow", None)
    with pytest.raises(ModuleNotFoundError) as info:
        result_of([point(1, -77.0)]).to_arrow()
    assert info.value.name == "water_ogcapi._arrow"
    assert "water-ogcapi[arrow]" not in str(info.value)


def test_the_core_import_path_loads_no_frame_library() -> None:
    """D-09: only to_arrow() loads pyarrow and shapely."""
    heavy = ("pyarrow", "shapely", "numpy", "pandas", "geopandas", "polars")
    code = f"import sys, water_ogcapi; print([m for m in {heavy!r} if m in sys.modules])"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
