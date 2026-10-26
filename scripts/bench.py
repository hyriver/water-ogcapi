"""Measure the five D-14 benchmarks offline, against ``httpx2.MockTransport``.

Every items page repeats one NWIS daily-value feature from ``tests/fixtures/`` under
distinct ids, and each page's bytes are built once and served from memory, so the
numbers measure the client. ``docs/performance.md`` records a run.

``--fetch DIR`` saves real NWIS and FabricData pages from the live services, and
``--decode DIR`` compares ``json.loads`` with ``orjson.loads`` on them (Q-09).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from contextlib import aclosing, contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import orjson

from water_ogcapi import NWIS as BlockingNWIS
from water_ogcapi import Governor, Transport
from water_ogcapi._transport import parse_count
from water_ogcapi.aio import NWIS, FabricData, GeoConnex

if TYPE_CHECKING:
    from collections.abc import Iterator

    from water_ogcapi.aio import OGCService

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "nwis"
COLLECTION = "daily"
QUERY = {"monitoring_location_id": "USGS-01646500", "parameter_code": "00060"}
# A default key's counts with ample quota left, at which the governor never waits.
QUOTA = {"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "999"}
MIB = 2**20
# Real pages at each profile's default size: a year of 15-minute discharge at the
# Potomac gage, and the GAGES-II collection.
LIVE = {
    "nwis": (
        NWIS,
        "continuous",
        {
            "monitoring_location_id": "USGS-01646500",
            "parameter_code": "00060",
            "datetime": "2025-01-01/2025-12-31",
        },
    ),
    "fabricdata": (FabricData, "gagesii", {}),
}
LIVE_PAGES = 3
MIN_REMAINING = 100


def _body(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())["response"]["body"]


class Server:
    """Answer metadata from the NWIS fixtures and ``total`` features in pages.

    Parameters
    ----------
    total : int
        Features the query matches.
    fail_at : int, optional
        Offset of the page answered once with a 429 and ``Retry-After: 1``.

    Attributes
    ----------
    requests, throttled, built : int
        Requests answered, 429s sent, and pages built, for checks that a run did what
        its table claims.
    """

    def __init__(self, total: int, *, fail_at: int | None = None) -> None:
        self.total = total
        self.fail_at = fail_at
        self.requests = 0
        self.throttled = 0
        self.built = 0
        self._metadata = {
            "collections": json.dumps(_body("collections.json")).encode(),
            "queryables": json.dumps(_body(f"{COLLECTION}/queryables.json")).encode(),
        }
        self._feature = _body(f"{COLLECTION}/items-1.json")["features"][0]
        self._pages: dict[tuple[int, int, str | None], bytes] = {}

    def page(self, url: httpx2.URL) -> bytes:
        """Return the page ``url`` asks for, building it on first request."""
        offset = int(url.params.get("offset", "0"))
        count = max(0, min(int(url.params["limit"]), self.total - offset))
        more = offset + count < self.total
        href = str(url.copy_set_param("offset", str(offset + count))) if more else None
        key = (offset, count, href)
        if key not in self._pages:
            features = [{**self._feature, "id": str(i)} for i in range(offset, offset + count)]
            next_link = {"rel": "next", "href": href, "type": "application/geo+json"}
            links = [] if href is None else [next_link]
            doc = {
                "type": "FeatureCollection",
                "features": features,
                "numberReturned": count,
                "links": links,
            }
            self._pages[key] = json.dumps(doc).encode()
            self.built += 1
        return self._pages[key]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        self.requests += 1
        path = request.url.path
        if path.endswith("/collections"):
            return httpx2.Response(200, content=self._metadata["collections"], headers=QUOTA)
        if path.endswith("/queryables"):
            return httpx2.Response(200, content=self._metadata["queryables"], headers=QUOTA)
        if self.fail_at is not None and int(request.url.params.get("offset", "0")) == self.fail_at:
            self.fail_at = None
            self.throttled += 1
            headers = {**QUOTA, "x-ratelimit-remaining": "0", "retry-after": "1"}
            return httpx2.Response(429, headers=headers)
        # A copy, as a network read allocates one; a shared object would hide keep_pages.
        content = memoryview(self.page(request.url)).tobytes()
        return httpx2.Response(200, content=content, headers=QUOTA)

    def transport(self) -> Transport:
        """A default `Transport` that sends to this server."""
        return Transport(transport=httpx2.MockTransport(self.handler))


@contextmanager
def unpaced() -> Iterator[None]:
    """Skip the governor's waits, which move when requests start and never how many."""

    async def start_now(_: Governor) -> None:
        return None

    wait = Governor.wait
    Governor.wait = start_now
    try:
        yield
    finally:
        Governor.wait = wait


