"""JSON compact publié par Rain (`hydrologie_yavannome.json`).

Une entrée par source, avec unités, résolution, date et fraîcheur propres.
Aucune moyenne entre SIM, Copernicus SWI, Copernicus SSM et ERA5-Land :
ces grandeurs ne sont pas les mêmes. `null` = donnée absente ; 0 = valeur
réelle nulle ; une source indisponible a un statut, jamais des zéros.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from .calculations import window_sum
from .comparison import THRESHOLDS as COMPARED, compare
from .freshness import MAX_AGE_HOURS, Status, assess, reassess, valid_until, WINDOW_END
from .variables import SIM_FIELDS, VARIABLES

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).parent / "schema" / "hydrologie.schema.json"
SOURCES = ("sim", "copernicus_swi", "copernicus_ssm", "era5_land", "rain_precipitation")
SIM_NAMES = {v: k for k, v in SIM_FIELDS.items()}
SIM_COLUMNS = ["SWI", "SSWI_10J", "WG_RACINE", "WGI_RACINE", "PRELIQ", "PRENEI", "PRELIQ+PRENEI", "PE",
               "EVAP", "ETP", "DRAINC", "RUNC", "T", "TINF_H", "TSUP_H", "HU"]
PRECIP_WINDOWS = {"24h": 1, "72h": 3, "7d": 7, "14d": 14}
FLUX_WINDOWS = {"7d": 7}
# Conventions internes Rain (pas des seuils scientifiques) : variation sur 7 jours
# en deçà de laquelle la tendance est « stable », dans l'unité de chaque source.
TREND_THRESHOLDS = {"swi_sim": 0.02, "swi_copernicus": 2.0, "ssm_copernicus": 5.0, "soil_water": 0.01}
# Comparaison pluie SIM / Rain : conventions internes Rain, pas des seuils publiés.
# L'écart principal est la différence relative symétrique |S − R| / ((S + R) / 2),
# bornée à [0, 2] et insensible à l'ordre des sources (contrairement à S/R).
PRECIP_DRY_MAX_MM = 2.0           # les deux cumuls ≤ 2 mm : BOTH_DRY, l'écart relatif n'est pas significatif
PRECIP_REL_CONSISTENT_MAX = 0.25  # ≤ 25 % : CONSISTENT
PRECIP_REL_PARTIAL_MAX = 0.50     # ≤ 50 % : PARTIAL ; au-delà : DIVERGENT
PRECIP_ABS_NEGLIGIBLE_MM = 1.0    # écart ≤ 1 mm : jamais DIVERGENT (au plus PARTIAL)
PRECIP_ABS_MAJOR_MM = 10.0        # écart ≥ 10 mm : jamais CONSISTENT (au moins PARTIAL)
COMPARISON_WINDOWS = {"7d": 7, "14d": 14, "30d": 30}
PRECIP_WINDOW_WEIGHTS = {"7d": 3, "14d": 2, "30d": 1}  # la situation récente pèse davantage
PRECIP_CLASS_SCORE = {"CONSISTENT": 0, "BOTH_DRY": 0, "PARTIAL": 1, "DIVERGENT": 2}
PRECIP_GLOBAL_PARTIAL_FROM = 0.5     # score pondéré ∈ [0, 2] : < 0,5 CONSISTENT
PRECIP_GLOBAL_DIVERGENT_FROM = 1.25  # ≥ 1,25 DIVERGENT, sinon PARTIAL
RASTER_METADATA = ("raw_dn", "qflag_pct", "ssf", "noise", "source_unit")
LIMITS = ("Une maille SAFRAN de 8 km ou un pixel Copernicus de 1 km ne constitue pas une mesure physique "
          "effectuée sur la parcelle. Ces modèles et observations servent à suivre la tendance locale ; "
          "le contrôle réel du sol sur place reste supérieur pour juger sa travaillabilité.")


def _name(obs) -> str:
    if obs.source == "meteo_france_sim":
        return SIM_NAMES.get(obs.variable, obs.original_variable)
    return obs.original_variable


def _index(observations):
    """{entité (maille/pixel): {nom: {date: Observation}}}."""
    result = defaultdict(lambda: defaultdict(dict))
    for obs in observations:
        result[obs.grid_id][_name(obs)][obs.date] = obs
    return result


def _r(value, digits=4):
    return None if value is None else round(value, digits)


def trend(by_date: dict, anchor: str | None, threshold: float) -> dict:
    """Valeur J, valeur J−7 exacte et tendance ; jamais d'interpolation."""
    prior_date = (date.fromisoformat(anchor) - timedelta(days=7)).isoformat() if anchor else None
    latest = getattr(by_date.get(anchor), "value", None)
    prior = getattr(by_date.get(prior_date), "value", None)
    delta = _r(latest - prior) if latest is not None and prior is not None else None
    direction = None if delta is None else "rising" if delta > threshold else "falling" if delta < -threshold else "stable"
    return {"latest": latest, "latest_date": anchor, "d7": prior, "d7_date": prior_date,
            "delta_7d": delta, "trend": direction, "stable_threshold": threshold}


