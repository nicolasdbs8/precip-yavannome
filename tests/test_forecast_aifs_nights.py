"""Captures AIFS réelles et scénarios synthétiques explicites, sans réseau."""
import copy
import gzip
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from forecast_fixtures import FakeHttp, make_result, payload, real_entry, real_results, REAL_NOW
from weather_forecast import aggregation as agg
from weather_forecast.export import build_report, refresh_freshness, validate
from weather_forecast.nights import night_temperature
from weather_forecast.open_meteo import fetch, fetch_all, parse
from weather_forecast.presentation import provider_age, render
from weather_forecast.specs import AIFS_SINGLE, AIFS_ENS

UTC = timezone.utc


class AifsTests(unittest.TestCase):
    def result(self, spec=AIFS_ENS, members=51, drop=(), value=None):
        start = datetime(2026, 10, 20, tzinfo=UTC)
        body = payload(spec, start, 300, members=members, drop=drop,
                       value=value or (lambda variable, member, i, t: member / 10 if variable == 'precipitation' else member - 25))
        return make_result(spec, body, REAL_NOW)

    def test_real_captures_parse_single_and_51_members(self):
        for spec in (AIFS_SINGLE, AIFS_ENS):
            series = parse(real_entry(spec.key)['payload'], spec)
            stacks = series.members['precipitation'] if spec.kind == 'ensemble' else series.values
            self.assertEqual(len(stacks), 51 if spec.kind == 'ensemble' else 2)
            self.assertEqual(spec.native_step_hours, 6)
            self.assertTrue(all(b - a == timedelta(hours=1) for a, b in zip(series.times, series.times[1:])))

    def test_http_collection_explicit_endpoints_and_members(self):
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        http = FakeHttp({s.model_id: payload(s, now - timedelta(hours=1), 220) for s in (AIFS_SINGLE, AIFS_ENS)})
        results = fetch_all((AIFS_SINGLE, AIFS_ENS), http)
        self.assertTrue(all(r.usable for r in results.values()))
        self.assertEqual(len(results['aifs_ens'].series.members['temperature_2m']), 51)
        self.assertEqual({p['models'] for u, p, _ in http.calls if p}, {AIFS_SINGLE.model_id, AIFS_ENS.model_id})

    def test_precip_quantiles_and_probability_valid_denominator(self):
        r = self.result(members=5)
        r.series.members['precipitation']['member04'][30] = None
        block = agg.ensemble_period(r, agg.day_period(date(2026, 10, 21)))['precipitation']
        self.assertEqual(block['members_valid'], 4)
        self.assertEqual(block['median_mm'], 3.6)
        self.assertEqual(block['p10_mm'], 0.7)
        self.assertEqual(block['p90_mm'], 6.5)
        self.assertEqual(block['prob_ge_2mm'], .75)
        self.assertEqual(block['prob_ge_5mm'], .25)
        self.assertEqual(block['prob_ge_10mm'], 0)

    def test_hourly_precipitation_conserves_six_hour_totals(self):
        # Synthetic disaggregation of 12 mm / 6 h, never six copies of 12 mm.
        r = self.result(AIFS_SINGLE, value=lambda v, m, i, t: 2 if v == 'precipitation' else 5)
        d = date(2026, 10, 21)
        total = agg.deterministic_period(r, agg.day_period(d))['precipitation_mm']
        self.assertEqual(total, 4 * 12)
        periods = [agg.Period('half_day', d, 'TEST', agg.local_to_utc(d, h), agg.local_to_utc(d, h) + timedelta(hours=6)) for h in (0, 6, 12, 18)]
        self.assertEqual(sum(agg.deterministic_period(r, p)['precipitation_mm'] for p in periods), total)

    def test_real_api_six_hour_comparison_with_rounding_tolerance(self):
        for spec in (AIFS_SINGLE, AIFS_ENS):
            hourly = real_entry(spec.key)['payload']['hourly']
            path = Path(__file__).parent / 'fixtures' / 'open_meteo' / (spec.key + '_six_hour.json.gz')
            six = json.loads(gzip.decompress(path.read_bytes()))['hourly']
            checked = 0
            for key, values in six.items():
                if key == 'time':
                    continue
                lookup = dict(zip(hourly['time'], hourly[key]))
                for stamp, value in zip(six['time'], values):
                    samples = [lookup.get(stamp - i * 3600) for i in range(6)]
                    if value is not None and all(v is not None for v in samples):
                        # API precipitation rounded to 0.1 mm: six rounded values
                        # plus one rounded six-hour total imply <=0.35 mm error.
                        self.assertLessEqual(abs(sum(samples) - value), .350001)
                        checked += 1
            self.assertGreater(checked, 10)

    def test_consensus_identical_with_divergent_or_failed_aifs(self):
        conventional = real_results()
        baseline = build_report(conventional, REAL_NOW)
        for additions in ({s.key: self.result(s) for s in (AIFS_SINGLE, AIFS_ENS)},
                          fetch_all((AIFS_SINGLE, AIFS_ENS), FakeHttp({}))):
            out = build_report({**conventional, **additions}, REAL_NOW)
            for group in ('daily', 'half_days'):
                self.assertEqual([b['consensus'] for b in out[group]], [b['consensus'] for b in baseline[group]])
            self.assertEqual(out['status'], baseline['status'])
            self.assertEqual(out['data_fetched_at'], baseline['data_fetched_at'])
            self.assertEqual(validate(out), [])

    def test_stale_aifs_does_not_age_conventional_report(self):
        r = self.result()
        r.fetched_at -= timedelta(days=1)
        out = build_report({**real_results(), 'aifs_ens': r}, REAL_NOW)
        self.assertEqual(out['status'], 'FRESH')
        self.assertEqual(out['providers']['ensemble']['aifs_ens']['freshness']['status'], 'STALE')
        self.assertEqual(refresh_freshness(out, REAL_NOW + timedelta(hours=13))['status'], 'STALE')

    def test_failed_collection_has_no_fetch_time_or_freshness(self):
        failures = fetch_all((AIFS_SINGLE, AIFS_ENS), FakeHttp({}))
        # A missing offline cache and a malformed response are also failed collections.
        malformed = payload(AIFS_SINGLE, REAL_NOW, 2)
        malformed['hourly']['precipitation'].pop()
        for result in (*failures.values(), fetch(AIFS_SINGLE, None, offline=True),
                       fetch(AIFS_SINGLE, FakeHttp({AIFS_SINGLE.model_id: malformed}))):
            self.assertEqual(result.status, 'ERROR')
            self.assertIsNone(result.fetched_at)
            self.assertIsNone(result.series)
        out = build_report({**real_results(), **failures}, REAL_NOW)
        self.assertEqual(validate(out), [])
        self.assertEqual(out['status'], 'FRESH')
        for report in (out, refresh_freshness(out, REAL_NOW + timedelta(hours=1))):
            for group, key in (('deterministic', 'aifs_single'), ('ensemble', 'aifs_ens')):
                block = report['providers'][group][key]
                self.assertIsNone(block['fetched_at'])
                self.assertIsNone(block['freshness'])
                label = provider_age(report, group, key)
                self.assertIn('ERROR', label)
                self.assertIn('aucune donnée collectée', label)
                self.assertNotIn('FRESH', label)

    def test_legacy_error_is_not_presented_as_fresh_data(self):
        failed = fetch_all((AIFS_SINGLE, AIFS_ENS), FakeHttp({}))
        out = build_report({**real_results(), **failed}, REAL_NOW)
        for group, key in (('deterministic', 'aifs_single'), ('ensemble', 'aifs_ens')):
            # Simulate the incoherent metadata produced before this correction.
            block = out['providers'][group][key]
            block['fetched_at'] = REAL_NOW.isoformat()
            block['freshness'] = {'status': 'FRESH'}
            self.assertNotIn('FRESH', provider_age(out, group, key))
        refreshed = refresh_freshness(out, REAL_NOW + timedelta(hours=1))
        for group, key in (('deterministic', 'aifs_single'), ('ensemble', 'aifs_ens')):
            self.assertIsNone(refreshed['providers'][group][key]['freshness'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'forecast.html'
            render(out, path, REAL_NOW)
            html = path.read_text(encoding='utf8')
            self.assertIn('ERROR · aucune donnée collectée', html)
            self.assertNotIn('ERROR · FRESH', html)

    def test_night_minimum_and_member_probability(self):
        p = agg.night_period(date(2026, 10, 21))
        # Different members freeze at different hours: hourly probability averaging
        # would give a lower result than the correct memberwise minimum.
        r = self.result(members=3, value=lambda v, m, i, t: -2 if t == p.start + timedelta(hours=m) else 5)
        b = night_temperature(r, p)
        self.assertEqual(b['members_valid'], 3)
        self.assertEqual(b['prob_temperature_below_0c'], 1)
        self.assertEqual(b['min_median_c'], -2)
        self.assertEqual(b['reading'], 'INSUFFICIENT')
        det = self.result(AIFS_SINGLE, value=lambda v, m, i, t: -3 if t == p.start else 4)
        self.assertEqual(night_temperature(det, p)['temperature_min_c'], -3)
        self.assertTrue(night_temperature(det, p)['below_0c'])

    def test_missing_member_excluded_and_empty_is_not_zero(self):
        p = agg.night_period(date(2026, 10, 21))
        r = self.result(members=2)
        r.series.members['temperature_2m']['member01'][r.series.times.index(p.start)] = None
        b = night_temperature(r, p)
        self.assertEqual(b['members_valid'], 1)
        self.assertEqual(b['availability'], 'PARTIAL_MEMBERS')
        r.series.members['temperature_2m'] = {}
        b = night_temperature(r, p)
        self.assertEqual(b['members_valid'], 0)
        self.assertIsNone(b['prob_temperature_below_0c'])
        self.assertEqual(b['availability'], 'INCOMPLETE')

    def test_incomplete_night_does_not_publish_probability(self):
        p = agg.night_period(date(2026, 10, 21))
        r = self.result()
        i = r.series.times.index(p.end)
        for values in r.series.members['temperature_2m'].values():
            values[i] = None
        self.assertIsNone(night_temperature(r, p)['prob_temperature_below_0c'])

    def test_dst_spring_and_autumn(self):
        for day, duration in ((date(2026, 3, 28), 11), (date(2026, 10, 24), 13)):
            p = agg.night_period(day)
            self.assertEqual(p.hours, duration)
            start = p.start - timedelta(hours=1)
            r = make_result(AIFS_SINGLE, payload(AIFS_SINGLE, start, 60, value=lambda *args: -1), start)
            b = night_temperature(r, p)
            self.assertEqual(b['samples_expected'], duration + 1)
            self.assertEqual(b['temperature_min_c'], -1)

    def test_eight_nights_schema_html_and_legacy_reader(self):
        out = build_report({**real_results(), 'aifs_single': self.result(AIFS_SINGLE), 'aifs_ens': self.result()}, REAL_NOW)
        self.assertEqual(len(out['nights']), 8)
        self.assertEqual(validate(out), [])
        bad = copy.deepcopy(out)
        bad['nights'][0]['ensemble']['aifs_ens']['prob_temperature_below_0c'] = 2
        self.assertTrue(validate(bad))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'forecast.html'
            render(out, path, REAL_NOW)
            html = path.read_text(encoding='utf8')
            for text in ('Prévisions IA — ECMWF AIFS', 'Surveillance du gel — prochaines nuits', 'ni sol ni bourgeons', 'AIFS ENS'):
                self.assertIn(text, html)
            old = json.loads(Path('forecast_zp1.json').read_text(encoding='utf8'))
            old.pop('nights', None)
            self.assertEqual(validate(old), [])
            render(old, path, REAL_NOW + timedelta(days=90))


if __name__ == '__main__':
    unittest.main()
