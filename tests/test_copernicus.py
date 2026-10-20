"""Copernicus hors réseau : raster GeoTIFF en mémoire sur la grille CLMS officielle."""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import numpy as np

from soil_hydrology.cache import Cache
from soil_hydrology.copernicus import (ORIGIN_LAT, ORIGIN_LON, PIXEL_DEG, SSM, SWI, CopernicusProvider, Pixel,
                                       decode, latest_per_day, parse_product, read_pixels)
from soil_hydrology.models import Query

PIXELS = [Pixel(2821, 1842, 46.8080357, 5.4508929, .6874, 462.9), Pixel(2822, 1842, 46.7991071, 5.4508929, .3126, 535.0)]
ITEM = {"Id": "uuid-1", "Name": "c_gls_SWI1km_202610191200_CEURO_SCATSAR_V2.1.1_cog", "PublicationDate": "2026-10-20T15:27:46Z",
        "S3Path": "/eodata/CLMS/bio-geophysical/soil_water_index/swi_europe_1km_daily_v2/2026/10/19/c_gls_SWI1km_202610191200_CEURO_SCATSAR_V2.1.1_cog",
        "Online": True}


def raster(values: dict[tuple[int, int], int], transform=None):
    """GeoTIFF uint8 en mémoire couvrant les lignes 2815–2829, colonnes 1835–1849."""
    from rasterio.io import MemoryFile
    from rasterio.transform import Affine

    r0, c0 = 2815, 1835
    data = np.full((15, 15), 255, dtype="uint8")
    for (row, col), dn in values.items():
        data[row - r0, col - c0] = dn
    # Le fichier réel couvre toute l'Europe ; on reproduit sa géotransformée exacte.
    transform = transform or Affine(PIXEL_DEG, 0, ORIGIN_LON, 0, -PIXEL_DEG, ORIGIN_LAT)
    memfile = MemoryFile()
    full = np.full((r0 + 15, c0 + 15), 255, dtype="uint8")
    full[r0:, c0:] = data
    with memfile.open(driver="GTiff", height=full.shape[0], width=full.shape[1], count=1, dtype="uint8",
                      crs="EPSG:4326", transform=transform, nodata=255, tiled=True, blockxsize=256, blockysize=256) as ds:
        ds.write(full, 1)
    return memfile.open()


class Encoding(unittest.TestCase):
    def test_dn_to_percent_and_flags_are_never_values(self):
        self.assertEqual(decode(0), (0.0, "valid"))  # 0 % réel
        self.assertEqual(decode(200), (100.0, "valid"))
        self.assertEqual(decode(91), (45.5, "valid"))
        self.assertEqual(decode(241), (None, "ExceedingMin"))
        self.assertEqual(decode(254), (None, "LowQFLAG"))
        self.assertEqual(decode(255), (None, "NoData"))
        self.assertEqual(decode(220), (None, "invalid_dn_220"))
        self.assertEqual(decode(None), (None, "missing"))

    def test_product_name_parsing_and_latest_run(self):
        parsed = parse_product(ITEM)
        self.assertEqual(parsed["date"], date(2026, 10, 19))
        self.assertEqual(parsed["stem"].format(band="SWI010"), "c_gls_SWI1km-SWI010_202610191200_CEURO_SCATSAR_V2.1.1.tiff")
        newer = dict(ITEM, Id="uuid-2", Name=ITEM["Name"].replace("V2.1.1", "V2.1.2"))
        nc = dict(ITEM, Name=ITEM["Name"].replace("_cog", "_nc"))
        self.assertEqual(latest_per_day([ITEM, newer, nc])[date(2026, 10, 19)]["id"], "uuid-2")


class PixelRead(unittest.TestCase):
    def test_windowed_read_of_the_two_zp1_pixels(self):
        with raster({(2821, 1842): 120, (2822, 1842): 0}) as ds:
            self.assertEqual(read_pixels(ds, PIXELS), {"r2821_c1842": 120, "r2822_c1842": 0})

    def test_unexpected_grid_is_rejected(self):
        from rasterio.transform import Affine
        with raster({}, Affine(0.01, 0, -11, 0, -0.01, 72)) as ds:
            with self.assertRaises(ValueError):
                read_pixels(ds, PIXELS)


class ProviderTests(unittest.TestCase):
    def http(self):
        http = Mock()
        http.get.return_value = Mock(status_code=200, json=Mock(return_value={"value": [ITEM]}))
        return http

    def test_without_credentials_is_unavailable_auth_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = Mock()
            result = CopernicusProvider(SWI, Cache(Path(tmp) / "c.sqlite"), PIXELS, http=self.http(), env={},
                                        opener=opener).fetch(Query(46.8, 5.45, date(2026, 10, 15), date(2026, 10, 20)))
            self.assertEqual(result.status, "unavailable_auth")
            self.assertEqual(result.details["catalog_latest"], "2026-10-19")
            opener.assert_not_called()
            self.assertFalse(result.observations)

    def test_extraction_observations_flags_and_cache(self):
        values = {"SWI002": {(2821, 1842): 140, (2822, 1842): 254}, "QFLAG002": {(2821, 1842): 180, (2822, 1842): 40},
                  "SSF": {(2821, 1842): 1, (2822, 1842): 1}}
        opener = Mock(side_effect=lambda item, band: raster(values.get(band, {(2821, 1842): 100, (2822, 1842): 100})))
        env = {"CDSE_S3_ACCESS_KEY": "x", "CDSE_S3_SECRET_KEY": "y"}
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / "c.sqlite")
            query = Query(46.8, 5.45, date(2026, 10, 19), date(2026, 10, 19))
            result = CopernicusProvider(SWI, cache, PIXELS, http=self.http(), env=env, opener=opener).fetch(query)
            self.assertEqual(result.status, "available")
            by = {(o.grid_id, o.original_variable): o for o in result.observations}
            main = by["r2821_c1842", "SWI002"]
            self.assertEqual((main.value, main.unit, main.temporal_window, main.metadata["qflag_pct"]), (70.0, "%", "T=2", 90.0))
            flagged = by["r2822_c1842", "SWI002"]
            self.assertEqual((flagged.value, flagged.quality), (None, "LowQFLAG"))
            self.assertEqual(len(result.observations), 2 * len(SWI.bands))
            calls = opener.call_count
            again = CopernicusProvider(SWI, cache, PIXELS, http=self.http(), env=env, opener=opener).fetch(query)
            self.assertEqual(opener.call_count, calls)  # relu depuis le cache
            self.assertEqual(len(again.observations), len(result.observations))

    def test_ssm_product_is_separate_variable(self):
        opener = Mock(side_effect=lambda item, band: raster({(2821, 1842): 60, (2822, 1842): 255}))
        item = dict(ITEM, Name="c_gls_SSM1km_202610190000_CEURO_S1CSAR_V1.2.1_cog")
        http = Mock()
        http.get.return_value = Mock(status_code=200, json=Mock(return_value={"value": [item]}))
        with tempfile.TemporaryDirectory() as tmp:
            result = CopernicusProvider(SSM, Cache(Path(tmp) / "c.sqlite"), PIXELS, http=http,
                                        env={"CDSE_USERNAME": "u", "CDSE_PASSWORD": "p"}, opener=opener).fetch(
                Query(46.8, 5.45, date(2026, 10, 19), date(2026, 10, 19)))
            self.assertEqual({o.variable for o in result.observations}, {"ssm_copernicus"})
            self.assertEqual(result.observations[0].value, 30.0)
            self.assertEqual(result.observations[0].metadata["noise"], 30.0)


if __name__ == "__main__":
    unittest.main()
