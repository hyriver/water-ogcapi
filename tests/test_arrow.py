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
import pyproj
import pytest
import shapely

from water_ogcapi import CRS, Page, Pagination, QueryResult
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


def served(directory: str, count: int) -> tuple[OGCService, str, dict[str, str]]:
    """Serve the recorded pages of ``directory`` with their recorded headers."""
    records = [
        json.loads((FIXTURES / directory / f"items-{n}.json").read_text(encoding="utf-8"))
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
    return service, rest.split("/")[0], first["params"]


def collected(directory: str, count: int) -> QueryResult:
    service, collection, params = served(directory, count)
    return asyncio.run(service.collect(collection, params=params))


def iterated(directory: str, count: int) -> list[Page]:
    service, collection, params = served(directory, count)

    async def pages() -> list[Page]:
        return [page async for page in service.iter_pages(collection, params=params)]

    return asyncio.run(pages())


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


def page_of(
    body: dict[str, Any] | None, content: bytes | None = None, headers: dict[str, str] | None = None
) -> Page:
    return Page(
        url="https://example.test/collections/c/items",
        params={},
        status=200,
        headers=headers or {},
        links=(),
        fetched_at=0.0,
        number_matched=None,
        number_returned=None,
        feature_count=0,
        body=body,
        content=content,
    )


def collection_of(*features: dict[str, Any]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


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
        b"ARROW:extension:metadata": json.dumps({"crs": pyproj.CRS(CRS84).to_json_dict()}).encode(),
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


def test_a_third_coordinate_is_kept_in_iso_wkb() -> None:
    table = result_of([point(1, -77.0, 12.5)]).to_arrow()
    [wkb] = table.column("geometry").to_pylist()
    assert wkb[1:5] == (1001).to_bytes(4, "little"), "ISO Point Z"
    assert shapely.get_z(shapely.from_wkb(wkb)) == 12.5


def lat_first(coordinates: list[Any], kind: str = "Point") -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": 1,
        "geometry": {"type": kind, "coordinates": coordinates},
        "properties": {},
    }


@pytest.mark.parametrize(
    ("feature", "expected"),
    [
        (lat_first([38.9, -77.0, 12.5]), "POINT Z (-77 38.9 12.5)"),
        (
            lat_first([[[38.8, -77.1], [38.8, -77.0], [38.9, -77.0], [38.8, -77.1]]], "Polygon"),
            "POLYGON ((-77.1 38.8, -77 38.8, -77 38.9, -77.1 38.8))",
        ),
    ],
)
def test_a_latitude_first_crs_is_written_x_first(feature: dict[str, Any], expected: str) -> None:
    """An OGC server sends EPSG:4326 coordinates latitude first; GeoArrow reads x/y."""
    crs = CRS(EPSG4326, (EPSG4326,), EPSG4326, "content_crs")
    table = result_of([feature], crs).to_arrow()
    [geometry] = shapely.from_wkb(table.column("geometry").to_pylist())
    assert shapely.to_wkt(geometry, rounding_precision=6) == expected
    meta = json.loads(table.schema.field("geometry").metadata[b"ARROW:extension:metadata"])
    assert meta == {"crs": pyproj.CRS(EPSG4326).to_json_dict()}
    assert gpd.GeoDataFrame.from_arrow(table).crs == pyproj.CRS("EPSG:4326")


@pytest.mark.parametrize(
    ("crs", "swapped"),
    [
        ("EPSG:4326+5703", True),  # compound, latitude first
        ("EPSG:2193", True),  # northing first
        ("EPSG:32661", True),  # polar, northing first
        ("EPSG:3031", False),  # polar, an easting that points north
        ("EPSG:3413", False),  # polar, an easting that points south
        ("EPSG:4978", False),  # geocentric
    ],
)
def test_the_swap_follows_the_first_axis_name(crs: str, *, swapped: bool) -> None:
    feature = {**point(1, 0.0), "geometry": {"type": "Point", "coordinates": [1.0, 2.0, 3.0]}}
    table = result_of([feature], CRS(crs, (crs,), crs, "content_crs")).to_arrow()
    [geometry] = shapely.from_wkb(table.column("geometry").to_pylist())
    xyz = (shapely.get_x(geometry), shapely.get_y(geometry), shapely.get_z(geometry))
    assert xyz == ((2.0, 1.0, 3.0) if swapped else (1.0, 2.0, 3.0))


def test_a_swap_keeps_each_geometry_s_dimensions() -> None:
    members = [
        {"type": "Point", "coordinates": [1.0, 2.0]},
        {"type": "Point", "coordinates": [3.0, 4.0, 5.0]},
    ]
    geometries = [
        {"type": "Point", "coordinates": [38.9, -77.0]},
        {"type": "Point", "coordinates": [38.9, -77.0, 5.0]},
        {"type": "GeometryCollection", "geometries": members},
        {"type": "LineString", "coordinates": []},
        None,
    ]
    features = [
        {"type": "Feature", "id": n, "geometry": g, "properties": {}}
        for n, g in enumerate(geometries)
    ]
    crs = CRS(EPSG4326, (EPSG4326,), EPSG4326, "content_crs")
    wkb = result_of(features, crs).to_arrow().column("geometry").to_pylist()
    assert [None if w is None else shapely.to_wkt(shapely.from_wkb(w)) for w in wkb] == [
        "POINT (-77 38.9)",
        "POINT Z (-77 38.9 5)",
        "GEOMETRYCOLLECTION Z (POINT (2 1), POINT Z (4 3 5))",
        "LINESTRING EMPTY",
        None,
    ]


@pytest.mark.parametrize(
    ("crs", "first"),
    [
        ("http://www.opengis.net/def/crs/OGC/1.3/CRS83", (-77.0, 38.9)),
        ("http://www.opengis.net/def/crs/EPSG/0/3857", (-8585809.2, 4714480.2)),
    ],
)
def test_an_x_first_crs_is_written_as_sent(crs: str, first: tuple[float, float]) -> None:
    """The CRSs NWIS and GeoConnex return for a crs request (live, D-60)."""
    feature = {**point(1, first[0]), "geometry": {"type": "Point", "coordinates": list(first)}}
    table = result_of([feature], CRS(crs, (crs,), crs, "content_crs")).to_arrow()
    [geometry] = shapely.from_wkb(table.column("geometry").to_pylist())
    assert (shapely.get_x(geometry), shapely.get_y(geometry)) == first
    assert gpd.GeoDataFrame.from_arrow(table).crs == pyproj.CRS(crs)


def test_an_empty_result_is_an_empty_table() -> None:
    assert result_of([]).to_arrow().shape == (0, 0)


@pytest.mark.parametrize(
    ("features", "crs", "message"),
    [
        ([point(1, -77.0)], CRS("EPSG:3857", (), None, "unconfirmed"), "unconfirmed"),
        ([point(1, -77.0)], CRS("urn:x:none", (), "urn:x:none", "content_crs"), "cannot read"),
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


@pytest.mark.parametrize("module", ["pyarrow", "shapely", "pyproj"])
@pytest.mark.parametrize(
    "source", [result_of([point(1, -77.0)]), page_of(collection_of(point(1, -77.0)))]
)
def test_without_the_extra_the_error_names_it(
    monkeypatch: pytest.MonkeyPatch, module: str, source: QueryResult | Page
) -> None:
    monkeypatch.delitem(sys.modules, "water_ogcapi._arrow", raising=False)
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(ModuleNotFoundError, match=r"water-ogcapi\[arrow\]") as info:
        source.to_arrow()
    assert info.value.name == module


def test_another_missing_module_is_raised_as_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "water_ogcapi._arrow", None)
    with pytest.raises(ModuleNotFoundError) as info:
        result_of([point(1, -77.0)]).to_arrow()
    assert info.value.name == "water_ogcapi._arrow"
    assert "water-ogcapi[arrow]" not in str(info.value)


def test_pages_convert_one_by_one_into_the_result_s_table() -> None:
    tables = [page.to_arrow() for page in iterated("nwis/monitoring-locations", 3)]
    expected = collected("nwis/monitoring-locations", 3).to_arrow()
    assert len(tables) == 3
    assert all(table.num_rows < expected.num_rows for table in tables)
    joined = pa.concat_tables(tables, promote_options="permissive")
    assert joined.equals(expected)
    assert joined.schema.equals(expected.schema, check_metadata=True)


def test_pages_whose_columns_differ_join_permissively() -> None:
    first = page_of(collection_of(point(1, -77.0, a=None))).to_arrow()
    second = page_of(collection_of(point(2, -77.1, a="x", b=1))).to_arrow()
    joined = pa.concat_tables([first, second], promote_options="permissive")
    assert joined.column("a").to_pylist() == [None, "x"]
    assert joined.column("b").to_pylist() == [None, 1]


def test_a_page_reads_its_crs_from_its_own_header() -> None:
    feature = {**point(1, 0.0), "geometry": {"type": "Point", "coordinates": [38.9, -77.0]}}
    page = page_of(collection_of(feature), headers={"Content-Crs": f"<{EPSG4326}>"})
    geometry = shapely.from_wkb(page.to_arrow().column("geometry")[0].as_py())
    assert (shapely.get_x(geometry), shapely.get_y(geometry)) == (-77.0, 38.9)


def test_an_item_page_is_one_row() -> None:
    table = page_of(point(7, -77.0, a=1)).to_arrow()
    assert table.column("id").to_pylist() == [7]
    assert table.column("a").to_pylist() == [1]


def test_a_page_without_a_body_decodes_its_content() -> None:
    content = json.dumps(collection_of(point(1, -77.0), point(2, -77.1))).encode()
    assert page_of(None, content).to_arrow().column("id").to_pylist() == [1, 2]


def test_a_page_that_kept_neither_body_nor_content_raises() -> None:
    with pytest.raises(ValueError, match="kept no content"):
        page_of(None).to_arrow()


def test_the_core_import_path_loads_no_frame_library() -> None:
    """D-09: only to_arrow() loads pyarrow, shapely, and pyproj."""
    heavy = ("pyarrow", "shapely", "pyproj", "numpy", "pandas", "geopandas", "polars")
    code = f"import sys, water_ogcapi; print([m for m in {heavy!r} if m in sys.modules])"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"
