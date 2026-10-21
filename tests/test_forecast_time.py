"""Point ZP1 et gestion du temps : UTC brut, journées Europe/Paris de 23/24/25 h."""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from forecast_fixtures import SPECS, make_result, payload
from weather_forecast import aggregation as agg
from weather_forecast.open_meteo import request_params
from weather_forecast.specs import ALL_MODELS, POINT

UTC = timezone.utc
ROOT = Path(__file__).parent.parent


class Geography(unittest.TestCase):
    def test_exact_zp1_point(self):
        self.assertEqual((POINT["lat"], POINT["lon"]), (46.8039042, 5.450162))
        self.assertEqual(POINT["parcel_id"], "39160000ZP0001")
        self.assertEqual(POINT["timezone"], "Europe/Paris")
        self.assertEqual(str(agg.PARIS), "Europe/Paris")

    def test_same_centroid_as_hydrology_module(self):
        hydro = json.loads((ROOT / "hydrologie_yavannome.json").read_text(encoding="utf-8"))
        self.assertEqual(hydro["parcel"]["idu"], POINT["parcel_id"])
        self.assertEqual((hydro["parcel"]["centroid"]["lat"], hydro["parcel"]["centroid"]["lon"]), (POINT["lat"], POINT["lon"]))

    def test_every_request_uses_point_and_utc(self):
        for spec in ALL_MODELS:
            params = request_params(spec)
            self.assertEqual((params["latitude"], params["longitude"]), (46.8039042, 5.450162))
            self.assertEqual((params["timezone"], params["timeformat"]), ("GMT", "unixtime"))


class LocalPeriods(unittest.TestCase):
    def test_utc_to_paris_summer_and_winter(self):
        self.assertEqual(agg.to_local(datetime(2026, 10, 21, 10, 0, tzinfo=UTC)).isoformat(), "2026-10-21T12:00:00+02:00")
        self.assertEqual(agg.to_local(datetime(2026, 10, 27, 10, 0, tzinfo=UTC)).isoformat(), "2026-10-27T11:00:00+01:00")

    def test_ambiguous_local_hour_on_25_october(self):
        first = agg.to_local(datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
        second = agg.to_local(datetime(2026, 10, 25, 1, 30, tzinfo=UTC))
        self.assertEqual((first.hour, first.minute, first.utcoffset()), (2, 30, timedelta(hours=2)))
        self.assertEqual((second.hour, second.minute, second.utcoffset()), (2, 30, timedelta(hours=1)))
        self.assertEqual(second.fold, 1)

    def test_normal_day(self):
        day = agg.day_period(date(2026, 10, 22))
        self.assertEqual((day.start, day.end), (datetime(2026, 10, 21, 22, tzinfo=UTC), datetime(2026, 10, 22, 22, tzinfo=UTC)))
        self.assertEqual(day.hours, 24)

    def test_winter_time_change_day_has_25_hours(self):
        day = agg.day_period(date(2026, 10, 25))
        self.assertEqual((day.start, day.end), (datetime(2026, 10, 24, 22, tzinfo=UTC), datetime(2026, 10, 25, 23, tzinfo=UTC)))
        self.assertEqual(day.hours, 25)
        self.assertEqual(agg.day_period(date(2026, 10, 26)).hours, 24)
        self.assertEqual(agg.day_period(date(2027, 3, 28)).hours, 23)  # passage à l'heure d'été

    def test_half_days_follow_local_clock(self):
        before, after = agg.half_day_period(date(2026, 10, 24), "MORNING"), agg.half_day_period(date(2026, 10, 25), "MORNING")
        self.assertEqual((before.start.hour, before.end.hour), (4, 10))   # 06–12 CEST
        self.assertEqual((after.start.hour, after.end.hour), (5, 11))     # 06–12 CET
        noon = agg.half_day_period(date(2026, 10, 25), "AFTERNOON")
        self.assertEqual((noon.start.hour, noon.end.hour, noon.hours), (11, 17, 6))

    def test_step_membership_excludes_start_includes_end(self):
        day = agg.day_period(date(2026, 10, 25))
        times = [day.start + timedelta(hours=i) for i in range(27)]
        idx = agg.indices(times, day.start, day.end)
        self.assertEqual((times[idx[0]], times[idx[-1]], len(idx)), (day.start + timedelta(hours=1), day.end, 25))

    def test_daily_sum_counts_actual_steps(self):
        """1 mm/h : 25 mm le 25/10, 24 mm la veille — jamais 24 pas supposés."""
        spec = SPECS["ecmwf"]
        result = make_result(spec, payload(spec, datetime(2026, 10, 22, tzinfo=UTC), 24 * 7,
                                           lambda v, m, i, t: 1.0 if v == "precipitation" else 5.0),
                             datetime(2026, 10, 22, 6, tzinfo=UTC))
        sunday = agg.deterministic_period(result, agg.day_period(date(2026, 10, 25)))
        saturday = agg.deterministic_period(result, agg.day_period(date(2026, 10, 24)))
        self.assertEqual((sunday["precipitation_mm"], sunday["hours_valid"]), (25.0, 25))
        self.assertEqual((saturday["precipitation_mm"], saturday["hours_valid"]), (24.0, 24))

    def test_local_today(self):
        self.assertEqual(agg.local_today(datetime(2026, 10, 24, 22, 30, tzinfo=UTC)), date(2026, 10, 25))
        self.assertEqual(agg.local_today(datetime(2026, 10, 25, 22, 30, tzinfo=UTC)), date(2026, 10, 25))


if __name__ == "__main__":
    unittest.main()
