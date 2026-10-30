"""Tests for the result types."""

from __future__ import annotations

import copy
import dataclasses
import json
import pickle
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlencode

import pytest

from water_ogcapi import CRS, Link, Page, Pagination, QueryResult
from water_ogcapi._results import CRS84, REDACTED, crs_of, redact_url
from water_ogcapi.exceptions import ValidationError

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
        geojson={"type": "FeatureCollection", "features": []},
        pages=pages,
        pagination=pagination,
        crs=CRS(None, (), CRS84, "default"),
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
        # Returned as given: a rebuild would lowercase the scheme and drop the bare "?".
        ("HTTPS://example.com/items?", "HTTPS://example.com/items?"),
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


class LyingURL(str):
    """A URL that ``urlsplit()`` reads without its query, userinfo, or fragment."""

    __slots__ = ()

    def lstrip(self, *_: object) -> str:
        return URL


def test_a_url_that_is_not_a_str_redacts_to_redacted() -> None:
    assert (
        redact_url(LyingURL(f"https://u:{SECRET}@example.com/items?api_key={SECRET}")) == REDACTED
    )


def test_a_page_refuses_a_url_that_is_not_a_str() -> None:
    with pytest.raises(ValidationError, match=r"URL must be str, got LyingURL$") as excinfo:
        make_page(url=LyingURL(f"{URL}?api_key={SECRET}"))
    assert SECRET not in str(excinfo.value)


class LooksLikeDate(str):
    """A header name that a case-folded check reads as ``date``."""

    __slots__ = ()

    def lower(self) -> str:
        return "date"


class Shifting(Mapping[Any, str]):
    """A mapping that names ``f`` on its first read and ``bad`` on every later one."""

    def __init__(self, bad: object) -> None:
        self.bad = bad
        self.reads = 0

    def __getitem__(self, key: object) -> str:
        return "json" if key == "f" else SECRET

    def __iter__(self) -> Iterator[Any]:
        self.reads += 1
        return iter(["f"] if self.reads == 1 else [self.bad])

    def __len__(self) -> int:
        return 1


@pytest.mark.parametrize("field", ["params", "headers"])
def test_a_page_refuses_a_name_that_is_not_a_str(field: str) -> None:
    with pytest.raises(ValidationError, match=r"names must be str, got bytes$") as excinfo:
        make_page(**{field: {b"api_key": SECRET}})
    assert SECRET not in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "bad"), [("params", b"api_key"), ("headers", LooksLikeDate("X-Api-Key"))]
)
def test_a_page_stores_the_names_it_checked(field: str, bad: object) -> None:
    page = make_page(**{field: Shifting(bad)})
    assert SECRET.encode() not in pickle.dumps(page)
    assert SECRET not in repr(getattr(page, field))


@pytest.mark.parametrize(
    "clone", [copy.copy, copy.deepcopy, lambda p: pickle.loads(pickle.dumps(p))]
)
def test_page_copies_stay_read_only(clone: Any) -> None:
    page = clone(make_page(params={"f": "json"}, links=[Link(href=URL)]))
    assert dict(page.params) == {"f": "json"}
    assert type(page.links) is tuple
    changes = [
        lambda: page.params.__setitem__("f", "html"),
        lambda: page.params.update(f="html"),
        lambda: page.params.pop("f"),
        page.params.clear,
        lambda: page.headers.setdefault("etag", "x"),
        lambda: page.headers.__ior__({"etag": "x"}),
    ]
    for change in changes:
        with pytest.raises(TypeError, match="read-only"):
            change()
    assert dict(page.params) == {"f": "json"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.url = "https://example.com"


def test_asdict_and_json_work() -> None:
    page = make_page(params={"api_key": SECRET, "f": "json"}, links=[Link(href=URL, rel="self")])
    record = dataclasses.asdict(make_result(page))
    assert record["pages"][0]["params"] == {"api_key": REDACTED, "f": "json"}
    assert SECRET not in json.dumps(record)


def test_rejects_raw_links_and_pages() -> None:
    with pytest.raises(TypeError, match="links must hold only Link"):
        make_page(links=[{"href": f"{URL}?api_key={SECRET}"}])
    with pytest.raises(TypeError, match="pages must hold only Page"):
        make_result(f"{URL}?api_key={SECRET}")  # pyright: ignore[reportArgumentType]


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
        crs=crs_of([page]),
    )
    assert "PAYLOAD" not in repr(page)
    assert "PAYLOAD" not in repr(result)
    assert result.pages == (page,)


@pytest.mark.parametrize(
    ("completeness", "stop_reason", "valid"),
    [
        ("complete", None, True),
        ("unknown", None, True),
        ("limited", "failure", True),
        ("limited", None, False),
        ("complete", "failure", False),
        ("unknown", "failure", False),
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


UTM = "http://www.opengis.net/def/crs/EPSG/0/26918"


@pytest.mark.parametrize(
    ("params", "headers", "expected"),
    [
        ({}, [{}], (None, CRS84, "default")),
        # A server that applies crs names it in Content-Crs.
        ({"crs": UTM}, [{}], (UTM, None, "unconfirmed")),
        ({"crs": CRS84}, [{}], (CRS84, CRS84, "default")),
        ({}, [{"Content-Crs": f"<{CRS84}>"}] * 2, (None, CRS84, "content_crs")),
        # The server's header outranks the request.
        ({"crs": UTM}, [{"Content-Crs": f"<{CRS84}>"}], (UTM, CRS84, "content_crs")),
        ({}, [{"Content-Crs": f"<{CRS84}>"}, {}], (None, None, "unknown")),
        ({}, [{"Content-Crs": f"<{CRS84}>"}, {"Content-Crs": f"<{UTM}>"}], (None, None, "unknown")),
        # A repeated header arrives comma-joined.
        ({}, [{"Content-Crs": f"<{CRS84}>, <{CRS84}>"}], (None, CRS84, "content_crs")),
        ({}, [{"Content-Crs": f"<{CRS84}>, <{UTM}>"}], (None, None, "unknown")),
    ],
)
def test_crs_provenance(
    params: dict[str, str], headers: list[dict[str, str]], expected: tuple[Any, ...]
) -> None:
    url = f"{URL}?{urlencode({'f': 'json', **params})}"
    pages = [make_page(url=url, params=params, headers=h) for h in headers]
    crs = crs_of(pages)
    assert (crs.requested, crs.effective, crs.source) == expected
    assert len(crs.page_crs) == len(headers)


def test_crs_reads_the_request_as_sent() -> None:
    """A traversal resumed from a next link has no params; its URL holds the query."""
    resumed = make_page(url=f"{URL}?offset=2&crs={UTM}", params={})
    assert crs_of([resumed]).requested == UTM


def test_crs_of_no_pages_is_the_default() -> None:
    assert crs_of([]) == CRS(None, (), CRS84, "default")


def test_crs_is_unknown_when_a_next_link_drops_or_changes_the_requested_crs() -> None:
    first = make_page(url=f"{URL}?f=json&crs={UTM}", params={"f": "json", "crs": UTM})
    dropped = make_page(url=f"{URL}?offset=2", params={})
    changed = make_page(url=f"{URL}?offset=2&crs={CRS84}", params={})
    same = make_page(url=f"{URL}?offset=2&crs={UTM}", params={})
    assert crs_of([first, dropped]).source == "unknown"
    assert crs_of([first, changed]).effective is None
    kept = crs_of([first, same])
    assert (kept.effective, kept.source) == (None, "unconfirmed")
