"""Tests for NWIS partitioned fan-out (D-35, D-56), against a mock that filters as NWIS does."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx2
import pytest

import water_ogcapi
from water_ogcapi import Page, QueryResult, Transport
from water_ogcapi._query import Checkpoint, Request
from water_ogcapi.aio import NWIS, GeoConnex
from water_ogcapi.exceptions import PaginationError, PartialResultsError, ValidationError

ITEMS = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/daily/items"
SITES = ["USGS-01", "USGS-02", "USGS-03", "USGS-04", "USGS-05"]
TIMES = [
    "2023-12-31",
    "2024-01-01",
    "2024-02-29",
    "2024-03-01",
    "2024-12-31T00:00:00+00:00",
    "2024-12-31T23:59:00+00:00",
    "2025-01-01T00:00:00+00:00",
    "2025-06-30",
    "2025-07-01T00:00:00+00:00",
]
ROWS = [(site, time) for site in SITES for time in TIMES]

type Hook = Callable[[httpx2.Request, list[tuple[str, str]]], Awaitable[httpx2.Response | None]]


def instant(text: str) -> datetime:
    """Read a time or bound as NWIS does: a date is its midnight in UTC."""
    if "T" not in text:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), UTC)
    return datetime.fromisoformat(text).astimezone(UTC)


def matching(request: httpx2.Request) -> list[tuple[str, str]]:
    """The rows a first request selects: its sites and its closed, inclusive interval."""
    params = request.url.params
    sites = SITES
    if "monitoring_location_id" in params:
        sites = params["monitoring_location_id"].split(",")
    elif request.content:
        sites = json.loads(request.content)["args"][1]
    rows = [row for row in ROWS if row[0] in sites]
    if "datetime" in params:
        low, high = map(instant, params["datetime"].split("/"))
        rows = [row for row in rows if low <= instant(row[1]) <= high]
    return rows


def mock(
    hook: Hook | None = None, *, max_concurrent: int = 4
) -> tuple[Transport, list[httpx2.Request]]:
    """A transport paging ``matching`` rows, ``limit`` a page, by a cursor holding the offset.

    Like NWIS, a ``next`` link keeps the request's params and drops a POST body, so the
    client must re-send it. ``hook`` may answer a request in place of the mock.
    """
    seen: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        rows = matching(request)
        if hook is not None and (reply := await hook(request, rows)) is not None:
            return reply
        start = int(request.url.params.get("cursor", "0"))
        size = int(request.url.params["limit"])
        body: dict[str, Any] = {
            "type": "FeatureCollection",
            "numberReturned": len(rows[start : start + size]),
            "features": [
                {
                    "type": "Feature",
                    "id": f"{site}/{time}",
                    "geometry": None,
                    "properties": {"monitoring_location_id": site, "time": time},
                }
                for site, time in rows[start : start + size]
            ],
            "links": [],
        }
        if start + size < len(rows):
            href = httpx2.URL(ITEMS, params={**request.url.params, "cursor": str(start + size)})
            body["links"] = [{"rel": "next", "type": "application/geo+json", "href": str(href)}]
        return httpx2.Response(200, json=body)

    transport = Transport(
        transport=httpx2.MockTransport(handler), max_retries=0, max_concurrent=max_concurrent
    )
    return transport, seen


def nwis(hook: Hook | None = None, *, max_concurrent: int = 4) -> tuple[NWIS, list[httpx2.Request]]:
    transport, seen = mock(hook, max_concurrent=max_concurrent)
    return NWIS(api_key="", transport=transport), seen


def collect(service: NWIS, **query: Any) -> QueryResult | PartialResultsError:
    async def run() -> QueryResult:
        async with service:
            return await service.collect("daily", validate=False, **query)

    try:
        return asyncio.run(run())
    except PartialResultsError as err:
        return err


def ids(result: QueryResult) -> list[str]:
    return [feature["id"] for feature in result.geojson["features"]]


def first_params(seen: list[httpx2.Request]) -> list[dict[str, str]]:
    return [dict(r.url.params) for r in seen if "cursor" not in r.url.params]


WIDE = {"datetime": "2023-06-15/2025-07-01", "limit": "2"}


@pytest.mark.parametrize("period", ["year", "month"])
def test_a_period_split_returns_the_unsplit_features_once(period: str) -> None:
    whole = collect(nwis()[0], params=WIDE)
    split = collect(nwis()[0], params=WIDE, split_datetime=period)
    assert isinstance(whole, QueryResult)
    assert isinstance(split, QueryResult)
    assert len(ids(split)) == len(set(ids(split)))
    assert sorted(ids(split)) == sorted(ids(whole))
    assert len(ids(whole)) == len(ROWS), "the interval holds every row, both ends included"
    assert split.pagination.completeness == "complete"


def test_year_pieces_end_one_microsecond_before_the_next_begins() -> None:
    service, seen = nwis()
    collect(
        service, params={"datetime": "2023-06-15/2025-02-01T12:00:00+02:00"}, split_datetime="year"
    )
    assert sorted(p["datetime"] for p in first_params(seen)) == [
        "2023-06-15T00:00:00Z/2023-12-31T23:59:59.999999Z",
        "2024-01-01T00:00:00Z/2024-12-31T23:59:59.999999Z",
        "2025-01-01T00:00:00Z/2025-02-01T10:00:00Z",
    ]


def test_a_value_in_the_last_microsecond_of_a_period_is_fetched_once() -> None:
    """NWIS compares bounds to the microsecond, so disjoint pieces leave no gap (D-56)."""
    late = ("USGS-01", "2024-12-31T23:59:59.999999+00:00")
    ROWS.append(late)
    try:
        split = collect(nwis()[0], params=WIDE, split_datetime="year")
        whole = collect(nwis()[0], params=WIDE)
    finally:
        ROWS.remove(late)
    assert isinstance(split, QueryResult)
    assert isinstance(whole, QueryResult)
    assert "/".join(late) in ids(whole)
    assert sorted(ids(split)) == sorted(ids(whole))


def test_an_interval_within_one_period_is_sent_whole() -> None:
    service, seen = nwis()
    collect(service, params={"datetime": "2024-03-01/2024-03-31"}, split_datetime="year")
    assert [p["datetime"] for p in first_params(seen)] == ["2024-03-01/2024-03-31"]


@pytest.mark.parametrize("form", ["params", "ids"])
def test_a_site_split_returns_the_unsplit_features_once(form: str) -> None:
    listed = [*SITES, "USGS-02"]
    query: dict[str, Any] = (
        {"params": {"monitoring_location_id": ",".join(listed), "limit": "4"}}
        if form == "params"
        else {"ids": listed, "id_property": "monitoring_location_id", "params": {"limit": "4"}}
    )
    whole = collect(nwis()[0], **query)
    service, seen = nwis()
    split = collect(service, **query, sites_per_piece=2)
    assert isinstance(whole, QueryResult)
    assert isinstance(split, QueryResult)
    assert sorted(ids(split)) == sorted(ids(whole))
    assert len(ids(split)) == len(set(ids(split))) == len(ROWS)
    if form == "params":
        pieces = sorted(p["monitoring_location_id"] for p in first_params(seen))
        assert pieces == ["USGS-01,USGS-02", "USGS-03,USGS-04", "USGS-05"]
    else:
        assert all(r.method == "POST" and r.content for r in seen), "every next link re-posts"


def test_pieces_run_concurrently_up_to_max_concurrent() -> None:
    flying = 0
    peak = 0

    async def hold(_: httpx2.Request, __: list[tuple[str, str]]) -> None:
        nonlocal flying, peak
        flying += 1
        peak = max(peak, flying)
        await asyncio.sleep(0.01)
        flying -= 1

    service, seen = nwis(hold, max_concurrent=2)
    result = collect(service, params={"monitoring_location_id": ",".join(SITES)}, sites_per_piece=1)
    assert isinstance(result, QueryResult)
    assert len(first_params(seen)) == len(SITES)
    assert peak == 2


def test_a_failed_middle_piece_keeps_the_others_and_resumes_alone() -> None:
    """L-02: the pages of the other pieces reach the caller, and the failure names its URL."""

    async def fail_second(
        request: httpx2.Request, _: list[tuple[str, str]]
    ) -> httpx2.Response | None:
        if request.url.params.get("monitoring_location_id") == "USGS-02":
            return httpx2.Response(503)
        return None

    sites = {"monitoring_location_id": "USGS-01,USGS-02,USGS-03"}
    err = collect(nwis(fail_second)[0], params=sites, sites_per_piece=1)
    assert isinstance(err, PartialResultsError)
    assert {i.partition("/")[0] for i in ids(err.result)} == {"USGS-01", "USGS-03"}
    assert [f.status for f in err.failures] == [503]
    assert "monitoring_location_id=USGS-02" in err.failures[0].url
    assert err.result.pagination.completeness == "limited"
    assert err.checkpoint is not None
    assert [dict(r.params)["monitoring_location_id"] for r in err.checkpoint.pending] == ["USGS-02"]
    service, seen = nwis()
    result = collect(service, resume=err.result)
    assert isinstance(result, QueryResult)
    assert [p["monitoring_location_id"] for p in first_params(seen)] == ["USGS-02"]
    whole = collect(nwis()[0], params=sites)
    assert isinstance(whole, QueryResult)
    assert sorted(ids(result)) == sorted(ids(whole))


async def pages(service: NWIS, **query: Any) -> list[Page] | PaginationError:
    async with service:
        try:
            return [page async for page in service.iter_pages("daily", validate=False, **query)]
        except PaginationError as err:
            return err


def test_after_a_failure_no_new_piece_starts() -> None:
    async def fail(_: httpx2.Request, __: list[tuple[str, str]]) -> httpx2.Response:
        return httpx2.Response(503)

    service, seen = nwis(fail, max_concurrent=1)
    sites = {"monitoring_location_id": "USGS-01,USGS-02,USGS-03"}
    err = asyncio.run(pages(service, params=sites, sites_per_piece=1))
    assert isinstance(err, PaginationError)
    assert len(seen) == 1
    assert err.checkpoint is not None
    assert [dict(r.params)["monitoring_location_id"] for r in err.checkpoint.pending] == [
        "USGS-01",
        "USGS-02",
        "USGS-03",
    ]


def test_every_checkpoint_resumes_to_the_whole_result() -> None:
    """Each page's checkpoint lists every chain in flight, so no resume loses a feature."""
    query = {
        "params": {"monitoring_location_id": "USGS-01,USGS-02", "limit": "3"},
        "sites_per_piece": 1,
    }
    got = asyncio.run(pages(nwis()[0], **query))
    assert isinstance(got, list)
    every = sorted(f["id"] for page in got for f in page.body["features"])  # type: ignore[index]
    assert len(every) == len(set(every)) == 2 * len(TIMES)
    for n, page in enumerate(got[:-1]):
        assert page.checkpoint is not None
        before = [f["id"] for p in got[: n + 1] for f in p.body["features"]]  # type: ignore[index]
        rest = asyncio.run(pages(nwis()[0], resume=page.checkpoint))
        assert isinstance(rest, list)
        after = [f["id"] for p in rest for f in p.body["features"]]  # type: ignore[index]
        assert sorted(before + after) == every
    assert got[-1].checkpoint is None


