# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -all
#     formats: ipynb,py:percent
#     notebook_metadata_filter: kernelspec,jupytext
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.6
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Cache, quota, and resume
#
# Three things happen behind every query: metadata is cached, requests are spaced so the
# service's quota lasts, and a query that fails partway keeps what it fetched. This
# notebook shows each one against WaterData.

# %%
from water_ogcapi import WaterData, configure_logger

wd = WaterData()

# %% [markdown]
# ## A cache hit
#
# At the DEBUG level the package logs every request it sends. The first `collections()`
# call fetches `/collections`; the second is served from the cache, so it logs nothing.
# Entries last an hour; the next call after that fetches the document again.

# %%
configure_logger(level="DEBUG")
first = wd.collections()
second = wd.collections()
print(len(first.collections or ()), "collections, the same both times:", first == second)

# %% [markdown]
# `refresh()` drops the cache, so the next call requests the collections again.

# %%
wd.refresh()
_ = wd.collections()
configure_logger()

# %% [markdown]
# ## The quota governor
#
# Each WaterData response carries the key's quota in `X-RateLimit-Limit` and
# `X-RateLimit-Remaining`, and every `Page` keeps them.

# %%
DC = (-77.12, 38.80, -76.91, 38.99)
pages = wd.iter_pages("monitoring-locations", bbox=DC, params={"limit": "1"})
page = next(pages)
pages.close()
print({k: v for k, v in page.headers.items() if k.startswith("x-ratelimit")})

# %% [markdown]
# The service's `Governor` reads those counts. While more than an eighth of the quota is
# left, requests start back to back; below that, it spaces their starts further apart
# as the quota runs down, up to 60 s. WaterData counts its quota over an hour, so the same
# `Governor` with a one-minute window and a quota of 100 shows the effect in seconds.
# Each step reports a response with fewer requests left, then times the wait before the
# next request may start. `Governor.wait()` is a coroutine, so `Client.run()` runs it on
# the package's event loop, in a script or a notebook.

# %%
import time

from water_ogcapi import Client, Governor


async def time_waits() -> dict[int, float]:
    governor = Governor(window=60.0)
    await governor.wait()  # the first request never waits
    waits: dict[int, float] = {}
    for remaining in (40, 20, 14, 12, 10, 8, 6):
        counts = {"x-ratelimit-limit": "100", "x-ratelimit-remaining": str(remaining)}
        governor.observe(200, counts)
        start = time.monotonic()
        await governor.wait()
        waits[remaining] = time.monotonic() - start
    return waits


with Client() as client:
    waits = client.run(time_waits())
{left: round(wait, 1) for left, wait in waits.items()}

# %%
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(6, 3.5))
ax.plot(list(waits), list(waits.values()), marker="o")
ax.axvline(100 / 8, color="gray", linestyle=":", label="an eighth of the quota")
ax.invert_xaxis()
ax.set(xlabel="Requests left of 100", ylabel="Wait before the next start (s)")
ax.set(title="Governor spacing as the quota runs down")
ax.legend()
fig.tight_layout()

# %% [markdown]
# ## Resume after a failure
#
# A transport that answers one `/items` request with a 503 stands in for an outage. With
# retries off, the query fails partway: `collect()` raises `PartialResultsError`, whose
# `result` holds the pages that arrived and a checkpoint of the requests still to send.

# %%
import httpx2

from water_ogcapi import Transport
from water_ogcapi.exceptions import PartialResultsError


class FailOnce(httpx2.AsyncBaseTransport):
    """Answer the second `/items` request with a 503 and send the rest."""

    def __init__(self) -> None:
        self.network = httpx2.AsyncHTTPTransport(http2=True)
        self.items = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/items"):
            self.items += 1
            if self.items == 2:
                return httpx2.Response(503, request=request)
        return await self.network.handle_async_request(request)


QUERY = {
    "monitoring_location_id": "USGS-01646500",
    "parameter_code": "00060",
    "statistic_id": "00003",
    "datetime": "2021-01-01/2025-12-31",
}
flaky = WaterData(transport=Transport(transport=FailOnce(), max_retries=0))
try:
    flaky.collect("daily", params=QUERY, split_datetime="year")
except PartialResultsError as err:
    partial = err.result
    print(err.failures[0].status, "from", err.failures[0].url)
else:
    msg = "FailOnce lets no query through whole"
    raise AssertionError(msg)
flaky.close()
pending = partial.checkpoint.pending if partial.checkpoint else ()
print(len(partial.geojson["features"]), "days kept from", partial.pagination.page_count, "pages")
print(len(pending), "request(s) left to send")

# %% [markdown]
# `collect(resume=...)` sends only the requests the checkpoint lists, on any `WaterData`
# instance, and returns the whole result.

# %%
whole = wd.collect("daily", resume=partial)
print(len(whole.geojson["features"]), "days in", whole.pagination.page_count, "pages")
print(whole.pagination.completeness, whole.pagination.checks)

# %%
wd.close()
