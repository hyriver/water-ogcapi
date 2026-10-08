"""Tests for the result types."""

from __future__ import annotations

import copy
import dataclasses
import pickle
from types import MappingProxyType
from typing import Any

import pytest

from water_ogcapi import Link, Page, Pagination, QueryResult
from water_ogcapi._results import REDACTED, redact_url

SECRET = "SECRET"
URL = "https://example.com/collections/c/items"


def make_page(**overrides: Any) -> Page:
    fields: dict[str, Any] = {
        "url": f"{URL}?f=json",
        "params": {"f": "json"},
        "status": 200,
        "headers": {"Content-Type": "application/geo+json"},
        "links": (),
        "fetched_at": 1.0,
        "number_matched": None,
        "number_returned": 2,
        "feature_count": 2,
    }
    return Page(**(fields | overrides))


def make_result(*pages: Page) -> QueryResult:
    pagination = Pagination(
        number_matched=None, number_returned=2, page_count=1, completeness="complete"
    )
    return QueryResult(
        geojson={"type": "FeatureCollection", "features": []}, pages=pages, pagination=pagination
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{URL}?f=json", f"{URL}?f=json"),
        (f"{URL}?api_key={SECRET}&f=json", f"{URL}?api_key={REDACTED}&f=json"),
        (f"{URL}?f=json;API_KEY={SECRET}", f"{URL}?f=json;API_KEY={REDACTED}"),
        (f"{URL}?%61pi_key={SECRET}", f"{URL}?%61pi_key={REDACTED}"),
        (f"{URL}?api\t_key={SECRET}", f"{URL}?api_key={REDACTED}"),
        (f"{URL}?cursor=%FF%2B&api_key={SECRET}", f"{URL}?cursor=%FF%2B&api_key={REDACTED}"),
        (f"{URL}?api_key_hint=1", f"{URL}?api_key_hint=1"),
        (f"https://user:{SECRET}@example.com/c", "https://example.com/c"),
        (f"{URL}#api_key={SECRET}", URL),
        ("https://[::1/c", REDACTED),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected


def test_no_credential_in_repr_pickle_or_any_field() -> None:
    page = make_page(
        url=f"https://u:{SECRET}@example.com/items?cursor=x;api_key={SECRET}#api_key={SECRET}",
        params={"api_key": SECRET, "API_KEY": SECRET, "f": "json"},
        headers={
            "X-Api-Key": SECRET,
            "Authorization": SECRET,
            "Set-Cookie": SECRET,
            "Content-Crs": "<http://www.opengis.net/def/crs/OGC/1.3/CRS84>",
            "X-RateLimit-Remaining": "998",
        },
        links=[Link(href=f"{URL}?cursor=abc&api_key={SECRET}", rel="next")],
    )
    result = make_result(page)
    for obj in (page, result):
        restored = pickle.loads(pickle.dumps(obj))
        assert restored == obj
        assert SECRET not in repr(obj)
        assert SECRET.encode() not in pickle.dumps(obj)
        for f in dataclasses.fields(obj):
            assert SECRET not in repr(getattr(obj, f.name)), f.name
            assert SECRET not in repr(getattr(restored, f.name)), f.name
    assert dict(page.headers) == {
        "content-crs": "<http://www.opengis.net/def/crs/OGC/1.3/CRS84>",
        "x-ratelimit-remaining": "998",
    }
    assert page.links[0].href == f"{URL}?cursor=abc&api_key={REDACTED}"


@pytest.mark.parametrize(
    "clone", [copy.copy, copy.deepcopy, lambda p: pickle.loads(pickle.dumps(p))]
)
def test_page_copies_stay_read_only(clone: Any) -> None:
    page = clone(make_page(params={"f": "json"}))
    assert type(page.params) is MappingProxyType
    assert type(page.headers) is MappingProxyType
    with pytest.raises(TypeError):
        page.params["f"] = "html"
    with pytest.raises(TypeError):
        page.headers["etag"] = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.url = "https://example.com"


def test_json_decodes_a_new_object_each_call() -> None:
    page = make_page(content=b'{"type": "FeatureCollection", "features": []}')
    first, second = page.json(), page.json()
    assert first == second == {"type": "FeatureCollection", "features": []}
    assert first is not second
    with pytest.raises(ValueError, match="kept no content"):
        make_page().json()


def test_repr_leaves_out_payloads() -> None:
    page = make_page(body={"features": ["PAYLOAD"]}, content=b"PAYLOAD")
    result = QueryResult(
        geojson={"features": ["PAYLOAD"]},
        pages=[page],
        pagination=Pagination(
            number_matched=1, number_returned=1, page_count=1, completeness="complete"
        ),
    )
    assert "PAYLOAD" not in repr(page)
    assert "PAYLOAD" not in repr(result)
    assert result.pages == (page,)


@pytest.mark.parametrize(
    ("completeness", "stop_reason", "valid"),
    [
        ("complete", None, True),
        ("unknown", None, True),
        ("limited", "limit", True),
        ("limited", "failure", True),
        ("limited", None, False),
        ("complete", "failure", False),
        ("unknown", "limit", False),
    ],
)
def test_stop_reason_set_exactly_when_limited(
    completeness: Any, stop_reason: Any, valid: bool
) -> None:
    def build() -> Pagination:
        return Pagination(
            number_matched=None,
            number_returned=0,
            page_count=0,
            completeness=completeness,
            stop_reason=stop_reason,
        )

    if valid:
        assert build().stop_reason == stop_reason
    else:
        with pytest.raises(ValueError, match="stop_reason"):
            build()
