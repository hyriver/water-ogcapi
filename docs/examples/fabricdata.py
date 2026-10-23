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
# # Querying FabricData
#
# `FabricData` queries the USGS Geospatial Fabric service. It takes no key and never
# receives `USGS_API_KEY`. This notebook looks up the service's collections and the
# properties of `gagesii`, the GAGES-II streamgages, then collects the gages in a box
# around Washington, DC, narrows the query with a CQL2 filter, and maps the result.
# `to_arrow()` turns a result into a table for geopandas, which needs the `arrow` extra.

# %%
from water_ogcapi import FabricData

DC = (-77.12, 38.80, -76.91, 38.99)
fabric = FabricData()

# %% [markdown]
# A service keeps its HTTP connections open until it is closed. In a script, use it in
# a `with` block, `with FabricData() as fabric:`, which closes it on exit. A notebook keeps one
# service across cells, so it calls `fabric.close()` explicitly in its last cell.

# %% [markdown]
# ## Look at the collection
#
# `collections()` returns the index and `queryables()` the properties of one collection
# that a query can name. Both are cached for an hour. Each query checks its collection
# and parameter names against that cache first, so a typo raises an error that lists the
# valid names.

# %%
ids = [collection.id for collection in fabric.collections().collections]
print(len(ids), "collections:", ", ".join(ids[:6]), "...")

# %%
for name, prop in fabric.queryables("gagesii").properties.items():
    print(f"{name:12} {prop.type or prop.format}")

# %% [markdown]
# ## Collect the gages in a box
#
# `collect()` gathers every page into one FeatureCollection and records how the traversal
# went: the checks that passed and the CRS the server named. FabricData reports
# `numberMatched`, so the `count` check compares it with the features received. It sends
# `X-RateLimit-*` headers on every response, which the client uses to space its requests.

# %%
gages = fabric.collect("gagesii", bbox=DC)
print(len(gages.geojson["features"]), "gages in", gages.pagination.page_count, "page(s)")
print(gages.pagination.completeness, gages.pagination.checks)
print(gages.crs.effective, gages.crs.source)
print("X-RateLimit-Limit:", gages.pages[0].headers["x-ratelimit-limit"])

# %% [markdown]
# ## Filter with CQL2
#
# A CQL2-Text `filter` combines with the bounding box. Only the gages that drain more
# than 100 km²:

# %%
large = fabric.collect("gagesii", bbox=DC, filter="drain_sqkm > 100")
for feature in large.geojson["features"]:
    props = feature["properties"]
    print(feature["id"], props["staname"], props["drain_sqkm"])

# %% [markdown]
# ## Read the gages with geopandas
#
# `to_arrow()` returns a pyarrow Table whose `geometry` column carries the CRS, so
# geopandas reads it as a GeoDataFrame with that CRS. Each gage is a `MultiPoint` of one
# point.

# %%
import geopandas as gpd

frame = gpd.GeoDataFrame.from_arrow(gages.to_arrow())
print(frame.crs)
frame[["staid", "staname", "state", "drain_sqkm"]].sort_values("staid")

# %% [markdown]
# ## Map the gages
#
# The dashed rectangle is the query's bounding box. contextily draws a basemap under the
# gages; its tiles are in Web Mercator (EPSG:3857), so every layer is reprojected to it.

# %%
import contextily as ctx
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import shapely

points = frame.to_crs(3857)
big = gpd.GeoDataFrame.from_arrow(large.to_arrow()).to_crs(3857)
box = gpd.GeoSeries([shapely.box(*DC)], crs=frame.crs).to_crs(3857)
ax = points.plot(color="dimgray", markersize=40, figsize=(6, 6.5), label="All gages")
big.plot(
    ax=ax,
    color="tab:blue",
    edgecolor="white",
    markersize=70,
    label="Drainage area over 100 km²",
)
halo = [pe.withStroke(linewidth=2.5, foreground="white")]
for row in points.itertuples():
    x, y = row.geometry.centroid.coords[0]
    ax.annotate(
        row.staid, (x, y), xytext=(5, 4), textcoords="offset points", fontsize=7, path_effects=halo
    )
box.boundary.plot(ax=ax, color="gray", linestyle="--")
west, south, east, north = box.total_bounds
ax.set_xlim(west - 1000, east + 1000)  # meters
ax.set_ylim(south - 1000, north + 1000)
ctx.add_basemap(ax, source=ctx.providers.Esri.WorldGrayCanvas)
ax.legend(loc="upper center", bbox_to_anchor=(0.5, 0), ncols=2, frameon=False)
ax.set(title="GAGES-II streamgages around Washington, DC")
ax.set_axis_off()
plt.tight_layout()

# %%
fabric.close()