def _spread(values: list[float], scale: float = 1.0, digits: int = 0) -> str:
    """Median with the range, as ``median (min to max)``."""
    low, mid, high = (
        round(v * scale, digits) for v in (min(values), statistics.median(values), max(values))
    )
    return f"{mid:g} ({low:g} to {high:g})"


def cold_child(start: float, imported: float) -> None:
    """Print, as JSON, seconds to import, from construction to the first feature, and for TLS.

    The mock transport skips the TLS context a real first request builds, so it is timed
    on its own.
    """
    server = Server(3)
    # The only page has no next link, so a page built for any URL serves the query.
    server.page(httpx2.URL("https://example.org/items?limit=3"))
    built = time.perf_counter()
    service = BlockingNWIS(transport=server.transport())
    pages = service.iter_pages(COLLECTION, params=QUERY)
    page = next(iter(pages))
    first = time.perf_counter()
    assert page.body is not None
    assert [f["id"] for f in page.body["features"]] == ["0", "1", "2"]
    assert server.built == 1
    service.close()
    tls_start = time.perf_counter()
    httpx2.create_ssl_context()
    tls = time.perf_counter() - tls_start
    times = {"import": imported - start, "first_feature": first - built, "tls": tls}
    print(json.dumps(times))  # noqa: T201


def cold_start(runs: int) -> list[str]:
    """Time a fresh interpreter from ``import water_ogcapi`` to the first feature."""
    child = (
        "import time; start = time.perf_counter(); import water_ogcapi; "
        "imported = time.perf_counter(); import runpy; "
        f"runpy.run_path({str(Path(__file__))!r})['cold_child'](start, imported)"
    )
    times: dict[str, list[float]] = {"import": [], "first_feature": [], "tls": []}
    for _ in range(runs):
        out = subprocess.run(  # noqa: S603
            [sys.executable, "-c", child], capture_output=True, text=True, check=True
        ).stdout
        for name, seconds in json.loads(out).items():
            times[name].append(seconds)
    return [
        f"| `import water_ogcapi` | {_spread(times['import'], 1000)} ms |",
        f"| Construction to first feature | {_spread(times['first_feature'], 1000)} ms |",
        f"| TLS context, which the mock skips | {_spread(times['tls'], 1000)} ms |",
    ]


def round_trips() -> list[str]:
    """Count the requests behind a one-page result."""

    async def run() -> list[int]:
        server = Server(3)
        counts = []
        async with NWIS(transport=server.transport()) as service:
            for validate in (True, True, False):
                before = server.requests
                result = await service.collect(COLLECTION, params=QUERY, validate=validate)
                assert result.pagination.completeness == "complete"
                assert len(result.geojson["features"]) == server.total
                counts.append(server.requests - before)
        return counts

    first, cached, unchecked = asyncio.run(run())
    return [
        f"| First query, `validate=True` | {first} |",
        f"| Same query again, metadata cached | {cached} |",
        f"| `validate=False` | {unchecked} |",
    ]


def requests_per_100k() -> list[str]:
    """Count the requests that fetch 100,000 features at each profile's page size."""

    async def run(cls: type[OGCService]) -> tuple[int, int]:
        server = Server(100_000)
        async with cls(transport=server.transport()) as service:
            result = await service.collect(COLLECTION, params=QUERY)
        assert len(result.geojson["features"]) == server.total
        assert result.pagination.completeness == "complete"
        return server.requests, result.pagination.page_count

    rows = []
    with unpaced():
        for cls in (NWIS, FabricData, GeoConnex):
            requests, pages = asyncio.run(run(cls))
            limit = cls.profile.default_limit if cls.profile else None
            rows.append(f"| {cls.__name__} | {limit:,} | {pages} | {requests} |")
    return rows