def test_closing_the_iterator_cancels_the_pieces_in_flight() -> None:
    cancelled = asyncio.Event()

    async def stall(request: httpx2.Request, _: list[tuple[str, str]]) -> None:
        if request.url.params.get("monitoring_location_id") == "USGS-02":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def run() -> None:
        service, _ = nwis(stall)
        async with service:
            items = service.iter_pages(
                "daily",
                params={"monitoring_location_id": "USGS-01,USGS-02"},
                sites_per_piece=1,
                validate=False,
            )
            await anext(items)
            await items.aclose()  # type: ignore[attr-defined]
            assert cancelled.is_set(), "aclose() returns once the piece in flight is cancelled"

    asyncio.run(run())


def test_a_request_already_in_flight_is_not_sent_twice() -> None:
    request = Request(
        url=ITEMS, params={"f": "json", "monitoring_location_id": "USGS-01", "limit": "50"}
    )
    start = Checkpoint(base_url=nwis()[0].base_url, collection="daily", pending=(request, request))
    service, seen = nwis()
    err = asyncio.run(pages(service, resume=start))
    assert isinstance(err, PaginationError)
    assert len(seen) == 1
    assert "repeats" in str(err.failures[0])


def test_the_blocking_class_splits_too() -> None:
    transport, seen = mock()
    with water_ogcapi.NWIS(api_key="", transport=transport) as blocking:
        result = blocking.collect("daily", params=WIDE, split_datetime="year", validate=False)
    assert len(ids(result)) == len(ROWS)
    assert len(first_params(seen)) == 3


