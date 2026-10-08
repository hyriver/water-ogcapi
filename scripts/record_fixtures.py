"""Record live responses from NWIS, FabricData, and GeoConnex into ``tests/fixtures/``.

Follow ``.agents/skills/live-api/SKILL.md``. NWIS requests carry the key from
``USGS_API_KEY`` in ``X-Api-Key``; FabricData and GeoConnex requests carry none. Each
request goes through ``Transport(max_retries=0)``, about 40 in all, and the run stops
when NWIS reports fewer than 100 requests remaining. No request header is stored, and
the key's value is replaced in everything written.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from water_ogcapi._transport import Transport

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
NWIS = "https://api.waterdata.usgs.gov/ogcapi/v1"
FABRICDATA = "https://api.water.usgs.gov/fabric/pygeoapi"
GEOCONNEX = "https://reference.geoconnex.us"
DC_BBOX = "-77.12,38.80,-76.91,38.99"
SITE = "USGS-01646500"
MIN_REMAINING = 100
# The stored body is decoded JSON, so wire-encoding headers would misdescribe it.
DROPPED_HEADERS = frozenset({"content-encoding", "content-length", "set-cookie"})


def metadata(
    service: str, base: str, collection: str
) -> list[tuple[str, str, dict[str, str], int]]:
    url = f"{base}/collections/{collection}"
    return [
        (f"{service}/{collection}/collection", url, {}, 1),
        (f"{service}/{collection}/queryables", f"{url}/queryables", {}, 1),
        (f"{service}/{collection}/schema", f"{url}/schema", {}, 1),
    ]


# Fixture path, first URL, params, and pages to fetch by following `next` links.
PLAN = [
    ("nwis/landing", f"{NWIS}/", {}, 1),
    ("nwis/collections", f"{NWIS}/collections", {}, 1),
    *metadata("nwis", NWIS, "monitoring-locations"),
    (
        "nwis/monitoring-locations/items",
        f"{NWIS}/collections/monitoring-locations/items",
        {"bbox": DC_BBOX, "limit": "3"},
        3,
    ),
    (
        f"nwis/monitoring-locations/item-{SITE}",
        f"{NWIS}/collections/monitoring-locations/items/{SITE}",
        {},
        1,
    ),
    *metadata("nwis", NWIS, "daily"),
    (
        "nwis/daily/items",
        f"{NWIS}/collections/daily/items",
        {
            "monitoring_location_id": SITE,
            "parameter_code": "00060",
            "datetime": "2026-09-01/2026-09-30",
            "limit": "3",
        },
        3,
    ),
    ("fabricdata/landing", f"{FABRICDATA}/", {}, 1),
    ("fabricdata/collections", f"{FABRICDATA}/collections", {}, 1),
    *metadata("fabricdata", FABRICDATA, "gagesii"),
    (
        "fabricdata/gagesii/items",
        f"{FABRICDATA}/collections/gagesii/items",
        {"bbox": DC_BBOX, "limit": "5"},
        2,
    ),
    (
        "fabricdata/gagesii/sorted",
        f"{FABRICDATA}/collections/gagesii/items",
        {"bbox": DC_BBOX, "limit": "5", "sortby": "staid"},
        2,
    ),
    ("geoconnex/landing", f"{GEOCONNEX}/", {}, 1),
    ("geoconnex/collections", f"{GEOCONNEX}/collections", {}, 1),
    *metadata("geoconnex", GEOCONNEX, "gages"),
    (
        "geoconnex/gages/sorted",
        f"{GEOCONNEX}/collections/gages/items",
        {"bbox": DC_BBOX, "limit": "5", "sortby": "id"},
        3,
    ),
    (
        "geoconnex/hu02/items",
        f"{GEOCONNEX}/collections/hu02/items",
        {"limit": "10", "skipGeometry": "true"},
        3,
    ),
]


def next_href(body: dict[str, object]) -> str | None:
    links = body.get("links")
    if not isinstance(links, list):
        return None
    return next(
        (link["href"] for link in links if isinstance(link, dict) and link.get("rel") == "next"),
        None,
    )


async def main() -> None:
    key = os.environ.get("USGS_API_KEY", "")
    if not key:
        sys.exit("export USGS_API_KEY first")
    recorded = datetime.now(UTC).date().isoformat()
    async with Transport(max_retries=0, timeout=60) as transport:
        for name, first, params, pages in PLAN:
            url, query = first, {"f": "json", **params}
            for number in range(1, pages + 1):
                # Decided per page: a next link on another host or scheme gets no key.
                headers = {"X-Api-Key": key} if url.startswith(f"{NWIS}/") else None
                resp = await transport.get(url, params=query, headers=headers)
                body = resp.json()
                envelope = {
                    "recorded": recorded,
                    "request": {"url": resp.url, "params": query},
                    "response": {
                        "status": resp.status,
                        "headers": {
                            k: v for k, v in resp.headers.items() if k not in DROPPED_HEADERS
                        },
                        "body": body,
                    },
                }
                text = json.dumps(envelope, indent=2, ensure_ascii=False) + "\n"
                if key in text:
                    text = text.replace(key, "REDACTED")
                path = ROOT / (f"{name}-{number}.json" if pages > 1 else f"{name}.json")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                remaining = resp.headers.get("x-ratelimit-remaining")
                if headers and remaining is not None and int(remaining) < MIN_REMAINING:
                    sys.exit("stopping: under MIN_REMAINING requests left")
                href = next_href(body)
                if href is None:
                    break
                url, query = href, {}


if __name__ == "__main__":
    asyncio.run(main())