def cumuls(by_date: dict, anchor: str, windows: dict, missing: dict, label: str) -> dict:
    values = {d: o.value if hasattr(o, "value") else o for d, o in by_date.items()}
    output = {}
    for key, n in windows.items():
        total, _, gaps = window_sum(values, date.fromisoformat(anchor), n)
        output[key] = _r(total, 2)
        if gaps:
            missing[f"{label}:{key}"] = gaps
    return output


def _columns(series: dict, names) -> dict:
    dates = sorted({d for s in series.values() for d in s})
    columns = {"date": dates}
    for name in names:
        if name in series:
            columns[name] = [getattr(series[name].get(d), "value", None) for d in dates]
    return columns


def _flags(series: dict) -> dict:
    """Valeurs nulles avec cause (drapeau producteur), pour ne pas confondre avec une absence."""
    out = {}
    for name, by_date in series.items():
        flagged = {d: o.quality for d, o in by_date.items() if o.value is None and o.quality not in ("valid", "missing")}
        if flagged:
            out[name] = flagged
    return out


def _base(source: str, result, latest: str | None, now: datetime, partial=False) -> dict:
    block = assess(source, result.status if result else "not_configured",
                   date.fromisoformat(latest) if latest else None, now, partial)
    block["messages"] = list(result.messages) if result else []
    return block


def sim_block(result, now: datetime) -> dict:
    index = _index(result.observations)
    grids = result.details.get("grids", [])
    blocks, anchors = [], {}
    for grid in grids:
        series = index.get(grid["id"], {})
        swi = series.get("SWI", {})
        anchor = max((d for d, o in swi.items() if o.value is not None), default=None)
        anchors[grid["id"]] = anchor
        block = {**grid, "latest": None, "derived": None, "series": _columns(series, SIM_COLUMNS)}
        if anchor:
            missing = {}
            block["latest"] = {"date": anchor, "values": {n: getattr(series[n].get(anchor), "value", None)
                                                          for n in SIM_COLUMNS if n in series}}
            block["derived"] = {
                "anchor_date": anchor,
                "swi": trend(swi, anchor, TREND_THRESHOLDS["swi_sim"]),
                "cumuls_mm": {
                    **{n: cumuls(series.get(n, {}), anchor, PRECIP_WINDOWS, missing, n) for n in ("PRELIQ", "PRENEI", "PRELIQ+PRENEI")},
                    **{n: cumuls(series.get(n, {}), anchor, FLUX_WINDOWS, missing, n) for n in ("EVAP", "ETP", "PE", "DRAINC", "RUNC")}},
                "missing_days": missing}
        blocks.append(block)
    parcel = [g["id"] for g in grids if g["role"] == "parcel"]
    primary = anchors.get(parcel[0]) if parcel else None
    partial = any(anchors.get(g) != primary for g in parcel)
    block = _base("sim", result, primary, now, partial)
    block.update({
        "product": "SIM2 quotidien (SAFRAN-ISBA)", "provider": "Météo-France",
        "dataset_id": result.details.get("dataset_id"), "spatial_resolution_m": 8000,
        "day_convention": "DATE J = cumul/moyenne ]J 06 UTC, J+1 06 UTC] (T, HU : 00–00 UTC ; TINF_H : 18–18 UTC)",
        "units": {n: VARIABLES[SIM_FIELDS[n]].unit for n in SIM_COLUMNS if n in SIM_FIELDS} | {"PRELIQ+PRENEI": "mm"},
        "unit_notes": {"SWI": VARIABLES["swi_sim"].limits, "SSWI_10J": VARIABLES["sswi_sim"].limits},
        "primary_grid": parcel[0] if parcel else None, "grids": blocks,
        "provenance": result.details.get("extractions", [])})
    return block