@pytest.mark.parametrize(
    ("query", "match"),
    [
        ({"split_datetime": "week", "params": WIDE}, "takes 'year' or 'month'"),
        ({"split_datetime": "year"}, "datetime interval in params"),
        ({"split_datetime": "year", "params": {"datetime": "2020-01-01/.."}}, "both ends"),
        ({"split_datetime": "year", "params": {"datetime": "2020-01-01"}}, "both ends"),
        (
            {"split_datetime": "year", "params": {"datetime": "2021-01-01/2020-01-01"}},
            "ends before",
        ),
        (
            {"split_datetime": "year", "params": {"datetime": "2020-01-01T00:00/2021-01-01"}},
            "offset",
        ),
        ({"sites_per_piece": 0, "params": WIDE}, "positive integer"),
        ({"sites_per_piece": True, "params": WIDE}, "positive integer"),
        ({"sites_per_piece": 2.5, "params": WIDE}, "positive integer"),
        ({"sites_per_piece": 2, "params": WIDE}, "takes a site list"),
        ({"sites_per_piece": 2, "split_datetime": "year", "params": WIDE}, "not both"),
    ],
)
def test_a_malformed_split_sends_nothing(query: dict[str, Any], match: str) -> None:
    service, seen = nwis()
    with pytest.raises(ValidationError, match=match):
        asyncio.run(pages(service, **query))
    assert seen == []


