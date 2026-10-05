"""The package logger and ``configure_logger()`` (docs/design.md D-20).

Examples
--------
>>> from water_ogcapi import configure_logger
>>> configure_logger(level="INFO")  # doctest: +SKIP
>>> configure_logger(file="run.log", file_only=True)  # doctest: +SKIP
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from pathlib import Path
from typing import Literal

__all__ = ["configure_logger", "logger"]

type Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | int

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
# Handlers carrying these names belong to configure_logger(); any other handler on the
# logger was attached by the caller and is left alone.
_CONSOLE = "water_ogcapi.console"
_FILE = "water_ogcapi.file"
_swap = threading.Lock()

logger = logging.getLogger("water_ogcapi")
logger.setLevel(logging.DEBUG)
# Isolated from the root logger: another package's root config neither captures nor
# floods these records, and Jupyter shows each record once.
logger.propagate = False


def _level(value: Level) -> int:
    number = _LEVELS.get(value.upper()) if isinstance(value, str) else value
    if number is None or number not in _LEVELS.values():
        msg = f"invalid log level {value!r}; use one of {', '.join(_LEVELS)}"
        raise ValueError(msg)
    return number


def configure_logger(
    *,
    verbose: bool | None = None,
    level: Level | None = None,
    file: str | Path | None = None,
    file_level: Level = "DEBUG",
    file_mode: Literal["a", "w"] = "a",
    file_only: bool = False,
) -> None:
    """Set where the package logger writes, replacing the previous configuration.

    Each call states the whole configuration: an argument left out returns to its
    default, so ``configure_logger(level="DEBUG")`` after ``configure_logger(file=...)``
    turns file logging off. ``configure_logger()`` restores the import-time setup, a
    console handler on stderr at WARNING.

    Parameters
    ----------
    verbose : bool, optional
        Shortcut for the console level: ``True`` is DEBUG, ``False`` is WARNING.
        ``level`` wins when both are given.
    level : str or int, optional
        Console level, such as ``"INFO"``. Defaults to WARNING.
    file : str or Path, optional
        Also write to this file, encoded as UTF-8. Parent directories are created.
    file_level : str or int, optional
        File level. Defaults to DEBUG.
    file_mode : {'a', 'w'}, optional
        Append to the file or overwrite it. Defaults to ``'a'``.
    file_only : bool, optional
        Write to the file and not the console. Requires ``file``.

    Raises
    ------
    ValueError
        If a level or ``file_mode`` is invalid, or ``file_only`` is set without
        ``file``. The previous configuration stays in place.
    OSError
        If the file cannot be opened. The previous configuration stays in place.
    """
    if level is not None:
        console_level = _level(level)
    else:
        console_level = logging.DEBUG if verbose else logging.WARNING
    file_level_number = _level(file_level)
    if file_mode not in {"a", "w"}:
        msg = f"invalid file_mode {file_mode!r}; use 'a' or 'w'"
        raise ValueError(msg)
    if file_only and file is None:
        msg = "file_only=True requires file"
        raise ValueError(msg)

    # Build every new handler before removing the old ones, so a failure changes nothing.
    handlers: list[logging.Handler] = []
    if not file_only:
        console = logging.StreamHandler(sys.stderr)
        console.set_name(_CONSOLE)
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        handlers.append(console)
    if file is not None:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The default is the locale's encoding (cp1252 on most Windows setups before
        # Python 3.15), and logging drops a record it cannot encode.
        to_file = logging.FileHandler(path, mode=file_mode, encoding="utf-8")
        to_file.set_name(_FILE)
        to_file.setLevel(file_level_number)
        to_file.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%Y/%m/%d %H:%M:%S"
            )
        )
        handlers.append(to_file)

    with _swap:
        for old in [h for h in logger.handlers if h.get_name() in {_CONSOLE, _FILE}]:
            logger.removeHandler(old)
            # A retired file that fails its last flush must not block the new setup.
            with contextlib.suppress(OSError):
                old.close()
        for new in handlers:
            logger.addHandler(new)


configure_logger()
