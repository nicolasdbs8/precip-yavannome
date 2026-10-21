"""Fixtures hors réseau du module de prévision : réponses Open-Meteo réelles
(capturées au point ZP1 le 21/10/2026 vers 10:01 UTC, gzip) et générateur de
réponses synthétiques au format exact de l'API."""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weather_forecast.open_meteo import ModelResult, assess, endpoint, parse, request_params
from weather_forecast.specs import ALL_MODELS

for _name in ("weather_forecast", "forecast"):  # pannes simulées : journal silencieux pendant les tests
    logging.getLogger(_name).setLevel(logging.CRITICAL)

FIXTURES = Path(__file__).parent / "fixtures" / "open_meteo"
REAL_NOW = datetime(2026, 10, 21, 10, 5, tzinfo=timezone.utc)
SPECS = {s.key: s for s in ALL_MODELS}
UNITS = {"temperature_2m": "°C", "surface_temperature": "°C", "precipitation": "mm", "rain": "mm", "snowfall": "cm",
         "wind_speed_10m": "km/h", "wind_gusts_10m": "km/h", "relative_humidity_2m": "%", "et0_fao_evapotranspiration": "mm"}


def real_entry(key: str) -> dict:
    return json.loads(gzip.decompress((FIXTURES / f"{key}.json.gz").read_bytes()))


def make_result(spec, payload, fetched_at, model_run=None) -> ModelResult:
    series = parse(payload, spec)
    status, reason = assess(spec, series, fetched_at)
    return ModelResult(spec, status, reason, series, fetched_at, False, model_run, "fixture",
                       {"endpoint": endpoint(spec), "params": request_params(spec)})


def real_results() -> dict:
    out = {}
    for key, spec in SPECS.items():
        entry = real_entry(key)
        out[key] = make_result(spec, entry["payload"], datetime.fromisoformat(entry["fetched_at"]), entry["model_run"])
    return out


def payload(spec, start: datetime, hours: int, value=lambda variable, member, i, t: 0.0, until: datetime | None = None,
            members: int | None = None, drop=()) -> dict:
    """Réponse synthétique : `value(variable, membre, i, t)` ; null après `until` ; variables `drop` absentes."""
    times = [start + timedelta(hours=i) for i in range(hours)]
    hourly = {"time": [int(t.timestamp()) for t in times]}
    units = {"time": "unixtime"}
    count = members or spec.members_expected or 1
    for variable in spec.variables:
        if variable in drop:
            continue
        for m in range(count):
            key = variable if m == 0 else f"{variable}_member{m:02d}"
            hourly[key] = [None if until and t > until else value(variable, m, i, t) for i, t in enumerate(times)]
            units[key] = UNITS[variable]
    return {"latitude": 46.8, "longitude": 5.45, "elevation": 210.0, "utc_offset_seconds": 0, "timezone": "GMT",
            "hourly_units": units, "hourly": hourly}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code, self.body = status_code, body

    def json(self):
        if isinstance(self.body, (dict, list)):
            return self.body
        raise ValueError("pas du JSON")

    @property
    def text(self):
        return json.dumps(self.body) if isinstance(self.body, (dict, list)) else str(self.body)


class FakeHttp:
    """`routes[model_id]` : payload (200) ou code HTTP ; meta.json via `runs[meta_id]` (liste consommée)."""

    def __init__(self, routes, runs=None):
        self.routes, self.runs, self.calls = routes, runs or {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        if "/static/meta.json" in url:
            meta_id = url.split("/data/")[1].split("/")[0]
            runs = self.runs.get(meta_id)
            if not runs:
                return FakeResponse(404, {"error": True, "reason": "Not Found"})
            return FakeResponse(200, {"last_run_initialisation_time": runs.pop(0) if len(runs) > 1 else runs[0]})
        route = self.routes.get((params or {}).get("models"), 500)
        if isinstance(route, int):
            return FakeResponse(route, {"error": True, "reason": f"HTTP {route} simulé"})
        return FakeResponse(200, route)
