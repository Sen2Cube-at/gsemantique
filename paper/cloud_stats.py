#!/usr/bin/env python
# coding: utf-8

import geopandas as gpd
import gsemantique as gsq
import json
import logging
import os
import pandas as pd
import semantique as sq
import warnings
from datetime import datetime
from shapely.geometry import Polygon

logger = logging.getLogger("gsq.data.search")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler()
logger.addHandler(stream_handler)

warnings.filterwarnings("ignore")

output_dir = f"results/{datetime.now().strftime('%H%M%S')}"

# read European NUTS regions as AOI
nuts = gpd.read_file("aoi_europe.geojson")
aoi_polygons = nuts[nuts["LEVL_CODE"] == 1]
excl_list = ["RUP FR — Régions Ultrapériphériques Françaises"]
aoi_polygons = aoi_polygons[~aoi_polygons["NUTS_NAME"].isin(excl_list)]

# define spatio-temporal extent
res = 500
epsg = 3035
t_start, t_end = "2022-01-01", "2022-02-01"
aoi = aoi_polygons.to_crs(4326)
space = sq.SpatialExtent(aoi)

# load data catalog
ds_catalog = gsq.DatasetCatalog()
ds_catalog.load()

# define sentinel mapping & parameters
s2_map = sq.mapping.Semantique()
s2_map["entity"] = {}
s2_map["entity"]["valid"] = {
    "color": sq.layer("Planetary", "classification", "scl").evaluate("not_equal", 0)
}
s2_map["entity"]["cloud"] = {
    "color": sq.layer("Planetary", "classification", "scl").evaluate("in", [8, 9, 10])
}

params = {
    "sentinel": {
        "layer_key": ("Planetary", "classification", "scl"),
        "mapping": s2_map,
        "cloud_meta_col": "eo:cloud_cover",
    }
}


class MetaSearch:
    # define recipe for cloud-free search
    recipe = sq.QueryRecipe()
    recipe["all"] = (
        sq.entity("valid")
        .groupby_time(["year", "dayofyear"])
        .reduce("mode", "time")
        .concatenate("new_time")
        .reduce("count", "new_time")
        .apply_custom("update_na", na_value=-99)
        .apply_custom("change_dtype", dtype="int16")
    )
    recipe["cloudless"] = (
        sq.entity("valid")
        .evaluate("not_missing")
        .filter(sq.self())
        .filter(sq.entity("cloud").evaluate("not"))
        .groupby_time(["year", "dayofyear"])
        .reduce("mode", "time")
        .concatenate("new_time")
        .reduce("count", "new_time")
        .apply_custom("update_na", na_value=-99)
        .apply_custom("change_dtype", dtype="int16")
    )

    def __init__(
        self, layer_search_key, sat_mapping, t_start, t_end, cloud_thres, output_dir
    ):
        """
        Search for cloud-free data based on metadata statistics of cloud coverage.

        Args:
            layer_search_key (tuple): The layer key to search for.
            sat_mapping (dict): The mapping specfic to the satellite data.
            t_start (str): The start date in the format YYYY-MM-DD.
            t_end (str): The end date in the format YYYY-MM-DD.
            cloud_thres (int): The cloud coverage threshold.
            output_dir (str): The output path to save the results.
        """
        self.layer_search_key = layer_search_key
        self.sat_mapping = sat_mapping
        self.t_start = t_start
        self.t_end = t_end
        self.cloud_thres = cloud_thres
        self.output_dir = output_dir
        self.fdr = None
        self.th = None

    def run(self):
        # search for data
        bounds_df = aoi.to_crs(4326).bounds
        aoi["geometry"] = bounds_df.apply(
            lambda row: Polygon(
                [
                    (row["minx"], row["miny"]),
                    (row["maxx"], row["miny"]),
                    (row["maxx"], row["maxy"]),
                    (row["minx"], row["maxy"]),
                ]
            ),
            axis=1,
        )
        self.fdr = gsq.Finder(ds_catalog, self.t_start, self.t_end, aoi)
        self.fdr.search_man(self.layer_search_key)

        # filter by cloud cover
        stac_json = self.fdr.item_coll.to_dict()
        gdf = gpd.GeoDataFrame.from_features(stac_json, "epsg:4326")
        keep_idx = gdf[gdf["eo:cloud_cover"] <= self.cloud_thres].index.values
        item_coll = [x for idx, x in enumerate(self.fdr.item_coll) if idx in keep_idx]

        # construct datacube
        with open(gsq.LAYOUT_PATH, "r") as file:
            dc = sq.datacube.STACCube(
                json.load(file),
                src=item_coll,
                group_by_solar_day=False,
            )

        # create TileHandler instance & execute processing
        context = dict(
            recipe=MetaSearch.recipe,
            datacube=dc,
            mapping=self.sat_mapping,
            space=space,
            time=sq.TemporalExtent(pd.Timestamp(t_start), pd.Timestamp(t_end)),
            spatial_resolution=[-res, res],
            crs=epsg,
            chunksize_t="1W",
            chunksize_s=512,
            merge_mode="merged",
            out_dir=self.output_dir,
            reauth=True,
            custom_verbs={"update_na": gsq.update_na, "change_dtype": gsq.change_dtype},
        )
        th = gsq.TileHandlerParallel(n_procs=os.cpu_count(), **context)
        th.execute()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run semantic cloud search.")
    parser.add_argument(
        "--t_start",
        type=str,
        default=t_start,
        help="The start date in the format YYYY-MM-DD",
    )
    parser.add_argument(
        "--t_end", type=str, default=t_end, help="The end date in the format YYYY-MM-DD"
    )
    parser.add_argument(
        "--cloud_thresh", type=int, default=100, help="The cloud coverage threshold"
    )
    parser.add_argument(
        "--output_dir", type=str, default=output_dir, help="The output directory"
    )
    args = parser.parse_args()

    t_start = args.t_start
    t_end = args.t_end
    root_dir = args.output_dir
    cloud_thresh = args.cloud_thresh

    sub_dir = (
        f"sentinel_{t_start.replace('-','')}_{t_end.replace('-','')}_c{cloud_thresh}"
    )
    out_dir = os.path.join(root_dir, sub_dir)

    cfs = MetaSearch(
        params["sentinel"]["layer_key"],
        params["sentinel"]["mapping"],
        t_start,
        t_end,
        cloud_thresh,
        out_dir,
    )
    cfs.run()
