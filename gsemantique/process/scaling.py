import gc
import geopandas as gpd
import numpy as np
import os
import pandas as pd
import rasterio as rio
import rioxarray as rxr
import shutil
import time
import threading
import uuid
import warnings
import xarray as xr

from copy import deepcopy
from enum import Enum
from itertools import product
from multiprocess import Pool
from shapely.geometry import box
from rioxarray.merge import merge_arrays
from tqdm import tqdm

import semantique as sq
from semantique import exceptions
from semantique.datacube import STACCube
from semantique.extent import SpatialExtent, TemporalExtent
from semantique.processor.arrays import Collection
from semantique.processor.core import FakeProcessor, QueryProcessor
from .vrt import virtual_merge


class MergeMode(Enum):
    NONE = None
    MERGED = "merged"
    VRT_SHAPES = "vrt_shapes"
    VRT_TILES = "vrt_tiles"


class TileHandler:
    """Handler for executing a query in a (spatially or temporally) tiled manner.
    Note that it currently only supports non-grouped outputs, i.e. results that are
    concatenated after a groupby operation was called.

    Parameters
    ----------
      recipe : QueryRecipe
        The query recipe to be processed.
      datacube : Datacube
        The datacube instance to process the query against.
      mapping : Mapping
        The mapping instance to process the query against.
      space : SpatialExtent
        The spatial extent in which the query should be processed.
      time : TemporalExtent
        The temporal extent in which the query should be processed.
      spatial_resolution : :obj:`list`
        Spatial resolution of the grid. Should be given as a list in the format
        `[y, x]`, where y is the cell size along the y-axis, x is the cell size
        along the x-axis, and both are given as :obj:`int` or :obj:`float`
        value expressed in the units of the CRS. These values should include
        the direction of the axes. For most CRSs, the y-axis has a negative
        direction, and hence the cell size along the y-axis is given as a
        negative number.
      crs : optional
        Coordinate reference system in which the grid should be created. Can be
        given as any object understood by the initializer of
        :class:`pyproj.crs.CRS`. This includes :obj:`pyproj.crs.CRS` objects
        themselves, as well as EPSG codes and WKT strings. If :obj:`None`, the
        CRS of the extent itself is used.
      chunksize_t : int, tbd
        Temporal chunksize
      chunksize_s : int, tbd
        Spatial chunksize
      tile_dim : tbd
      merge_mode: Mode of how tiled results shall be merged. Options are None,
      "merged", "vrt_tiles", "vrt_shapes".
          None - Tiled results will be returned as provided by
              `QueryProcessor.execute()` and stored in a list accessible as
              `self.tile_results`.
          "merged" - Tiled results will be postprocessed to ensure at most 3D
                  array-like outputs and will then be spatio-temporally merged.
                  It represents a balance between the highest possible similarity
                  of the result to a run of the recipes without `TileHandler` and
                  the practical reusability of the results (since every output is
                  an array with at most 3 dimension that can be easily processed
                  further). This is the default option.
          "vrt_shapes" - Tiled results will be postprocessed to ensure at most 3D
                      array-like outputs. They will be stored as individual,
                      irregularly shaped tiles and a virtual raster will be
                      created. This option is only available for spatially not
                      temporally tiled processing. It is useful for large or
                      spatially irregularly arranged outputs that cannot be
                      merged directly into a single output.
          "vrt_tiles" - Tiled results will be postprocessed to ensure at most 3D
                      array-like outputs. They will be stored as individual,
                      regularly shaped tiles and a virtual raster will be created.
                      This option is only available for spatially not temporally
                      tiled processing. It is useful if the uniformity of the
                      output styles is relevant (e.g., because the output is to be
                      used as patch input for a subsequent neural network model).
                      However, the regular tiling (without strict cropping to the
                      spatio-temporal AOI extent) results in a slightly increased
                      data volume output compared to "vrt_shapes".
      out_dir: Output directory to write results to. Options are None or a string to
              an output directory. This option is interdependent with the merge_mode:
          None - Only available if merge_mode is in [None, "merged"]. Outputs won't be
              written to disk but will be accessible via `self.tile_results`.
          <output path> - Only available for ["merged", "vrt_shapes", "vrt_tiles"].
                    Outputs will be written to the specified path and additionally be
                    accessible via `self.tile_results`.
      caching : bool, tbd
      reauth : bool, tbd
      verbose : bool, tbd
      **config :
        Additional configuration parameters forwarded to QueryRecipe.execute.
        See :class:`QueryRecipe`, respectively :class:`QueryProcessor`.
    """

    def __init__(
        self,
        recipe,
        datacube,
        mapping,
        space,
        time,
        spatial_resolution,
        crs=None,
        chunksize_t="1W",
        chunksize_s=1024,
        tile_dim=None,
        merge_mode="merged",
        out_dir=None,
        caching=True,
        reauth=True,
        verbose=True,
        **config,
    ):
        # parse args
        self.recipe = recipe
        self.datacube = datacube
        self.mapping = mapping
        self.space = space
        self.time = time
        self.spatial_resolution = spatial_resolution
        self.crs = crs
        self.chunksize_t = chunksize_t
        self.chunksize_s = chunksize_s
        self.tile_dim = tile_dim
        self.merge_mode = merge_mode
        self.out_dir = out_dir
        self.caching = caching
        self.reauth = reauth
        self.verbose = verbose
        self.config = config
        # init additional args
        self.grid = None
        self.tile_results = []
        self.cache = None
        self.stop_flag = False
        # retrieve crs information
        if not self.crs:
            self.crs = self.space.crs
        # retrieve tiling dimension
        self.get_tile_dim()
        # check merge- & outdir-dependent prerequisites
        if merge_mode not in {mode.value for mode in MergeMode}:
            raise ValueError(
                f"Invalid merge_mode: {merge_mode}. Must be one of {[mode.value for mode in MergeMode]}"
            )
        if self.merge_mode:
            if "vrt" in self.merge_mode and not self.out_dir:
                raise ValueError(
                    f"An 'out_dir' argument must be provided when merge is set to {self.merge_mode}."
                )
            elif "vrt" in self.merge_mode and self.tile_dim == sq.dimensions.TIME:
                raise NotImplementedError(
                    "If tiling is done along the temporal dimension, 'vrt_*' is "
                    "currently not available as a merge strategy."
                )
        else:
            self.out_dir = None
        # create output directory
        if self.out_dir:
            os.makedirs(self.out_dir)
        # continous re-auth
        if self.reauth:
            self.signing_thread_event = threading.Event()
            self.signing_thread = threading.Thread(target=self._continuous_signing)
            self.signing_thread.daemon = True
            self.signing_thread.start()

    def __del__(self):
        if hasattr(self, "signing_thread_event") and self.signing_thread_event.is_set():
            self.signing_thread_event.set()
            self.signing_thread.join()

    def get_tile_dim(self):
        """Returns dimension usable for tiling & parallelisation of recipe execution.
        Calls `.get_op_dims()` to get dimensions which should be kept together to
        ensure safe tiling.

        Note: EO data is usually organised in a time-first file structure, i.e. each file
        contains many spatial observations for one point in time. Therefore, temporal chunking
        is favourable if possible. However, choosing a reasonable default for temporal chunksize
        is more difficult as the temporal spacing of EO obsevrations is unknown prior to data fetching.
        Therefore, chunking in space is set as a default if the processing chain as given by the
        recipe allows it.
        """
        reduce_dims = TileHandler.get_op_dims(self.recipe)
        # retrieve tile dimension as non-used dimension
        if not reduce_dims:
            if not self.tile_dim:
                self.tile_dim = sq.dimensions.SPACE
        elif reduce_dims == [sq.dimensions.TIME]:
            if self.tile_dim:
                if self.tile_dim != sq.dimensions.SPACE:
                    warnings.warn(
                        f"Tiling dimension {self.tile_dim} will be overwritten. Tiling dimension is set to 'space'."
                    )
            self.tile_dim = sq.dimensions.SPACE
        elif reduce_dims == [sq.dimensions.SPACE]:
            if self.tile_dim:
                if self.tile_dim != sq.dimensions.TIME:
                    warnings.warn(
                        f"Tiling dimension {self.tile_dim} will be overwritten. Tiling dimension is set to 'time'."
                    )
            self.tile_dim = sq.dimensions.TIME
        else:
            warnings.warn("Tiling not feasible. Tiling dimension is set to 'None'.")
            self.tile_dim = None

    def get_tile_grid(self):
        """Creates spatial or temporal grid according to tiling dimension to enable
        subsequent sequential iteration over small sub-parts of the total extent object
        via .execute().
        """
        if self.tile_dim == sq.dimensions.TIME:
            # create temporal grid
            self.grid = self.create_temporal_grid(
                self.time["start"], self.time["end"], self.chunksize_t
            )

        elif self.tile_dim == sq.dimensions.SPACE:
            # create spatial grid
            if self.merge_mode == "vrt_tiles":
                precise_shp = False
            else:
                precise_shp = True
            self.grid = self.create_spatial_grid(
                self.space,
                self.spatial_resolution,
                self.chunksize_s,
                self.crs,
                precise=precise_shp,
                verbose=self.verbose,
            )

    def execute(self):
        """Runs the QueryProcessor.execute() method for all tiles."""
        # A) dry-run is performed to set up cache
        self.preview()

        # B) eval recipe & postprocess in tile-wise manner
        for i, tile in tqdm(
            enumerate(self.grid),
            disable=not self.verbose,
            total=len(self.grid),
            desc="executing recipe in tiled manner",
        ):
            # run workflow for single tile
            context = self._create_context(
                **{self.tile_dim: tile}, cache=deepcopy(self.cache)
            )
            response = self._execute_workflow(context)
            # missing response possible in cases where
            # self.tile_dim = sq.dimensions.TIME & trim=True
            if response:
                if not self.merge_mode:
                    self.tile_results.append(response)
                else:
                    # postprocess response
                    if self.tile_dim == sq.dimensions.TIME:
                        response = self._postprocess_temporal(response)
                    elif self.tile_dim == sq.dimensions.SPACE:
                        response = self._postprocess_spatial(response)
                    # write result (in-memory or to disk)
                    if self.merge_mode == "merged":
                        self.tile_results.append(response)
                    else:
                        for layer in response.keys():
                            out_dir = os.path.join(self.out_dir, layer)
                            out_path = os.path.join(out_dir, f"{i}.tif")
                            os.makedirs(out_dir, exist_ok=True)
                            response[layer].rio.to_raster(out_path)
                            self.tile_results.append(out_path)

        # C) optional merge of results
        if self.tile_results:
            if self.merge_mode:
                if self.merge_mode == "merged":
                    self.merge_single()
                elif "vrt" in self.merge_mode:
                    self.merge_vrt()

    def merge_single(self):
        """Merge results obtained for individual tiles by stitching them
        temporally or spatially depending on the tiling dimension.
        """
        joint_res = {}
        res_keys = self.tile_results[0].keys()
        # merge recipes results
        for k in res_keys:
            src_arrs = [x[k] for x in self.tile_results]
            if self.tile_dim == sq.dimensions.TIME:
                joint_arr = TileHandler._merge_temporal(src_arrs)
            elif self.tile_dim == sq.dimensions.SPACE:
                joint_arr = TileHandler._merge_spatial(
                    src_arrs, self.crs, self.spatial_resolution
                )
            joint_arr.name = k
            joint_res[k] = joint_arr
        self.tile_results = joint_res
        # write to out_dir
        if self.out_dir:
            for name, arr in self.tile_results.items():
                if self.tile_dim == sq.dimensions.TIME:
                    arr.attrs = {}
                    out_path = os.path.join(self.out_dir, f"{name}.nc")
                    arr.to_netcdf(out_path)
                elif self.tile_dim == sq.dimensions.SPACE:
                    out_path = os.path.join(self.out_dir, f"{name}.tif")
                    arr.rio.to_raster(out_path)

    def merge_vrt(self):
        """Merges results obtained for individual tiles by creating a virtual raster.
        Only available for spatial results obtained by a reduce-over-time. Not implemented
        for temporal results (i.e. timeseries obtained by a reduce-over-space).
        """
        res_keys = [os.path.dirname(x).split(os.sep)[-1] for x in self.tile_results]
        for k in np.unique(res_keys):
            res_dir = os.path.join(self.out_dir, k)
            srcs = [os.path.join(res_dir, x) for x in os.listdir(res_dir)]
            # ensure same bands across tiles
            self._equalize_bands(srcs)
            # create virtual raster
            res_path = os.path.join(self.out_dir, f"{k}.vrt")
            virtual_merge(srcs, dst_path=res_path)
            # create overview for vrt
            dst = rio.open(res_path, "r+")
            vrt_scales = [4, 8, 16, 32, 64, 128, 256, 512]
            vrt_scales = [x for x in vrt_scales if x < max(dst.shape)]
            dst.build_overviews(vrt_scales)
            dst.update_tags(ns="rio_overview")
            dst.close()

    def preview(self):
        """Estimator to preview the reipe and merge_mode dependent shapes
        and sizes of the outputs. Retrieves the tile size and evaluates the
        recipe for one of the tiles.
        """
        # preview info
        time_info = (
            "preview() is currently only implemented for "
            "spatial outputs. Unless you are processing very dense timeseries "
            "and/or processing many features it's save to assume that the size "
            "of your output is rather small, so don't worry about the memory space.\n"
        )
        space_info = (
            "The following numbers are rough estimations depending on the chosen "
            "strategy for merging the individual tile results. If merge='merged' "
            "is choosen the total size indicates a lower bound for how much RAM is "
            "required since the individual tile results will be stored in RAM before "
            "merging.\n"
        )

        # get tiling grid
        if not self.grid:
            self.get_tile_grid()

        # run fake processor to initialise cache
        if self.caching:
            tile = self.grid[0]
            context = self._create_context(**{self.tile_dim: tile})
            fp = FakeProcessor.parse(**context)
            fp.optimize().execute()
            self.cache = fp.cache

        # preview run of workflow for a single tile
        # requires iteration over tiles until a valid response is obtained
        # reason: some slices may not return a valid response (empty data)
        tile_idx = 0
        valid_response = False
        while not valid_response:
            if tile_idx >= len(self.grid):
                print("For none of the tiles a valid response could be calculated.")
                print("Check if the input data is within the spatio-temporal extent.")
                break
            tile = self.grid[tile_idx]
            context = self._create_context(
                **{self.tile_dim: tile}, preview=True, cache=deepcopy(self.cache)
            )
            response = self._execute_workflow(context)
            valid_response = True if response else False
            tile_idx += 1

        if valid_response:
            # postprocess response
            if self.tile_dim == sq.dimensions.TIME:
                response = self._postprocess_temporal(response)
            elif self.tile_dim == sq.dimensions.SPACE:
                response = self._postprocess_spatial(response)

            # get estimates based on preview run
            if self.tile_dim == sq.dimensions.TIME:
                print(time_info)
            elif self.tile_dim == sq.dimensions.SPACE:
                print(space_info)

                # retrieve amount of pixels for given spatial extent
                total_bbox = self.space._features.to_crs(self.crs).total_bounds
                width = total_bbox[2] - total_bbox[0]
                height = total_bbox[3] - total_bbox[1]
                num_pixels_x = int(np.ceil(width / abs(self.spatial_resolution[0])))
                num_pixels_y = int(np.ceil(height / abs(self.spatial_resolution[1])))
                xy_pixels = num_pixels_x * num_pixels_y

                # initialise dict to store layer information
                lyrs_info = {}
                for layer, arr in response.items():
                    # compile general layer information
                    lyr_info = {}
                    lyr_info["dtype"] = arr.dtype
                    lyr_info["res"] = self.spatial_resolution
                    lyr_info["crs"] = self.crs
                    # get array sizes (spatially & others)
                    xy_dims = [sq.dimensions.X, sq.dimensions.Y]
                    arr_xy_dims = [x for x in arr.dims if x in xy_dims]
                    arr_z_dims = [x for x in arr.dims if x not in xy_dims]
                    arr_xy = arr.isel(**{dim: 0 for dim in arr_z_dims})
                    arr_z = arr.isel(**{dim: 0 for dim in arr_xy_dims})
                    # extrapolate layer information for different merging strategies
                    lyr_info["merge"] = {}
                    # a) no merge
                    scale = len(self.grid) * (self.chunksize_s**2) / arr_xy.size
                    lyr_info["merge"]["None"] = {}
                    lyr_info["merge"]["None"]["n"] = len(self.grid)
                    lyr_info["merge"]["None"]["size"] = scale * arr.nbytes / (1024**3)
                    lyr_info["merge"]["None"]["shape"] = (
                        *arr_z.shape,
                        self.chunksize_s,
                        self.chunksize_s,
                    )
                    # b) vrt
                    vrt_scales = [4, 8, 16, 32, 64, 128, 256, 512]
                    size_tiles = lyr_info["merge"]["None"]["size"]
                    size_vrt = sum(
                        [
                            arr.nbytes * xy_pixels / arr_xy.size / (1024**3) / (x**2)
                            for x in vrt_scales
                        ]
                    )
                    lyr_info["merge"]["vrt_*"] = {}
                    lyr_info["merge"]["vrt_*"]["n"] = len(self.grid)
                    lyr_info["merge"]["vrt_*"]["size"] = size_tiles + size_vrt
                    lyr_info["merge"]["vrt_*"]["shape"] = (
                        *arr_z.shape,
                        self.chunksize_s,
                        self.chunksize_s,
                    )
                    # c) merge into single array
                    scale = xy_pixels / arr_xy.size
                    lyr_info["merge"]["merged"] = {}
                    lyr_info["merge"]["merged"]["n"] = 1
                    lyr_info["merge"]["merged"]["size"] = scale * arr.nbytes / (1024**3)
                    lyr_info["merge"]["merged"]["shape"] = (
                        *arr_z.shape,
                        num_pixels_x,
                        num_pixels_y,
                    )
                    lyrs_info[layer] = lyr_info

                # print general layer information
                max_l_lyr = max(len(r) for r in lyrs_info.keys())

                # part a) general information
                max_l_res = max(
                    [len(str(info["res"])) for lyr, info in lyrs_info.items()]
                )
                line_l = max_l_lyr + max_l_res + 19
                print(line_l * "-")
                print("General layer info")
                print(line_l * "-")
                print(
                    f"{'layer':{max_l_lyr}} : {'dtype':{9}} {'crs':{5}} {'res':{max_l_res}}"
                )
                print(line_l * "-")
                for lyr, info in lyrs_info.items():
                    print(
                        f"{lyr:{max_l_lyr}} : {str(info['dtype']):{9}} "
                        f"{str(info['crs']):{5}} {str(info['res']):{max_l_res}}"
                    )
                print(line_l * "-")
                print("")

                # part b) merge strategy dependend information
                for merge in lyrs_info[list(lyrs_info.keys())[0]]["merge"].keys():
                    total_n = sum(
                        [info["merge"][merge]["n"] for info in lyrs_info.values()]
                    )
                    total_size = sum(
                        [info["merge"][merge]["size"] for info in lyrs_info.values()]
                    )
                    shapes = [
                        str(info["merge"][merge]["shape"])
                        for info in lyrs_info.values()
                    ]
                    max_l_n = len(f"{total_n}")
                    max_l_size = len(f"{total_size:.2f}")
                    max_l_shape = max([len(x) for x in shapes])
                    line_l = max_l_lyr + max_l_n + max_l_size + max_l_shape + 18
                    print(line_l * "-")
                    print(f"Scenario: 'merge' = {merge}")
                    print(line_l * "-")
                    print(
                        f"{'layer':{max_l_lyr}} : {'size':^{max_l_size+3}}  "
                        f"{'tile n':^{max_l_n+8}}  {'tile shape':^{max_l_shape}}"
                    )
                    print(line_l * "-")
                    for lyr, info in lyrs_info.items():
                        lyr_info = info["merge"][merge]
                        print(
                            f"{lyr:{max_l_lyr}} : {lyr_info['size']:>{max_l_size}.2f} Gb  "
                            f"{lyr_info['n']:>{max_l_n}} tile(s)  {str(lyr_info['shape']):>{max_l_shape}}"
                        )
                    print(line_l * "-")
                    print(
                        f"{'Total':{max_l_lyr}}   {total_size:{max_l_size}.2f} Gb  {total_n:{max_l_n}} tile(s)"
                    )
                    print(line_l * "-")
                    print("")

    def _continuous_signing(self):
        """Calling resign function in a loop."""
        while not self.signing_thread_event.is_set():
            try:
                self.datacube.src = STACCube._sign_metadata(list(self.datacube.src))
                self.stop_flag = False
            except:
                self.stop_flag = True
            time.sleep(1)

    def _create_context(self, **kwargs):
        """Create execution context with dynamic space/time."""
        context = {
            "recipe": self.recipe,
            "datacube": self.datacube,
            "mapping": self.mapping,
            "space": self.space,
            "time": self.time,
            "crs": self.crs,
            "spatial_resolution": self.spatial_resolution,
            **self.config,
        }
        context.update(kwargs)
        return context

    def _equalize_bands(self, src_paths):
        """Postprocesses the response to ensure avaliability of all bands"""
        # get non-spatial dims (i.e. band dimension)
        with rxr.open_rasterio(src_paths[0]) as src_arr:
            band_dims = TileHandler._get_nonspatial_dims(src_arr)
            band_dim = band_dims[0]
            n_bands = len(src_arr[band_dim])
        # ensure same bands across arrays
        if n_bands > 1:
            band_dim = band_dims[0]
            # retrieve values for non-spatial dim
            band_names = []
            for src in src_paths:
                with rxr.open_rasterio(src) as src_arr:
                    for band in src_arr.long_name:
                        band_names.append(band)
            band_names = list(np.unique(sorted(band_names)))
            # introduce missing values for single arrays
            for src in src_paths:
                with rxr.open_rasterio(src) as src_arr:
                    # create list to hold any new bands that need to be created
                    new_bands = []
                    for band in band_names:
                        if band not in src_arr.long_name:
                            # create an array of NaNs with the same shape as one band of src_arr
                            nan_band = np.full_like(
                                src_arr.isel(**{band_dim: 0}), np.nan
                            )
                            nan_band = np.expand_dims(nan_band, 0)
                            coords = {
                                c: (c, src_arr.coords[c].values) for c in src_arr.dims
                            }
                            coords.update({band_dim: (band_dim, np.array([1]))})
                            new_band = xr.DataArray(
                                nan_band, dims=src_arr.dims, coords=coords, name=band
                            )
                            new_band.attrs["long_name"] = band
                            new_bands.append(new_band)
                    # combine original bands with new, previosuly missing bands
                    if new_bands:
                        dst_arr = xr.concat(new_bands + [src_arr], dim=band_dim)
                        dst_arr.attrs["long_name"] = [
                            *[x.long_name for x in new_bands],
                            *src_arr.long_name,
                        ]
                    else:
                        dst_arr = src_arr
                    band_order = [dst_arr.long_name.index(x) for x in band_names]
                    dst_arr = dst_arr.isel(**{band_dim: band_order})
                    dst_arr.attrs["long_name"] = band_names
                    dst_arr[band_dim] = np.arange(len(band_names)) + 1
                # write updated array to disk
                TileHandler._write_to_origin(dst_arr, src)

    def _execute_workflow(self, context):
        """
        Execute the workflow and handle response. Possible reauthentication problems with
        the items are handled by putting the processing on hold until the correct 
        authentication of the items can be confirmed again. 

        Errors originating from empty datacubes can occur due to tiling and need to be
        handled. Two different errors are possible: EmptyDataErrors and ValueErrors, both
        originating from datacube.retrieve() and often related to the option trim=True
        being set in the datacube config or the parse_extent function.
        """
        run_workflow = True
        retrieval_count = 0
        while run_workflow:
            retrieval_count += 1
            retrieval_error = False
            # check validity of datacube items (= Are items authenticated?)
            on_hold_count = 0
            while self.stop_flag:
                time.sleep(1)
                now = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(time.time())
                )
                if not on_hold_count:
                    print(f"{now}: Execution paused due to resign_error.")
                on_hold_count += 1
            if on_hold_count:
                print(f"{now}: Execution continued after resign_error.")
            # run actual workflow
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                try:
                    qp = QueryProcessor.parse(**context)
                    response = qp.optimize().execute()
                except exceptions.EmptyDataError:
                    response = None
                except AssertionError as e:
                    if "Empty reader_table" in str(e):
                        response = None
                    else:
                        raise
                except ValueError as e:
                    if "zero-size array" in str(e):
                        response = None
                    else:
                        raise
                except Exception:
                    retrieval_error = True
            # check validity of datacube items again
            run_workflow = False
            if self.stop_flag:
                err_msg = "Execution will be repeated due to resign error."
            elif retrieval_error:
                err_msg = "Execution will be repeated due to retrieval error."
            else:
                err_msg = None
            if err_msg:
                if retrieval_count == 1:
                    now = time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(time.time())
                    )
                    print(
                        f"{now}: {err_msg}",
                        flush=True
                    )
                run_workflow = True
                time.sleep(60)
        # return result
        return response

    def _merge_spatial(src_arrs, crs, res):
        """Merges spatially stratified results into an array"""
        # get dimensions of input array
        arr_dims = list(src_arrs[0].dims)
        # remove spatial dimensions
        if sq.dimensions.X in arr_dims:
            arr_dims.remove(sq.dimensions.X)
        if sq.dimensions.Y in arr_dims:
            arr_dims.remove(sq.dimensions.Y)
        # check if 3D input arrays
        if len(arr_dims):
            # possibly remaining dimension is the temporal one (e.g. year, season, etc)
            # retrieve temporal values
            time_dim = arr_dims[0]
            time_vals = [x[time_dim] for x in src_arrs]
            time_vals = np.unique(xr.concat(time_vals, dim=time_dim).values)
            # for each timestep merge results spatially first
            arrs_main = []
            for time_val in time_vals:
                arrs_sub = []
                for arr in src_arrs:
                    # slice array for given timestep
                    try:
                        arr_slice = arr.sel(**{time_dim: time_val})
                    except KeyError:
                        continue
                    # introduce band coordinate as index variable
                    new_arr = TileHandler._add_band_idx(arr_slice)
                    new_arr = new_arr.rio.write_crs(crs)
                    new_arr = TileHandler._write_transform(new_arr, res)
                    arrs_sub.append(new_arr)
                # spatial merge
                merged_arr = merge_arrays(arrs_sub, crs=crs)
                merged_arr = merged_arr[0].drop_vars("band")
                # re-introducing time dimension
                coords = {}
                coords[time_dim] = ([time_dim], np.array([str(time_val)]))
                coords.update(
                    {dim: (dim, merged_arr[dim].values) for dim in merged_arr.dims}
                )
                new_arr = xr.DataArray(
                    data=np.expand_dims(merged_arr.values, 0),
                    coords=coords,
                    attrs=merged_arr.attrs,
                )
                new_arr = new_arr.rio.write_crs(crs)
                new_arr = TileHandler._write_transform(new_arr, res)
                arrs_main.append(new_arr)
            # merge across time
            joint_arr = xr.concat(arrs_main, dim=time_dim)
            # persist band names
            joint_arr.attrs["long_name"] = [str(x) for x in joint_arr[time_dim].values]
            joint_arr.attrs["band_variable"] = time_dim
        else:
            # direct spatial merge possible
            arrs = []
            for arr in src_arrs:
                # introduce band coordinate as index variable
                new_arr = TileHandler._add_band_idx(arr)
                new_arr = new_arr.rio.write_crs(crs)
                new_arr = TileHandler._write_transform(new_arr, res)
                arrs.append(new_arr)
            # spatial merge
            joint_arr = merge_arrays(arrs, crs=crs)
            joint_arr = joint_arr[0].drop_vars("band")
        return joint_arr

    def _merge_temporal(src_arrs):
        """Merges temporally stratified results into an array"""
        if isinstance(src_arrs[0], xr.core.dataarray.DataArray):
            # merge across time
            dst_arr = xr.concat(src_arrs, dim=sq.dimensions.TIME)
        elif isinstance(src_arrs[0], Collection):
            dst_arrs = []
            # merge collection results
            for collection in src_arrs:
                grouper_vals = [x.name for x in collection]
                arr = xr.concat([x for x in collection], dim="grouper")
                arr = arr.assign_coords(grouper=grouper_vals)
                dst_arrs.append(arr)
            # merge across time
            dst_arr = xr.concat(dst_arrs, dim=sq.dimensions.TIME)
        else:
            raise NotImplementedError(f"No method for merging source array {src_arrs}.")
        return dst_arr

    def _postprocess_spatial(self, in_dict):
        """Postprocesses the response to ensure homogeneous response format,
        i.e. a dictionary containing xarrays with at most 3 dimensions
        """
        out_dict = {}
        for k in in_dict.keys():
            in_arr = in_dict[k]
            # convert collections (grouped outputs) into arrays
            # problematic since grouper variable may exist already
            # needed for grouped outputs!
            if isinstance(in_arr, Collection):
                grouper_vals = [x.name for x in in_arr]
                if isinstance(grouper_vals[0], tuple):
                    grouper_vals = [str(x) for x in grouper_vals]
                in_arr = xr.concat([x for x in in_arr], dim="_grouper")
                in_arr = in_arr.assign_coords(_grouper=grouper_vals)
            # add crs information
            in_arr = in_arr.rio.write_crs(self.crs)
            # flatten 4D outputs to 3D
            re_dims = TileHandler._get_nonspatial_dims(in_arr)
            if len(re_dims) > 1:
                in_arr = in_arr.stack(grouper=re_dims)
                in_arr = in_arr.transpose("grouper", ...)
            # persist band names for 3D outputs
            re_dims = TileHandler._get_nonspatial_dims(in_arr)
            if len(re_dims):
                re_dim = re_dims[0]
                re_vals = [str(x) for x in in_arr[re_dim].values]
                in_arr.attrs["long_name"] = re_vals
                in_arr.attrs["band_variable"] = re_dim
                # merge multilevel-indices into single-level indices
                coords = {}
                coords.update({dim: (dim, in_arr[dim].values) for dim in in_arr.dims})
                coords[re_dim] = (re_dim, in_arr.attrs["long_name"])
                in_arr = xr.DataArray(
                    data=in_arr.values,
                    coords=coords,
                    attrs=in_arr.attrs,
                )
                in_arr = in_arr.rio.write_crs(self.crs)
            # add spatial information if missing
            in_arr = TileHandler._write_transform(in_arr, self.spatial_resolution)
            out_dict[k] = in_arr
        return out_dict

    def _postprocess_temporal(self, in_dict):
        """Postprocesses the response to ensure homogeneous response format"""
        out_dict = {}
        for k in in_dict.keys():
            in_arr = in_dict[k]
            # convert collections (grouped outputs) into arrays
            if isinstance(in_arr, Collection):
                grouper_vals = [x.name for x in in_arr]
                in_arr = xr.concat([x for x in in_arr], dim="_grouper")
                in_arr = in_arr.assign_coords(_grouper=grouper_vals)
            out_dict[k] = in_arr
        return out_dict

    @staticmethod
    def _add_band_idx(in_arr):
        """Introduce band coordinate as index variable."""
        coords = {}
        coords["band"] = (["band"], np.array([1]))
        coords.update({dim: (dim, in_arr[dim].values) for dim in in_arr.dims})
        out_arr = xr.DataArray(
            data=np.expand_dims(in_arr.values, 0),
            coords=coords,
            attrs=in_arr.attrs,
        )
        return out_arr

    @staticmethod
    def create_spatial_grid(
        space, spatial_resolution, chunksize_s, crs, precise=True, verbose=True
    ):
        # create coarse spatial grid
        coarse_res = list(np.array(spatial_resolution) * chunksize_s)
        extent = space.rasterize(coarse_res, crs, all_touched=True)
        # get spatial spacings from coarse grid
        bounds = extent.rio.bounds()
        x_min, x_max = bounds[0], bounds[2]
        y_min, y_max = bounds[1], bounds[3]
        x_spacing = np.linspace(x_min, x_max, len(extent.x) + 1)
        y_spacing = np.linspace(y_min, y_max, len(extent.y) + 1)
        # construct sub-ranges
        _spatial_grid = list(
            product(
                [x for x in zip(x_spacing, x_spacing[1:])],
                [y for y in zip(y_spacing, y_spacing[1:])],
            )
        )
        _spatial_grid = [[x[0], y[0], x[1], y[1]] for x, y in _spatial_grid]
        # preprocess - get proportion of half pixel as min size for overlap
        pxl_area = np.multiply(*np.abs(spatial_resolution))
        tile_area = box(*_spatial_grid[0]).area
        ovlp_thres = 0.5 * pxl_area / tile_area
        # preprocess - create polygons for point & linestring features
        pxl_radius = np.sqrt(pxl_area / np.pi)
        space = space.features.to_crs(extent.rio.crs)
        space.geometry = space.geometry.apply(
            lambda x: x.buffer(pxl_radius) if not x.area else x
        )
        # preprocess - create spatial index for performance improvements
        space_sindex = space.sindex
        # filter & mask tiles for shape geometry
        spatial_grid = []
        bbox_tile = gpd.GeoDataFrame(index=[0], columns=["geometry"], crs=crs)
        for tile in tqdm(
            _spatial_grid,
            disable=not verbose,
            total=len(_spatial_grid),
            desc="creating spatial grid",
        ):
            # construct gdf for given tile
            bbox_tile.at[0, "geometry"] = box(*tile)
            # pre-filter possible matches (pm) based on spatial index
            pm_idxs = list(space_sindex.intersection(bbox_tile.geometry[0].bounds))
            pms = space.iloc[pm_idxs]
            pms = pms[pms.intersects(bbox_tile.geometry[0])]
            # precise overlay tile with SpatialExtent
            if not pms.empty:
                overlay_kwargs = {
                    "right": space,
                    "how": "intersection",
                    "keep_geom_type": False,
                }
                tile_shape = bbox_tile.overlay(**overlay_kwargs)
                # remove sliver linestrings & point geoms
                tile_shape = tile_shape.explode(index_parts=True)
                keep_idx = tile_shape["geometry"].geom_type == "Polygon"
                tile_shape = tile_shape[keep_idx].dissolve()
                # evaluate overlay to decide if tile is included
                if precise:
                    if ((tile_shape.area / bbox_tile.area) >= ovlp_thres).iloc[0]:
                        spatial_grid.append(SpatialExtent(tile_shape))
                else:
                    if space.intersects(bbox_tile.unary_union).any():
                        if ((tile_shape.area / bbox_tile.area) >= ovlp_thres).iloc[0]:
                            spatial_grid.append(SpatialExtent(bbox_tile))
        return spatial_grid

    @staticmethod
    def create_temporal_grid(t_start, t_end, chunksize_t):
        time_grid = pd.date_range(t_start, t_end, freq=chunksize_t)
        time_grid = (
            [pd.Timestamp(t_start), *time_grid]
            if t_start not in time_grid
            else time_grid
        )
        time_grid = (
            [*time_grid, pd.Timestamp(t_end)]
            if t_end not in time_grid
            else time_grid
        )
        time_grid = [x for x in zip(time_grid, time_grid[1:])]
        time_grid = [TemporalExtent(*t) for t in time_grid]
        return time_grid

    @staticmethod
    def get_op_dims(recipe_piece, dims=None):
        """Retrieves the dimensions over which operations take place.
        All operations indicated by verbs (such as reduce, groupby, etc) are considered.
        """
        if dims is None:
            dims = []
        if isinstance(recipe_piece, dict):
            # check if this dictionary matches the criteria
            if recipe_piece.get("type") == "verb":
                dim = recipe_piece.get("params").get("dimension")
                if dim:
                    dims.append(dim)
            # recursively search for values
            for value in recipe_piece.values():
                TileHandler.get_op_dims(value, dims)
        elif isinstance(recipe_piece, list):
            # if it's a list apply the function to each item in the list
            for item in recipe_piece:
                TileHandler.get_op_dims(item, dims)
        # categorise used dimensions into temporal & spatial dimensions
        dim_lut = {
            sq.dimensions.TIME: sq.dimensions.TIME,
            sq.dimensions.SPACE: sq.dimensions.SPACE,
        }
        dim_lut.update(
            {
                x: sq.dimensions.TIME
                for x in TileHandler._get_class_components(sq.components.time).values()
            }
        )
        dim_lut.update(
            {
                x: sq.dimensions.SPACE
                for x in TileHandler._get_class_components(sq.components.space).values()
            }
        )
        _dims = []
        for x in dims:
            try:
                _dims.append(dim_lut[x])
            except KeyError:
                pass
        dims = list(np.unique(_dims))
        return dims

    @staticmethod
    def _get_class_components(class_obj):
        """
        Function to get all components of the class along with their values
        """
        components = {}
        for attribute in dir(class_obj):
            if not attribute.startswith("__") and not callable(
                getattr(class_obj, attribute)
            ):
                components[attribute] = getattr(class_obj, attribute)
        return components

    @staticmethod
    def _get_nonspatial_dims(in_arr):
        arr_dims = list(in_arr.dims)
        if sq.dimensions.X in arr_dims:
            arr_dims.remove(sq.dimensions.X)
        if sq.dimensions.Y in arr_dims:
            arr_dims.remove(sq.dimensions.Y)
        return arr_dims

    @staticmethod
    def _write_to_origin(arr, path):
        """
        Write an opened rioxarray back to its original path,
        circumvents permission errors by temporarily writing to a new path
        & renaming afterwards
        """
        suffix = str(uuid.uuid4()).replace("-", "_")
        base, ext = os.path.splitext(path)
        temp_path = f"{base}_{suffix}_{ext}"
        arr.rio.to_raster(temp_path)
        shutil.move(temp_path, path)

    @staticmethod
    def _write_transform(arr, res):
        """
        Adds rio metadata if array is of size 1x1,
        bounds and resolution are written

        Args:
            arr (xarray): Array
            res (tuple/list): Resolution in the order of [y, x]
        """
        if len(arr.x) == 1 and len(arr.y) == 1:
            x_coord, y_coord = (
                arr.x.values[0],
                arr.y.values[0],
            )
            transform = rio.transform.from_origin(
                west=x_coord - (res[1] / 2),
                north=y_coord + (res[0] / 2),
                xsize=res[1],
                ysize=-res[0],
            )
            arr.rio.write_transform(transform, inplace=True)
        return arr


