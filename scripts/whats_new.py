"""Write ``docs/whats-new.md`` from the published GitHub releases.

``pixi r docs`` runs this before the build, so the build needs network access and fails
without it. ``GITHUB_TOKEN``, when set, lifts the anonymous limit of 60 requests an hour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from roadmap import API, REPO, fetch


def render(releases: list[dict[str, Any]]) -> str:
    """Return the page: one section per release, newest first, holding its notes."""
    lines = [
        "# What's New",
        "",
        f"The notes of each [GitHub release](https://github.com/{REPO}/releases).",
    ]
    published = [r for r in releases if not r["draft"]]
    for release in sorted(published, key=lambda r: r["published_at"], reverse=True):
        body = release["body"].replace("\r\n", "\n").strip()
        # git-cliff heads a final release's notes with its version and date.
        if body.startswith("## "):
            body = body.partition("\n")[2].strip()
        tag = release["tag_name"]
        label = " (pre-release)" if release["prerelease"] else ""
        lines += [
            "",
            f"## [{tag}]({release['html_url']}) - {release['published_at'][:10]}{label}",
            "",
            body,
        ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    page = render(fetch(f"{API}/releases?per_page=100"))
    Path("docs/whats-new.md").write_text(page, encoding="utf-8")
