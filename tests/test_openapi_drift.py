"""Tests for the OpenAPI drift check."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from water_ogcapi import Transport

_spec = importlib.util.spec_from_file_location(
    "openapi_drift", Path(__file__).parents[1] / "scripts" / "openapi_drift.py"
)
assert _spec is not None
assert _spec.loader is not None
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)

LIMIT = {
    "name": "limit",
    "in": "query",
    "required": False,
    "style": "form",
    "schema": {"type": "integer", "maximum": 1000, "description": "words", "title": "words"},
    "description": "how many",
}
CQL2 = {"$ref": "https://example.org/cql2.json"}
# A property named "description" is a name, and an object-valued default is data; both
# survive, while the schema's own description is prose.
CRS = {
    "type": "object",
    "properties": {"description": {"type": "string", "description": "words"}},
    "default": {"description": "data"},
}
DOC: dict[str, Any] = {
    "openapi": "3.0.2",
    "info": {"title": "T", "version": "1.0", "description": "words", "contact": {}},
    "servers": [{"url": "https://example.org", "description": "words"}],
    "tags": [{"name": "server", "description": "words"}],
    "paths": {
        "/collections/a/items": {
            "summary": "words",
            "parameters": [{"name": "crs", "in": "query", "schema": CRS}],
            "servers": [{"url": "https://alt.example.org", "description": "words"}],
            "get": {
                "summary": "words",
                "parameters": [LIMIT, {"$ref": "#/components/parameters/f"}],
                "responses": {"200": {"description": "ok"}, "400": {}},
            },
            "post": {
                "description": "words",
                "requestBody": {
                    "description": "words",
                    "content": {"application/json": {"schema": CQL2, "example": "words"}},
                },
                "responses": {"200": {}},
            },
        },
        "/collections/b/items": {
            "post": {
                "requestBody": {"$ref": "#/components/requestBodies/q"},
                "responses": {"200": {}},
            },
        },
    },
    "components": {
        "parameters": {"f": {"name": "f", "in": "query", "schema": {"type": "string"}}},
        "schemas": {
            "description": {"type": "object", "description": "words"},
            "poly": {"discriminator": {"mapping": {"title": "#/components/schemas/description"}}},
        },
        "examples": {"e": {"summary": "words", "value": {"title": "payload"}}},
    },
    "security": [{"title": []}],
    "x-vendor": {"description": "data"},
}
ITEMS = "/collections/a/items"


def serving(
    doc: dict[str, Any], seen: list[httpx2.Request], *, down: str | None = None
) -> Transport:
    """A transport answering every service's OpenAPI request with `doc`; `down` answers 503."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert any(str(request.url).startswith(url) for url in drift.SERVICES.values())
        if down and str(request.url).startswith(drift.SERVICES[down]):
            return httpx2.Response(503)
        return httpx2.Response(200, json=doc)

    return Transport(transport=httpx2.MockTransport(handler), max_retries=0)


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(drift, "ROOT", tmp_path)
    return tmp_path


