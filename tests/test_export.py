"""JSON compact : cumuls, tendance, fraîcheur, 0 ≠ null, pas de moyenne inter-sources, schéma, page."""
import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from soil_hydrology.export import build_report, dumps, trend, validate
from soil_hydrology.freshness import assess
from soil_hydrology.models import Nature, Observation, ProviderResult
from soil_hydrology.parcel import load_parcel
from soil_hydrology.sim import SimGrid

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
FETCHED = "2026-10-20T10:00:00+00:00"
A = SimGrid(8360, 22090, 46.8392, 5.43194, "parcel", 4614.1, 1.0, 0.0)
B = SimGrid(8360, 22010, 46.7673, 5.42782, "adjacent", 0.0, 0.0, 33.9)
END = date(2026, 10, 19)
PARCEL = load_parcel().summary()


def sim_obs(grid, day, variable, original, value, unit="mm"):
    return Observation(timestamp=f"{day}T06:00:00+00:00", date=str(day), latitude=grid.lat, longitude=grid.lon,
                       source="meteo_france_sim", product="SIM2 quotidien", provider="Météo-France", variable=variable,
                       original_variable=original, value=value, unit=unit, nature=Nature.MODEL, retrieved_at=FETCHED,
                       grid_id=grid.id, quality="valid" if value is not None else "missing")


def sim_result(days=20, end=END, grids=(A, B), skip=(), swi=lambda i: 0.10 + 0.005 * i):
    observations = []
    for grid in grids:
        for i in range(days):
            day = end - timedelta(days=days - 1 - i)
            rain = None if (grid.id, str(day)) in skip else (1.0 if i % 2 else 0.0)
            observations += [sim_obs(grid, day, "swi_sim", "SWI", round(swi(i), 4), "1"),
                             sim_obs(grid, day, "rainfall", "PRELIQ", rain), sim_obs(grid, day, "snowfall", "PRENEI", 0.0),
                             sim_obs(grid, day, "precipitation", "PRELIQ+PRENEI", rain)]
            for var, name in (("actual_et", "EVAP"), ("potential_et", "ETP"), ("effective_precipitation", "PE"),
                              ("drainage", "DRAINC"), ("runoff", "RUNC")):
                observations.append(sim_obs(grid, day, var, name, 0.5))
    return ProviderResult("Météo-France SIM", observations=observations,
                          details={"dataset_id": "6569b27598256cc583c917a7", "grids": [A.to_dict(), B.to_dict()][:len(grids)],
                                   "extractions": []})


def rain_result(values):
    return ProviderResult("Précipitations Rain", observations=[
        Observation(timestamp=f"{d}T00:00:00+00:00", date=d, latitude=46.8, longitude=5.45, source=f"rain_{src}",
                    product=src, provider="Rain", variable="precipitation", original_variable="cumul_mm", value=v,
                    unit="mm", nature=Nature.REANALYSIS, retrieved_at=FETCHED) for d, (v, src) in values.items()])


def report(**results):
    base = {"sim": sim_result(), "rain_precipitation": rain_result({})}
    base.update(results)
    return build_report(PARCEL, base, NOW)