def raster_block(source: str, result, now: datetime, unit: str, label_meta: dict) -> dict:
    """Copernicus / ERA5 : une entrée par pixel, une colonne par bande ou couche."""
    # Humidité du sol seulement : les autres variables ERA5 restent dans le cache.
    index = _index([o for o in result.observations if o.variable in TREND_THRESHOLDS]) if result else {}
    pixels = (result.details.get("pixels") if result else None) or [{"id": k} for k in index]
    blocks, anchors = [], []
    for pixel in pixels:
        series = index.get(pixel["id"], {})
        names = sorted(series)
        anchor = max((d for s in series.values() for d, o in s.items() if o.value is not None), default=None)
        anchors.append(anchor)
        block = {**pixel, "latest": None, "derived": None, "series": _columns(series, names), "flags": _flags(series)}
        if anchor:
            sample = next(iter(next(iter(series.values())).values()))
            block["latest"] = {"date": anchor, "values": {n: getattr(series[n].get(anchor), "value", None) for n in names},
                               "quality": {n: getattr(series[n].get(anchor), "quality", "missing") for n in names},
                               "metadata": {n: {k: v for k, v in series[n][anchor].metadata.items() if k in RASTER_METADATA}
                                            for n in names if anchor in series[n]}}
            block["derived"] = {"anchor_date": anchor, "trends": {n: trend(series[n], anchor, TREND_THRESHOLDS[sample.variable]) for n in names}}
        blocks.append(block)
    latest = max(filter(None, anchors), default=None)
    partial = any(a is None for a in anchors) and latest is not None
    block = _base(source, result, latest, now, partial)
    block.update({"unit": unit, **label_meta, "pixels": blocks, "details": {k: v for k, v in (result.details if result else {}).items() if k != "pixels"}})
    if latest and all(p.get("overlap_fraction") is not None for p in pixels):
        block["parcel_weighted"] = parcel_weighted(blocks, latest)
    return block


def parcel_weighted(pixels: list[dict], day: str) -> dict:
    """Moyenne pondérée par la part de parcelle de chaque pixel, à l'intérieur d'un
    même produit et d'une même date. Un pixel sans valeur valide (drapeau, absent)
    est exclu, jamais compté 0 : `covered_fraction` dit quelle part de ZP1 est représentée."""
    values, covered = {}, {}
    names = sorted({n for p in pixels for n in p["series"] if n != "date"})
    for name in names:
        pairs = [(p["overlap_fraction"], dict(zip(p["series"]["date"], p["series"][name])).get(day)) for p in pixels]
        valid = [(w, v) for w, v in pairs if v is not None]
        weight = sum(w for w, _ in valid)
        values[name] = _r(sum(w * v for w, v in valid) / weight, 2) if weight > 0 else None
        covered[name] = _r(weight, 4)
    return {"date": day, "values": values, "covered_fraction": covered,
            "weights": {p["id"]: p["overlap_fraction"] for p in pixels},
            "method": "Σ(part de ZP1 × valeur) / Σ(parts des pixels valides), même produit et même date ; "
                      "valeurs individuelles conservées dans pixels[]"}


def rain_block(result, now: datetime) -> dict:
    """Pluie Rain existante (COMÉPHORE / ANTILOPE-LAME_D_EAU), indépendante de SIM."""
    by_source = defaultdict(dict)
    for obs in (result.observations if result else []):
        by_source[obs.source.removeprefix("rain_")][obs.date] = obs.value
    dates = sorted({d for s in by_source.values() for d in s})
    daily, origin = {}, {}
    for d in dates:
        # COMÉPHORE (réanalyse) prime sur le temps réel le même jour.
        for source in ("comephore", "antilope", "pluviometre_site"):
            if by_source.get(source, {}).get(d) is not None:
                daily[d], origin[d] = by_source[source][d], source
                break
    # Journée en cours (00–24 UTC non terminée) : affichée mais exclue des cumuls.
    complete = [d for d in daily if valid_until(date.fromisoformat(d), 0) <= now]
    latest = max(complete, default=None)
    block = _base("rain_precipitation", result, latest, now)
    missing = {}
    block.update({"product": "Rain : COMÉPHORE (réanalyse) prioritaire, sinon LAME_D_EAU (radar temps réel, source « antilope »)",
                  "provider": "Météo-France via precip_yavannome.csv", "unit": "mm",
                  "spatial_resolution": "COMÉPHORE 1 km ; LAME_D_EAU (source « antilope ») 500 m",
                  "day_convention": "journée 00–24 UTC", "in_progress_dates": sorted(set(daily) - set(complete)),
                  "cumuls_mm": cumuls({d: daily[d] for d in complete}, latest, PRECIP_WINDOWS, missing, "rain") if latest else None,
                  "missing_days": missing,
                  "series": {"date": dates, "mm": [daily.get(d) for d in dates], "source": [origin.get(d) for d in dates]}})
    return block


