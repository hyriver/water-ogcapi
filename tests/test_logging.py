"""Tests for the package logger (docs/design.md D-20)."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from water_ogcapi import configure_logger
from water_ogcapi._logging import logger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _restore_default() -> Iterator[None]:
    yield
    configure_logger()


def own_handlers() -> dict[str, logging.Handler]:
    """The handlers configure_logger() manages, by name."""
    named = {h.get_name() or "": h for h in logger.handlers}
    return {name: h for name, h in named.items() if name.startswith("water_ogcapi.")}


def test_import_setup_is_isolated() -> None:
    """In a fresh interpreter, records reach stderr at WARNING and never the root logger."""
    code = (
        "import logging, water_ogcapi; log = logging.getLogger('water_ogcapi'); "
        "print([(h.get_name(), logging.getLevelName(h.level)) for h in log.handlers], "
        "log.propagate)"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "[('water_ogcapi.console', 'WARNING')] False"


def test_each_call_replaces_the_configuration(tmp_path: Path) -> None:
    configure_logger(level="INFO", file=tmp_path / "run.log")
    first = own_handlers()
    assert {name: h.level for name, h in first.items()} == {
        "water_ogcapi.console": logging.INFO,
        "water_ogcapi.file": logging.DEBUG,
    }

    configure_logger(verbose=True)
    assert {name: h.level for name, h in own_handlers().items()} == {
        "water_ogcapi.console": logging.DEBUG
    }
    old_file = first["water_ogcapi.file"]
    assert isinstance(old_file, logging.FileHandler)
    assert old_file.stream is None, "the replaced file handler is closed"


def test_file_only_writes_to_the_file_alone(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "run.log"
    configure_logger(file=path, file_only=True, file_mode="w")
    assert list(own_handlers()) == ["water_ogcapi.file"]
    logger.debug("page fetched")
    configure_logger()
    assert "DEBUG    page fetched" in path.read_text(encoding="utf-8")


def test_failed_close_still_applies_the_new_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logger(file=tmp_path / "run.log")
    old = own_handlers()["water_ogcapi.file"]
    assert isinstance(old, logging.FileHandler)

    def full_disk() -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(old, "flush", full_disk)
    configure_logger(level="INFO")
    assert {name: h.level for name, h in own_handlers().items()} == {
        "water_ogcapi.console": logging.INFO
    }
    assert old.stream is None


def test_caller_handlers_are_left_alone() -> None:
    mine = logging.NullHandler()
    logger.addHandler(mine)
    try:
        configure_logger(level="ERROR")
        assert mine in logger.handlers
    finally:
        logger.removeHandler(mine)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"level": "LOUD"}, ValueError),
        ({"level": True}, ValueError),
        ({"file_level": 5}, ValueError),
        ({"file_mode": "x"}, ValueError),
        ({"file_only": True}, ValueError),
        ({"file": "DIRECTORY"}, OSError),
    ],
)
def test_rejected_call_keeps_the_configuration(
    tmp_path: Path, kwargs: dict[str, Any], error: type[Exception]
) -> None:
    configure_logger(level="INFO", file=tmp_path / "run.log")
    before = list(logger.handlers)
    if kwargs.get("file") == "DIRECTORY":
        kwargs["file"] = tmp_path
    with pytest.raises(error):
        configure_logger(**kwargs)
    assert logger.handlers == before


def test_file_is_utf8_whatever_the_locale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ASCII or cp1252 locale would drop a record holding a non-Latin-1 name."""

    def ascii_locale(encoding: str | None, *_: object) -> str:
        return "ascii" if encoding is None else encoding

    # FileHandler takes its default encoding from io.text_encoding(), which answers
    # "utf-8" in UTF-8 mode and "locale" otherwise.
    monkeypatch.setattr(io, "text_encoding", ascii_locale)
    path = tmp_path / "run.log"
    configure_logger(file=path, file_only=True)
    logger.warning("WAIĀKEA STREAM")
    configure_logger()
    assert "WAIĀKEA STREAM" in path.read_text(encoding="utf-8")
