import aiohttp
import asyncio
import nest_asyncio
import numpy as np
import pandas as pd
import pystac
import os
import shutil
import stac_asset
import time
from aiohttp_retry import RetryClient, ExponentialRetry
from copy import deepcopy
from datetime import datetime
from semantique.datacube import STACCube
from shapely.geometry import shape
from shapely.ops import unary_union
from stac_asset.http_client import HttpClient
from stac_asset.planetary_computer_client import PlanetaryComputerClient
from tqdm import tqdm
from tempfile import TemporaryDirectory


class Downloader:
    def __init__(self, item_coll, out_dir=None, **kwargs):
        """
        Downloader class tailored to the needs of handling single &
        multi-collection outputs as generated by the Finder. Results will
        be downloaded & a STAC-conformant metadata description (catalog,
        collection and item level) with corresponding relative local links
        will be established.

        Note: The created STAC metadata json are containing all the originally
        provided information of the input item_coll, incl. for example the extra
        asset field "semantique:key". Apart from the updated links, the STAC
        metadata json is therefore equivalent to the input item_coll.

        Args:
            item_coll (pystac.ItemCollection or list of pystac.item.Item):
                The Finder result (search.py) to be downloaded.
            out_dir (str): The directory to download the files to. If not specified,
                a new directory will be created with the current timestamp.
            **kwargs (dict): Keyword arguments forwarded to _STACDownloader.
        """
        self.item_coll = item_coll
        self.kwargs = kwargs
        if not out_dir:
            self.out_dir = f"data_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        else:
            self.out_dir = out_dir
        os.makedirs(self.out_dir, exist_ok=True)

    def run(self, by_collection=True):
        """
        Runs the download process.

        Args:
            by_collection (bool): Whether to download items in a grouped manner.
        """
        # download process
        if by_collection:
            self._download_grouped()
        else:
            self._download_ungrouped()
        # creation of STAC metadata catalogs
        catalog_name = "root_catalog"
        catalog_desc = "Root catalog containing multiple collections."
        self._create_and_save_catalog(self.out_dir, catalog_name, catalog_desc)

    def _create_and_save_catalog(self, root_dir, catalog_name, catalog_desc):
        """
        Creates and saves a STAC catalog from item collections either in the root or subdirectories.

        Args:
            root_dir (str): Directory containing item collections and possibly other directories.
            catalog_name (str): Name for the new catalog.
            catalog_desc (str): Description for the new catalog.
        """
        output_path = root_dir
        catalog = pystac.Catalog(id=catalog_name, description=catalog_desc)
        catalog.set_self_href(os.path.join(output_path, "catalog.json"))

        # Check for an item collection directly in the root directory
        root_item_collection_path = os.path.join(root_dir, "item-collection.json")
        if os.path.exists(root_item_collection_path):
            self._load_and_add_collection(
                root_item_collection_path, output_path, catalog, ""
            )

        # Traverse each subdirectory representing a collection
        for coll_dir in [
            d
            for d in os.listdir(output_path)
            if os.path.isdir(os.path.join(output_path, d))
        ]:
            item_collection_path = os.path.join(
                output_path, coll_dir, "item-collection.json"
            )
            if os.path.exists(item_collection_path):
                self._load_and_add_collection(
                    item_collection_path,
                    output_path,
                    catalog,
                    os.path.join(output_path, coll_dir),
                )

        # Normalize asset hrefs relative to the catalog location and save it
        catalog.normalize_hrefs(output_path)
        catalog.make_all_asset_hrefs_relative()

        # tbd: this function here _create_and_save_catalog is slow
        # specifically: it scales exponentially with amount of items
        # Related issue: https://github.com/stac-utils/pystac/issues/1207
        catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)

    def _create_full_extent(self, stac_item_list):
        """
        Create the spatio-temporal extent of a collection based on the
        provided list of STAC items.
        """
        polygons = []
        datetimes = []
        for index, stac_item in enumerate(stac_item_list):
            geometry = stac_item.geometry
            polygons.append(shape(geometry))
            datetime = stac_item.get_datetime()
            datetimes.append(datetime)
        spatial_extent = self._get_spatial_extent(polygons)
        temporal_extent = self._get_temporal_extent(min(datetimes), max(datetimes))
        collection_extent = pystac.Extent(
            spatial=spatial_extent, temporal=temporal_extent
        )
        return collection_extent

    def _download_grouped(self):
        """
        Download items in a grouped manner, i.e. keeping items of same
        collections together in one collection folder.
        """
        # compile referenced assets, collections & product ids
        colls = [x.get_collection().id for x in self.item_coll]
        ids = [x.id for x in self.item_coll]
        items_df = pd.DataFrame({"collection": colls, "id": ids})
        # group to retrieve different collections to be downloaded separately
        grouped_df = (
            items_df.reset_index()
            .groupby("collection")
            .agg(lambda x: list(x))
            .reset_index()
            .assign(
                item=lambda df: df["index"].apply(
                    lambda x: [self.item_coll[i] for i in x]
                )
            )
        )
        # download individual collections
        stac_coll_paths = []
        for idx, row in grouped_df.iterrows():
            print(f"{row['collection']} (collection {idx+1}/{len(grouped_df)})")
            nest_asyncio.apply()
            item_coll = pystac.ItemCollection(row["item"])
            dwl = _STACDownloader(
                item_coll=item_coll,
                out_dir=os.path.join(self.out_dir, row["collection"]),
                **self.kwargs,
            )
            asyncio.run(dwl.run())
            stac_coll_paths.append(
                os.path.join(self.out_dir, row["collection"], "item-collection.json")
            )
            print("")

    def _download_ungrouped(self):
        """
        Download items in an ungrouped manner, i.e. all items are treated to
        belong to one collection and are downloaded into a single folder.
        """
        nest_asyncio.apply()
        dwl = _STACDownloader(
            item_coll=self.item_coll, out_dir=self.out_dir, **self.kwargs
        )
        asyncio.run(dwl.run())

    def _get_spatial_extent(self, polygons):
        """
        Create a spatial extent based on the provided list of polygons.
        """
        # fix ill-defined geometries using zero-buffer
        polygons = [p.buffer(0) for p in polygons]
        # find union of geometries
        unioned_geometry = unary_union(polygons)
        return pystac.SpatialExtent(bboxes=[unioned_geometry.bounds])

    def _get_temporal_extent(self, startime, endtime):
        """
        Create a temporal extent based on the provided list of polygons.
        """
        time_interval = [startime, endtime]
        temporal_extent = pystac.TemporalExtent(intervals=[time_interval])
        return temporal_extent

    def _load_and_add_collection(
        self, item_collection_path, output_path, catalog, coll_path
    ):
        """
        Load an item collection from a specified path and add it to a catalog.

        Args:
            item_collection_path (str): Path to the item collection JSON file.
            output_path (str): Output path for saving the catalog.
            catalog (pystac.Catalog): Catalog to add the collection to.
            coll_path (str): Path where the collection resides.
        """
        item_collection = pystac.ItemCollection.from_file(item_collection_path)
        # get spatial & temporal extent of collection
        stac_items = list(item_collection)
        extent = self._create_full_extent(stac_items)
        # create collection
        collection_id = os.path.basename(coll_path)
        collection = pystac.Collection(
            id=collection_id,
            description=f"Collection for {collection_id}",
            extent=extent,
        )
        collection.set_self_href(item_collection_path)
        # add items to the new collection
        for item in item_collection.items:
            item.set_self_href(os.path.join(coll_path, item.id + ".json"))
            collection.add_item(item)
        # add updated collection to the catalog
        catalog.add_child(collection)
        # remove original item-collection file
        os.remove(item_collection_path)


