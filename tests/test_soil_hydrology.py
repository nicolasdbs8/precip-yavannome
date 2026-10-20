"""Tests hors réseau : modèles, cumuls, cache, pluie Rain, convergence.
SIM, géographie, Copernicus et export : test_sim.py, test_geo_parcel.py, test_copernicus.py, test_export.py."""
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

import requests

from precip_common import TARGET_LAT, TARGET_LON
from soil_hydrology.cache import Cache
from soil_hydrology.calculations import cumulative
from soil_hydrology.comparison import compare
from soil_hydrology.models import Nature, Observation, ProviderResult, Query
from soil_hydrology.providers import RainPrecipitationProvider, collect
from soil_hydrology.transport import cached_document
from soil_hydrology.variables import convert, parse_value

FETCHED = "2024-03-02T12:00:00+00:00"


def obs(day="2024-02-29", value=1.0, source="test", variable="precipitation", **kwargs):
    unit = "mm" if variable == "precipitation" else "m³/m³"
    return Observation(timestamp=f"{day}T00:00:00+00:00", date=day, latitude=TARGET_LAT, longitude=TARGET_LON,
                       source=source, product=source, provider=source, variable=variable,
                       original_variable=variable, value=value, unit=unit, nature=Nature.MODEL,
                       retrieved_at=FETCHED, quality="valid", temporal_window="00–24 UTC", **kwargs)


class ModelsAndCalculations(unittest.TestCase):
    def test_query_validation(self):
        for lat, lon in [(91, 0), (0, 181), (float("nan"), 0)]:
            with self.assertRaises(ValueError):
                Query(lat, lon, date(2024, 1, 1), date(2024, 1, 2))
        with self.assertRaises(ValueError):
            Query(0, 0, date(2024, 2, 1), date(2024, 1, 1))

    def test_timezone_required_and_preserved(self):
        data = obs().to_dict()
        data["timestamp"] = "2024-03-01T01:00:00+01:00"
        self.assertEqual(Observation(**data).timestamp, data["timestamp"])
        data["timestamp"] = "2024-03-01T00:00:00"
        with self.assertRaises(ValueError):
            Observation(**data)

    def test_units(self):
        self.assertAlmostEqual(convert(273.15, "K", "°C"), 0)
        self.assertEqual(convert(.02, "m", "mm"), 20)
        self.assertEqual(convert(12, "kg/m²", "mm"), 12)
        with self.assertRaises(ValueError):
            convert(.63, "indice brut", "%")

    def test_missing_and_invalid_values(self):
        for value in ["", "NaN", None, "MQ"]:
            self.assertEqual(parse_value(value, "soil_water")[1], "missing")
        for value in ["inf", "abc", "-1", "1.5"]:
            self.assertEqual(parse_value(value, "soil_water")[1], "invalid")
        self.assertEqual(parse_value("-2.3", "effective_precipitation"), (-2.3, "valid"))
        self.assertEqual(parse_value("0,28", "root_liquid"), (.28, "valid"))

    def test_aggregations_all_windows_and_leap_year(self):
        end = date(2024, 3, 1)
        values = [obs(str(end - timedelta(days=i)), 2) for i in range(90)]
        for window in [1, 3, 7, 15, 30, 90]:
            self.assertEqual(cumulative(values, end, window)["sum_mm"], window * 2)
        self.assertEqual(cumulative(values, end, 3)["start"], "2024-02-28")
        self.assertEqual(cumulative(values, date(2024, 1, 1), 3)["sum_mm"], 6)

    def test_missing_and_duplicate_days_never_zero_fill(self):
        values = [obs("2024-02-28", 2), obs("2024-03-01", 3)]
        total = cumulative(values, date(2024, 3, 1), 3)
        self.assertIsNone(total["sum_mm"])
        self.assertEqual(total["partial_sum_mm"], 5)
        self.assertEqual(total["missing_dates"], ["2024-02-29"])
        total = cumulative([obs(), obs()], date(2024, 2, 29), 1)
        self.assertIsNone(total["sum_mm"])

    def test_cannot_sum_sources_or_indices(self):
        with self.assertRaises(ValueError):
            cumulative([obs(source="a"), obs(source="b")], date(2024, 2, 29), 1)
        with self.assertRaises(ValueError):
            cumulative([obs(variable="soil_water")], date(2024, 2, 29), 1)