@pytest.mark.parametrize("query", [{"split_datetime": "year"}, {"sites_per_piece": 2}])
def test_only_nwis_splits(query: dict[str, Any]) -> None:
    transport = Transport(transport=httpx2.MockTransport(lambda _: httpx2.Response(500)))

    async def run() -> None:
        async with GeoConnex(transport=transport) as service:
            await service.collect("hu02", validate=False, **query)

    with pytest.raises(ValidationError, match="only NWIS"):
        asyncio.run(run())


def test_a_resume_takes_no_split() -> None:
    request = Request(url=ITEMS, params={"f": "json"})
    start = Checkpoint(base_url=nwis()[0].base_url, collection="daily", pending=(request,))
    with pytest.raises(ValidationError, match="drop split_datetime"):
        asyncio.run(pages(nwis()[0], resume=start, split_datetime="year"))


def test_a_failure_while_the_caller_holds_a_page_stops_new_pieces() -> None:
    """A piece that fails between steps still keeps the next piece from starting."""
    held = asyncio.Event()

    async def fail_late(
        request: httpx2.Request, _: list[tuple[str, str]]
    ) -> httpx2.Response | None:
        if request.url.params.get("monitoring_location_id") == "USGS-02":
            await held.wait()
            return httpx2.Response(503)
        return None

    async def run() -> PaginationError | None:
        service, seen = nwis(fail_late, max_concurrent=2)
        sites = {"monitoring_location_id": "USGS-01,USGS-02,USGS-03"}
        async with service:
            items = service.iter_pages("daily", params=sites, sites_per_piece=1, validate=False)
            await anext(items)
            held.set()
            await asyncio.sleep(0.05)  # USGS-02 fails while the caller holds the page
            try:
                async for _ in items:
                    pass
            except PaginationError as err:
                assert [p.get("monitoring_location_id") for p in first_params(seen)] == [
                    "USGS-01",
                    "USGS-02",
                ]
                return err
        return None

    err = asyncio.run(run())
    assert err is not None
    assert err.checkpoint is not None
    assert [dict(r.params)["monitoring_location_id"] for r in err.checkpoint.pending] == [
        "USGS-02",
        "USGS-03",
    ]


def test_pages_come_in_the_order_their_responses_arrived() -> None:
    """Responses that arrive while the caller holds a page keep their arrival order."""
    gates = {"USGS-02": asyncio.Event(), "USGS-03": asyncio.Event()}
    finished: list[str] = []

    async def ordered(request: httpx2.Request, _: list[tuple[str, str]]) -> None:
        site = request.url.params.get("monitoring_location_id", "")
        if site in gates:
            await gates[site].wait()
        finished.append(site)

    async def run() -> list[str]:
        service, _ = nwis(ordered)
        sites = {"monitoring_location_id": "USGS-01,USGS-02,USGS-03"}
        async with service:
            items = service.iter_pages("daily", params=sites, sites_per_piece=1, validate=False)
            first = await anext(items)
            gates["USGS-03"].set()
            await asyncio.sleep(0.05)
            gates["USGS-02"].set()
            await asyncio.sleep(0.05)
            rest = [page async for page in items]
        return [p.params["monitoring_location_id"] for p in (first, *rest)]

    assert asyncio.run(run()) == ["USGS-01", "USGS-03", "USGS-02"]
    assert finished == ["USGS-01", "USGS-03", "USGS-02"]


