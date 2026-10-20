"""Copernicus hors réseau : raster GeoTIFF en mémoire sur la grille CLMS officielle."""
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from soil_hydrology.cache import Cache
from soil_hydrology.copernicus import (MAX_CONSECUTIVE_FAILURES, ORIGIN_LAT, ORIGIN_LON, PIXEL_DEG, SSM, SWI,
                                       CopernicusProvider, Pixel, S3Reader,
                                       decode, latest_per_day, parse_product, read_pixels)
from soil_hydrology.freshness import assess
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


S3_ENV = {"CDSE_S3_ACCESS_KEY": "test-access", "CDSE_S3_SECRET_KEY": "test-secret"}
QUERY = Query(46.8, 5.45, date(2026, 10, 19), date(2026, 10, 19))


def catalog_item(day: int, product="SWI"):
    stamp = f"202610{day:02d}" + ("1200" if product == "SWI" else "0000")
    name = (f"c_gls_SWI1km_{stamp}_CEURO_SCATSAR_V2.1.1_cog" if product == "SWI"
            else f"c_gls_SSM1km_{stamp}_CEURO_S1CSAR_V1.2.1_cog")
    folder = "soil_water_index/swi_europe_1km_daily_v2" if product == "SWI" else "surface_soil_moisture/ssm_europe_1km_daily_v1"
    return dict(ITEM, Id=f"uuid-{day}", Name=name, S3Path=f"/eodata/CLMS/bio-geophysical/{folder}/2026/10/{day:02d}/{name}")


class FakeClientError(Exception):
    """Même structure que botocore.exceptions.ClientError (attribut `response`)."""

    def __init__(self, code, status):
        super().__init__(f"An error occurred ({code}) when calling the HeadObject operation")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


class FakeBoto3:
    """Module boto3 minimal : enregistre sessions, clients et HEAD."""

    def __init__(self, head_error=None):
        self.sessions, self.clients, self.heads, self.head_error = [], [], [], head_error

    def Session(self, **kwargs):
        fake = self

        class Creds:
            def get_frozen_credentials(self):
                return Mock(access_key=kwargs["aws_access_key_id"], secret_key=kwargs["aws_secret_access_key"], token=None)

        class Session:
            region_name = kwargs.get("region_name")

            def get_credentials(self):
                return Creds()

            def client(self, service, **options):
                fake.clients.append((service, options))
                client = Mock()

                def head_object(**params):
                    fake.heads.append(params)
                    if fake.head_error:
                        raise fake.head_error
                    return {"ContentLength": 8864075}
                client.head_object.side_effect = head_object
                return client

        self.sessions.append(kwargs)
        return Session()


