"""Compare the live OpenAPI documents with the snapshots in ``tests/fixtures`` (D-63).

A snapshot is the whole document minus its prose (descriptions, summaries, titles, and
examples), with ``info`` cut to the title and version. A wording change is therefore not
drift, and every other key is kept, so no list of what to keep needs maintaining. A
``$ref`` stays the reference string and is never resolved. The requests send no key: the
documents are public.

``pixi r -e test315 drift`` exits 1 on any difference, a missing snapshot, or a service
that did not answer, and ``pixi r -e test315 drift --update`` rewrites the snapshots of
the services that answered with today's date.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from water_ogcapi import Transport
from water_ogcapi._services import FABRICDATA_PROFILE, GEOCONNEX_PROFILE, NWIS_PROFILE
from water_ogcapi.exceptions import ServiceError

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
SERVICES = {
    "nwis": NWIS_PROFILE.base_url + "openapi",
    "fabricdata": FABRICDATA_PROFILE.base_url + "openapi",
    "geoconnex": GEOCONNEX_PROFILE.base_url + "openapi",
}
PROSE_KEYS = frozenset({"description", "summary", "title", "example", "examples"})
# Values under these keys, and under any `x-` extension, are data: a prose-named key
# inside them is kept.
LITERAL_KEYS = frozenset({"enum", "default", "const", "value"})
# Maps keyed by user-chosen names, where a key named like prose is a name.
NAME_MAPS = frozenset(
    {
        "$defs",
        "definitions",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "encoding",
        "headers",
        "links",
        "mapping",
        "paths",
        "patternProperties",
        "properties",
        "scopes",
        "variables",
    }
)


def _member(key: str, value: Any, *, names: bool) -> Any:
    if names:
        return _scrub(value)
    if key in LITERAL_KEYS or key.startswith("x-"):
        return value
    if key == "security" and isinstance(value, list):
        # Security requirements are maps keyed by scheme names.
        return [_scrub(v, names=True) for v in value]
    return _scrub(value, names=key in NAME_MAPS)


def _scrub(value: Any, *, names: bool = False) -> Any:
    """Return `value` without its prose keys; `names` marks a map keyed by names."""
    if isinstance(value, dict):
        return {
            k: _member(k, v, names=names) for k, v in value.items() if names or k not in PROSE_KEYS
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def reduce_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the document without its prose, `info` cut to the title and version.

    Reducing a reduced document changes nothing, so a stored snapshot is a fixed point.
    """
    info = doc.get("info", {})
    reduced = _scrub(doc)
    reduced["info"] = {"title": info.get("title"), "version": info.get("version")}
    # Each section of components is a map keyed by names.
    reduced["components"] = {
        section: _scrub(entries, names=True)
        for section, entries in doc.get("components", {}).items()
    }
    return reduced


def render(snapshot: dict[str, Any]) -> str:
    """Serialize a snapshot the way the fixture files store it."""
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def drift(name: str, stored: dict[str, Any] | None, live: dict[str, Any]) -> str:
    """Return a unified diff from the stored snapshot to the live one, empty when equal."""
    if stored is None:
        return f"{name}: no snapshot; run with --update\n"
    lines = difflib.unified_diff(
        render(stored).splitlines(keepends=True),
        render(live).splitlines(keepends=True),
        fromfile=f"{name}/openapi.json",
        tofile=f"{name} live",
        n=2,
    )
    return "".join(lines)


def snapshot_path(name: str) -> Path:
    """Return where a service's snapshot lives."""
    return ROOT / name / "openapi.json"


def load(name: str) -> dict[str, Any] | None:
    """Return a service's stored snapshot, or ``None`` when there is none."""
    path = snapshot_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["openapi"]


async def run(*, update: bool, transport: Transport | None = None) -> int:
    """Fetch every document and compare or rewrite the snapshots; return the exit code."""
    recorded = datetime.now(UTC).date().isoformat()
    failed = 0
    async with transport or Transport(timeout=60) as sender:
        for name, url in SERVICES.items():
            try:
                resp = await sender.get(url, params={"f": "json"})
            except ServiceError as err:
                failed += 1
                sys.stdout.write(f"{name}: not fetched: {err}\n")
                continue
            live = reduce_document(resp.json())
            if update:
                path = snapshot_path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                text = render({"recorded": recorded, "url": url, "openapi": live})
                path.write_text(text, encoding="utf-8")
                continue
            report = drift(name, load(name), live)
            if report:
                failed += 1
            else:
                report = f"{name}: matches the snapshot, version {live['info']['version']}\n"
            sys.stdout.write(report)
    return 1 if failed else 0


def main(argv: list[str] | None = None, transport: Transport | None = None) -> int:
    """Run the check from the command line; `transport` lets a test serve the documents."""
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["--update"]):
        sys.exit(f"usage: {sys.argv[0]} [--update]")
    return asyncio.run(run(update=bool(args), transport=transport))


if __name__ == "__main__":
    sys.exit(main())
