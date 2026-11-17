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
# `GeoConnex` queries the reference features at `reference.geoconnex.us`, and
# `GeoConnexFeatures` every feature registered in GeoConnex at `features.geoconnex.us`.
# Neither takes a key or receives `USGS_API_KEY`. This notebook lists the 22 two-digit
# hydrologic regions of `hu02` without their geometry, streams them a few at a time, and
# maps the 18 regions of the contiguous United States with a CQL2 filter. It then finds
# every feature along the Charles River, from all contributors, and fetches the full
# attributes of its stream gages from the reference collection. `to_arrow()` turns each
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
print(sum(len(page.content or b"") for page in regions.pages) / 1e3, "kB")

# %%
import polars as pl

pl.Config.set_tbl_rows(22)
pl.DataFrame(regions.to_arrow()).select("id", "name", "gnis_id").sort("id")

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
print(", ".join(gc.queryables("hu02").properties or {}))

# %% [markdown]
# `huc2` is a string, so a CQL2-Text `filter` of `huc2 <= '18'` keeps the 18 regions of the
# contiguous United States and leaves out Alaska, Hawaii, the Caribbean, and the South
# Pacific, which sit far from the rest. With geometry, the response is much larger than
# the list above.

# %%
conus = gc.collect("hu02", filter="huc2 <= '18'", keep_pages=True)
print(len(conus.geojson["features"]), "regions,", end=" ")
print(round(sum(len(page.content or b"") for page in conus.pages) / 1e6), "MB")
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

# %% [markdown]
# ## Find features from every contributor
#
# `GeoConnexFeatures` has one collection, `GeoconnexFeatures`, holding the reference
# features above and those of every other contributor, with five properties each. Its
# `mainstem_uri` ties a feature to a reference mainstem. The Charles River in
# Massachusetts is mainstem `2290857`, and `item()` fetches it from `GeoConnex`:

# %%
from water_ogcapi import GeoConnexFeatures

gcf = GeoConnexFeatures()
river = gc.item("mainstems", "2290857").body or {}
print(river["properties"]["name_at_outlet"], river["properties"]["uri"])

# %% [markdown]
# Its pages carry no `numberMatched`, so a query ends on the first page without a `next`
# link. `skipGeometry` keeps the response to the properties:

# %%
on_river = gcf.collect(
    "GeoconnexFeatures",
    params={"mainstem_uri": river["properties"]["uri"], "skipGeometry": "true"},
)
print(len(on_river.geojson["features"]), "features in", on_river.pagination.page_count, "pages")
print(on_river.pagination.completeness, on_river.pagination.checks)

# %% [markdown]
# `geoconnex_sitemap` names the source of each feature: a reference collection such as
# `ref:gages`, or a contributor such as the Water Quality Portal, `epa:wqp`.

# %%
features = pl.DataFrame(on_river.to_arrow())
features.group_by("geoconnex_sitemap").len().sort("len", descending=True)

# %% [markdown]
# ## Fetch the full attributes
#
# A reference feature's `id` here is its `uri` property in `GeoConnex`, so `ids` fetches
# the river's stream gages from `gages` with every attribute:

# %%
uris = features.filter(pl.col("geoconnex_sitemap") == "ref:gages")["id"].to_list()
gages = gc.collect("gages", ids=uris, id_property="uri")
print(len(uris), "gages found,", len(gages.geojson["features"]), "fetched")
pl.DataFrame(gages.to_arrow()).select("provider_id", "name", "nhdpv2_comid").sort(
    "provider_id"
).head(10)

# %%
gage_frame = gpd.GeoDataFrame.from_arrow(gages.to_arrow()).to_crs(5070)
river_frame = gpd.GeoDataFrame.from_features([river], crs=4326).to_crs(5070)
ax = river_frame.plot(color="tab:blue", linewidth=1.5, figsize=(6, 5))
gage_frame.plot(ax=ax, color="tab:red", markersize=8)
ax.set(title="Stream gages along the Charles River")
ax.set_axis_off()
plt.tight_layout()

# %%
gc.close()
gcf.close()
