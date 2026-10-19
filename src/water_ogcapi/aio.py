"""The async service classes, for code that runs its own event loop (D-48).

Each class has the name and constructor of its blocking twin in `water_ogcapi`, and each
method is a coroutine, apart from `iter_pages`, which returns an async iterator. A
`Transport` binds to the event loop of its first request (D-10).

```python
import asyncio

from water_ogcapi.aio import NWIS


async def main() -> None:
    async with NWIS() as nwis:
        result = await nwis.collect("daily", params={"monitoring_location_id": "USGS-01646500"})


asyncio.run(main())
```
"""

from __future__ import annotations

from water_ogcapi._client import OGCService
from water_ogcapi._services import NWIS, FabricData, GeoConnex

__all__ = ["NWIS", "FabricData", "GeoConnex", "OGCService"]