class _STACDownloader:
    def __init__(self, item_coll, assets=None, out_dir=None, retries=3, **kwargs):
        """
        Generic class downloading specified assets for a given item collection.

        Args:
            item_coll (pystac.ItemCollection or list of pystac.item.Item):
                The item collection to download.
            assets (list): A list of asset keys to download. Defaults to None,
                which downloads all assets.
            out_dir (str): The directory to download the files to. If not specified,
                a new directory will be created with the current timestamp.
            retries (int): The number of retries to attempt the whole download.
            **kwargs (dict): Keyword arguments forwarded to ._async_download().
        """
        self.item_coll = item_coll
        self.assets = assets
        self.retries = retries
        if not out_dir:
            self.out_dir = f"data_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        else:
            self.out_dir = out_dir
        self.kwargs = kwargs

    async def run(self):
        """
        Executes the download processes in a retry manner.
        """
        for i in range(self.retries):
            print(f"Download loop {i}")
            # Download & cleanup
            await self._async_download(**self.kwargs)
            self._remove_empty_items(self.out_dir)
            # Check if current item collection contains all items
            coll_path = os.path.join(self.out_dir, "item-collection.json")
            coll = pystac.ItemCollection.from_file(coll_path)
            ratio = len(coll) / len(self.item_coll)
            if ratio == 1.0:
                break
            elif i < self.retries - 1:
                print("Retry download to get all items.")
            else:
                print("Not all items retrieved. Please check the download process.")
        print(f"Downloaded items: {len(coll)}/{len(self.item_coll)}")
        print(f"Success rate: {ratio:.2%}")

    async def _async_download(self, preview_size=10, reauth_batch_size=1000):
        """
        Download the items in the item collection to the output directory asynchronously.

        Args:
            assets (list): A list of asset keys to download.
            preview_size (int): The number of items to download for the preview run.
                Used to estimate the size of the download.
            reauth_batch_size (int): The number of items to download in each batch in an
                asynchronous way. Used to control at which interval items are resigned.
                Items are resigned every reauth_batch_size items. Reauth_batch_size = 1
                implies that every item is resigned before being downloaded, and the
                download is done in a synchronous way. If reauth_batch_size is None,
                no reauthentication will be performed and all items will be downloaded
                in a completely asnychronous manner.
        """
        # Set up download parameters
        opt_retry = ExponentialRetry(attempts=self.retries)
        opt_timeout = aiohttp.client.ClientTimeout(total=1800)
        stac_config = dict(warn=True)
        if self.assets:
            stac_config["include"] = self.assets
        stac_config = stac_asset.Config(**stac_config)

        # Preview run to estimate size
        if len(self.item_coll) >= preview_size:
            print("Estimating size of download...")

            # Perform download for subsample
            with TemporaryDirectory() as temp_dir:

                # Subsample items for preview run
                np.random.seed(42)
                pre_coll = np.random.choice(
                    self.item_coll, size=preview_size, replace=False
                )
                pre_coll = pystac.ItemCollection(items=pre_coll)

                # Starting the progress bar / message handler
                messages = asyncio.Queue()
                message_handler_task = asyncio.create_task(
                    self._async_message_handling(messages, len(pre_coll), temp_dir)
                )

                # Downloading the item collection in batched manner
                batch_size = reauth_batch_size or len(pre_coll)
                for i in range(0, len(pre_coll), batch_size):
                    # create single batch
                    batch = pre_coll[i : i + batch_size]
                    # reauth 
                    if reauth_batch_size is not None:
                        signed = False
                        on_hold_count = 0
                        while not signed:
                            try:
                                batch = STACCube._sign_metadata(list(batch))
                                signed = True
                            except:
                                on_hold_count += 1
                                if on_hold_count == 1:
                                    now = time.strftime(
                                        "%Y-%m-%d %H:%M:%S",
                                        time.localtime(time.time())
                                    )
                                    print(f"{now}: Download paused due to resign_error.")
                                time.sleep(1)
                        if on_hold_count:
                            print(f"{now}: Download will be continued after resign_error.")
                    else:
                        batch = pystac.ItemCollection(items=batch)
                    batch = pystac.ItemCollection(deepcopy(batch))

                    await stac_asset.download_item_collection(
                        item_collection=batch,
                        directory=temp_dir,
                        keep_non_downloaded=False,
                        config=stac_config,
                        clients=[
                            HttpClient(
                                RetryClient(
                                    aiohttp.ClientSession(timeout=opt_timeout),
                                    retry_options=opt_retry,
                                ),
                                check_content_type=False,
                            ),
                            PlanetaryComputerClient(
                                RetryClient(
                                    aiohttp.ClientSession(timeout=opt_timeout),
                                    retry_options=opt_retry,
                                ),
                                check_content_type=False,
                            ),
                        ],
                        messages=messages,
                    )

                # Signal the message handler to stop
                await messages.put(None)
                await message_handler_task

                # Clean directory
                self._remove_empty_items(temp_dir)

                # Evaluate size
                n_items = len(os.listdir(temp_dir))
                mean_size = (
                    _STACDownloader._get_dir_size(temp_dir)
                    / n_items
                    * len(self.item_coll)
                )
                sub_dirs = [os.path.join(temp_dir, x.id) for x in pre_coll]
                std_size = np.std([_STACDownloader._get_dir_size(x) for x in sub_dirs])
                ci_size = 1.96 * std_size / ((n_items - 1) ** 0.5) * len(self.item_coll)
                print(
                    f"Estimated total size: {_STACDownloader._sizeof_fmt(mean_size)} \xb1 "
                    f"{_STACDownloader._sizeof_fmt(ci_size)} (95% confidence interval)"
                )
        else:
            print("Not enough items to estimate size. Skipping preview run.")

        # Starting the progress bar / message handler
        messages = asyncio.Queue()
        message_handler_task = asyncio.create_task(
            self._async_message_handling(messages, len(self.item_coll), self.out_dir)
        )

        # Downloading the item collection in batched manner
        batch_size = reauth_batch_size or len(self.item_coll)
        for i in range(0, len(self.item_coll), batch_size):
            # create single batch
            batch = self.item_coll[i : i + batch_size]
            # reauth 
            if reauth_batch_size is not None:
                signed = False
                on_hold_count = 0
                while not signed:
                    try:
                        batch = STACCube._sign_metadata(list(batch))
                        signed = True
                    except:
                        on_hold_count += 1
                        if on_hold_count == 1:
                            now = time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(time.time())
                            )
                            print(f"{now}: Download paused due to resign_error.")
                        time.sleep(1)
                if on_hold_count:
                    print(f"{now}: Download will be continued after resign_error.")
            else:
                batch = pystac.ItemCollection(items=batch)
            batch = pystac.ItemCollection(deepcopy(batch))

            # download batch
            await stac_asset.download_item_collection(
                item_collection=batch,
                directory=self.out_dir,
                keep_non_downloaded=False,
                file_name=f"item-collection-batch-{i}.json",
                config=stac_config,
                clients=[
                    HttpClient(
                        RetryClient(
                            aiohttp.ClientSession(timeout=opt_timeout),
                            retry_options=opt_retry,
                        ),
                        check_content_type=False,
                    ),
                    PlanetaryComputerClient(
                        RetryClient(
                            aiohttp.ClientSession(timeout=opt_timeout),
                            retry_options=opt_retry,
                        ),
                        check_content_type=False,
                    ),
                ],
                messages=messages,
            )

        # Signal the message handler to stop
        await messages.put(None)
        await message_handler_task

        # Merge the item collections
        coll_paths = [
            os.path.join(self.out_dir, f)
            for f in os.listdir(self.out_dir)
            if f.startswith("item-collection-batch-")
        ]
        all_items = []
        for f in coll_paths:
            item_collection = pystac.ItemCollection.from_file(f)
            all_items.extend(item_collection.items)
            os.remove(f)
        item_coll = pystac.ItemCollection(items=all_items)
        item_coll.save_object(os.path.join(self.out_dir, "item-collection.json"))

    async def _async_message_handling(
        self, messages, total_files, directory, interval=1
    ):
        """
        Handle messages from the download process and update progress bars.

        Args:
            messages (asyncio.Queue): The queue to receive messages from the download process.
            total_files (int): The total number of files to download.
            directory (str): The directory where the files are being downloaded.
            interval (int): The interval in seconds at which to update the progress bars.
        """
        size_bar = tqdm(
            total=None,
            desc="Downloading EO data",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            miniters=1,
        )
        # variables to keep track of progress
        last_checked = time.time()
        prev_size = 0

        # start the background task
        background_task = asyncio.create_task(
            self._calculate_dir_size(directory, interval)
        )

        while True:
            message = await messages.get()
            # check finished
            if message is None:
                break
            # update every interval seconds
            current_time = time.time()
            if current_time - last_checked >= interval:
                last_checked = current_time
                size_bar.update(self._dir_size - prev_size)
                prev_size = self._dir_size

        # cancel the background task when done
        background_task.cancel()

        # close the progress bars when done
        size_bar.close()

    async def _calculate_dir_size(self, directory, interval):
        """
        Calculate the size of the directory at regular intervals.

        Args:
            directory (str): The directory to calculate the size of.
            interval (int): The interval in seconds at which to calculate the size.
        """
        while True:
            self._dir_size = _STACDownloader._get_dir_size(directory)
            await asyncio.sleep(interval)

    def _remove_empty_items(self, out_path):
        """
        Remove empty item directories from the output directory.

        Args:
            out_path (str): The path to the output directory.
        """
        # find empty item dirs & delete them
        empty_dirs = _STACDownloader._find_empty_subdirs(out_path)
        for dir in empty_dirs:
            shutil.rmtree(dir)
        # update item collection
        coll_path = os.path.join(out_path, "item-collection.json")
        in_coll = pystac.ItemCollection.from_file(coll_path)
        all_items = [x.id for x in in_coll.items]
        rm_items = [os.path.split(x)[-1] for x in empty_dirs]
        for id in rm_items:
            all_items.remove(id)
        keep_items = [x for x in in_coll.items if x.id in all_items]
        out_coll = pystac.ItemCollection(items=keep_items)
        # write back to file
        out_coll.save_object(coll_path)

    @staticmethod
    def _find_empty_subdirs(directory):
        """
        Return a list of empty subdirectories within the given directory.

        Args:
            directory (str): The directory to search for empty subdirectories.
        """
        empty_dirs = []
        for dirpath, dirnames, filenames in os.walk(directory):
            if not dirnames and not filenames:
                empty_dirs.append(dirpath)
            for dirname in list(dirnames):
                full_path = os.path.join(dirpath, dirname)
                if not os.listdir(full_path):
                    empty_dirs.append(full_path)
                    dirnames.remove(dirname)
        return empty_dirs

    @staticmethod
    def _get_dir_size(directory):
        """Calculate the total size of files in the specified directory.

        Args:
            directory (str): The path to the directory whose size is to be calculated.

        Returns:
            int: Total size of files in the directory in bytes.
        """
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(directory):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                # skip if it is symbolic link
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
        return total_size

    @staticmethod
    def _sizeof_fmt(num, suffix="B"):
        """
        Convert a number of bytes to a human-readable format.

        Args:
            num (int): The number of bytes.
            suffix (str): The suffix to use for the unit.
        """
        for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
            if abs(num) < 1024.0:
                return f"{num:.1f}{unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f}Yi{suffix}"