def classify_precipitation(sim_mm: float, rain_mm: float) -> dict:
    """Écart entre deux cumuls de pluie d'une même fenêtre (mm, ≥ 0)."""
    difference = sim_mm - rain_mm
    magnitude = round(abs(difference), 9)  # 2,2 − 1,2 doit valoir 1 mm, pas 1,0000000000000002
    mean = (sim_mm + rain_mm) / 2
    relative = magnitude / mean if mean > 0 else None  # 0 vs 0 : non défini, pas de division par zéro
    low, high = min(sim_mm, rain_mm), max(sim_mm, rain_mm)
    ratio = high / low if low > 0 else None  # une source à 0 : ratio non défini (relative le remplace)
    direction = "EQUAL" if magnitude == 0 else "SIM_HIGHER" if difference > 0 else "RAIN_HIGHER"
    if high <= PRECIP_DRY_MAX_MM:
        classification, reason = "BOTH_DRY", f"les deux cumuls ≤ {PRECIP_DRY_MAX_MM:g} mm"
    else:
        if relative <= PRECIP_REL_CONSISTENT_MAX:
            classification = "CONSISTENT"
        elif relative <= PRECIP_REL_PARTIAL_MAX:
            classification = "PARTIAL"
        else:
            classification = "DIVERGENT"
        reason = f"écart relatif symétrique {relative:.0%}"
        if classification == "DIVERGENT" and magnitude <= PRECIP_ABS_NEGLIGIBLE_MM:
            classification, reason = "PARTIAL", reason + f", mais écart ≤ {PRECIP_ABS_NEGLIGIBLE_MM:g} mm"
        elif classification == "CONSISTENT" and magnitude >= PRECIP_ABS_MAJOR_MM:
            classification, reason = "PARTIAL", reason + f", mais écart ≥ {PRECIP_ABS_MAJOR_MM:g} mm"
    return {"difference_mm": _r(difference, 1), "absolute_difference_mm": _r(magnitude, 1),
            "symmetric_relative_difference": _r(relative, 3), "ratio": _r(ratio, 3), "direction": direction,
            "classification": classification, "reason": reason}


def aggregate_precipitation(windows: dict) -> dict:
    """Verdict global pondéré vers les fenêtres récentes, avec règles explicites."""
    used = {k: w["classification"] for k, w in windows.items() if w["classification"] in PRECIP_CLASS_SCORE}
    incomplete = [k for k in windows if k not in used]
    if not used:
        return {"status": "INSUFFICIENT", "score": None, "rule": "aucune fenêtre complète", "incomplete_windows": incomplete}
    weight = sum(PRECIP_WINDOW_WEIGHTS[k] for k in used)
    score = sum(PRECIP_WINDOW_WEIGHTS[k] * PRECIP_CLASS_SCORE[c] for k, c in used.items()) / weight
    status = ("DIVERGENT" if score >= PRECIP_GLOBAL_DIVERGENT_FROM else
              "PARTIAL" if score >= PRECIP_GLOBAL_PARTIAL_FROM else "CONSISTENT")
    rule = f"score pondéré {score:.2f} (poids {', '.join(f'{k}={PRECIP_WINDOW_WEIGHTS[k]}' for k in used)})"
    recent = [used.get("7d"), used.get("14d")]
    if recent == ["DIVERGENT", "DIVERGENT"]:
        status, rule = "DIVERGENT", rule + " ; 7 j et 14 j divergents"
    elif all(c in ("CONSISTENT", "BOTH_DRY") for c in recent) and status == "DIVERGENT":
        status, rule = "PARTIAL", rule + " ; 7 j et 14 j cohérents : divergence ancienne seulement"
    return {"status": status, "score": round(score, 3), "rule": rule, "incomplete_windows": incomplete}


