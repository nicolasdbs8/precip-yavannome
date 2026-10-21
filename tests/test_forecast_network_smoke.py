"""Smoke test réseau Open-Meteo, optionnel :
    RAIN_NETWORK_TESTS=1 python -m unittest discover -s tests -p "test_forecast_network_smoke.py" -v

Interroge le vrai Open-Meteo au point ZP1 (sans cache) et vérifie que chaque
modèle répond au format attendu. Jamais exécuté par défaut.
"""
import os
import unittest
from datetime import datetime, timezone

from weather_forecast.export import build_report, validate
from weather_forecast.open_meteo import fetch_all, session
from weather_forecast.specs import ALL_MODELS, POINT


@unittest.skipUnless(os.environ.get("RAIN_NETWORK_TESTS") == "1", "RAIN_NETWORK_TESTS=1 pour les tests réseau")
class OpenMeteoSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = fetch_all(ALL_MODELS, session())

    def test_http_ok_and_coherent_grid_points(self):
        for key, result in self.results.items():
            with self.subTest(key):
                self.assertIn(result.status, ("FRESH", "PARTIAL"), result.reason)
                grid = result.series.grid
                self.assertLess(abs(grid["lat"] - POINT["lat"]), 0.15)
                self.assertLess(abs(grid["lon"] - POINT["lon"]), 0.15)
                self.assertTrue(result.series.times)

    def test_short_range_models_cover_tomorrow(self):
        now = datetime.now(timezone.utc)
        for key in ("arome_hd", "arome", "arpege"):
            series = self.results[key].series
            future = [v for t, v in zip(series.times, series.values["precipitation"]) if t > now and v is not None]
            self.assertGreaterEqual(len(future), 24, key)

    def test_ensembles_members_and_plausible_values(self):
        ecmwf = self.results["ecmwf_ens"].series.members
        self.assertEqual(len(ecmwf["precipitation"]), 51)
        self.assertEqual(len(self.results["icon_eu_eps"].series.members["precipitation"]), 40)
        for result in self.results.values():
            series = result.series
            stacks = list(series.members.get("temperature_2m", {}).values()) or [series.values["temperature_2m"]]
            temps = [v for values in stacks for v in values if v is not None]
            self.assertTrue(temps)
            self.assertTrue(all(-40 < v < 45 for v in temps))
            precip_stacks = list(series.members.get("precipitation", {}).values()) or [series.values["precipitation"]]
            self.assertTrue(all(0 <= v < 100 for values in precip_stacks for v in values if v is not None))

    def test_report_from_live_data_is_valid(self):
        report = build_report(self.results, datetime.now(timezone.utc))
        self.assertEqual(validate(report), [])
        self.assertTrue(report["daily"] and report["half_days"])


if __name__ == "__main__":
    unittest.main()