class Derived(unittest.TestCase):
    def test_cumuls_24h_72h_7d_14d_and_fluxes(self):
        grid = report()["sources"]["sim"]["grids"][0]
        c = grid["derived"]["cumuls_mm"]
        # Pluie 1 mm un jour sur deux ; le dernier jour (i=19) vaut 1.
        self.assertEqual(c["PRELIQ"], {"24h": 1.0, "72h": 2.0, "7d": 4.0, "14d": 7.0})
        self.assertEqual(c["PRENEI"]["7d"], 0.0)  # 0 réel
        self.assertEqual(c["EVAP"], {"7d": 3.5})
        self.assertEqual(set(c), {"PRELIQ", "PRENEI", "PRELIQ+PRENEI", "EVAP", "ETP", "PE", "DRAINC", "RUNC"})

    def test_swi_d7_delta_and_trend(self):
        swi = report()["sources"]["sim"]["grids"][0]["derived"]["swi"]
        self.assertEqual((swi["latest_date"], swi["d7_date"]), ("2026-10-19", "2026-10-12"))
        self.assertAlmostEqual(swi["delta_7d"], 0.035)
        self.assertEqual(swi["trend"], "rising")
        by_date = {"2026-10-19": sim_obs(A, END, "swi_sim", "SWI", .5, "1")}
        self.assertEqual(trend(by_date, "2026-10-19", .02)["trend"], None)  # J−7 absent : pas d'invention
        by_date["2026-10-12"] = sim_obs(A, END - timedelta(days=7), "swi_sim", "SWI", .49, "1")
        self.assertEqual(trend(by_date, "2026-10-19", .02)["trend"], "stable")

    def test_missing_day_makes_window_null_not_zero(self):
        out = report(sim=sim_result(skip={(A.id, "2026-10-17")}))["sources"]["sim"]["grids"][0]["derived"]
        self.assertEqual(out["cumuls_mm"]["PRELIQ"]["24h"], 1.0)
        self.assertIsNone(out["cumuls_mm"]["PRELIQ"]["72h"])
        self.assertIsNone(out["cumuls_mm"]["PRELIQ"]["7d"])
        self.assertEqual(out["missing_days"]["PRELIQ:7d"], ["2026-10-17"])

    def test_sim_swi_kept_on_published_scale(self):
        sim = report()["sources"]["sim"]
        self.assertEqual(sim["units"]["SWI"], "1")
        self.assertLess(sim["grids"][0]["latest"]["values"]["SWI"], 1)  # jamais ×100


class Freshness(unittest.TestCase):
    def test_fresh_stale_unavailable(self):
        self.assertEqual(assess("sim", "available", date(2026, 10, 19), NOW)["status"], "FRESH")
        stale = assess("sim", "available", date(2026, 10, 17), NOW)
        self.assertEqual((stale["status"], stale["age_hours"]), ("STALE", 57.0))
        self.assertEqual(assess("sim", "available", None, NOW)["status"], "UNAVAILABLE")
        self.assertEqual(assess("sim", "temporary_error", None, NOW)["status"], "ERROR")
        self.assertEqual(assess("copernicus_swi", "unavailable_auth", None, NOW)["status"], "UNAVAILABLE_AUTH")
        # SWI : date nominale J 12:00 UTC ; seuil 72 h.
        self.assertEqual(assess("copernicus_swi", "available", date(2026, 10, 18), NOW)["age_hours"], 51.0)
        self.assertEqual(assess("copernicus_swi", "available", date(2026, 10, 18), NOW)["status"], "FRESH")
        self.assertEqual(assess("copernicus_swi", "available", date(2026, 10, 17), NOW)["status"], "STALE")
        self.assertEqual(assess("era5_land", "available", date(2026, 10, 14), NOW)["status"], "STALE")
        cached = assess("sim", "stale", date(2026, 10, 19), NOW)
        self.assertIn("valeurs issues du cache", cached["status_reason"])

    def test_old_sim_is_reported_stale_end_to_end(self):
        sim = report(sim=sim_result(end=date(2026, 10, 10)))["sources"]["sim"]
        self.assertEqual(sim["status"], "STALE")
        self.assertEqual(sim["source_date"], "2026-10-10")

    def test_unavailable_source_has_status_and_no_zeros(self):
        out = report(copernicus_swi=ProviderResult("Copernicus", "unavailable_auth", messages=["Identifiants CDSE absents"]))
        block = out["sources"]["copernicus_swi"]
        self.assertEqual(block["status"], "UNAVAILABLE_AUTH")
        self.assertIsNone(block["source_date"])
        self.assertEqual(out["sources"]["era5_land"]["status"], "DISABLED")
        self.assertNotIn(": 0,", json.dumps(block))

    def test_zero_and_null_are_distinct_in_json(self):
        grid = json.loads(dumps(report(sim=sim_result(skip={(A.id, "2026-10-17")}))))["sources"]["sim"]["grids"][0]
        rain = dict(zip(grid["series"]["date"], grid["series"]["PRELIQ"]))
        self.assertIsNone(rain["2026-10-17"])
        self.assertEqual(rain["2026-10-18"], 0.0)
        self.assertIsInstance(rain["2026-10-18"], float)

    def test_carry_over_recomputes_age(self):
        first = report()
        later = build_report(PARCEL, {"sim": None, "rain_precipitation": rain_result({})}, NOW + timedelta(days=3), first)
        sim = later["sources"]["sim"]
        self.assertEqual(sim["carried_over_from"], first["generated_at"])
        self.assertEqual(sim["status"], "STALE")
        self.assertGreater(sim["age_hours"], 48)


