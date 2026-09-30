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
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Getting started
#
# A placeholder that exercises the docs pipeline: Markdown, code, text output, and a figure. Real examples arrive with the query API.

# %% [markdown]
# Errors carry the status code, the number of attempts, and the URL:

# %%
from water_ogcapi.exceptions import ServiceError

url = "https://api.waterdata.usgs.gov/ogcapi/v0/collections"
print(ServiceError("service unavailable", url, status=503, attempts=4))

# %% [markdown]
# The transport waits 0.5 s before its first retry, doubles the wait each time, and caps it at 60 s. The default allows three retries; the figure shows eight.

# %%
import matplotlib.pyplot as plt

attempts = range(8)
delays = [min(0.5 * 2**n, 60.0) for n in attempts]

fig, ax = plt.subplots(figsize=(6, 3.5))
ax.bar(attempts, delays)
ax.set(xlabel="Retry", ylabel="Wait (s)", title="Backoff schedule")
fig.tight_layout()
