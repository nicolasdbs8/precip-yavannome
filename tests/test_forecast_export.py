"""JSON forecast_zp1 : schéma, invariants, fraîcheur, report en cas d'échec, CLI, page."""
import copy
import json
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest import mock

import forecast_update
from forecast_fixtures import REAL_NOW, SPECS, real_results
from weather_forecast.export import ForecastUnavailable, build_report, carry_over, dumps, refresh_freshness, validate
from weather_forecast.open_meteo import ModelResult
from weather_forecast.presentation import render

UTC = timezone.utc


def real_report():
    return build_report(real_results(), REAL_NOW)


def all_errors():
    return {k: ModelResult(spec, "ERROR", "HTTP 503 : simulé") for k, spec in SPECS.items()}


class RealReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = real_report()

    def test_real_json_is_valid(self):
        self.assertEqual(validate(self.report), [])
        self.assertEqual(validate(json.loads(dumps(self.report))), [])

    def test_content(self):
        r = self.report
        self.assertEqual((r["status"], r["max_age_hours"], r["carried_over_from"]), ("FRESH", 12, None))
        self.assertEqual((len(r["daily"]), len(r["half_days"])), (8, 16))
        self.assertEqual(r["daily"][0]["date"], "2026-10-21")
        self.assertGreaterEqual(sum(b["status"] == "FRESH" for b in r["providers"]["deterministic"].values()), 5)
        self.assertEqual(set(r["providers"]["ensemble"]), {"ecmwf_ens", "icon_eu_eps"})
        dst = next(d for d in r["daily"] if d["date"] == "2026-10-25")
        self.assertEqual(dst["hours"], 25)
        self.assertEqual(dst["deterministic"]["ecmwf"]["hours_valid"], 25)
        self.assertEqual(r["providers"]["deterministic"]["arome"]["model_run"], "2026-10-21T03:00:00+00:00")
        self.assertIsNone(r["providers"]["ensemble"]["ecmwf_ens"]["model_run"])
        self.assertEqual(r["attribution"]["open_meteo"]["licence"], "CC BY 4.0")

    def test_horizons_are_explicit(self):
        days = {d["lead_day"]: d for d in self.report["daily"]}
        self.assertIn("arome", days[3]["horizon"]["out_of_horizon"])
        self.assertIn("arpege", days[5]["horizon"]["out_of_horizon"])
        self.assertIn("icon_eu_eps", days[6]["horizon"]["out_of_horizon"])
        self.assertIsNone(days[6]["ensemble"]["icon_eu_eps"]["precipitation"]["median_mm"])
        for d in days.values():
            self.assertNotEqual(d["consensus"]["confidence"] == "HIGH" and d["lead_day"] >= 6, True)
        self.assertEqual(days[6]["consensus"]["confidence_cap_reason"], "échéance longue : un seul système d'ensemble disponible")

    def test_ruissellement_metrics(self):
        day = self.report["daily"][1]
        self.assertEqual(set(day["preceding_72h_forecast_precip_mm_by_model"]), set(day["deterministic"]))
        self.assertIsNotNone(day["preceding_72h_forecast_precip_mm_by_model"]["ecmwf"])
        half = self.report["half_days"][2]
        self.assertIn("hours", half["hours_since_last_forecast_rain_ge_0_2mm"]["arome"])

    def test_no_cross_model_average_nor_decision(self):
        text = dumps(self.report)
        for forbidden in ("average", "GO", "NO-GO", "WAIT", "USEFUL_FOR_AUGER"):
            self.assertNotIn(f'"{forbidden}', text)