class TileHandlerParallel(TileHandler):
    """Handler for executing a query in exhaustive multiprocessing manner.
    Contrary to the TileHandler, which only parallelises data loading, the
    TileHandlerParallel class allows to parallelise the recipe execution, too.
    The number of used    
    
    Parameters
    ----------
      args: dict
        TileHandler arguments, see class defintion of TileHandler
      n_cores : int, optional
        The number of cores to be used for parallel processing.
    """

    def __init__(self, *args, n_procs=os.cpu_count(), **kwargs):
        # reauth is not serializable -> disable
        # reauth will be done individually prior to data loading
        kwargs["reauth"] = False
        super().__init__(*args, **kwargs)
        self.n_procs = n_procs
        self.preview()


    def execute(self):
        """Main function to distribute tasks to worker processes."""
        # Get grid idxs
        grid_idxs = np.arange(len(self.grid))

        # Pool setup: create n_procs workers, each initialized with its own copy of self
        with Pool(
            processes=self.n_procs,
            initializer=worker_initializer,
            initargs=(self,)
        ) as pool:
            tile_results = list(
                tqdm(
                    pool.imap_unordered(worker_task, grid_idxs, chunksize=1),
                    total=len(grid_idxs),
                    smoothing=0.1,
                )
            )
            pool.close()
            pool.join()

        # Filter None results
        tile_results = [x for x in tile_results if x is not None]

        # Merge results as needed
        if tile_results:
            if self.merge_mode:
                if self.merge_mode == "merged":
                    self.tile_results = tile_results
                    self.merge_single()
                elif "vrt" in self.merge_mode:
                    self.tile_results = [x for sl in tile_results for x in sl]
                    self.merge_vrt()
            else:
                self.tile_results = tile_results


