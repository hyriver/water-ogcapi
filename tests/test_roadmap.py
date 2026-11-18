"""Tests for the roadmap page generator."""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from typing import Self

import pytest

_spec = importlib.util.spec_from_file_location(
    "roadmap", Path(__file__).parents[1] / "scripts" / "roadmap.py"
)
assert _spec is not None
assert _spec.loader is not None
roadmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(roadmap)


def _issue(number: int, state: str, milestone: int, *, pr: bool = False) -> dict[str, object]:
    issue: dict[str, object] = {
        "number": number,
        "state": state,
        "title": f"Issue {number} <x> [y]",
        "html_url": f"https://example.org/{number}",
        "milestone": {"number": milestone},
    }
    if pr:
        issue["pull_request"] = {}
    return issue


def test_render_counts_issues_and_folds_done_ones() -> None:
    milestones = [{"number": 2, "title": "M3: Later"}, {"number": 1, "title": "M2: First"}]
    issues = [
        _issue(3, "closed", 1),
        _issue(1, "open", 1),
        _issue(2, "open", 1),
        _issue(4, "closed", 1, pr=True),
    ]
    page = roadmap.render(milestones, issues, dt.date(2026, 10, 7))

    assert "built on 2026-10-07" in page
    assert page.index("## M2: First") < page.index("## M3: Later")
    assert '<progress value="1" max="3"' in page
    assert "1 of 3 done (33%)" in page
    assert page.index("example.org/1)") < page.index("example.org/2)") < page.index("<details")
    assert "- [x] [Issue 3 &lt;x&gt; \\[y\\]](https://example.org/3)" in page
    assert "example.org/4" not in page
    assert page.rstrip().endswith("## M3: Later\n\nNo issues yet.")


@pytest.mark.parametrize(
    ("title", "label"),
    [
        ("a\\] b", "a\\\\\\] b"),
        ("`dict[str, Any]`", "`dict[str, Any]`"),
        ("*b* <i> & c", "\\*b\\* &lt;i&gt; &amp; c"),
        ("x` y", "x\\` y"),
    ],
)
def test_item_keeps_titles_literal(title: str, label: str) -> None:
    issue = {"state": "open", "title": title, "html_url": "https://example.org/1"}
    assert roadmap._item(issue) == f"- [ ] [{label}](https://example.org/1)"


class _Reply:
    def __init__(self, link: str) -> None:
        self.headers = {"Link": link}

    def read(self) -> bytes:
        return b"[]"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_fetch_keeps_the_token_off_a_next_link_outside_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []

    class _Opener:
        def open(self, request: object, timeout: float) -> _Reply:  # noqa: ARG002
            sent.append(request.full_url)  # type: ignore[attr-defined]
            return _Reply('<https://evil.example/2>; rel="next"')

    monkeypatch.setenv("GITHUB_TOKEN", "SECRET")
    monkeypatch.setattr(roadmap.urllib.request, "build_opener", lambda *_: _Opener())
    with pytest.raises(ValueError, match="outside the GitHub API"):
        roadmap.fetch(f"{roadmap.API}/milestones")
    assert sent == [f"{roadmap.API}/milestones"]


def test_fetch_follows_no_redirect() -> None:
    assert roadmap._NoRedirect().redirect_request() is None
