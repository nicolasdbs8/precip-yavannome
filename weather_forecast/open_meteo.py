"""Accès Open-Meteo : un appel par modèle, en UTC, séries validées avant usage.

Un modèle en panne n'empêche pas les autres d'être collectés : chaque appel
renvoie un `ModelResult` avec son propre statut. Les heures sont demandées en
GMT (`timezone=GMT`, `timeformat=unixtime`) ; la conversion Europe/Paris n'a
lieu qu'à l'agrégation.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from soil_hydrology.transport import session as base_session

from .specs import FORBIDDEN_MODEL_TOKENS, POINT, SECONDARY_VARIABLES, ModelSpec

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
META_URL = {"deterministic": "https://api.open-meteo.com/data/{}/static/meta.json",
            "ensemble": "https://ensemble-api.open-meteo.com/data/{}/static/meta.json"}
USER_AGENT = "precip-yavannome/1.0 (+https://github.com/nicolasdbs8/precip-yavannome ; usage personnel non commercial)"
TIMEOUT = (10, 60)          # (connexion, lecture) en secondes
PAST_DAYS = 3               # heures passées : cumuls « 24–72 h précédentes » dès J0
FORECAST_DAYS = 9           # couvre la fin de J+7 locale quelle que soit l'heure UTC
CACHE_TTL_S = 2 * 3600      # relance immédiate ou tests : pas de nouvel appel identique
MODEL_RUN_MAX_AGE_H = 36    # run annoncé plus ancien (ou futur) : meta.json jugé incohérent, non publié
EXPECTED_UNITS = {"temperature_2m": "°C", "surface_temperature": "°C", "precipitation": "mm", "rain": "mm",
                  "snowfall": "cm", "wind_speed_10m": "km/h", "wind_gusts_10m": "km/h", "relative_humidity_2m": "%",
                  "et0_fao_evapotranspiration": "mm"}
log = logging.getLogger(__name__)


class OpenMeteoError(RuntimeError):
    """Réponse inutilisable (HTTP, réseau, format) pour un modèle."""


class SeriesError(OpenMeteoError):
    """Séries horaires incohérentes : jamais réparées silencieusement."""


@dataclass
class Series:
    times: list[datetime]                       # UTC, pas horaire strict
    values: dict[str, list[float | None]]       # déterministe : variable → valeurs
    members: dict[str, dict[str, list[float | None]]] = field(default_factory=dict)  # ensemble : variable → membre → valeurs
    grid: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


@dataclass
class ModelResult:
    spec: ModelSpec
    status: str                     # FRESH | PARTIAL | UNAVAILABLE | ERROR
    reason: str
    series: Series | None = None
    fetched_at: datetime | None = None
    from_cache: bool = False
    model_run: str | None = None
    model_run_note: str | None = None
    request: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status in ("FRESH", "PARTIAL") and self.series is not None


def session():
    """Session du dépôt (retry borné 429/5xx, backoff) + User-Agent identifiable."""
    http = base_session()
    http.headers["User-Agent"] = USER_AGENT
    return http


def check_explicit(model_id: str) -> str:
    if not model_id or any(token in model_id for token in FORBIDDEN_MODEL_TOKENS):
        raise ValueError(f"Modèle Open-Meteo non explicite refusé : {model_id!r}")
    return model_id


def endpoint(spec: ModelSpec) -> str:
    return ENSEMBLE_URL if spec.kind == "ensemble" else FORECAST_URL


def request_params(spec: ModelSpec) -> dict:
    return {"latitude": POINT["lat"], "longitude": POINT["lon"], "models": check_explicit(spec.model_id),
            "hourly": ",".join(spec.variables), "timezone": "GMT", "timeformat": "unixtime",
            "past_days": PAST_DAYS, "forecast_days": FORECAST_DAYS, "temperature_unit": "celsius",
            "wind_speed_unit": "kmh", "precipitation_unit": "mm", "cell_selection": "land"}


def _number(value, where):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SeriesError(f"{where} : valeur non numérique {value!r}")
    return float(value)


def parse(payload: dict, spec: ModelSpec) -> Series:
    """Valide et convertit une réponse JSON Open-Meteo (une seule localisation, un seul modèle)."""
    if not isinstance(payload, dict):
        raise OpenMeteoError("réponse JSON inattendue (pas un objet)")
    if payload.get("error"):
        raise OpenMeteoError(f"Open-Meteo : {payload.get('reason')}")
    if payload.get("utc_offset_seconds") != 0:
        raise SeriesError(f"décalage UTC {payload.get('utc_offset_seconds')!r} : les séries doivent être en UTC")
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or not hourly.get("time"):
        raise SeriesError("bloc hourly absent ou vide")
    raw_times = hourly["time"]
    if any(isinstance(t, bool) or not isinstance(t, int) for t in raw_times):
        raise SeriesError("hourly.time doit contenir des horodatages Unix entiers (timeformat=unixtime)")
    steps = {b - a for a, b in zip(raw_times, raw_times[1:])}
    if steps - {3600}:
        raise SeriesError(f"pas de temps irrégulier ({sorted(steps)} s) : 3600 s attendu")
    times = [datetime.fromtimestamp(t, timezone.utc) for t in raw_times]
    units = payload.get("hourly_units", {})
    series = Series(times, {}, grid={"lat": payload.get("latitude"), "lon": payload.get("longitude"),
                                     "elevation_m": payload.get("elevation")})
    for variable in spec.variables:
        keys = {k: v for k, v in hourly.items() if k == variable or k.startswith(variable + "_member")}
        if not keys:
            series.missing.append(variable)
            continue
        for key, values in keys.items():
            if not isinstance(values, list) or len(values) != len(times):
                raise SeriesError(f"{key} : {len(values) if isinstance(values, list) else '?'} valeurs pour {len(times)} heures")
            parsed = [_number(v, key) for v in values]
            expected = EXPECTED_UNITS.get(variable)
            # Variable non servie : série entièrement nulle et unité « undefined », ignorée.
            if expected and any(v is not None for v in parsed) and units.get(key) not in (None, expected):
                raise SeriesError(f"{key} : unité {units.get(key)!r}, {expected!r} attendu")
            if spec.kind == "ensemble":
                member = "member00" if key == variable else key[len(variable) + 1:]
                series.members.setdefault(variable, {})[member] = parsed
            else:
                series.values[variable] = parsed
        if spec.kind == "deterministic" and all(v is None for v in series.values[variable]):
            series.missing.append(variable)
    return series


def last_valid_time(series: Series, variable: str = "precipitation") -> datetime | None:
    stacks = series.members.get(variable, {}).values() if series.members else [series.values.get(variable, [])]
    best = None
    for values in stacks:
        for i in range(len(values) - 1, -1, -1):
            if values[i] is not None:
                best = series.times[i] if best is None or series.times[i] > best else best
                break
    return best


def assess(spec: ModelSpec, series: Series, fetched_at: datetime) -> tuple[str, str]:
    """Statut d'un modèle collecté sans erreur."""
    last = last_valid_time(series)
    if last is None or last <= fetched_at:
        return "UNAVAILABLE", "aucune précipitation prévue publiée après l'heure de collecte"
    problems = []
    # Manque anormal : variable demandée, non secondaire, que ce modèle sert d'habitude.
    required = [v for v in series.missing if v not in spec.not_provided and v not in SECONDARY_VARIABLES]
    if required:
        problems.append("variables manquantes : " + ", ".join(required))
    if spec.members_expected:
        received = len(series.members.get("precipitation", {}))
        if received < spec.members_expected:
            problems.append(f"{received} membres reçus sur {spec.members_expected}")
        if received > spec.members_expected:
            raise SeriesError(f"{received} membres pour {spec.members_expected} attendus")
    ahead = (last - fetched_at).total_seconds() / 3600
    if ahead < 0.5 * spec.nominal_horizon_hours:
        problems.append(f"horizon observé {ahead:.0f} h, nominal {spec.nominal_horizon_hours} h")
    if problems:
        return "PARTIAL", " ; ".join(problems)
    note = f" ; non fournies par ce modèle : {', '.join(series.missing)}" if series.missing else ""
    return "FRESH", f"horizon observé {ahead:.0f} h après la collecte{note}"


