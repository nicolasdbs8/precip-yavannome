"""Agrégation : horizons, quantiles et probabilités d'ensemble, cumuls précédents."""
import unittest
from datetime import date, datetime, timedelta, timezone

from forecast_fixtures import REAL_NOW, SPECS, make_result, payload, real_results
from weather_forecast import aggregation as agg

UTC = timezone.utc
START = datetime(2026, 10, 21, tzinfo=UTC)
FETCHED = datetime(2026, 10, 21, 3, tzinfo=UTC)


def ensemble(per_member, members=10, spec="ecmwf_ens", variable="precipitation", at=None):
    """Ensemble synthétique : membre m → `per_member[m]` sur `variable` (à l'heure `at` seulement si donnée)."""
    def value(v, m, i, t):
        return per_member[m] if v == variable and (at is None or t == at) else 0.0
    return make_result(SPECS[spec], payload(SPECS[spec], START, 24 * 9, value, members=members), FETCHED)


class Horizons(unittest.TestCase):
    def deterministic(self, key, hours_ahead):
        spec = SPECS[key]
        body = payload(spec, START - timedelta(days=3), 24 * 12, lambda v, m, i, t: 0.4,
                       until=FETCHED + timedelta(hours=hours_ahead))
        return make_result(spec, body, FETCHED)

    def test_arome_absent_after_horizon(self):
        arome = self.deterministic("arome", 48)   # dernière échéance 23/10 03 UTC
        partial = agg.deterministic_period(arome, agg.day_period(date(2026, 10, 23)))
        outside = agg.deterministic_period(arome, agg.day_period(date(2026, 10, 24)))
        self.assertEqual(partial["availability"], "PARTIAL_HORIZON")
        self.assertEqual(outside["availability"], "OUT_OF_HORIZON")
        for block in (partial, outside):
            self.assertIsNone(block["precipitation_mm"])
            self.assertIsNone(block["temperature_min_c"])
        self.assertEqual(agg.deterministic_period(arome, agg.day_period(date(2026, 10, 22)))["precipitation_mm"], 9.6)

    def test_arpege_absent_after_horizon(self):
        arpege = self.deterministic("arpege", 96)
        self.assertEqual(agg.deterministic_period(arpege, agg.day_period(date(2026, 10, 24)))["availability"], "AVAILABLE")
        self.assertEqual(agg.deterministic_period(arpege, agg.day_period(date(2026, 10, 25)))["availability"], "PARTIAL_HORIZON")
        out = agg.deterministic_period(arpege, agg.day_period(date(2026, 10, 26)))
        self.assertEqual((out["availability"], out["precipitation_mm"]), ("OUT_OF_HORIZON", None))

    def test_icon_eu_eps_absent_after_day_5_on_real_data(self):
        icon = real_results()["icon_eu_eps"]
        today = agg.local_today(REAL_NOW)
        j5 = agg.ensemble_period(icon, agg.day_period(today + timedelta(days=5)))
        j6 = agg.ensemble_period(icon, agg.day_period(today + timedelta(days=6)))
        self.assertEqual(j5["availability"], "PARTIAL_HORIZON")
        self.assertEqual(j6["availability"], "OUT_OF_HORIZON")
        for block in (j5, j6):
            self.assertEqual(block["precipitation"]["members_valid"], 0)
            self.assertIsNone(block["precipitation"]["median_mm"])
            self.assertIsNone(block["precipitation"]["prob_ge_2mm"])
        j4 = agg.ensemble_period(icon, agg.day_period(today + timedelta(days=4)))
        self.assertEqual((j4["availability"], j4["precipitation"]["members_valid"]), ("AVAILABLE", 40))

    def test_series_starting_mid_period_is_incomplete_not_horizon(self):
        ens = real_results()["ecmwf_ens"]   # IFS ENS 9 km : pas d'heures passées
        block = agg.ensemble_period(ens, agg.day_period(agg.local_today(REAL_NOW)))
        self.assertEqual(block["availability"], "INCOMPLETE")
        self.assertIsNone(block["precipitation"]["median_mm"])


