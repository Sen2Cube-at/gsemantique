#!/usr/bin/env python
# coding: utf-8

import geopandas as gpd
import gsemantique as gsq
import logging
import json
import os
import pandas as pd
import semantique as sq
import warnings

from shapely.geometry import box

logger = logging.getLogger("gsq.data.search")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler()
logger.addHandler(stream_handler)

output_dir = "results/forests"

# load AoI
_aoi = gpd.read_file("aoi_forest.geojson").to_crs(4326)
xmin, ymin, xmax, ymax = _aoi.total_bounds
aoi_bbox = box(xmin, ymin, xmax, ymax)

# define extent
res = 20
epsg = 3035
t_start = "2020-01-01"
t_end = "2024-01-01"
time = sq.TemporalExtent(pd.Timestamp(t_start), pd.Timestamp(t_end))
space = sq.SpatialExtent(_aoi)

# load data catalog
ds_catalog = gsq.DatasetCatalog()
ds_catalog.load()


# custom functions: preprocessing for SAR data
def rescale_sar(obj, track_types=True, **kwargs):
    # self-contained imports
    import numpy as np
    import semantique as sq

    # rescale SAR data
    out = obj.copy(deep=True)
    out.values = 20 * np.log10(obj.values) - 83

    # track value types
    if track_types:
        out.sq.value_type = sq.processor.types.get_value_type(out)

    return out


# custom functions: persistence of forest disturbances
def calc_persistence(x, track_types=True, **kwargs):
    # self-contained imports
    import numpy as np
    import semantique as sq

    def persistence_from_latest(arr, axis):
        reversed_arr = np.flip(arr, axis=axis)
        mask = reversed_arr > 0
        consecutive_counts = np.zeros(reversed_arr.shape, dtype=int)
        temp_count = np.zeros(mask.shape, dtype=int)
        temp_count[mask] = 1
        consecutive_counts = np.cumsum(temp_count, axis=axis)
        streak_mask = np.cumprod(mask, axis=axis, dtype=int)
        consecutive_counts *= streak_mask
        consecutive_counts = np.max(consecutive_counts, axis=axis)
        return np.where(np.nanmax(mask, axis=0), consecutive_counts, np.nan)

    # apply the reduction
    out = x.reduce(persistence_from_latest, **kwargs).astype(np.float32)

    # track value types
    if track_types:
        out.sq.value_type = sq.processor.types.get_value_type(out)

    return out


# create two forest definitions
forest_defs = {
    "forest_esa": {
        "class": (
            sq.layer("Planetary", "classification", "esa_lc")
            .evaluate("equal", 10)
            .filter_time("year", "equal", 2020)
            .reduce("any", "time")
        )
    },
    "forest_own": {
        "coherence": (
            sq.layer("ASF", "coherence", "s1_coh12_vv")
            .filter_time("year", "equal", 2020)
            .evaluate("divide", 100)
            .reduce("max", "time")
            .evaluate("less", 0.3)
        ),
        "reflectance": (
            sq.layer("ASF", "reflectance", "s1_amp_vh")
            .filter_time("year", "equal", 2020)
            .reduce("min", "time")
            .apply_custom("rescale_sar")
            .evaluate("in", sq.interval(-16.25, -5))
        ),
        "elevation": (
            sq.layer("Planetary", "topography", "dem")
            .reduce("mean", "time")
            .evaluate("less", 2250)
        ),
    },
}

# define relevant entities
mapping = sq.mapping.Semantique()
mapping["entity"] = {}
mapping["entity"]["forest"] = None  # will be plugged in later
mapping["entity"]["vegetation"] = {
    "color": sq.layer("Planetary", "classification", "scl").evaluate("equal", 4)
}
mapping["entity"]["valid_obs"] = {
    "color": sq.layer("Planetary", "classification", "scl").evaluate("in", [4, 5, 6])
}
mapping["entity"]["all"] = {
    "color": sq.layer("Planetary", "classification", "scl").evaluate("not_equal", 0)
}

# define two recipes
recipes = {"robust": None, "sensitive": None}

