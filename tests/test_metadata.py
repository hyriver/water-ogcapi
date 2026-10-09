"""Tests for the collection metadata types."""

from __future__ import annotations

import copy
import json
import pickle
from pathlib import Path
from typing import Any

import pytest

from water_ogcapi import (
    Collection,
    Collections,
    Extent,
    Link,
    Property,
    Schema,
    SpatialExtent,
    TemporalExtent,
)

FIXTURES = Path(__file__).parent / "fixtures"
CASES = [
    *((path, Collections) for path in sorted(FIXTURES.glob("*/collections.json"))),
    *((path, Collection) for path in sorted(FIXTURES.glob("*/*/collection.json"))),
    *((path, Schema) for path in sorted(FIXTURES.glob("*/*/queryables.json"))),
    *((path, Schema) for path in sorted(FIXTURES.glob("*/*/schema.json"))),
]
COLLECTION = {
    "id": "gages",
    "title": "Gages",
    "links": [{"href": "https://example.com/c", "rel": "self", "hreflang": "en-US"}],
    "extent": {
        "spatial": {"bbox": [[-180, -90, 180, 90], [-10.5, 1, 0, 2, -5, 5]], "crs": "CRS84"},
        "temporal": {"interval": [[None, "2020-01-01T00:00:00Z"], ["2021-01-01", None]]},
    },
    "crs": ["CRS84", "EPSG:4326"],
    "storageCrs": "CRS84",
}


def body(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())["response"]["body"]


def known_members_in_extra(obj: Any) -> list[str]:
    """Members a field names that ended up in ``extra``, anywhere under ``obj``."""
    if isinstance(obj, tuple | list):
        return [name for item in obj for name in known_members_in_extra(item)]
    if isinstance(obj, dict):
        return [name for item in obj.values() for name in known_members_in_extra(item)]
    if not hasattr(obj, "_MEMBERS"):
        return []
    found = [name for name in obj._MEMBERS if name in obj.extra]
    for name, _ in obj._MEMBERS.values():
        found += known_members_in_extra(getattr(obj, name))
    return found


@pytest.mark.parametrize(("path", "kind"), CASES, ids=lambda case: str(case)[-40:])
def test_fixture_round_trip_drops_nothing(path: Path, kind: type[Any]) -> None:
    doc = body(path)
    parsed = kind.from_dict(doc)
    assert parsed.to_dict() == doc
    # A round trip alone would pass with every member in extra.
    assert known_members_in_extra(parsed) == []


def test_fixtures_cover_every_type() -> None:
    assert {kind for _, kind in CASES} == {Collections, Collection, Schema}
    assert len(CASES) == 15


def test_collection_fields() -> None:
    collection = Collection.from_dict(COLLECTION)
    assert collection.to_dict() == COLLECTION
    assert collection.crs == ("CRS84", "EPSG:4326")
    assert collection.storage_crs == "CRS84"
    assert collection.links == (Link("https://example.com/c", rel="self", hreflang="en-US"),)
    assert collection.extent == Extent(
        spatial=SpatialExtent(bbox=((-180, -90, 180, 90), (-10.5, 1, 0, 2, -5, 5)), crs="CRS84"),
        temporal=TemporalExtent(interval=((None, "2020-01-01T00:00:00Z"), ("2021-01-01", None))),
    )
    assert collection.title == "Gages"
    assert collection.description is None


def test_nwis_extra_members_stay() -> None:
    collections = Collections.from_dict(body(FIXTURES / "nwis" / "collections.json"))
    assert collections.collections is not None
    extras = {c.id: set(c.extra) for c in collections.collections if c.extra}
    assert extras == {"edr/daily": {"data_queries", "parameter_names"}}


def test_schema_property_role() -> None:
    schema = Schema.from_dict(body(FIXTURES / "nwis" / "daily" / "queryables.json"))
    assert schema.properties is not None
    roles = {name: p.role for name, p in schema.properties.items() if p.role}
    assert roles == {"geometry": "primary-geometry", "id": "id", "time": "primary-instant"}
    assert set(schema.extra) == {"$id", "$schema", "type"}


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("crs", "EPSG:4326"),
        ("crs", ["CRS84", 4326]),
        ("storageCrs", None),
        ("keywords", None),
        ("extent", []),
        ("links", [{"rel": "self"}]),
        ("links", [{"href": 1}]),
        ("title", 5),
    ],
)
def test_malformed_member_stays_in_extra(member: str, value: Any) -> None:
    doc = COLLECTION | {member: value}
    collection = Collection.from_dict(doc)
    assert collection.extra[member] == value
    assert collection.to_dict() == doc


@pytest.mark.parametrize(
    ("kind", "doc"),
    [
        (SpatialExtent, {"bbox": [[0, 0, 1]]}),
        (SpatialExtent, {"bbox": [[0, 0, 1, True]]}),
        (SpatialExtent, {"bbox": [0, 0, 1, 1]}),
        (SpatialExtent, {"crs": None}),
        (TemporalExtent, {"interval": [[None]]}),
        (TemporalExtent, {"interval": [["2020", 2021]]}),
        (TemporalExtent, {"interval": "2020/2021"}),
        (TemporalExtent, {"trs": 1}),
    ],
)
def test_malformed_extent_member_stays_in_extra(kind: type[Any], doc: dict[str, Any]) -> None:
    parsed = kind.from_dict(doc)
    (member,) = doc
    assert getattr(parsed, member) is None
    assert parsed.extra == doc
    assert parsed.to_dict() == doc


@pytest.mark.parametrize("doc", [{}, {"id": 7}, {"id": None}])
def test_collection_needs_a_string_id(doc: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="'id'"):
        Collection.from_dict(doc)


@pytest.mark.parametrize("entry", [{"title": "no id"}, "gages"])
def test_collections_rejects_an_entry_without_id(entry: object) -> None:
    with pytest.raises(ValueError, match=r"'id'|must be an object"):
        Collections.from_dict({"collections": [COLLECTION, entry]})


def test_link_needs_a_string_href() -> None:
    with pytest.raises(ValueError, match="'href'"):
        Link.from_dict({"rel": "self"})


def test_link_href_is_redacted_on_the_way_out() -> None:
    doc = {"href": "https://example.com/c?api_key=SECRET", "rel": "self", "templated": False}
    link = Link.from_dict(doc)
    assert link.to_dict() == {
        "href": "https://example.com/c?api_key=REDACTED",
        "rel": "self",
        "templated": False,
    }


def test_parsed_objects_are_read_only_and_detached() -> None:
    doc = copy.deepcopy(COLLECTION) | {"x": {"nested": [1]}}
    collection = Collection.from_dict(doc)
    doc["x"]["nested"].append(2)
    assert collection.extra["x"] == {"nested": [1]}
    with pytest.raises(TypeError, match="read-only"):
        collection.extra["y"] = 1  # type: ignore[index]
    out = collection.to_dict()
    out["x"]["nested"].append(3)
    assert collection.extra["x"] == {"nested": [1]}


def test_pickle_and_hash() -> None:
    schema = Schema.from_dict(body(FIXTURES / "geoconnex" / "gages" / "schema.json"))
    collection = Collection.from_dict(COLLECTION | {"x": 1})
    for obj in (schema, collection):
        clone = pickle.loads(pickle.dumps(obj))
        assert clone == obj
        assert hash(clone) == hash(obj)
    assert isinstance(next(iter(schema.properties.values())), Property)  # type: ignore[union-attr]
