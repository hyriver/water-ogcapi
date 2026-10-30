"""Fail when ``pixi.lock`` pins a PyPI sdist outside ``ALLOWED``.

Installing an sdist builds it, which needs a toolchain the user may lack: orjson's needed
Rust 1.95 on Linux (D-71). The CI runners carry current compilers, so an install there
does not catch it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED = {
    # No Python 3.15 wheel on any platform (D-71).
    "pyyaml",
    # No Python 3.15 wheel for macOS, so `docs` builds it there with a C compiler.
    "watchdog",
}

if __name__ == "__main__":
    lock = sys.argv[1] if len(sys.argv) > 1 else "pixi.lock"
    text = Path(lock).read_text(encoding="utf-8")
    entries = re.findall(r"""^- pypi: ["']?([^"'\s]+)["']?$""", text, re.MULTILINE)
    # An entry without a scheme is a local path: the editable project itself.
    paths = [urlsplit(entry).path for entry in entries if "://" in entry]
    if not paths:
        sys.exit(f"{lock}: no PyPI package matched, so the pattern is out of date")
    # Anything but a wheel builds on install: sdists in any archive format and git sources.
    builds = {path.rsplit("/", 1)[-1] for path in paths if not path.endswith(".whl")}
    names = {build: build.rsplit("-", 1)[0].lower().replace("_", "-") for build in builds}
    if bad := sorted(build for build, name in names.items() if name not in ALLOWED):
        sys.exit(f"{lock} pins source builds, which need a build toolchain: {', '.join(bad)}")