def test_the_blocking_iterator_yields_every_piece() -> None:
    transport, seen = mock()
    sites = {"monitoring_location_id": ",".join(SITES)}
    with water_ogcapi.NWIS(api_key="", transport=transport) as blocking:
        got = list(blocking.iter_pages("daily", params=sites, sites_per_piece=2, validate=False))
    assert sorted(p.params["monitoring_location_id"] for p in got) == [
        "USGS-01,USGS-02",
        "USGS-03,USGS-04",
        "USGS-05",
    ]
    assert len(seen) == 3


def test_a_malformed_split_fails_before_the_query_check() -> None:
    """A split is checked before validate fetches metadata, so nothing is sent (D-56)."""
    service, seen = nwis()

    async def run() -> None:
        async with service:
            await service.collect("daily", params=WIDE, split_datetime="week")  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="takes 'year' or 'month'"):
        asyncio.run(run())
    assert seen == []


def test_chains_in_flight_finish_and_every_failure_is_named() -> None:
    """After a failure the other chains follow their next links to the end (D-56, L-02)."""

    async def two_failures(
        request: httpx2.Request, _: list[tuple[str, str]]
    ) -> httpx2.Response | None:
        site = request.url.params.get("monitoring_location_id")
        cursor = request.url.params.get("cursor")
        if (site, cursor) in {("USGS-02", None), ("USGS-03", "4")}:
            return httpx2.Response(503 if cursor is None else 500)
        return None

    sites = {"monitoring_location_id": "USGS-01,USGS-02,USGS-03", "limit": "2"}
    err = collect(nwis(two_failures)[0], params=sites, sites_per_piece=1)
    assert isinstance(err, PartialResultsError)
    assert sorted(f.status for f in err.failures) == [500, 503]
    got = Counter(i.partition("/")[0] for i in ids(err.result))
    assert got == {"USGS-01": len(TIMES), "USGS-03": 4}
    assert err.checkpoint is not None
    pending = {
        (dict(r.params).get("monitoring_location_id"), r.url) for r in err.checkpoint.pending
    }
    assert len(pending) == 2
    result = collect(nwis()[0], resume=err.result)
    whole = collect(nwis()[0], params=sites)
    assert isinstance(result, QueryResult)
    assert isinstance(whole, QueryResult)
    assert sorted(ids(result)) == sorted(ids(whole))


def test_refused_requests_alone_leave_no_checkpoint() -> None:
    """Every refusal is settled, so a loop resuming until no checkpoint is left ends."""
    good = Request(
        url=ITEMS, params={"f": "json", "monitoring_location_id": "USGS-01", "limit": "50"}
    )
    away = [Request(url=f"https://elsewhere.test/items/{n}") for n in (1, 2)]
    start = Checkpoint(base_url=nwis()[0].base_url, collection="daily", pending=(good, *away))
    service, seen = nwis()
    err = asyncio.run(pages(service, resume=start))
    assert isinstance(err, PaginationError)
    assert len(seen) == 1
    assert [str(f).startswith("refused a URL outside") for f in err.failures] == [True, True]
    assert err.checkpoint is None


def test_a_failure_done_before_its_callback_ran_stops_new_pieces() -> None:
    """The stop rule reads task state: a resume can come before a done callback runs."""
    held = asyncio.Event()
    failing: list[asyncio.Task[Any]] = []

    async def fail_on_release(
        request: httpx2.Request, _: list[tuple[str, str]]
    ) -> httpx2.Response | None:
        if request.url.params.get("monitoring_location_id") == "USGS-02":
            task = asyncio.current_task()
            assert task is not None
            failing.append(task)
            await held.wait()
            return httpx2.Response(503)
        return None

    async def run() -> list[str | None]:
        service, seen = nwis(fail_on_release, max_concurrent=2)
        sites = {"monitoring_location_id": "USGS-01,USGS-02,USGS-03"}
        async with service:
            items = service.iter_pages("daily", params=sites, sites_per_piece=1, validate=False)
            await anext(items)
            held.set()
            # Polled: an event set by a done callback would resume after the window.
            while not failing[0].done():  # noqa: ASYNC110
                await asyncio.sleep(0)
            with pytest.raises(PaginationError):
                async for _ in items:
                    pass
        return [p.get("monitoring_location_id") for p in first_params(seen)]

    assert asyncio.run(run()) == ["USGS-01", "USGS-02"]
