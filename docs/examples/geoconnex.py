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
# # Querying GeoConnex
#
# `GeoConnex` queries the reference features at `reference.geoconnex.us`. It takes no key
# and never receives `USGS_API_KEY`. This notebook lists the 22 two-digit hydrologic
# regions of `hu02` without their geometry, streams them a few at a time, and maps the 18
# regions of the contiguous United States with a CQL2 filter. `to_arrow()` turns each
# result into a table for polars or geopandas, which needs the `arrow` extra.

# %%
from water_ogcapi import GeoConnex

gc = GeoConnex()

# %% [markdown]
# A service keeps its HTTP connections open until it is closed. In a script, use it in
# a `with` block, `with GeoConnex() as gc:`, which closes it on exit. A notebook keeps one
# service across cells, so it calls `gc.close()` explicitly in its last cell.

# %% [markdown]
# ## List the regions
#
# Geometry is most of a response. `skipGeometry` asks the server to leave it out, and
# `keep_pages=True` keeps each page's bytes in `Page.content`, so the notebook can report
# their size. `to_arrow()` on features without geometry returns a table of the properties
# and the feature `id`, which polars reads.

# %%
regions = gc.collect("hu02", params={"skipGeometry": "true"}, keep_pages=True)
print(len(regions.geojson["features"]), "regions in", regions.pagination.page_count, "page(s)")
print(regions.pagination.completeness, regions.pagination.checks)
print(sum(len(page.content) for page in regions.pages) / 1e3, "kB")

# %%
import polars as pl

pl.Config.set_tbl_rows(22)
pl.from_arrow(regions.to_arrow()).select("id", "name", "gnis_id").sort("id")

# %% [markdown]
# ## Stream pages
#
# `iter_pages()` yields one `Page` per response and follows each page's `next` link. A
# `limit` of 10 splits the 22 regions into three pages. GeoConnex reports `numberMatched`
# on every page.

# %%
for page in gc.iter_pages("hu02", params={"skipGeometry": "true", "limit": "10"}):
    print(page.status, page.feature_count, page.number_matched)

# %% [markdown]
# ## Filter with CQL2
#
# `queryables()` lists the properties of a collection that a filter can name:

# %%
print(", ".join(gc.queryables("hu02").properties))

# %% [markdown]
# `huc2` is a string, so a CQL2-Text `filter` of `huc2 <= '18'` keeps the 18 regions of the
# contiguous United States and leaves out Alaska, Hawaii, the Caribbean, and the South
# Pacific, which sit far from the rest. With geometry, the response is much larger than
# the list above.

# %%
conus = gc.collect("hu02", filter="huc2 <= '18'", keep_pages=True)
print(len(conus.geojson["features"]), "regions,", end=" ")
print(round(sum(len(page.content) for page in conus.pages) / 1e6), "MB")
print(conus.pagination.completeness, conus.pagination.checks)
print(conus.crs.effective, conus.crs.source)

# %% [markdown]
# ## Map the regions
#
# `to_arrow()` returns a pyarrow Table whose `geometry` column carries the CRS, so
# geopandas reads it as a GeoDataFrame with that CRS. EPSG:5070 is an equal-area
# projection of the contiguous United States, so the polygon areas it gives are true areas.

# %%
import geopandas as gpd
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt

frame = gpd.GeoDataFrame.from_arrow(conus.to_arrow()).to_crs(5070)
frame["area"] = frame.area / 1e9  # thousand km²
ax = frame.plot(
    column="area",
    cmap="Blues",
    edgecolor="white",
    linewidth=0.8,
    figsize=(7, 4.6),
    legend=True,
    legend_kwds={"label": "Area (thousand km²)", "shrink": 0.6},
)
for point, code in zip(frame.representative_point(), frame["huc2"], strict=True):
    ax.annotate(
        code,
        (point.x, point.y),
        ha="center",
        va="center",
        fontsize=8,
        path_effects=[pe.withStroke(linewidth=2, foreground="white")],
    )
ax.set(title="Hydrologic regions of the contiguous United States")
ax.set_axis_off()
plt.tight_layout()

# %%
gc.close()
