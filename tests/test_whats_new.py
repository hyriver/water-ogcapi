"""Tests for the What's New page generator."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_scripts = Path(__file__).parents[1] / "scripts"
# whats_new imports fetch from its sibling roadmap.py.
sys.path.insert(0, str(_scripts))
_spec = importlib.util.spec_from_file_location("whats_new", _scripts / "whats_new.py")
assert _spec is not None
assert _spec.loader is not None
whats_new = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(whats_new)


def _release(tag: str, day: str, body: str, **flags: bool) -> dict[str, object]:
    return {
        "tag_name": tag,
        "published_at": f"{day}T12:00:00Z",
        "html_url": f"https://example.org/{tag}",
        "body": body,
        "draft": flags.get("draft", False),
        "prerelease": flags.get("prerelease", False),
    }


def test_render_orders_releases_and_replaces_cliff_heading() -> None:
    releases = [
        _release("v0.1.0rc1", "2026-10-09", "Reserves the name.", prerelease=True),
        _release(
            "v0.2.0", "2026-11-14", "## [0.2.0] - 2026-11-14\r\n\r\nSummary.\r\n\r\n### Added"
        ),
        _release("v0.3.0", "2026-11-20", "Unfinished.", draft=True),
    ]
    page = whats_new.render(releases)

    assert page.index("## [v0.2.0](https://example.org/v0.2.0) - 2026-11-14\n\nSummary.") < (
        page.index("## [v0.1.0rc1](https://example.org/v0.1.0rc1) - 2026-10-09 (pre-release)")
    )
    assert "[0.2.0]" not in page
    assert "\r" not in page
    assert "v0.3.0" not in page
    assert page.endswith("Reserves the name.\n")
