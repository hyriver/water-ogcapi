"""Write ``docs/roadmap.md`` from the GitHub milestones and their issues.

``pixi r docs`` runs this before the build, so the build needs network access and fails
without it. ``GITHUB_TOKEN``, when set, lifts the anonymous limit of 60 requests an hour.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

REPO = "hyriver/water-ogcapi"
API = f"https://api.github.com/repos/{REPO}"


def fetch(url: str) -> list[dict[str, Any]]:
    """Return every item of a paginated GitHub list endpoint."""
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        request = urllib.request.Request(next_url, headers=headers)  # noqa: S310
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            items.extend(json.load(response))
            link = re.search(r'<([^>]+)>; rel="next"', response.headers.get("Link", ""))
        next_url = link.group(1) if link else None
    return items


# Python-Markdown's backslash-escapable characters. Code spans stay as written, since the
# parser shows them verbatim and HTML-escapes them itself.
_ESCAPABLE = re.compile(r"([\\`*_{}\[\]()>#+\-.!])")
_CODE_SPAN = re.compile(r"(`+).+?(?<!`)\1(?!`)")


def _md(text: str) -> str:
    """Return ``text`` as literal Markdown, keeping its code spans as GitHub shows them."""
    parts: list[str] = []
    pos = 0
    for span in [*_CODE_SPAN.finditer(text), None]:
        end = span.start() if span else len(text)
        parts.append(_ESCAPABLE.sub(r"\\\1", html.escape(text[pos:end], quote=False)))
        if span:
            parts.append(span.group())
            pos = span.end()
    return "".join(parts)


def _item(issue: dict[str, Any]) -> str:
    box = "x" if issue["state"] == "closed" else " "
    return f"- [{box}] [{_md(issue['title'])}]({issue['html_url']})"


def render(milestones: list[dict[str, Any]], issues: list[dict[str, Any]], day: dt.date) -> str:
    """Return the roadmap page: one section per milestone, open issues first."""
    lines = [
        "# Roadmap",
        "",
        (
            f"Progress on each [GitHub milestone](https://github.com/{REPO}/milestones), "
            f"generated from GitHub when the site was built on {day:%Y-%m-%d}."
        ),
    ]
    # The issues endpoint also returns pull requests.
    issues = sorted((i for i in issues if "pull_request" not in i), key=lambda i: i["number"])
    for milestone in sorted(milestones, key=lambda m: m["number"]):
        mine = [i for i in issues if i["milestone"]["number"] == milestone["number"]]
        done = [i for i in mine if i["state"] == "closed"]
        lines += ["", f"## {_md(milestone['title'])}", ""]
        if not mine:
            lines.append("No issues yet.")
            continue
        lines += [
            f'<progress value="{len(done)}" max="{len(mine)}" style="width: 100%"></progress>',
            "",
            f"{len(done)} of {len(mine)} done ({100 * len(done) // len(mine)}%)",
            "",
            *(_item(i) for i in mine if i["state"] == "open"),
        ]
        if done:
            lines += [
                "",
                '<details markdown="1">',
                f"<summary>{len(done)} done</summary>",
                "",
                *(_item(i) for i in done),
                "",
                "</details>",
            ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    page = render(
        fetch(f"{API}/milestones?state=all&per_page=100"),
        fetch(f"{API}/issues?milestone=*&state=all&per_page=100"),
        dt.datetime.now(dt.UTC).date(),
    )
    Path("docs/roadmap.md").write_text(page, encoding="utf-8")
