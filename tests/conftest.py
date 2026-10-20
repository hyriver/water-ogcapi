"""Shared test setup."""

from __future__ import annotations

import pytest

from water_ogcapi import Governor


@pytest.fixture(autouse=True)
def unpaced(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the governor's waits offline, apart from tests marked ``paced``."""
    if request.node.get_closest_marker("network") or request.node.get_closest_marker("paced"):
        return

    async def start_now(_: Governor) -> None:
        return None

    monkeypatch.setattr(Governor, "wait", start_now)