def compare_precipitation(sim: dict, rain: dict) -> dict:
    """SIM PRELIQ+PRENEI (maille principale) vs pluie Rain, sur des cumuls ≥ 7 jours."""
    out = {"method": (f"Écart relatif symétrique |S−R|/((S+R)/2) : ≤ {PRECIP_REL_CONSISTENT_MAX:.0%} CONSISTENT, "
                      f"≤ {PRECIP_REL_PARTIAL_MAX:.0%} PARTIAL, sinon DIVERGENT ; BOTH_DRY si les deux ≤ {PRECIP_DRY_MAX_MM:g} mm ; "
                      f"écart ≤ {PRECIP_ABS_NEGLIGIBLE_MM:g} mm jamais DIVERGENT, ≥ {PRECIP_ABS_MAJOR_MM:g} mm jamais CONSISTENT. "
                      f"Global : score pondéré (7 j ×3, 14 j ×2, 30 j ×1 ; CONSISTENT/BOTH_DRY 0, PARTIAL 1, DIVERGENT 2), "
                      f"DIVERGENT dès {PRECIP_GLOBAL_DIVERGENT_FROM:g}, PARTIAL dès {PRECIP_GLOBAL_PARTIAL_FROM:g} ; "
                      "7 j et 14 j divergents → DIVERGENT ; 7 j et 14 j cohérents → jamais DIVERGENT."),
           "caveat": "SIM : maille 8 km, journées 06–06 UTC ; Rain : pixel 1 km (COMÉPHORE) ou 500 m (LAME_D_EAU), journées 00–24 UTC. "
                     "Un écart journalier est attendu ; seuls les cumuls pluri-journaliers sont comparés.",
           "anchor_date": None, "windows": {}}
    grid = next((g for g in sim.get("grids", []) if g["id"] == sim.get("primary_grid")), None)
    if not grid or not rain.get("source_date") or not grid.get("latest"):
        out.update(aggregate_precipitation({}))
        return out
    sim_daily = dict(zip(grid["series"]["date"], grid["series"].get("PRELIQ+PRENEI", [])))
    rain_daily = {d: v for d, v in zip(rain["series"]["date"], rain["series"]["mm"]) if d not in rain["in_progress_dates"]}
    anchor = min(grid["latest"]["date"], rain["source_date"])
    out["anchor_date"] = anchor
    for label, n in COMPARISON_WINDOWS.items():
        s, _, s_gap = window_sum(sim_daily, date.fromisoformat(anchor), n)
        r, _, r_gap = window_sum(rain_daily, date.fromisoformat(anchor), n)
        entry = {"window": label, "sim_mm": _r(s, 1), "rain_mm": _r(r, 1), "difference_mm": None,
                 "absolute_difference_mm": None, "symmetric_relative_difference": None, "ratio": None,
                 "direction": None, "classification": "INCOMPLETE", "reason": "cumul incomplet"}
        if s is not None and r is not None:
            # Sur les cumuls publiés (0,1 mm, précision SIM) : métriques recalculables depuis le JSON.
            entry.update(classify_precipitation(entry["sim_mm"], entry["rain_mm"]))
        else:
            entry["missing"] = {"sim": s_gap, "rain": r_gap}
        # Alias de schema_version 1 (lecteurs antérieurs) : diff_mm = difference_mm, flag = classification.
        entry.update(diff_mm=entry["difference_mm"], flag=entry["classification"])
        out["windows"][label] = entry
    out.update(aggregate_precipitation(out["windows"]))
    return out


def moisture_trends(results: dict) -> dict:
    """Convergence qualitative des tendances (comparison.compare) : un vote par source,
    aucun niveau moyenné. Seulement pour les sources relancées dans ce passage."""
    observations = [o for key, r in results.items() if r is not None and key != "rain_precipitation" for o in r.observations]
    dates = [o.date for o in observations if o.variable in COMPARED and o.value is not None]
    if not dates:
        return {"category": "données insuffisantes", "evidence": []}
    result = compare(observations, date.fromisoformat(max(dates)))
    result["evidence"] = [{k: e[k] for k in ("source", "variable", "delta", "unit", "threshold", "direction")}
                          | {"first_date": e["first"]["date"], "last_date": e["last"]["date"]} for e in result["evidence"]]
    return result


