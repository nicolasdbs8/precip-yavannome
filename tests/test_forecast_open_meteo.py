"""Client Open-Meteo : modèles explicites, séries validées, pannes isolées, cache, run."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from forecast_fixtures import REAL_NOW, SPECS, FakeHttp, make_result, payload, real_entry
from soil_hydrology.cache import Cache
from weather_forecast import open_meteo as om
from weather_forecast.export import ForecastUnavailable, build_report
from weather_forecast.specs import ALL_MODELS, ENSEMBLES

UTC = timezone.utc
EXPECTED_IDS = {"arome_hd": "meteofrance_arome_france_hd", "arome": "meteofrance_arome_france",
                "arpege": "meteofrance_arpege_europe", "ecmwf": "ecmwf_ifs", "icon": "dwd_icon_eu",
                "icon_global": "dwd_icon_global", "ecmwf_ens": "ecmwf_ifs_europe_ensemble",
                "icon_eu_eps": "dwd_icon_eu_eps_native"}


def routes():
    return {s.model_id: real_entry(s.key)["payload"] for s in ALL_MODELS}


class ExplicitModels(unittest.TestCase):
    def test_all_models_requested_explicitly(self):
        self.assertEqual({s.key: s.model_id for s in ALL_MODELS}, EXPECTED_IDS)
        for spec in ALL_MODELS:
            params = om.request_params(spec)
            self.assertEqual(params["models"], spec.model_id)
            self.assertEqual(om.endpoint(spec), om.ENSEMBLE_URL if spec in ENSEMBLES else om.FORECAST_URL)

    def test_no_best_match_or_seamless(self):
        for bad in ("best_match", "meteofrance_seamless", "dwd_icon_seamless_eps", ""):
            with self.assertRaises(ValueError):
                om.check_explicit(bad)
        http = FakeHttp(routes())
        with tempfile.TemporaryDirectory() as tmp:
            om.fetch_all(ALL_MODELS, http, Cache(Path(tmp) / "c.sqlite"))
        data_calls = [params for url, params, _ in http.calls if "/static/meta.json" not in url]
        self.assertEqual(len(data_calls), len(ALL_MODELS))
        for params in data_calls:
            self.assertIn("models", params)  # sans « models », Open-Meteo appliquerait best_match
            self.assertNotIn("best_match", str(params))
            self.assertNotIn("seamless", str(params))

    def test_timeout_and_user_agent(self):
        http = FakeHttp(routes())
        om.download(http, SPECS["arome"])
        self.assertTrue(all(timeout == om.TIMEOUT for _, _, timeout in http.calls))
        self.assertIn("precip-yavannome", om.session().headers["User-Agent"])


class Parsing(unittest.TestCase):
    def test_real_fixtures_parse_with_exact_members(self):
        for key, spec in SPECS.items():
            series = om.parse(real_entry(key)["payload"], spec)
            for values in series.values.values():
                self.assertEqual(len(values), len(series.times))
            if spec.kind == "ensemble":
                self.assertEqual(len(series.members["precipitation"]), spec.members_expected)
        self.assertEqual(len(om.parse(real_entry("ecmwf_ens")["payload"], SPECS["ecmwf_ens"]).members["precipitation"]), 51)
        self.assertEqual(len(om.parse(real_entry("icon_eu_eps")["payload"], SPECS["icon_eu_eps"]).members["precipitation"]), 40)

    def test_arome_hd_declared_missing_variables_stay_fresh(self):
        spec = SPECS["arome_hd"]
        result = make_result(spec, real_entry("arome_hd")["payload"], REAL_NOW)
        self.assertEqual(result.status, "FRESH")
        self.assertIn("rain", result.series.missing)

    def test_unexpected_missing_variable_is_partial(self):
        spec = SPECS["arpege"]
        body = payload(spec, datetime(2026, 10, 21, tzinfo=UTC), 120, drop=("wind_gusts_10m",))
        result = make_result(spec, body, datetime(2026, 10, 21, 1, tzinfo=UTC))
        self.assertEqual(result.status, "PARTIAL")
        self.assertIn("wind_gusts_10m", result.reason)

    def test_inconsistent_lengths_raise(self):
        body = copy.deepcopy(real_entry("arome")["payload"])
        body["hourly"]["precipitation"].pop()
        with self.assertRaisesRegex(om.SeriesError, "precipitation"):
            om.parse(body, SPECS["arome"])

    def test_irregular_steps_non_utc_and_bad_values_raise(self):
        base = real_entry("arome")["payload"]
        gap = copy.deepcopy(base)
        gap["hourly"]["time"][5] += 1800
        local = copy.deepcopy(base)
        local["utc_offset_seconds"] = 7200
        text = copy.deepcopy(base)
        text["hourly"]["precipitation"][3] = "1.2"
        for body in (gap, local, text):
            with self.assertRaises(om.SeriesError):
                om.parse(body, SPECS["arome"])

    def test_nulls_are_kept_as_none(self):
        series = om.parse(real_entry("arome")["payload"], SPECS["arome"])
        self.assertIsNone(series.values["precipitation"][-1])  # au-delà de l'horizon AROME
        self.assertNotIn(None, series.values["precipitation"][:24])

    def test_fewer_members_is_partial(self):
        spec = SPECS["icon_eu_eps"]
        body = payload(spec, datetime(2026, 10, 21, tzinfo=UTC), 130, members=30)
        self.assertEqual(make_result(spec, body, datetime(2026, 10, 21, 1, tzinfo=UTC)).status, "PARTIAL")

    def test_no_future_data_is_unavailable(self):
        spec = SPECS["arome"]
        body = payload(spec, datetime(2026, 10, 21, tzinfo=UTC), 72, until=datetime(2026, 10, 21, 9, tzinfo=UTC))
        self.assertEqual(make_result(spec, body, datetime(2026, 10, 21, 10, tzinfo=UTC)).status, "UNAVAILABLE")


class Resilience(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self.tmp.name) / "c.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_model_http_500_keeps_the_others(self):
        table = routes()
        table["meteofrance_arpege_europe"] = 500
        results = om.fetch_all(ALL_MODELS, FakeHttp(table), self.cache)
        self.assertEqual(results["arpege"].status, "ERROR")
        self.assertIn("500", results["arpege"].reason)
        self.assertTrue(all(r.usable for k, r in results.items() if k != "arpege"))

    def test_open_meteo_down_is_a_clean_error(self):
        results = om.fetch_all(ALL_MODELS, FakeHttp({}), self.cache)
        self.assertTrue(all(r.status == "ERROR" for r in results.values()))
        with self.assertRaises(ForecastUnavailable):
            build_report(results, REAL_NOW)

    def test_network_exception_is_reported(self):
        import requests

        class Broken:
            def get(self, *args, **kwargs):
                raise requests.ConnectionError("DNS")
        result = om.fetch(SPECS["arome"], Broken(), self.cache)
        self.assertEqual(result.status, "ERROR")
        self.assertIn("réseau", result.reason)

    def test_retry_is_bounded(self):
        retry = om.session().get_adapter("https://api.open-meteo.com").max_retries
        self.assertLessEqual(retry.total, 3)
        self.assertIn(429, retry.status_forcelist)
        self.assertIn(503, retry.status_forcelist)

    def test_cache_avoids_identical_calls_and_refresh_bypasses(self):
        http = FakeHttp(routes())
        om.fetch(SPECS["arome"], http, self.cache)
        calls = len(http.calls)
        self.assertTrue(om.fetch(SPECS["arome"], http, self.cache).from_cache)
        self.assertEqual(len(http.calls), calls)
        self.assertFalse(om.fetch(SPECS["arome"], http, self.cache, refresh=True).from_cache)
        self.assertGreater(len(http.calls), calls)

    def test_offline_without_cache_is_error_and_keeps_fetch_time(self):
        self.assertEqual(om.fetch(SPECS["arome"], None, self.cache, offline=True).status, "ERROR")
        om.fetch(SPECS["arome"], FakeHttp(routes()), self.cache)
        cached = om.fetch(SPECS["arome"], None, self.cache, offline=True)
        self.assertTrue(cached.from_cache)
        self.assertLess(datetime.now(UTC) - cached.fetched_at, timedelta(minutes=5))


class ModelRun(unittest.TestCase):
    def run_for(self, runs):
        return om.download(FakeHttp(routes(), {"meteofrance_arome_france0025": runs}), SPECS["arome"])

    def test_published_when_stable(self):
        stamp = int((datetime.now(UTC) - timedelta(hours=4)).timestamp())
        self.assertIsNotNone(self.run_for([stamp])["model_run"])

    def test_not_published_when_changed_or_incoherent(self):
        stamp = int((datetime.now(UTC) - timedelta(hours=4)).timestamp())
        self.assertIsNone(self.run_for([stamp, stamp + 10800])["model_run"])
        self.assertIsNone(self.run_for([stamp - 15 * 86400])["model_run"])  # meta.json périmé

    def test_not_invented_without_meta(self):
        entry = om.download(FakeHttp(routes()), SPECS["ecmwf_ens"])
        self.assertIsNone(entry["model_run"])
        self.assertIn("non exposée", entry["model_run_note"])


if __name__ == "__main__":
    unittest.main()