class ForbiddenValues(unittest.TestCase):
    def setUp(self):
        self.report = real_report()

    def broken(self, mutate):
        r = copy.deepcopy(self.report)
        mutate(r)
        return validate(r)

    def test_detected(self):
        cases = {
            "status": lambda r: r.update(status="GOOD"),
            "prob": lambda r: r["daily"][1]["ensemble"]["ecmwf_ens"]["precipitation"].update(prob_ge_2mm=1.5),
            "confidence": lambda r: r["daily"][1]["consensus"].update(confidence="VERY_HIGH"),
            "best_match": lambda r: r["providers"]["deterministic"]["ecmwf"].update(model_id="best_match"),
            "average": lambda r: r["daily"][1]["consensus"].update(average_precipitation_of_all_models=3.0),
            "mixed members": lambda r: r["daily"][1]["ensemble"]["ecmwf_ens"]["precipitation"].update(members_valid=91),
            "single ensemble HIGH": lambda r: r["daily"][7]["consensus"].update(confidence="HIGH"),
            "point": lambda r: r["point"].update(lat=46.8),
            "stale as fresh": lambda r: r.update(age_hours=20.0),
            "published out of horizon": lambda r: r["daily"][4]["deterministic"]["arome"].update(precipitation_mm=1.0),
            "decision key": lambda r: r["daily"][1].update(auger_decision="GO"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.assertTrue(self.broken(mutate))


class Freshness(unittest.TestCase):
    def test_fresh_then_stale(self):
        report = real_report()
        self.assertEqual(refresh_freshness(report, REAL_NOW + timedelta(hours=11))["status"], "FRESH")
        late = refresh_freshness(report, REAL_NOW + timedelta(hours=13))
        self.assertEqual((late["status"], late["daily"][0]["period_state"]), ("STALE", "PAST"))
        self.assertEqual(validate(late), [])

    def test_recent_previous_carried_over(self):
        previous = real_report()
        now = REAL_NOW + timedelta(hours=3)
        out = carry_over(previous, now, "Open-Meteo indisponible")
        self.assertEqual((out["status"], out["carried_over_from"], out["last_attempt"]["outcome"]),
                         ("FRESH", previous["generated_at"], "FAILED"))
        self.assertEqual(out["daily"], refresh_freshness(previous, now)["daily"])
        self.assertEqual(validate(out), [])

    def test_old_previous_is_stale_and_keeps_origin(self):
        once = carry_over(real_report(), REAL_NOW + timedelta(hours=6), "panne")
        twice = carry_over(once, REAL_NOW + timedelta(days=2), "panne")
        self.assertEqual(twice["status"], "STALE")
        self.assertEqual(twice["carried_over_from"], real_report()["generated_at"])
        self.assertEqual(validate(twice), [])


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.json, self.html = self.dir / "forecast_zp1.json", self.dir / "forecast_zp1.html"
        self.args = ["--json", str(self.json), "--html", str(self.html), "--cache", str(self.dir / "c.sqlite")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_failure_keeps_previous_json(self):
        self.json.write_text(dumps(real_report()), encoding="utf-8")
        with mock.patch.object(forecast_update, "fetch_all", return_value=all_errors()):
            self.assertEqual(forecast_update.main(self.args), 1)
        kept = json.loads(self.json.read_text(encoding="utf-8"))
        self.assertEqual(len(kept["daily"]), 8)
        self.assertIsNotNone(kept["carried_over_from"])
        self.assertEqual(kept["last_attempt"]["outcome"], "FAILED")

    def test_failure_without_previous_writes_nothing(self):
        with mock.patch.object(forecast_update, "fetch_all", return_value=all_errors()):
            self.assertEqual(forecast_update.main(self.args), 1)
        self.assertFalse(self.json.exists())

    def test_success_validate_and_render_only(self):
        with mock.patch.object(forecast_update, "fetch_all", return_value=real_results()):
            self.assertEqual(forecast_update.main(self.args), 0)
        self.assertEqual(forecast_update.main(self.args + ["--validate"]), 0)
        self.html.unlink()
        self.assertEqual(forecast_update.main(self.args + ["--render-only"]), 0)
        self.assertTrue(self.html.exists())

    def test_invalid_json_returns_3(self):
        report = real_report()
        report["status"] = "GOOD"
        self.json.write_text(json.dumps(report), encoding="utf-8")
        self.assertEqual(forecast_update.main(self.args + ["--validate"]), 3)

    def test_build_refuses_empty(self):
        with self.assertRaises(ForecastUnavailable):
            build_report(all_errors(), REAL_NOW)


class Page(unittest.TestCase):
    def test_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.html"
            render(real_report(), path, REAL_NOW + timedelta(hours=1))
            html = path.read_text(encoding="utf-8")
        for text in ("Prévision météo ZP1", "hors horizon", "IFS ENS", "ICON-EU-EPS", "Weather data by Open-Meteo.com",
                     "sans moyenne entre modèles", "Plafond", "matin 06–12 h", "ICON-G 17,2 (relais non compté)"):
            self.assertIn(text, html)
        self.assertNotIn("green", html.lower())

    def test_stale_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.html"
            render(real_report(), path, REAL_NOW + timedelta(days=1))
            self.assertIn("Prévision périmée", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