class ProviderTests(unittest.TestCase):
    def http(self, items):
        http = Mock()
        http.get.return_value = Mock(status_code=200, json=Mock(return_value={"value": items}))
        return http

    def provider(self, tmp, items, boto3=None, opener=None, product=SWI, query=QUERY):
        reader = S3Reader("test-access", "test-secret", boto3_module=boto3 or FakeBoto3())
        provider = CopernicusProvider(product, Cache(Path(tmp) / "c.sqlite"), PIXELS, http=self.http(items),
                                      env=S3_ENV, reader=reader, opener=opener)
        return provider, provider.fetch(query)

    def test_without_credentials_is_unavailable_auth_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            opener = Mock()
            result = CopernicusProvider(SWI, Cache(Path(tmp) / "c.sqlite"), PIXELS, http=self.http([ITEM]), env={},
                                        opener=opener).fetch(Query(46.8, 5.45, date(2026, 10, 15), date(2026, 10, 20)))
            self.assertEqual(result.status, "unavailable_auth")
            self.assertEqual(result.details["catalog_latest"], "2026-10-19")
            self.assertEqual(assess("copernicus_swi", result.status, None, datetime.now(timezone.utc))["status"], "UNAVAILABLE_AUTH")
            opener.assert_not_called()

    def test_env_uses_awssession_never_gdal_key_options(self):
        from rasterio.session import AWSSession
        reader = S3Reader("test-access", "test-secret", boto3_module=FakeBoto3())
        with patch("rasterio.Env") as env:
            reader.env()
        options = env.call_args.kwargs
        self.assertNotIn("AWS_ACCESS_KEY_ID", options)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", options)
        self.assertIsInstance(options["session"], AWSSession)
        self.assertEqual((options["AWS_S3_ENDPOINT"], options["AWS_VIRTUAL_HOSTING"], options["AWS_HTTPS"]),
                         ("eodata.dataspace.copernicus.eu", "FALSE", "YES"))
        with reader.env():  # le vrai rasterio.Env accepte cette configuration (EnvError auparavant)
            pass

    def test_awssession_construction_and_cdse_endpoint(self):
        fake = FakeBoto3()
        reader = S3Reader("test-access", "test-secret", boto3_module=fake)
        with patch("rasterio.Env") as env:
            reader.env()
        options = env.call_args.kwargs["session"].get_credential_options()
        self.assertEqual((options["AWS_S3_ENDPOINT"], options["AWS_REGION"]), ("eodata.dataspace.copernicus.eu", "default"))
        self.assertEqual(fake.sessions[0], {"aws_access_key_id": "test-access", "aws_secret_access_key": "test-secret",
                                            "region_name": "default"})
        item = parse_product(catalog_item(19))
        self.assertIn("OK", "OK " + reader.preflight(item, "SWI002"))
        self.assertEqual(fake.clients[0], ("s3", {"endpoint_url": "https://eodata.dataspace.copernicus.eu", "region_name": "default"}))
        self.assertEqual(fake.heads[0]["Bucket"], "eodata")
        self.assertEqual(fake.heads[0]["Key"], "CLMS/bio-geophysical/soil_water_index/swi_europe_1km_daily_v2/2026/10/19/"
                         "c_gls_SWI1km_202610191200_CEURO_SCATSAR_V2.1.1_cog/c_gls_SWI1km-SWI002_202610191200_CEURO_SCATSAR_V2.1.1.tiff")
        self.assertEqual(reader.path(item, "SWI002"), "s3://eodata/" + fake.heads[0]["Key"])

    def test_boto3_403_is_unavailable_auth(self):
        opener = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            _, result = self.provider(tmp, [catalog_item(19)], FakeBoto3(FakeClientError("403", 403)), opener)
        self.assertEqual(result.status, "unavailable_auth")
        self.assertTrue(result.details["s3_preflight"].startswith("KO"))
        self.assertIn("refusées", result.messages[0])
        self.assertNotIn("test-secret", " ".join(result.messages))
        opener.assert_not_called()

    def test_global_session_error_fails_fast_once(self):
        items = [catalog_item(d) for d in range(1, 31)]
        opener = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch.object(S3Reader, "env", side_effect=RuntimeError("GDAL indisponible")):
            _, result = self.provider(tmp, items, opener=opener, query=Query(46.8, 5.45, date(2026, 10, 1), date(2026, 10, 30)))
        self.assertEqual(result.status, "temporary_error")
        self.assertEqual(len(result.messages), 1)
        opener.assert_not_called()

    def test_preflight_ok_but_rasterio_error_is_error_and_stops(self):
        items = [catalog_item(d) for d in range(1, 31)]
        opener = Mock(side_effect=OSError("not recognized as a supported file format"))
        with tempfile.TemporaryDirectory() as tmp:
            _, result = self.provider(tmp, items, opener=opener, query=Query(46.8, 5.45, date(2026, 10, 1), date(2026, 10, 30)))
        self.assertTrue(result.details["s3_preflight"].startswith("OK"))
        self.assertEqual(result.status, "temporary_error")
        self.assertEqual(assess("copernicus_swi", result.status, None, datetime.now(timezone.utc))["status"], "ERROR")
        self.assertEqual(opener.call_count, MAX_CONSECUTIVE_FAILURES)  # pas 30 tentatives
        self.assertEqual(len(result.messages), 1)
        self.assertIn("27 produit(s) non tenté(s)", result.messages[0])

    def test_isolated_product_error_does_not_stop_other_days(self):
        def opener(path):
            if "202610181200" in path:
                raise OSError("tuile corrompue")
            return raster({(2821, 1842): 120, (2822, 1842): 100})
        items = [catalog_item(d) for d in (17, 18, 19)]
        with tempfile.TemporaryDirectory() as tmp:
            _, result = self.provider(tmp, items, opener=opener, query=Query(46.8, 5.45, date(2026, 10, 17), date(2026, 10, 19)))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.details["days_read"], 2)
        self.assertEqual({o.date for o in result.observations}, {"2026-10-17", "2026-10-19"})
        self.assertEqual(len(result.messages), 1)

    def test_preflight_ok_and_valid_raster_extracts_values_and_caches(self):
        values = {"SWI002": {(2821, 1842): 140, (2822, 1842): 254}, "QFLAG002": {(2821, 1842): 180, (2822, 1842): 40},
                  "SSF": {(2821, 1842): 1, (2822, 1842): 1}}

        def opener(path):
            band = path.rsplit("-", 1)[1].split("_")[0]
            return raster(values.get(band, {(2821, 1842): 100, (2822, 1842): 100}))
        opener = Mock(side_effect=opener)
        with tempfile.TemporaryDirectory() as tmp:
            provider, result = self.provider(tmp, [catalog_item(19)], opener=opener)
            self.assertEqual((result.status, result.details["days_read"]), ("available", 1))
            self.assertTrue(result.details["s3_preflight"].startswith("OK : HEAD s3://eodata/CLMS/"))
            self.assertTrue(all(c.args[0].startswith("s3://eodata/CLMS/") for c in opener.call_args_list))
            by = {(o.grid_id, o.original_variable): o for o in result.observations}
            main = by["r2821_c1842", "SWI002"]
            self.assertEqual((main.value, main.unit, main.temporal_window, main.metadata["qflag_pct"]), (70.0, "%", "T=2", 90.0))
            flagged = by["r2822_c1842", "SWI002"]
            self.assertEqual((flagged.value, flagged.quality), (None, "LowQFLAG"))
            self.assertEqual(len(result.observations), 2 * len(SWI.bands))
            calls = opener.call_count
            again = provider.fetch(QUERY)
            self.assertEqual(opener.call_count, calls)  # relu depuis le cache
            self.assertEqual(again.details["days_from_cache"], 1)

    def test_ssm_product_is_separate_variable(self):
        opener = Mock(side_effect=lambda path: raster({(2821, 1842): 60, (2822, 1842): 255}))
        with tempfile.TemporaryDirectory() as tmp:
            _, result = self.provider(tmp, [catalog_item(19, "SSM")], opener=opener, product=SSM)
        self.assertEqual({o.variable for o in result.observations}, {"ssm_copernicus"})
        self.assertEqual(result.observations[0].value, 30.0)
        self.assertEqual(result.observations[0].metadata["noise"], 30.0)
        self.assertIsNone(result.observations[1].value)  # 255 NoData, jamais 0


if __name__ == "__main__":
    unittest.main()