def _get_json(http, url, params=None):
    try:
        response = http.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:  # inclut RetryError après les réessais bornés
        raise OpenMeteoError(f"réseau : {type(exc).__name__} : {exc}") from exc
    if response.status_code != 200:
        try:
            reason = response.json().get("reason")
        except ValueError:
            reason = response.text[:200]
        raise OpenMeteoError(f"HTTP {response.status_code} : {reason}")
    try:
        return response.json()
    except ValueError as exc:
        raise OpenMeteoError("réponse non JSON") from exc


def _run_time(http, spec: ModelSpec) -> str | None:
    if not spec.meta_id:
        return None
    try:
        meta = _get_json(http, META_URL[spec.kind].format(spec.meta_id))
        stamp = meta.get("last_run_initialisation_time")
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if isinstance(stamp, int) else None
    except OpenMeteoError as exc:
        log.info("%s : meta.json indisponible (%s)", spec.key, exc)
        return None


def download(http, spec: ModelSpec) -> dict:
    """Prévision + heure du run lue avant et après (publiée seulement si identique)."""
    url, params = endpoint(spec), request_params(spec)
    before = _run_time(http, spec)
    payload = _get_json(http, url, params)
    after = _run_time(http, spec) if before else None
    fetched = datetime.now(timezone.utc)
    if before and before == after:
        age = (fetched - datetime.fromisoformat(before)) / timedelta(hours=1)
        if 0 <= age <= MODEL_RUN_MAX_AGE_H:
            run, note = before, "meta.json last_run_initialisation_time, identique avant et après la requête"
        else:
            run, note = None, f"meta.json incohérent (run annoncé {before}) : non publié"
    elif before:
        run, note = None, "run modifié pendant la collecte : non publié"
    else:
        run, note = None, "heure du run non exposée par Open-Meteo pour ce modèle"
    return {"fetched_at": fetched.isoformat(timespec="seconds"), "payload": payload, "model_run": run, "model_run_note": note}