def peak_memory() -> list[str]:
    """Trace the Python heap during `collect` against the number of features."""

    async def collect(server: Server, *, keep_pages: bool) -> tuple[int, int]:
        async with NWIS(transport=server.transport()) as service:
            # The first request builds the HTTP client, which the trace leaves out.
            await service.collections()
            tracemalloc.start()
            result = await service.collect(
                COLLECTION, params=QUERY, validate=False, keep_pages=keep_pages
            )
            held, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        assert len(result.geojson["features"]) == server.total
        return held, peak

    rows = []
    for total in (1_000, 10_000, 100_000):
        server = Server(total)
        # Build the pages first so the trace holds only what the client allocates.
        asyncio.run(collect(server, keep_pages=False))
        built = server.built
        for keep_pages in (False, True):
            held, peak = asyncio.run(collect(server, keep_pages=keep_pages))
            assert server.built == built
            rows.append(
                f"| {total:,} | {keep_pages} | {peak / MIB:.1f} | {held / MIB:.1f} "
                f"| {peak / total / 1024:.2f} |"
            )
    return rows


def recovery(runs: int) -> list[str]:
    """Time a 5,000-feature query whose third page first gets a 429 with ``Retry-After: 1``."""

    async def run(server: Server) -> tuple[float, int]:
        before = server.requests
        async with NWIS(transport=server.transport()) as service:
            start = time.perf_counter()
            result = await service.collect(COLLECTION, params={**QUERY, "limit": "1000"})
            elapsed = time.perf_counter() - start
        features = result.geojson["features"]
        assert result.pagination.completeness == "complete"
        assert len({f["id"] for f in features}) == len(features) == server.total
        return elapsed, server.requests - before

    rows = []
    for label, fail_at in (("No 429", None), ("429 on page 3", 2_000)):
        server = Server(5_000)
        # Build the pages first so the timer holds only the client's work.
        asyncio.run(run(server))
        built = server.built
        results = []
        for _ in range(runs):
            server.fail_at, server.throttled = fail_at, 0
            results.append(asyncio.run(run(server)))
            assert server.throttled == (fail_at is not None)
            assert server.built == built
        requests = {r for _, r in results}
        assert len(requests) == 1
        seconds = [s for s, _ in results]
        rows.append(f"| {label} | {requests.pop()} | {_spread(seconds, 1, 3)} s | complete |")
    return rows


def fetch(folder: Path) -> None:
    """Save three real pages per service and the seconds the client took to return each.

    Pages come through `iter_pages`, so the package's own checks keep the key on NWIS's
    origin (D-34, D-40, D-53). Sends six requests with no retry, and writes no page that
    holds the key (``.agents/skills/live-api/SKILL.md``).
    """

    async def run() -> dict[str, float]:
        seconds = {}
        key = os.environ.get("USGS_API_KEY", "").encode()
        async with Transport(timeout=120, max_retries=0) as transport:
            for name, (cls, collection, params) in LIVE.items():
                pages = cls(transport=transport).iter_pages(
                    collection, params=params, validate=False
                )
                async with aclosing(pages):
                    for number in range(1, LIVE_PAGES + 1):
                        start = time.perf_counter()
                        page = await anext(pages, None)
                        if page is None:
                            break
                        seconds[f"{name}-{number}"] = time.perf_counter() - start
                        assert page.content is not None
                        # A JSON escape can spell the key, so the decoded page is checked too.
                        decoded = json.dumps(page.body).encode()
                        if key and (key in page.content or key in decoded):
                            msg = f"{name} page {number} holds the key; nothing written"
                            raise RuntimeError(msg)
                        (folder / f"{name}-{number}.json").write_bytes(page.content)
                        remaining = parse_count(page.headers.get("x-ratelimit-remaining"))
                        if remaining is not None and remaining < MIN_REMAINING:
                            msg = f"{name} reports {remaining} requests left; stopping"
                            raise RuntimeError(msg)
        return seconds

    folder.mkdir(parents=True, exist_ok=True)
    # A stopped capture leaves no timings, so --decode cannot pair new pages with old ones.
    (folder / "seconds.json").unlink(missing_ok=True)
    (folder / "seconds.json").write_text(json.dumps(asyncio.run(run()), indent=1))


