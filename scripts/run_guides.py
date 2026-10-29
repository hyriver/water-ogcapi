"""Run the ``python`` blocks of each guide page in order, as one script per page.

The blocks call the live APIs, so follow ``.agents/skills/live-api/SKILL.md``. A block
whose fence follows a ``<!-- not run -->`` line is shown and never run. Each page runs as
a script file, so a worker process can import the functions a page defines.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

GUIDE = Path(__file__).parents[1] / "docs" / "guide"
FENCE = re.compile(r"^( *)```python[^\n]*\n(.*?)^\1```$", re.MULTILINE | re.DOTALL)
SKIP = "<!-- not run -->"


def blocks(page: Path) -> list[tuple[int, str]]:
    """Return the line and code of each block of ``page`` to run."""
    text = page.read_text()
    matches = list(FENCE.finditer(text))
    # A fence the pattern misses, such as one on a list item's line, would pass unrun.
    if len(matches) != text.count("```python"):
        sys.exit(f"{page}: {text.count('```python')} python fences, {len(matches)} recognized")
    found: list[tuple[int, str]] = []
    for match in matches:
        before = text[: match.start()].rstrip().rsplit("\n", 1)[-1].strip()
        if before != SKIP:
            found.append((text.count("\n", 0, match.start()) + 2, textwrap.dedent(match[2])))
    return found


def main() -> None:
    """Run the pages named on the command line, or every guide page."""
    pages = [Path(arg) for arg in sys.argv[1:]] or sorted(GUIDE.glob("*.md"))
    failed: list[Path] = []
    for page in pages:
        found = blocks(page)
        if not found:
            continue
        code = "".join(f"# {page}:{line}\n{block}\n" for line, block in found)
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / f"{page.stem.replace('-', '_')}.py"
            script.write_text(code)
            run = subprocess.run([sys.executable, str(script)], check=False)  # noqa: S603
        if run.returncode:
            failed.append(page)
        print(f"{'FAILED' if run.returncode else 'ok'}: {page}, {len(found)} blocks")  # noqa: T201
    if failed:
        sys.exit(f"{len(failed)} of {len(pages)} pages failed")


if __name__ == "__main__":
    main()
