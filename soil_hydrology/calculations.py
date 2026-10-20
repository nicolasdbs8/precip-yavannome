"""Agrégations calendaires : une absence ne devient jamais une pluie nulle."""
from collections import defaultdict
from datetime import date, timedelta

from .models import days

WINDOWS = (1, 3, 7, 15, 30, 90)


def group_series(observations):
    result = defaultdict(list)
    for obs in observations:
        result[obs.series_key].append(obs)
    return {key: sorted(values, key=lambda o: o.date) for key, values in result.items()}


def window_sum(values: dict[str, float | None], end: date, window: int):
    """Somme sur `window` journées finissant à `end` ; None si un jour manque.
    Retourne (somme complète ou None, somme partielle ou None, dates manquantes)."""
    if window < 1:
        raise ValueError("Fenêtre positive requise")
    start = end - timedelta(days=window - 1)
    usable = {str(d): values[str(d)] for d in days(start, end) if values.get(str(d)) is not None}
    missing = [str(d) for d in days(start, end) if str(d) not in usable]
    partial = sum(usable.values()) if usable else None
    return (partial if not missing else None), partial, missing


def cumulative(observations, end: date, window: int):
    if window < 1:
        raise ValueError("Fenêtre positive requise")
    if len({o.series_key for o in observations}) > 1:
        raise ValueError("Un cumul ne doit contenir qu'une série homogène")
    if any(o.unit != "mm" or o.temporal_resolution != "P1D" for o in observations):
        raise ValueError("Le cumul exige des flux quotidiens en mm")
    start = end - timedelta(days=window - 1)
    by_day = defaultdict(list)
    for obs in observations:
        if str(start) <= obs.date <= str(end):
            by_day[obs.date].append(obs)
    # Un doublon contradictoire rend la journée inutilisable, jamais nulle.
    usable = {day: values[0] for day, values in by_day.items() if len(values) == 1 and values[0].value is not None}
    total, partial, missing = window_sum({day: o.value for day, o in usable.items()}, end, window)
    return {"start": str(start), "end": str(end), "days": window,
            "sum_mm": total, "partial_sum_mm": partial,
            "available_days": len(usable), "missing_dates": missing,
            "intraday_coverage": "unknown" if any(o.quality == "coverage_unknown" for o in usable.values()) else "producer daily product"}