META = {
    "copernicus_swi": {"product": "CLMS Soil Water Index Europe 1 km daily v2", "provider": "Copernicus Land Monitoring Service",
                       "spatial_resolution_m": 1000, "nature": "satellite (ASCAT + Sentinel-1), filtre exponentiel",
                       "band_meaning": "SWI{T} : indice d'eau du sol pour un temps caractéristique T (jours), pas une profondeur",
                       "day_convention": "date nominale J 12:00 UTC (observations ]J−1 12 UTC, J 12 UTC])",
                       "latency": "nominale ~1–2 jours"},
    "copernicus_ssm": {"product": "CLMS Surface Soil Moisture Europe 1 km daily v1", "provider": "Copernicus Land Monitoring Service",
                       "spatial_resolution_m": 1000, "nature": "satellite Sentinel-1, premiers centimètres",
                       "band_meaning": "SSM : degré de saturation relatif de surface ; NOISE dans metadata",
                       "day_convention": "journée J 00–24 UTC (passages Sentinel-1 irréguliers)", "latency": "nominale ~1–2 jours"},
    "era5_land": {"product": "ERA5-Land (CDS)", "provider": "ECMWF / Copernicus Climate Change Service",
                  "spatial_resolution_m": 9000, "nature": "réanalyse, contrôle historique indépendant",
                  "band_meaning": "swvl1..4 : couches 0–7, 7–28, 28–100, 100–289 cm (m³/m³)",
                  "day_convention": "moyenne 00–24 UTC", "latency": "~5 jours ; jamais temps réel"},
}


def disabled_block(source: str, reason: str, now: datetime) -> dict:
    block = assess(source, "not_configured", None, now)
    block.update(status_reason=reason, messages=[])
    return block


def build_report(parcel: dict, results: dict, now: datetime, previous: dict | None = None,
                 generator: str = "soil_hydrology_update.py") -> dict:
    """`results[source]` : ProviderResult, ou None si la source n'a pas été relancée
    (le bloc précédent est alors repris avec un âge recalculé et signalé)."""
    sources = {}
    for source in SOURCES:
        result = results.get(source)
        if result is None:
            old = (previous or {}).get("sources", {}).get(source)
            if old:
                sources[source] = {**reassess(old, source, now), "carried_over_from": previous["generated_at"]}
            else:
                sources[source] = disabled_block(source, "source non exécutée lors de cette mise à jour", now)
            continue
        if source == "sim":
            sources[source] = sim_block(result, now)
        elif source == "rain_precipitation":
            sources[source] = rain_block(result, now)
        else:
            unit = "%" if source.startswith("copernicus") else "m³/m³"
            sources[source] = raster_block(source, result, now, unit, META[source])
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": now.isoformat(timespec="seconds"), "generator": generator,
        "parcel": parcel, "limits": LIMITS,
        "conventions": {"null": "donnée absente ou drapeau producteur (voir flags)", "zero": "valeur réelle nulle",
                        "status": [s.value for s in Status], "max_age_hours": MAX_AGE_HOURS,
                        "validity_window_end": {k: {"days_after": d, "utc_hour": h} for k, (d, h) in WINDOW_END.items()},
                        "no_cross_source_average": True},
        "sources": sources,
        "comparisons": {"precipitation_sim_vs_rain": compare_precipitation(sources["sim"], sources["rain_precipitation"]),
                        "soil_moisture_trends": moisture_trends(results)},
    }


def dumps(report: dict) -> str:
    # allow_nan=False : un NaN n'est jamais publié (il deviendrait une valeur ambiguë).
    return json.dumps(report, ensure_ascii=False, allow_nan=False, indent=1) + "\n"


def validate(report: dict) -> list[str]:
    """Schéma JSON + invariants métier ; liste vide si valide."""
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
              for e in jsonschema.Draft202012Validator(schema).iter_errors(report)]
    json.dumps(report, allow_nan=False)
    for name, block in report.get("sources", {}).items():
        has_date = block.get("source_date") is not None
        if block.get("status") in ("FRESH", "STALE", "PARTIAL") and not has_date:
            errors.append(f"sources/{name}: statut {block['status']} sans source_date")
        if block.get("status") in ("UNAVAILABLE", "UNAVAILABLE_AUTH", "ERROR", "DISABLED") and has_date \
                and "carried_over_from" not in block:
            errors.append(f"sources/{name}: statut {block['status']} avec une source_date")
        limit = block.get("max_age_hours")
        if block.get("status") == "FRESH" and limit is not None and block.get("age_hours", 0) > limit:
            errors.append(f"sources/{name}: FRESH alors que l'âge dépasse {limit} h")
    return errors
