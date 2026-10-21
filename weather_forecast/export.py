"""`forecast_zp1.json` : prévision ponctuelle ZP1, un bloc par modèle, sans moyenne.

`null` = valeur non calculable (hors horizon, pas manquant, variable non
fournie) ; 0 = valeur prévue nulle. La divergence entre modèles est conservée
telle quelle ; seules des métriques de consensus explicites sont ajoutées.
"""
from __future__ import annotations

import copy
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from . import aggregation as agg
from . import confidence
from .open_meteo import CACHE_TTL_S, PAST_DAYS, hours_ahead, last_valid_time
from .specs import ATTRIBUTION, DETERMINISTIC, ENSEMBLES, POINT

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "schema" / "forecast.schema.json"
MAX_AGE_HOURS = 12
DAYS = 8   # J0 (aujourd'hui, heure de Paris) à J+7
# Clés interdites partout dans le JSON : pas de moyenne inter-modèles, pas de décision agronomique.
FORBIDDEN_KEY_PARTS = ("average", "auger", "tariere", "workab", "travaillab", "go_no_go", "decision")
LIMITS = [
    "Prévisions de modèles numériques interpolées au point ZP1 par Open-Meteo : la maille la plus proche "
    "(1,5 à 11 km) ne résout ni le relief local, ni la haie, ni les averses isolées.",
    "Ce ne sont pas des observations : la pluie réellement tombée est mesurée par Rain "
    "(ANTILOPE / COMÉPHORE) et l'état du sol par le module Sol / Hydrologie.",
    "Les heures passées (cumuls 24–72 h précédents, J0 écoulé) proviennent des runs Open-Meteo antérieurs, "
    "pas de mesures.",
    "Au-delà de 90 h (IFS) les pas natifs deviennent tri-horaires puis hexa-horaires : la répartition horaire "
    "est celle d'Open-Meteo et les demi-journées lointaines sont approximatives.",
    "Aucun score de travaillabilité : la décision tarière / plantation croise cette prévision avec "
    "hydrologie_yavannome.json, les observations terrain, l'agenda et les échéances de plants.",
]


class ForecastUnavailable(RuntimeError):
    """Aucun modèle exploitable : le JSON précédent n'est jamais remplacé par un objet vide."""


def _iso(moment):
    return None if moment is None else moment.isoformat(timespec="seconds")


def distance_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return round(2 * 6371.0 * math.asin(math.sqrt(a)), 2)


def provider_block(result, last_complete_day) -> dict:
    spec, series = result.spec, result.series
    future = [t for t in series.times if t > result.fetched_at] if series else []
    grid = dict(series.grid) if series else {}
    if grid:
        grid["distance_km"] = distance_km(POINT["lat"], POINT["lon"], grid.get("lat"), grid.get("lon"))
    block = {
        "provider": spec.provider, "model_name": spec.model_name, "model_id": spec.model_id, "kind": spec.kind,
        "endpoint": result.request.get("endpoint"), "region": spec.region, "nominal_resolution_km": spec.nominal_resolution_km,
        "role": spec.role,
        "forecast_horizon": {"nominal_hours": spec.nominal_horizon_hours, "observed_hours_after_fetch": hours_ahead(result),
                             "last_complete_local_day": last_complete_day},
        "fetched_at": _iso(result.fetched_at), "from_cache": result.from_cache, "status": result.status,
        "status_reason": result.reason,
        "first_forecast_time": _iso(future[0]) if future else None,
        "last_forecast_time": _iso(last_valid_time(series)) if series else None,
        "series_start": _iso(series.times[0]) if series else None,
        "model_run": result.model_run, "model_run_note": result.model_run_note,
        "grid_point": grid or None, "variables_requested": list(spec.variables),
        "variables_missing": list(series.missing) if series else None,
    }
    if spec.kind == "ensemble":
        block["members_expected"] = spec.members_expected
        block["members_received"] = len(series.members.get("precipitation", {})) if series else 0
    return block


def failed_for(results: dict, period, now) -> list[str]:
    """Modèles en panne dont l'horizon nominal aurait dû couvrir la période."""
    return sorted(k for k, r in results.items() if not r.usable
                  and period.end <= now + timedelta(hours=r.spec.nominal_horizon_hours))


def horizon_summary(blocks: dict) -> dict:
    return {"out_of_horizon": sorted(k for k, b in blocks.items() if b["availability"] == "OUT_OF_HORIZON"),
            "partial_horizon": sorted(k for k, b in blocks.items() if b["availability"] == "PARTIAL_HORIZON"),
            "incomplete": sorted(k for k, b in blocks.items() if b["availability"] == "INCOMPLETE"),
            "in_error": sorted(k for k, b in blocks.items() if b["availability"].startswith("MODEL_"))}