# define robust recipe
recipe = sq.QueryRecipe()
recipe["forest"] = sq.entity("forest")
recipe["stats"] = (
    sq.entity("all")
    .filter(sq.entity("valid_obs"))
    .groupby_time("year")
    .reduce("count", "time")
    .concatenate("year")
    .apply_custom("change_dtype", dtype="int16")
)
recipe["mask"] = (
    sq.result("stats")
    .filter(sq.result("forest"))
    .reduce("min", "year")
    .evaluate("greater", 5)
)
recipe["status_orginal"] = (
    sq.entity("vegetation")
    .filter(sq.result("mask"))
    .filter(sq.entity("valid_obs"))
    .filter_time("year", "equal", 2020)
    .reduce("percentage", "time")
    .apply_custom("change_dtype", dtype="int16")
)
recipe["status_post"] = (
    sq.entity("vegetation")
    .filter(sq.result("mask"))
    .filter(sq.entity("valid_obs"))
    .filter_time("year", "greater", 2020)
    .groupby_time("year")
    .reduce("percentage", "time")
    .concatenate("year")
    .apply_custom("change_dtype", dtype="float32")
)
recipe["change"] = (
    sq.result("status_post")
    .evaluate("subtract", sq.result("status_orginal"))
    .evaluate("absolute")
    .filter(sq.self().evaluate("greater", 20))
    .apply_custom("update_na")
)
recipe["magnitude"] = sq.result("change").reduce("mean", "year")
recipe["persistance"] = sq.result("change").reduce("consecutive_nonzero", "year")
recipes["robust"] = recipe

# define sensitive recipe
recipe = sq.QueryRecipe()
recipe["forest"] = sq.entity("forest")
recipe["stats"] = (
    sq.entity("all")
    .filter(sq.entity("valid_obs"))
    .groupby_time("year")
    .reduce("count", "time")
    .concatenate("year")
    .apply_custom("change_dtype", dtype="int16")
)
recipe["mask"] = (
    sq.result("stats")
    .filter(sq.result("forest"))
    .reduce("min", "year")
    .evaluate("greater", 5)
)
recipe["status_orginal"] = (
    sq.entity("vegetation")
    .filter(sq.result("mask"))
    .filter(sq.entity("valid_obs"))
    .filter_time("year", "equal", 2020)
    .reduce("percentage", "time")
    .apply_custom("change_dtype", dtype="int16")
)
recipe["status_post"] = (
    sq.entity("vegetation")
    .filter(sq.result("mask"))
    .filter(sq.entity("valid_obs"))
    .filter_time("year", "greater", 2020)
    .groupby_time("year")
    .reduce("percentage", "time")
    .concatenate("year")
    .apply_custom("change_dtype", dtype="float32")
)
recipe["change"] = (
    sq.result("status_post")
    .evaluate("subtract", sq.result("status_orginal"))
    .evaluate("absolute")
    .filter(sq.self().evaluate("greater", 10))
    .apply_custom("update_na")
)
recipe["magnitude"] = sq.result("change").reduce("max", "year")
recipe["persistance"] = (
    sq.result("change").evaluate("greater", 0).reduce("count", "year")
)
recipes["sensitive"] = recipe

if __name__ == "__main__":
    # iterate over entity & recipe definitions
    for f_def, f_v in forest_defs.items():
        for r_def, r_v in recipes.items():

            # plug in entity definition
            mapping["entity"]["forest"] = f_v

            # create Finder instance & search for relevant data
            fdr = gsq.Finder(ds_catalog, t_start, t_end, aoi_bbox)
            fdr.search_auto(
                recipe,
                mapping,
                custom_verbs={
                    "update_na": gsq.update_na,
                    "change_dtype": gsq.change_dtype,
                    "rescale_sar": rescale_sar,
                },
                custom_reducers={"consecutive_nonzero": calc_persistence},
            )

            # init datacube
            with open(gsq.LAYOUT_PATH, "r") as file:
                dc = sq.datacube.STACCube(
                    json.load(file),
                    src=fdr.item_coll,
                    group_by_solar_day=True,
                    dask_params=None,
                )

            # create TileHandler instance
            out_dir = os.path.join(output_dir, f"{f_def}_{r_def}")
            context = dict(
                recipe=r_v,
                datacube=dc,
                mapping=mapping,
                space=space,
                time=time,
                spatial_resolution=[-res, res],
                crs=epsg,
                chunksize_t="1W",
                chunksize_s=1024,
                merge_mode="merged",
                out_dir=out_dir,
                reauth=True,
                custom_verbs={
                    "update_na": gsq.update_na,
                    "change_dtype": gsq.change_dtype,
                    "rescale_sar": rescale_sar,
                },
                custom_reducers={"consecutive_nonzero": calc_persistence},
                track_types=True,
            )

            # execute processing
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                th = gsq.TileHandler(**context)
                th.execute()