def fetch(spec: ModelSpec, http, cache=None, offline=False, refresh=False) -> ModelResult:
    url, params = endpoint(spec), request_params(spec)
    request = {"endpoint": url, "params": params}
    key = cache.key("open-meteo-v1", url, params) if cache else None
    entry = cache.get(key, None if offline else (0 if refresh else CACHE_TTL_S)) if cache else None
    from_cache = entry is not None
    try:
        if entry is None:
            if offline:
                raise OpenMeteoError("absent du cache local (--offline)")
            entry = download(http, spec)
            if cache:
                cache.put(key, entry)
        fetched_at = datetime.fromisoformat(entry["fetched_at"])
        series = parse(entry["payload"], spec)
        status, reason = assess(spec, series, fetched_at)
    except OpenMeteoError as exc:
        log.warning("%s (%s) : %s", spec.key, spec.model_id, exc)
        return ModelResult(spec, "ERROR", str(exc), request=request)
    if from_cache:
        reason += f" ; réponse du cache local ({entry['fetched_at']})"
    return ModelResult(spec, status, reason, series, fetched_at, from_cache, entry.get("model_run"),
                       entry.get("model_run_note"), request)


def fetch_all(specs, http, cache=None, offline=False, refresh=False) -> dict[str, ModelResult]:
    return {spec.key: fetch(spec, http, cache, offline, refresh) for spec in specs}


def hours_ahead(result: ModelResult) -> float | None:
    last = last_valid_time(result.series) if result.series else None
    return None if last is None or result.fetched_at is None else round((last - result.fetched_at) / timedelta(hours=1), 1)