class EnsembleStatistics(unittest.TestCase):
    def test_quantiles_linear_interpolation(self):
        self.assertEqual([agg.quantile(list(range(11)), q) for q in (0.1, 0.25, 0.5, 0.75, 0.9)], [1, 2.5, 5, 7.5, 9])
        self.assertAlmostEqual(agg.quantile([0.0, 1.0, 4.0, 10.0], 0.9), 8.2)

    def test_quantiles_and_thresholds_on_period_sums(self):
        sums = [0, 0.4, 0.5, 1.9, 2.1, 4.9, 5, 9.9, 10, 20]
        result = ensemble(sums, at=datetime(2026, 10, 22, 8, tzinfo=UTC))  # une seule heure non nulle par membre
        stats = agg.ensemble_period(result, agg.half_day_period(date(2026, 10, 22), "MORNING"))["precipitation"]
        self.assertEqual(stats["members_valid"], 10)
        self.assertEqual((stats["min_mm"], stats["max_mm"], stats["median_mm"]), (0.0, 20.0, 3.5))
        self.assertEqual((stats["p10_mm"], stats["p90_mm"]), (0.4, 11.0))
        self.assertEqual((stats["prob_ge_0_5mm"], stats["prob_ge_2mm"], stats["prob_ge_5mm"], stats["prob_ge_10mm"]),
                         (0.8, 0.6, 0.4, 0.2))

    def test_null_members_excluded_from_denominator(self):
        result = ensemble([1.0] * 10)
        period = agg.half_day_period(date(2026, 10, 22), "MORNING")
        idx = agg.indices(result.series.times, period.start, period.end)
        for member in ("member01", "member02", "member03", "member04"):
            result.series.members["precipitation"][member][idx[2]] = None
        stats = agg.ensemble_period(result, period)["precipitation"]
        self.assertEqual(stats["members_valid"], 6)
        self.assertEqual(stats["prob_ge_5mm"], 1.0)  # 6 / 6, pas 6 / 10

    def test_zero_valid_members_gives_null(self):
        stats = agg.precipitation_stats([])
        self.assertEqual(stats["members_valid"], 0)
        self.assertTrue(all(v is None for k, v in stats.items() if k != "members_valid"))
        self.assertIsNone(agg.temperature_stats([])["prob_temperature_below_0c"])
        self.assertIsNone(agg.wind_stats([])["prob_gust_ge_40kmh"])

    def test_freeze_and_gust_probabilities(self):
        temps = ensemble([-1.0, -0.1, 0.0, 0.1, 3.0], members=5, variable="temperature_2m")
        stats = agg.ensemble_period(temps, agg.day_period(date(2026, 10, 22)))["temperature"]
        self.assertEqual(stats["prob_temperature_below_0c"], 0.4)  # strictement < 0 °C
        gusts = ensemble([20, 39.9, 40, 59, 60, 80], members=6, variable="wind_gusts_10m")
        wind = agg.ensemble_period(gusts, agg.day_period(date(2026, 10, 22)))["wind"]
        self.assertEqual((wind["prob_gust_ge_40kmh"], wind["prob_gust_ge_60kmh"]), (0.667, 0.333))

    def test_systems_are_never_mixed(self):
        results = real_results()
        period = agg.day_period(date(2026, 10, 22))
        ecmwf = agg.ensemble_period(results["ecmwf_ens"], period)
        icon = agg.ensemble_period(results["icon_eu_eps"], period)
        self.assertEqual((ecmwf["precipitation"]["members_valid"], ecmwf["members_expected"]), (51, 51))
        self.assertEqual((icon["precipitation"]["members_valid"], icon["members_expected"]), (40, 40))
        self.assertNotIn(91, (ecmwf["precipitation"]["members_valid"], icon["precipitation"]["members_valid"]))


class PrecedingRain(unittest.TestCase):
    def setUp(self):
        spec = SPECS["ecmwf"]
        self.dry_from = datetime(2026, 10, 22, 6, tzinfo=UTC)
        body = payload(spec, START - timedelta(days=3), 24 * 12,
                       lambda v, m, i, t: (1.0 if t <= self.dry_from else 0.0) if v == "precipitation" else 8.0)
        self.result = make_result(spec, body, FETCHED)

    def test_preceding_windows(self):
        start = agg.day_period(date(2026, 10, 22)).start  # 21/10 22 UTC : tout est pluvieux avant
        self.assertEqual([agg.preceding_precipitation(self.result, start, h) for h in (24, 48, 72)], [24.0, 48.0, 72.0])
        self.assertIsNone(agg.preceding_precipitation(self.result, START - timedelta(days=2), 72))  # avant la série

    def test_hours_since_last_rain(self):
        noon = datetime(2026, 10, 22, 10, tzinfo=UTC)
        self.assertEqual(agg.hours_since_rain(self.result, noon), {"hours": 4, "lower_bound": False})
        self.assertEqual(agg.hours_since_rain(self.result, self.dry_from), {"hours": 0, "lower_bound": False})

    def test_hours_since_rain_lower_bound_and_out_of_series(self):
        spec = SPECS["ecmwf"]
        dry = make_result(spec, payload(spec, START, 48), FETCHED)
        self.assertEqual(agg.hours_since_rain(dry, START + timedelta(hours=10)), {"hours": 11, "lower_bound": True})
        self.assertEqual(agg.hours_since_rain(dry, START + timedelta(days=5)), {"hours": None, "lower_bound": False})


if __name__ == "__main__":
    unittest.main()