def period_block(period, lead_day, det, ens, now) -> dict:
    deterministic = {k: agg.deterministic_period(r, period) for k, r in det.items()}
    ensemble = {k: agg.ensemble_period(r, period) for k, r in ens.items()}
    out = {**period.describe(now), "lead_day": lead_day, "deterministic": deterministic, "ensemble": ensemble,
           "horizon": horizon_summary({**deterministic, **ensemble})}
    if period.kind == "day":
        for hours in agg.PRECEDING_WINDOWS_H:
            out[f"preceding_{hours}h_forecast_precip_mm_by_model"] = {
                k: agg.preceding_precipitation(r, period.start, hours) for k, r in det.items()}
    else:
        out["hours_since_last_forecast_rain_ge_0_2mm"] = {k: agg.hours_since_rain(r, period.start) for k, r in det.items()}
    out["consensus"] = confidence.assess(deterministic, ensemble, lead_day,
                                         failed_for({**det, **ens}, period, now))
    return out


def freshness(fetched_at: datetime, now: datetime) -> dict:
    age = round((now - fetched_at) / timedelta(hours=1), 1)
    return {"status": "FRESH" if age <= MAX_AGE_HOURS else "STALE", "age_hours": age, "max_age_hours": MAX_AGE_HOURS,
            "fresh_until": _iso(fetched_at + timedelta(hours=MAX_AGE_HOURS))}


def build_report(results: dict, now: datetime, generator: str = "forecast_update.py") -> dict:
    """`results` : {clé modèle: ModelResult} pour DETERMINISTIC + ENSEMBLES."""
    usable = [r for r in results.values() if r.usable]
    if not usable:
        raise ForecastUnavailable("aucun modèle Open-Meteo exploitable : " + " ; ".join(
            f"{k} {r.status} ({r.reason})" for k, r in results.items()))
    det = {s.key: results[s.key] for s in DETERMINISTIC if s.key in results}
    ens = {s.key: results[s.key] for s in ENSEMBLES if s.key in results}
    today = agg.local_today(now)
    daily, half_days = [], []
    for lead in range(DAYS):
        day = today + timedelta(days=lead)
        daily.append(period_block(agg.day_period(day), lead, det, ens, now))
        for slot in agg.HALF_DAYS:
            half_days.append(period_block(agg.half_day_period(day, slot), lead, det, ens, now))
    providers = {"deterministic": {}, "ensemble": {}}
    for group, models in (("deterministic", det), ("ensemble", ens)):
        for key, result in models.items():
            complete = [d["date"] for d in daily if d[group][key]["availability"] == "AVAILABLE"]
            providers[group][key] = provider_block(result, complete[-1] if complete else None)
    fetched = min(r.fetched_at for r in usable)
    not_fresh = sorted(k for k, r in results.items() if r.status != "FRESH")
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": _iso(now), "generator": generator,
        **freshness(fetched, now), "data_fetched_at": _iso(fetched), "carried_over_from": None,
        "last_attempt": {"at": _iso(now), "outcome": "PARTIAL" if not_fresh else "OK",
                         "reason": ("modèles non FRESH : " + ", ".join(not_fresh)) if not_fresh else "tous les modèles FRESH"},
        "point": POINT, "attribution": ATTRIBUTION, "conventions": conventions(),
        "providers": providers, "daily": daily, "half_days": half_days, "limits": LIMITS,
    }


