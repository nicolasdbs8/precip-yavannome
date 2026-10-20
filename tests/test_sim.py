"""SIM hors réseau : fixtures extraites du QUOT_SIM2_latest réel et de l'API tabulaire."""
import gzip
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import requests

from soil_hydrology.cache import Cache
from soil_hydrology.models import Query
from soil_hydrology.sim import (MeteoFranceSimProvider, SimFormatError, SimGrid, extract_rows, normalize_rows,
                                tabular_rows)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = (FIXTURES / "sim_quot_sample.csv").read_bytes()
A = SimGrid(8360, 22090, 46.8392, 5.43194, "parcel", 4614.1, 1.0, 0.0)
B = SimGrid(8360, 22010, 46.7673, 5.42782, "adjacent", 0.0, 0.0, 33.9)
CSV_URL = "https://meteofrance.test/QUOT_SIM2_latest.csv.gz"
PUBLISHED = "Tue, 20 Oct 2026 08:03:26 GMT"
LATEST = {"id": "rid-latest", "title": "QUOT_SIM2_latest", "url": CSV_URL, "last_modified": "2026-10-20T08:03:26+00:00",
          "extras": {"analysis:last-modified-at": "2026-10-20T08:03:26+00:00",
                     "analysis:parsing:finished_at": "2026-10-20T09:31:41+00:00"}}
QUERY = Query(46.8, 5.45, date(2026, 9, 30), date(2026, 10, 20))


class Response:
    def __init__(self, status=200, body=b"", headers=None, payload=None):
        self.status_code, self.headers, self._payload = status, headers or {}, payload
        self.raw = io.BytesIO(body)
        self.text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHTTP:
    """Routage minimal par URL ; `fail` simule une panne réseau."""

    def __init__(self, resource=LATEST, head_date=PUBLISHED, tabular=True, body=None, fail=False):
        self.resource, self.head_date, self.tabular, self.fail = resource, head_date, tabular, fail
        self.body = gzip.compress(SAMPLE) if body is None else body
        self.calls = []

    def head(self, url, **kwargs):
        self.calls.append(("HEAD", url))
        if self.fail:
            raise requests.ConnectionError("réseau coupé")
        return Response(headers={"Last-Modified": self.head_date})

    def get(self, url, params=None, headers=None, **kwargs):
        self.calls.append(("GET", url))
        if self.fail:
            raise requests.ConnectionError("réseau coupé")
        if "datasets" in url:
            return Response(body=json.dumps({"resources": [self.resource]}).encode())
        if "tabular" in url:
            if not self.tabular:
                return Response(status=503)
            page = json.loads((FIXTURES / "tabular_page.json").read_text(encoding="utf-8"))
            if params and params["LAMBY__exact"] != 22090:
                page["data"] = [dict(r, LAMBY=params["LAMBY__exact"]) for r in page["data"]]
            return Response(payload=page)
        if headers and headers.get("If-None-Match") == '"etag-1"':
            return Response(status=304)
        return Response(body=self.body, headers={"ETag": '"etag-1"', "Last-Modified": PUBLISHED,
                                                 "Content-Length": str(len(self.body))})