class PersistentWorker:
    def __init__(self, self_copy):
        """Initialize the worker process with its own copy of the self object."""
        self.th = self_copy
        self.th.datacube.config["dask_params"] = {"scheduler": "single-threaded"}
        self.th.datacube.config["reauth_individual"] = True

    def process_tile(self, tile_idx):
        """Process a single tile using the already initialized self and datacube."""
        # Create the context for the specific tile
        context_params = {
            **{self.th.tile_dim: self.th.grid[tile_idx]},
            "cache": self.th.cache,
            "datacube": self.th.datacube,
        }
        context = self.th._create_context(**context_params)

        # Evaluate the recipe
        response = self.th._execute_workflow(context)

        # Handle response and postprocess if necessary
        try:
            if response:
                if not self.th.merge_mode:
                    return response
                else:
                    if self.th.tile_dim == sq.dimensions.TIME:
                        response = self.th._postprocess_temporal(response)
                    elif self.th.tile_dim == sq.dimensions.SPACE:
                        response = self.th._postprocess_spatial(response)

                    # Write result (in-memory or to disk)
                    if self.th.merge_mode == "merged":
                        return response
                    else:
                        out = []
                        for layer in response.keys():
                            out_dir = os.path.join(self.th.out_dir, layer)
                            out_path = os.path.join(out_dir, f"{tile_idx}.tif")
                            os.makedirs(out_dir, exist_ok=True)
                            layer = response[layer].rio.write_crs(self.th.crs)
                            layer.rio.to_raster(out_path)
                            out.append(out_path)
                        return out
        finally:
            del context, context_params
            gc.collect()

def worker_initializer(self_copy):
    """Initializer for worker processes: creates a PersistentWorker instance."""
    global worker_instance
    worker_instance = PersistentWorker(self_copy)

def worker_task(tile_idx):
    """Function that each worker process will call for each task."""
    global worker_instance
    return worker_instance.process_tile(tile_idx)