def conventions() -> dict:
    return {
        "timestamps": "UTC ISO 8601 ; requêtes Open-Meteo en GMT (timeformat=unixtime)",
        "local_periods": "journées et demi-journées Europe/Paris (zoneinfo) ; pas horaires T avec début < T ≤ fin ; "
                         "une journée compte 23, 24 ou 25 pas",
        "half_days": {slot: f"{h0:02d}:00–{h1:02d}:00 Europe/Paris" for slot, (h0, h1) in agg.HALF_DAYS.items()},
        "hourly_semantics": "cumuls (precipitation, rain, snowfall, ET0) et rafales : heure précédant T ; autres : instant T",
        "completeness": "valeur publiée seulement si tous les pas de la période sont valides ; sinon null",
        "units": {"precipitation": "mm", "snowfall": "cm de neige fraîche (convention Open-Meteo : 7 cm ≈ 10 mm "
                                                 "d'équivalent en eau, soit mm = cm / 0,7)",
                  "temperature": "°C", "wind": "km/h", "relative_humidity": "%"},
        "quantiles": "interpolation linéaire entre membres triés (Hyndman & Fan type 7), par système d'ensemble",
        "probabilities": "membres valides dépassant le seuil / membres valides (≥ seuil ; gel : minimum < 0 °C)",
        "ensemble_members": "contrôle + perturbés ; jamais mélangés entre systèmes",
        "preceding_windows": f"cumuls déterministes sur ]début − N h, début] ; heures passées = runs Open-Meteo antérieurs "
                             f"(past_days={PAST_DAYS}), pas des observations",
        "hours_since_rain": f"depuis la fin du dernier pas ≥ {agg.RAIN_HOUR_THRESHOLD_MM} mm ; lower_bound=true : "
                            "aucun pas pluvieux dans la série, valeur minimale",
        "null": "valeur non calculable (hors horizon, pas manquant, variable non fournie)", "zero": "valeur prévue nulle",
        "freshness": f"FRESH si âge ≤ {MAX_AGE_HOURS} h depuis data_fetched_at, sinon STALE ; recalculer depuis "
                     "data_fetched_at au moment de la lecture",
        "cache_ttl_hours": CACHE_TTL_S / 3600, "no_cross_model_average": True,
        "precipitation_thresholds_mm": agg.PRECIP_THRESHOLDS_MM, "gust_thresholds_kmh": agg.GUST_THRESHOLDS_KMH,
        "confidence_rules": confidence.RULES,
    }


def refresh_freshness(report: dict, now: datetime) -> dict:
    """Recalcule âge, statut et état des périodes d'un JSON existant (lecture tardive, report)."""
    out = copy.deepcopy(report)
    out.update(freshness(datetime.fromisoformat(report["data_fetched_at"]), now))
    for period in out["daily"] + out["half_days"]:
        start, end = datetime.fromisoformat(period["start_utc"]), datetime.fromisoformat(period["end_utc"])
        period["period_state"] = "PAST" if end <= now else "ONGOING" if start < now else "FUTURE"
    return out


def carry_over(previous: dict, now: datetime, reason: str) -> dict:
    """Collecte en échec : reprend l'ancienne prévision, marquée, avec son âge réel."""
    out = refresh_freshness(previous, now)
    out["carried_over_from"] = previous.get("carried_over_from") or previous["generated_at"]
    out["generated_at"] = _iso(now)
    out["last_attempt"] = {"at": _iso(now), "outcome": "FAILED", "reason": reason}
    return out


def dumps(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, allow_nan=False, indent=1) + "\n"


def _keys(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield f"{path}/{key}", key
            yield from _keys(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _keys(value, f"{path}/{i}")


def validate(report: dict) -> list[str]:
    """Schéma JSON + invariants ; liste vide si valide."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
              for e in jsonschema.Draft202012Validator(schema).iter_errors(report)]
    try:
        json.dumps(report, allow_nan=False)
    except ValueError as exc:
        errors.append(f"valeur non finie : {exc}")
    for path, key in _keys(report):
        if not key.startswith("no_") and any(part in key.lower() for part in FORBIDDEN_KEY_PARTS):
            errors.append(f"{path}: clé interdite (moyenne inter-modèles ou décision agronomique)")
    if errors:
        return errors
    if (report["status"] == "FRESH") != (report["age_hours"] <= report["max_age_hours"]):
        errors.append(f"status {report['status']} incohérent avec age_hours {report['age_hours']}")
    for group in ("deterministic", "ensemble"):
        for key, block in report["providers"][group].items():
            if block["status"] in ("FRESH", "PARTIAL") and not (block["fetched_at"] and block["last_forecast_time"]):
                errors.append(f"providers/{group}/{key}: {block['status']} sans fetched_at / last_forecast_time")
    for period in report["daily"] + report["half_days"]:
        where = f"{period['date']} {period.get('slot', 'DAY')}"
        for key, block in period["deterministic"].items():
            if block["availability"] != "AVAILABLE" and block["precipitation_mm"] is not None:
                errors.append(f"{where} {key}: cumul publié alors que {block['availability']}")
        for key, block in period["ensemble"].items():
            for part in ("precipitation", "temperature", "wind"):
                if block[part]["members_valid"] > block["members_expected"]:
                    errors.append(f"{where} {key}/{part}: {block[part]['members_valid']} membres > {block['members_expected']}")
        if period["consensus"]["confidence"] == "HIGH" and period["consensus"]["ensemble_systems_available"] < 2:
            errors.append(f"{where}: HIGH avec moins de deux systèmes d'ensemble")
    return errors