class CacheAndRainTests(unittest.TestCase):
    def test_cache_expiry_and_metadata_http_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp) / "cache.sqlite")
            cache.put("key", {"value": 0})
            self.assertEqual(cache.get("key"), {"value": 0})
            self.assertIsNone(cache.get("key", ttl=-1))
            http = Mock()
            http.get.side_effect = requests.HTTPError("429")
            cache.put(cache.key("document-v1", "https://example.test"), "cached")
            self.assertEqual(cached_document(http, cache, "https://example.test", refresh=True), ("cached", True))
            with self.assertRaises(RuntimeError):
                cached_document(http, cache, "https://missing.test", offline=True)

    def test_rain_keeps_sources_and_rejects_other_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rain.csv"
            text = "date,cumul_mm,source\n2024-02-29,4,comephore\n2024-02-29,3,antilope\n2024-02-29,4.5,pluviometre_site\n"
            path.write_text(text)
            q = Query(TARGET_LAT, TARGET_LON, date(2024, 2, 29), date(2024, 2, 29))
            values = RainPrecipitationProvider(path).fetch(q).observations
            self.assertEqual(len(values), 3)
            self.assertEqual(values[-1].nature, Nature.LOCAL_SENSOR)
            self.assertEqual(values[1].spatial_resolution_m, 500)
            self.assertEqual(path.read_text(), text)
            result = RainPrecipitationProvider(path).fetch(Query(46, 5, q.start, q.end))
            self.assertEqual(result.status, "unavailable")

    def test_conflicting_csv_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rain.csv"
            path.write_text("date,cumul_mm,source\n2024-02-29,4,comephore\n2024-02-29,3,comephore\n")
            values = RainPrecipitationProvider(path).fetch(Query(TARGET_LAT, TARGET_LON, date(2024, 2, 29), date(2024, 2, 29))).observations
            self.assertEqual(len(values), 1)
            self.assertIsNone(values[0].value)

    def test_failure_isolation(self):
        broken = Mock(name="broken")
        broken.name = "broken"
        broken.fetch.side_effect = requests.HTTPError("503")
        good = Mock()
        good.fetch.return_value = ProviderResult("good", observations=[obs()])
        results = collect([broken, good], Query(46, 5, date(2024, 2, 29), date(2024, 2, 29)))
        self.assertEqual(results[0].status, "temporary_error")
        self.assertEqual(len(results[1].observations), 1)


class ComparisonTests(unittest.TestCase):
    def make_series(self, source, step, **kwargs):
        return [obs(f"2024-03-{i:02d}", .2 + i * step, source, "soil_water", depth_min_cm=0, depth_max_cm=7, **kwargs) for i in range(1, 8)]

    def test_convergence_and_contradiction(self):
        a, b, c = [self.make_series(s, .01) for s in ("a", "b", "c")]
        end = date(2024, 3, 7)
        self.assertEqual(compare(a + b, end)["category"], "convergence modérée")
        self.assertEqual(compare(a + b + c, end)["category"], "convergence forte")
        result = compare(a + self.make_series("b", -.01), end)
        self.assertEqual(result["category"], "données contradictoires")
        self.assertEqual(result["evidence"][0]["first"]["date"], "2024-03-01")

    def test_insufficient_and_stale_and_same_provider(self):
        a = self.make_series("a", .01)
        for values, end in [(a, date(2024, 3, 7)), ([], date(2024, 3, 7)),
                            (a + a, date(2024, 3, 7)),
                            (a + self.make_series("b", .01), date(2024, 3, 20))]:
            self.assertEqual(compare(values, end)["category"], "données insuffisantes")


if __name__ == "__main__":
    unittest.main()