def test_update_writes_a_snapshot_without_prose_and_sends_no_key(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USGS_API_KEY", "k")
    seen: list[httpx2.Request] = []
    assert drift.main(["--update"], transport=serving(DOC, seen)) == 0
    assert sorted(p.parent.name for p in root.glob("*/openapi.json")) == sorted(drift.SERVICES)
    assert [(r.url.params["f"], r.headers.get("x-api-key")) for r in seen] == [("json", None)] * 3
    stored = json.loads((root / "nwis" / "openapi.json").read_text(encoding="utf-8"))
    assert stored["url"] == drift.SERVICES["nwis"]
    assert "words" not in json.dumps(stored)
    doc = stored["openapi"]
    assert doc["info"] == {"title": "T", "version": "1.0"}
    assert doc["servers"] == [{"url": "https://example.org"}]
    item = doc["paths"][ITEMS]
    kept = {
        "type": "object",
        "properties": {"description": {"type": "string"}},
        "default": {"description": "data"},
    }
    assert item["parameters"] == [{"name": "crs", "in": "query", "schema": kept}]
    assert item["servers"] == [{"url": "https://alt.example.org"}]
    assert item["get"]["parameters"][0] == {
        "name": "limit",
        "in": "query",
        "required": False,
        "style": "form",
        "schema": {"type": "integer", "maximum": 1000},
    }
    assert item["get"]["responses"] == {"200": {}, "400": {}}
    assert item["post"]["requestBody"] == {"content": {"application/json": {"schema": CQL2}}}
    other = doc["paths"]["/collections/b/items"]["post"]
    assert other["requestBody"] == {"$ref": "#/components/requestBodies/q"}
    assert doc["components"]["schemas"] == {
        "description": {"type": "object"},
        "poly": {"discriminator": {"mapping": {"title": "#/components/schemas/description"}}},
    }
    assert doc["components"]["examples"] == {"e": {"value": {"title": "payload"}}}
    assert doc["security"] == [{"title": []}]
    assert doc["x-vendor"] == {"description": "data"}
    assert drift.reduce_document(doc) == doc


def test_a_wording_change_is_not_drift(capsys: pytest.CaptureFixture[str]) -> None:
    assert drift.main(["--update"], transport=serving(DOC, [])) == 0
    reworded = copy.deepcopy(DOC)
    reworded["info"]["description"] = "other words"
    reworded["paths"][ITEMS]["get"]["summary"] = "other words"
    reworded["paths"][ITEMS]["post"]["requestBody"]["description"] = "other words"
    reworded["paths"][ITEMS]["get"]["parameters"][0]["schema"]["title"] = "other words"
    reworded["components"]["schemas"]["description"]["description"] = "other words"
    assert drift.main([], transport=serving(reworded, [])) == 0
    assert capsys.readouterr().out.count("matches the snapshot, version 1.0") == 3


def _lower_limit(doc: dict[str, Any]) -> None:
    doc["paths"][ITEMS]["get"]["parameters"][0]["schema"]["maximum"] = 500


def _drop_json_body(doc: dict[str, Any]) -> None:
    del doc["paths"][ITEMS]["post"]["requestBody"]["content"]["application/json"]


def _drop_path_parameter(doc: dict[str, Any]) -> None:
    del doc["paths"][ITEMS]["parameters"]


def _point_body_elsewhere(doc: dict[str, Any]) -> None:
    doc["paths"]["/collections/b/items"]["post"]["requestBody"] = {
        "$ref": "#/components/requestBodies/r"
    }


def _deprecate_limit(doc: dict[str, Any]) -> None:
    doc["paths"][ITEMS]["get"]["parameters"][0]["deprecated"] = True


def _change_default_data(doc: dict[str, Any]) -> None:
    doc["paths"][ITEMS]["parameters"][0]["schema"]["default"] = {"description": "other"}


def _remap_discriminator(doc: dict[str, Any]) -> None:
    doc["components"]["schemas"]["poly"]["discriminator"]["mapping"]["title"] = (
        "#/components/schemas/poly"
    )


@pytest.mark.parametrize(
    ("change", "line"),
    [
        (_lower_limit, r'^-\s+"maximum": 1000,$'),
        (_drop_json_body, r'^-\s+"application/json": \{$'),
        (_drop_path_parameter, r'^-\s+"name": "crs",$'),
        (_point_body_elsewhere, r'^-\s+"\$ref": "#/components/requestBodies/q"$'),
        (_deprecate_limit, r'^\+\s+"deprecated": true,$'),
        (_change_default_data, r'^\+\s+"description": "other"$'),
        (_remap_discriminator, r'^\+\s+"title": "#/components/schemas/poly"$'),
    ],
)
def test_a_contract_change_fails_and_names_it(
    change: Callable[[dict[str, Any]], None], line: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert drift.main(["--update"], transport=serving(DOC, [])) == 0
    changed = copy.deepcopy(DOC)
    change(changed)
    assert drift.main([], transport=serving(changed, [])) == 1
    out = capsys.readouterr().out
    assert re.search(line, out, re.MULTILINE)
    assert out.count("--- ") == 3


def test_a_missing_snapshot_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert drift.main([], transport=serving(DOC, [])) == 1
    assert "nwis: no snapshot; run with --update" in capsys.readouterr().out


def test_a_service_that_does_not_answer_fails_without_stopping_the_others(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert drift.main(["--update"], transport=serving(DOC, [], down="fabricdata")) == 1
    assert sorted(p.parent.name for p in root.glob("*/openapi.json")) == ["geoconnex", "nwis"]
    assert drift.main([], transport=serving(DOC, [], down="fabricdata")) == 1
    out = capsys.readouterr().out
    assert out.count("fabricdata: not fetched: ") == 2
    assert out.count("matches the snapshot, version 1.0") == 2


def test_rejects_other_arguments() -> None:
    with pytest.raises(SystemExit, match="usage"):
        drift.main(["--bogus"])