class NoCrossSourceAverage(unittest.TestCase):
    def test_each_source_separate_with_own_unit(self):
        out = report()
        self.assertTrue(out["conventions"]["no_cross_source_average"])
        self.assertEqual(set(out["sources"]), {"sim", "copernicus_swi", "copernicus_ssm", "era5_land", "rain_precipitation"})
        text = json.dumps(out).lower()
        for forbidden in ("mean_moisture", "average_moisture", "humidite_moyenne", "combined"):
            self.assertNotIn(forbidden, text)

    def test_precipitation_comparison_flags(self):
        days = {str(END - timedelta(days=i)): (3.0, "antilope") for i in range(30)}
        out = report(rain_precipitation=rain_result(days))["comparisons"]["precipitation_sim_vs_rain"]
        self.assertEqual(out["windows"]["7d"]["flag"], "DIVERGENT")  # SIM 4 mm vs Rain 21 mm
        self.assertEqual(out["windows"]["30d"]["flag"], "INCOMPLETE")  # SIM : 20 jours seulement
        close = {str(END - timedelta(days=i)): (0.5, "comephore") for i in range(14)}
        out = report(rain_precipitation=rain_result(close))["comparisons"]["precipitation_sim_vs_rain"]
        self.assertEqual(out["windows"]["14d"]["flag"], "CONSISTENT")


class Schema(unittest.TestCase):
    def test_valid_report_and_invariants(self):
        out = json.loads(dumps(report()))
        self.assertEqual(validate(out), [])
        broken = copy.deepcopy(out)
        broken["sources"]["sim"]["source_date"] = None
        self.assertTrue(any("sans source_date" in e for e in validate(broken)))
        broken = copy.deepcopy(out)
        broken["sources"]["sim"]["status"] = "OK"
        self.assertTrue(validate(broken))
        broken = copy.deepcopy(out)
        broken["sources"]["sim"]["grids"][0]["series"]["SWI"][0] = "0.1"
        self.assertTrue(validate(broken))

    def test_nan_is_never_published(self):
        out = report()
        out["sources"]["sim"]["grids"][0]["series"]["SWI"][0] = float("nan")
        with self.assertRaises(ValueError):
            dumps(out)


class Page(unittest.TestCase):
    def test_render_escapes_and_shows_statuses(self):
        from soil_hydrology.presentation import render
        out = report(copernicus_swi=ProviderResult("Copernicus", "unavailable_auth", messages=["<script>x</script>"]))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "page.html"
            render(json.loads(dumps(out)), path)
            html = path.read_text(encoding="utf-8")
        self.assertIn("Sol / Hydrologie", html)
        self.assertIn("UNAVAILABLE_AUTH", html)
        self.assertIn("Maille 8360_22090", html)
        self.assertIn("ne constitue pas une mesure physique", html)
        self.assertNotIn("<script>x</script>", html)


if __name__ == "__main__":
    unittest.main()
