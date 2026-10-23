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
# # Querying NWIS
#
# `NWIS` queries the USGS Water Data API. It sends the key in `USGS_API_KEY`, when set, in
# the `X-Api-Key` header. This notebook asks for the monitoring locations in a box around
# Washington, DC, streams the pages as they arrive, collects a `QueryResult`, narrows the
# query with a CQL2 filter, and fetches daily discharge split by year. `to_arrow()` turns
# each result into a table for geopandas or polars, which needs the `arrow` extra.

# %%
from water_ogcapi import NWIS

DC = (-77.12, 38.80, -76.91, 38.99)
nwis = NWIS()

# %% [markdown]
# A service keeps its HTTP connections open until it is closed. In a script, use it in
# a `with` block, `with NWIS() as nwis:`, which closes it on exit. A notebook keeps one
# service across cells, so it calls `nwis.close()` explicitly in its last cell.

# %% [markdown]
# ## Stream pages
#
# `iter_pages()` yields one `Page` per response and follows each page's `next` link. A
# `limit` of 200 shows the paging; the default is 10,000 features a page. NWIS reports no
# `numberMatched`, so the total is known only once the last page arrives.

# %%
for page in nwis.iter_pages("monitoring-locations", bbox=DC, params={"limit": "200"}):
    print(page.status, page.feature_count, page.number_matched)

# %% [markdown]
# ## Collect a result
#
# `collect()` gathers every page into one FeatureCollection and records how the traversal
# went: the checks that passed and the CRS the server named.

# %%
sites = nwis.collect("monitoring-locations", bbox=DC)
print(len(sites.geojson["features"]), "features in", sites.pagination.page_count, "page(s)")
print(sites.pagination.completeness, sites.pagination.checks)
print(sites.crs.effective, sites.crs.source)

# %% [markdown]
# ## Filter with CQL2
#
# A CQL2-Text `filter` combines with the bounding box. Only the stream sites:

# %%
streams = nwis.collect("monitoring-locations", bbox=DC, filter="site_type_code = 'ST'")
for feature in streams.geojson["features"][:10]:
    print(feature["id"], feature["properties"]["monitoring_location_name"])

# %% [markdown]
# ## Map the sites
#
# `to_arrow()` returns a pyarrow Table whose `geometry` column carries the CRS, so
# geopandas reads it as a GeoDataFrame with that CRS.

# %%
import geopandas as gpd

frame = gpd.GeoDataFrame.from_arrow(sites.to_arrow())
print(frame.crs)
frame["site_type"].value_counts().head()

# %% [markdown]
# contextily draws a basemap under the points. Its tiles are in Web Mercator (EPSG:3857),
# so both layers are reprojected to it first.

# %%
import contextily as ctx

ax = frame.to_crs(3857).plot(color="dimgray", markersize=8, figsize=(6, 6), label="All sites")
gpd.GeoDataFrame.from_arrow(streams.to_arrow()).to_crs(3857).plot(
    ax=ax, color="tab:blue", edgecolor="white", linewidth=0.5, markersize=28, label="Stream sites"
)
ctx.add_basemap(ax, source=ctx.providers.Esri.WorldGrayCanvas)
ax.legend(loc="upper center", bbox_to_anchor=(0.5, 0), ncols=2, frameon=False)
ax.set(title="NWIS monitoring locations around Washington, DC")
ax.set_axis_off()

# %% [markdown]
# ## Daily discharge, split by year
#
# `split_datetime="year"` splits the interval into one piece a year and runs the pieces
# concurrently, as USGS's efficiency guidance recommends. Parameter `00060` is discharge
# and statistic `00003` the daily mean.

# %%
daily = nwis.collect(
    "daily",
    params={
        "monitoring_location_id": "USGS-01646500",
        "parameter_code": "00060",
        "statistic_id": "00003",
        "datetime": "2021-01-01/2025-12-31",
    },
    split_datetime="year",
)
print(len(daily.geojson["features"]), "days in", daily.pagination.page_count, "pages")
print(daily.pagination.completeness, daily.pagination.checks)

# %% [markdown]
# polars reads the same table. The pieces arrive in the order they finish, so sort by
# date before plotting.

# %%
import polars as pl

values = (
    pl.from_arrow(daily.to_arrow())
    .select(pl.col("time").str.to_date(), pl.col("value").cast(pl.Float64))
    .sort("time")
)
values.head()

# %%
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(7, 3.5))
ax.plot(values["time"], values["value"], linewidth=0.8)
ax.set(yscale="log", ylabel="Discharge (ft³/s)", title="Potomac River near Washington, DC")
fig.tight_layout()

# %%
nwis.close()
