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
# # Data files from the STAC API
#
# `WaterDataSTAC` queries the USGS Water Data STAC API, which lists data files: each item
# describes one file, and its `assets` link to that file. It sends the key in
# `USGS_STAC_API_KEY`, or in `USGS_API_KEY` when that is unset. This notebook lists the
# collections, finds every file of the Potomac River gage, maps the sites of a basin,
# then downloads the gage's daily values and plots its discharge. `to_arrow()` turns a
# result into a table for geopandas, which needs the `arrow` extra.

# %%
from water_ogcapi import WaterDataSTAC

SITE = "USGS-01646500"
stac = WaterDataSTAC()

# %% [markdown]
# A service keeps its HTTP connections open until it is closed. In a script, use it in
# a `with` block, `with WaterDataSTAC() as stac:`, which closes it on exit. A notebook
# keeps one service across cells, so it calls `stac.close()` explicitly in its last cell.

# %% [markdown]
# ## Look at the collections
#
# Each collection is one kind of file. `queryables()` lists the properties a query can
# name; the items carry site metadata, not the parameters or periods inside the files.

# %%
for collection in stac.collections().collections or ():
    print(f"{collection.id:12} {collection.title}")

# %%
print(", ".join(stac.queryables("daily").properties or {}))

# %% [markdown]
# ## Find one gage's files
#
# `ids` with `id_property` matches items by site. A site list longer than 50 runs as
# concurrent pieces, since the service refuses a long query URL. Each item's `assets`
# name its files: `continuous` splits the last 150 days, `current`, from the older
# `historical` values.

# %%
for collection in ("continuous", "daily", "ratings"):
    result = stac.collect(collection, ids=[SITE], id_property="monitoring_location_id")
    for item in result.geojson["features"]:
        print(f"{collection:11} {item['id']:24} {', '.join(item['assets'])}")

# %% [markdown]
# ## Map the sites of a basin
#
# The items carry each site's hydrologic unit code, so a CQL2-Text `LIKE` selects a
# basin by its prefix: here `02070008`, the Middle Potomac-Catoctin.

# %%
import geopandas as gpd

basin = stac.collect("daily", filter="hydrologic_unit_code LIKE '02070008%'")
sites = gpd.GeoDataFrame.from_arrow(basin.to_arrow())
print(len(sites), "sites with daily values,", basin.pagination.completeness)
sites[["id", "monitoring_location_name", "site_type", "drainage_area"]].head()

# %%
import contextily as ctx
import matplotlib.pyplot as plt
from xyzservices import providers

points = sites.to_crs(3857)
gage = points[points["id"] == SITE]
ax = points.plot(color="dimgray", markersize=25, figsize=(6, 6), label="Sites with daily values")
gage.plot(ax=ax, color="tab:blue", edgecolor="white", markersize=120, label="Potomac River gage")
ctx.add_basemap(ax, source=providers.Esri.WorldGrayCanvas)
ax.legend(loc="upper center", bbox_to_anchor=(0.5, 0), ncols=2, frameon=False)
ax.set(title="Daily-value files in HUC 02070008")
ax.set_axis_off()
plt.tight_layout()

# %% [markdown]
# ## Download and read the daily values
#
# The asset files need no key. `Client.download()` fetches one to disk, creating its
# folder. A download that carries a key counts against that key's hourly limit. The file
# is Parquet, with each value stored as text.

# %%
import tempfile
from pathlib import Path

import polars as pl

from water_ogcapi import Client

item = stac.item("daily", SITE).body or {}
with Client() as client:
    path = Path(tempfile.mkdtemp()) / f"{SITE}.parquet"
    download = client.download(item["assets"]["data"]["href"], path)
print(f"{download.size:,} bytes")

daily = pl.read_parquet(path).with_columns(pl.col("value").cast(pl.Float64))
daily.group_by("parameter_code", "statistic_id").len().sort("len", descending=True)

# %% [markdown]
# Parameter `00060` with statistic `00003` is the daily mean discharge, in ft³/s.

# %%
flow = daily.filter(
    pl.col("parameter_code") == "00060",
    pl.col("statistic_id") == "00003",
    pl.col("time").dt.year() >= 2016,
).sort("time")
fig, ax = plt.subplots(figsize=(8, 3.5))
ax.plot(flow["time"], flow["value"], linewidth=0.7)
ax.set(yscale="log", ylabel="Discharge (ft³/s)", title="Potomac River near Washington, DC")
plt.tight_layout()

# %% [markdown]
# The two `continuous` files can be read the same way. If both hold a row for the same
# `time_series_id` and `time`, keep the one with the latest `last_modified`.

# %%
stac.close()