class Parsing(unittest.TestCase):
    def test_gzip_stream_real_header_and_filter(self):
        with gzip.GzipFile(fileobj=io.BytesIO(gzip.compress(SAMPLE))) as stream:
            rows, stats = extract_rows(stream, [A, B])
        self.assertEqual(stats["header"][:3], ["LAMBX", "LAMBY", "DATE"])
        self.assertIn("SWI", stats["header"])
        self.assertIn("SSWI_10J", stats["header"])
        self.assertEqual(stats["lines_read"], 43)
        self.assertEqual((len(rows[A.id]), len(rows[B.id]), stats["lines_matched"]), (20, 20, 40))
        self.assertEqual({r["LAMBY"] for r in rows[A.id]}, {"22090"})

    def test_truncated_or_corrupted_gzip(self):
        broken = gzip.compress(SAMPLE)[:-200]
        with self.assertRaises(SimFormatError):
            with gzip.GzipFile(fileobj=io.BytesIO(broken)) as stream:
                extract_rows(stream, [A])
        with self.assertRaises(SimFormatError):
            with gzip.GzipFile(fileobj=io.BytesIO(b"not a gzip file at all")) as stream:
                extract_rows(stream, [A])

    def test_bad_header_and_malformed_local_line(self):
        with self.assertRaises(SimFormatError):
            extract_rows(io.StringIO("DATE;VALUE\n20240101;2\n"), [A])
        rows, stats = extract_rows(io.StringIO("LAMBX;LAMBY;DATE;SWI\n8360;22090;20240101\n8360;22090;20240102;0.2\n"), [A])
        self.assertEqual(stats["malformed_local_lines"], 1)
        self.assertEqual(len(rows[A.id]), 1)

    def test_normalization_units_aliases_missing_and_zero(self):
        with gzip.GzipFile(fileobj=io.BytesIO(gzip.compress(SAMPLE))) as stream:
            rows, _ = extract_rows(stream, [A])
        values = {(o.date, o.variable): o for o in normalize_rows(rows[A.id], QUERY, A, "2026-10-20T10:00:00+00:00", LATEST)}
        swi = values["2026-10-19", "swi_sim"]
        self.assertEqual((swi.value, swi.unit, swi.original_variable, swi.grid_id), (0.102, "1", "SWI", A.id))
        self.assertEqual(values["2026-10-19", "rainfall"].unit, "mm")
        self.assertEqual(values["2026-10-19", "snowfall"].value, 0.0)  # 0 réel, pas None
        self.assertEqual(values["2026-10-19", "precipitation"].original_variable, "PRELIQ+PRENEI")
        # Anciens fichiers : suffixe _Q ; valeur vide = absente, jamais 0.
        old = [{"DATE": "20261001", "SWI_Q": "0.25", "PRELIQ_Q": "", "PRENEI_Q": "0.0", "DRAINC_Q": "abc"}]
        values = {o.variable: o for o in normalize_rows(old, QUERY, A, "2026-10-20T10:00:00+00:00", LATEST)}
        self.assertEqual((values["swi_sim"].value, values["swi_sim"].original_variable), (0.25, "SWI_Q"))
        self.assertIsNone(values["rainfall"].value)
        self.assertEqual(values["rainfall"].quality, "missing")
        self.assertEqual(values["drainage"].quality, "invalid")
        self.assertIsNone(values["precipitation"].value)  # pas de total si un membre manque

    def test_tabular_rows_are_converted_to_csv_conventions(self):
        rows, stats = tabular_rows(FakeHTTP(), "rid-latest", [A])
        self.assertEqual(stats["lines_matched"], 3)
        first = rows[A.id][0]
        self.assertRegex(first["DATE"], r"^\d{8}$")
        self.assertNotIn("__id", first)
        self.assertIsInstance(first["SWI"], str)


class Provider(unittest.TestCase):
    def provider(self, http, tmp, **kwargs):
        return MeteoFranceSimProvider(Cache(Path(tmp) / "c.sqlite"), [A, B], http=http, **kwargs)

    def test_tabular_used_only_when_it_matches_the_published_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.provider(FakeHTTP(), tmp).fetch(QUERY)
            self.assertEqual(result.details["extractions"][0]["transport"], "tabular")
            result = self.provider(FakeHTTP(head_date="Wed, 21 Oct 2026 08:00:00 GMT"), tmp).fetch(QUERY)
            extraction = result.details["extractions"][0]
            self.assertEqual(extraction["transport"], "csv.gz")
            self.assertEqual(extraction["lines_matched"], 40)
            self.assertEqual(extraction["etag"], '"etag-1"')
            self.assertEqual(len(extraction["sha256"]), 64)
            self.assertTrue(extraction["fallback"][0].startswith("tabulaire écartée : copie tabulaire du"))

    def test_csv_conditional_get_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = FakeHTTP()
            first = self.provider(http, tmp, transport="csv").fetch(QUERY)
            second = self.provider(http, tmp, transport="csv").fetch(QUERY)
            self.assertFalse(first.details["extractions"][0]["cache_hit"])
            self.assertTrue(second.details["extractions"][0]["cache_hit"])
            self.assertEqual(len(first.observations), len(second.observations))
            self.assertEqual({o.grid_id for o in second.observations}, {A.id, B.id})

    def test_truncated_download_is_an_error_not_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.provider(FakeHTTP(tabular=False, body=gzip.compress(SAMPLE)[:-300]), tmp).fetch(QUERY)
            self.assertEqual(result.status, "temporary_error")
            self.assertFalse(result.observations)
            self.assertTrue(any("SimFormatError" in m for m in result.messages))

    def test_network_failure_falls_back_to_cache_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.provider(FakeHTTP(), tmp, transport="csv").fetch(QUERY)
            http = FakeHTTP(fail=True)
            provider = self.provider(http, tmp, transport="csv", refresh=True)
            # Catalogue en cache (rafraîchissement impossible) ; le CSV échoue → repli signalé.
            result = provider.fetch(QUERY)
            self.assertEqual(result.status, "stale")
            self.assertTrue(any("Repli sur l'extraction en cache" in m for m in result.messages))
            self.assertTrue(result.observations)

    def test_offline_uses_cache_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.provider(FakeHTTP(), tmp, transport="csv").fetch(QUERY)
            http = FakeHTTP(fail=True)
            result = self.provider(http, tmp, offline=True).fetch(QUERY)
            self.assertTrue(result.observations)
            self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