def _rss_growth(module: str, page: Path) -> float:
    """Bytes a fresh process's peak RSS grows by while ``module.loads`` decodes ``page``.

    tracemalloc reported four times this growth for orjson on an NWIS page, so memory
    here is resident memory.
    """
    child = (
        f"import {module}, resource; content = open({str(page)!r}, 'rb').read(); "
        "before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; "
        f"{module}.loads(content); "
        "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before)"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", child], capture_output=True, text=True, check=True
    ).stdout
    # ru_maxrss is in bytes on macOS and in KiB on Linux.
    return int(out) * (1 if sys.platform == "darwin" else 1024)


def decode(folder: Path, runs: int) -> list[str]:
    """Time ``json.loads`` against ``orjson.loads`` on the pages `fetch` saved.

    A page's seconds already include the client's own ``json.loads``, so the share and
    saving are over that time.
    """
    rows = []
    for name, wall in json.loads((folder / "seconds.json").read_text()).items():
        content = (folder / f"{name}.json").read_bytes()
        doc = json.loads(content)
        # Both must read the page alike; orjson rejects the NaN json.loads accepts.
        assert orjson.loads(content) == doc
        median, peak = {}, {}
        for loads in (json.loads, orjson.loads):
            times = []
            for _ in range(runs):
                start = time.perf_counter()
                decoded = loads(content)
                times.append(time.perf_counter() - start)
                # Freed after the timer stops, as the client keeps what it decodes.
                del decoded
            median[loads] = statistics.median(times)
            peak[loads] = _rss_growth(loads.__module__, folder / f"{name}.json")
        rows.append(
            f"| {name} | {len(content) / MIB:.1f} | {len(doc['features']):,} | {wall:.2f} "
            f"| {median[json.loads] * 1000:.0f} | {median[orjson.loads] * 1000:.0f} "
            f"| {median[json.loads] / wall:.1%} "
            f"| {(median[json.loads] - median[orjson.loads]) / wall:.1%} "
            f"| {peak[json.loads] / MIB:.1f} | {peak[orjson.loads] / MIB:.1f} |"
        )
    return rows


def main() -> None:
    """Print each benchmark as a Markdown table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=20, help="repeats of each timed benchmark")
    parser.add_argument("--fetch", type=Path, help="save live pages here, then stop")
    parser.add_argument("--decode", type=Path, help="compare decoders on the pages saved here")
    args = parser.parse_args()
    if args.fetch:
        fetch(args.fetch)
        return
    if args.decode:
        sections = [
            (
                "Decoding",
                (
                    "| Page | MiB | Features | Page s | `json` ms | `orjson` ms | `json` share "
                    "| `orjson` saves | `json` RSS MiB | `orjson` RSS MiB |"
                ),
                decode(args.decode, args.runs),
            )
        ]
    else:
        sections = [
            ("Cold start", "| Step | Median (range) |", cold_start(args.runs)),
            ("Round trips for a one-page result", "| Query | Requests |", round_trips()),
            (
                "Requests per 100,000 features",
                "| Service | Page size | Pages | Requests |",
                requests_per_100k(),
            ),
            (
                "Peak memory",
                "| Features | `keep_pages` | Peak MiB | Held MiB | Peak KiB per feature |",
                peak_memory(),
            ),
            (
                "Recovery from a 429",
                "| Run | Requests | Median (range) | Result |",
                recovery(max(3, args.runs // 4)),
            ),
        ]
    # An editable install keeps the version from install time, so name the commit.
    commit = subprocess.run(
        ["git", "describe", "--tags", "--always", "--dirty"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).parent,
    ).stdout.strip()
    print(  # noqa: T201
        f"water-ogcapi {commit}, httpx2 {version('httpx2')}, "
        f"Python {platform.python_version()}, {platform.system()} {platform.machine()}, "
        f"{args.runs} runs\n"
    )
    for title, header, rows in sections:
        separator = "|" + "|".join(" --- " for _ in range(header.count("|") - 1)) + "|"
        print(f"## {title}\n\n{header}\n{separator}\n" + "\n".join(rows) + "\n")  # noqa: T201


if __name__ == "__main__":
    main()
