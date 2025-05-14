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
from datetime import datetime
from shapely.geometry import Polygon

logger = logging.getLogger("gsq.data.search")
logger.setLevel(logging.INFO)
stream_handler = logging.StreamHandler()
logger.addHandler(stream_handler)

output_dir = f"results/{datetime.now().strftime('%H%M%S')}"

# read parcels
nuts = gpd.read_file("aoi_europe.geojson")
nuts_l2 = nuts[nuts["LEVL_CODE"] == 2]
aois = ["Niederösterreich", "Wien"]
aoi_polygons = nuts_l2[nuts_l2["NUTS_NAME"].isin(aois)]

# define spatio-temporal extent
res = 10
epsg = 3035
t_start, t_end = "2022-05-01", "2022-06-01"
aoi = aoi_polygons.to_crs(4326)
space = sq.SpatialExtent(aoi)

# load data catalog
ds_catalog = gsq.DatasetCatalog()
ds_catalog.load()

# define landsat mapping
cloud_bits = []
bit_mask = (1 << 1) | (1 << 2) | (1 << 3)
for i in range(2**16):
    if i & bit_mask:
        cloud_bits.append(i)
l_map = sq.mapping.Semantique()
l_map["entity"] = {}
l_map["entity"]["valid"] = {
    "color": sq.layer("Planetary", "reflectance", "lndst_qa").evaluate("not_equal", 1)
}
l_map["entity"]["cloud"] = {
    "color": sq.layer("Planetary", "reflectance", "lndst_qa").evaluate("in", cloud_bits)
}

# define sentinel mapping
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
    },
    "landsat": {
        "layer_key": ("Planetary", "reflectance", "lndst_qa"),
        "mapping": l_map,
        "cloud_meta_col": "landsat:cloud_cover_land",
    },
}


def model_cloudfree_composite(gran, bands=["s2_band04", "s2_band03", "s2_band02"]):
    """Model to create cloud-free composites of defined temporal granularity

    params:
        gran: temporal granularity, one of ["year"], ["month"], ["season"], ["quarter"] or a combination
        bands: list of bands to build the composite upon

    notes:
        For the temporal granularity only disjunct/partionable combinations are feasible.
        E.g. ["year", "month"] can be used as granularity to get monthly cloudfree composites,
        whereas "season" doesn't work in conjunction with "year" as it spans several years.
    """

    # 0. parse arguments
    main_gran = gran[-1]
    recipe = sq.QueryRecipe()

    # 1. define mask of AoI values
    recipe["aoi_mask"] = (
        sq.layer(*bands[0])
        .evaluate("is_missing")
        .reduce("all", "time")
        .apply_custom("update_na", na_value=-99)
        .apply_custom("change_dtype", dtype="int8")
    )

    # 2. create semantic composite as median of cloud-free images
    recipe["comp_semantic"] = (
        sq.collection(*[sq.layer(*x) for x in bands])
        .concatenate("band")
        .filter(sq.entity("cloud").evaluate("not"))
        .groupby_time(gran)
        .reduce("median", "time")
        .concatenate(main_gran)
        .apply_custom("update_na")
        .apply_custom("change_dtype", dtype="float32")
    )

    # 3. create median composite for reference
    recipe["comp_median"] = (
        sq.collection(*[sq.layer(*x) for x in bands])
        .concatenate("band")
        .groupby_time(gran)
        .reduce("median", "time")
        .concatenate(main_gran)
        .apply_custom("update_na")
        .apply_custom("change_dtype", dtype="float32")
    )
    return recipe


class CfCreator:
    # define recipe
    gran = ["year", "month"]
    bands = [
        ("Planetary", "reflectance", "s2_band08"),
        ("Planetary", "reflectance", "s2_band04"),
        ("Planetary", "reflectance", "s2_band03"),
        ("Planetary", "reflectance", "s2_band02"),
    ]
    recipe = model_cloudfree_composite(gran, bands)

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

        with open(gsq.LAYOUT_PATH, "r") as file:
            dc = sq.datacube.STACCube(
                json.load(file),
                src=[],
            )

        self.fdr.search_auto(
            CfCreator.recipe,
            self.sat_mapping,
            custom_verbs={"update_na": gsq.update_na, "change_dtype": gsq.change_dtype},
        )

        # filter by cloud cover
        stac_json = self.fdr.item_coll.to_dict()
        gdf = gpd.GeoDataFrame.from_features(stac_json, "epsg:4326")
        keep_idx = gdf[gdf["eo:cloud_cover"] <= self.cloud_thres].index.values
        item_coll = [x for idx, x in enumerate(self.fdr.item_coll) if idx in keep_idx]

        # # optional: download files
        # root, ext = os.path.split(self.output_dir)
        # data_dir = os.path.join(root, "data", ext)
        # dwn = gsq.Downloader(item_coll, data_dir, reauth_batch_size=50)
        # dwn.run()

        # # re-read requires to convert files to absolute references
        # catalog = pystac.Catalog.from_file(os.path.join(data_dir, "catalog.json"))
        # catalog.make_all_asset_hrefs_absolute()
        # item_list = [x for x in catalog.get_items(recursive=True)]
        # item_coll = pystac.ItemCollection(item_list)

        # construct datacube
        with open(gsq.LAYOUT_PATH, "r") as file:
            dc = sq.datacube.STACCube(
                json.load(file),
                src=item_coll,
                group_by_solar_day=True,
                dask_params=None,
            )

        # create TileHandler instance & execute processing
        context = dict(
            recipe=CfCreator.recipe,
            datacube=dc,
            mapping=self.sat_mapping,
            space=space,
            time=sq.TemporalExtent(pd.Timestamp(t_start), pd.Timestamp(t_end)),
            spatial_resolution=[-res, res],
            crs=epsg,
            chunksize_s=512,
            merge_mode="vrt_shapes",
            out_dir=self.output_dir,
            custom_verbs={"update_na": gsq.update_na, "change_dtype": gsq.change_dtype},
            track_types=False,
        )
        # catch runtime warning
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            self.th = gsq.TileHandlerParallel(n_procs=os.cpu_count(), **context)
            self.th.execute()


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

    for sensor in ["sentinel"]:
        sub_dir = f"{sensor}_{t_start.replace('-','')}_{t_end.replace('-','')}_c{cloud_thresh}"
        out_dir = os.path.join(root_dir, sub_dir)
        cfs = CfCreator(
            params[sensor]["layer_key"],
            params[sensor]["mapping"],
            t_start,
            t_end,
            cloud_thresh,
            out_dir,
        )
        cfs.run()
