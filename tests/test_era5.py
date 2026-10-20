import importlib.util
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from soil_hydrology.cache import Cache
from soil_hydrology.era5 import Era5LandProvider, parse_dataset, read_download
from soil_hydrology.models import Query


@unittest.skipUnless(importlib.util.find_spec("xarray") and importlib.util.find_spec("netCDF4"), "Installer requirements-hydrology.txt pour les tests NetCDF")
class Era5Tests(unittest.TestCase):
    def dataset(self):
        import numpy as np
        import pandas as pd
        import xarray as xr
        times = pd.date_range("2023-12-31", periods=25, freq="h")
        return xr.Dataset({
            "swvl1": (("valid_time", "latitude", "longitude"), np.full((25, 1, 1), .3), {"units": "m**3 m**-3"}),
            "t2m": (("valid_time", "latitude", "longitude"), np.full((25, 1, 1), 273.15), {"units": "K"}),
            "tp": (("valid_time", "latitude", "longitude"), np.arange(25).reshape(25, 1, 1) / 1000, {"units": "m"}),
            "e": (("valid_time", "latitude", "longitude"), np.full((25, 1, 1), -.003), {"units": "m"}),
        }, coords={"valid_time": times, "latitude": [46.8], "longitude": [5.5]})

    def query(self):
        return Query(46.803906, 5.450160, date(2023, 12, 31), date(2023, 12, 31))

    def test_units_depth_grid_and_accumulation_at_year_boundary(self):
        values = {o.variable: o for o in parse_dataset(self.dataset(), self.query(), "2024-01-05T00:00:00+00:00")}
        self.assertAlmostEqual(values["soil_water"].value, .3)
        self.assertEqual(values["soil_water"].depth_max_cm, 7)
        self.assertEqual(values["soil_water"].grid_longitude, 5.5)
        self.assertEqual(values["temperature"].value, 0)
        self.assertEqual(values["precipitation"].value, 24)  # Pas la somme 0+1+...+24.
        self.assertEqual(values["actual_et"].value, 3)

    def test_missing_hour_invalid_soil_unit_and_outside_grid(self):
        ds = self.dataset().isel(valid_time=slice(1, None))
        values = {o.variable: o for o in parse_dataset(ds, self.query(), "2024-01-05T00:00:00+00:00")}
        self.assertIsNone(values["soil_water"].value)
        self.assertEqual(values["precipitation"].value, 24)
        ds.swvl1.attrs["units"] = "%"
        with self.assertRaises(ValueError):
            parse_dataset(ds, self.query(), "2024-01-05T00:00:00+00:00")
        with self.assertRaises(ValueError):
            parse_dataset(self.dataset(), Query(0, 0, date(2023, 12, 31), date(2023, 12, 31)), "2024-01-05T00:00:00+00:00")

    def test_netcdf_and_zip(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.nc"
            self.dataset().to_netcdf(path)
            self.assertEqual(len(read_download(path, self.query(), "2024-01-05T00:00:00+00:00")), 4)
            zipped = Path(tmp) / "data.zip"
            with zipfile.ZipFile(zipped, "w") as archive:
                archive.write(path, "nested/data.nc")
            self.assertEqual(len(read_download(zipped, self.query(), "2024-01-05T00:00:00+00:00")), 4)

    def test_disabled_and_offline_no_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / "cache.sqlite")
            self.assertEqual(Era5LandProvider(cache).fetch(self.query()).status, "not_configured")
            self.assertEqual(Era5LandProvider(cache, enabled=True, offline=True).fetch(self.query()).status, "unavailable")

    def test_monthly_request_cache_and_network_failure(self):
        values = parse_dataset(self.dataset(), self.query(), "2024-01-05T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / "cache.sqlite")
            client = Mock()
            with patch("soil_hydrology.era5.read_download", return_value=values):
                result = Era5LandProvider(cache, enabled=True, client=client).fetch(self.query())
            self.assertEqual(len(result.observations), 4)
            self.assertEqual(client.retrieve.call_count, 2)
            first = client.retrieve.call_args_list[0].args[1]
            last = client.retrieve.call_args_list[1].args[1]
            self.assertIn("volumetric_soil_water_layer_4", first["variable"])
            self.assertEqual(last["year"], "2024")
            self.assertEqual(last["day"], ["01"])
            self.assertEqual(last["time"], ["00:00"])
            Era5LandProvider(cache, enabled=True, client=client).fetch(self.query())
            self.assertEqual(client.retrieve.call_count, 2)
            client.retrieve.side_effect = RuntimeError("Network unavailable")
            fallback = Era5LandProvider(cache, enabled=True, refresh=True, client=client).fetch(self.query())
            self.assertEqual(fallback.status, "stale")
            self.assertEqual(len(fallback.observations), 4)

    def test_four_layers_not_collapsed_and_invalid_value(self):
        ds = self.dataset()
        for layer in range(2, 5):
            ds[f"swvl{layer}"] = ds.swvl1.copy(deep=True)
        ds.swvl4.values[0, 0, 0] = 9
        values = [o for o in parse_dataset(ds, self.query(), "2024-01-05T00:00:00+00:00") if o.variable == "soil_water"]
        self.assertEqual([(o.depth_min_cm, o.depth_max_cm) for o in values], [(0, 7), (7, 28), (28, 100), (100, 289)])
        self.assertIsNone(values[-1].value)


if __name__ == "__main__":
    unittest.main()
