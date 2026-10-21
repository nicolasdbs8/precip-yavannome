"""Agrégation par période locale (journée, demi-journée) — un modèle à la fois.

Convention horaire Open-Meteo : la valeur horodatée T (UTC) d'un cumul
(précipitation, pluie, neige, ET0) ou d'une rafale couvre l'heure ]T−1 h, T] ;
les autres variables sont instantanées à T. Une période locale [début, fin[
retient donc les pas T tels que début < T ≤ fin, en UTC. Les bornes locales
sont converties avec ZoneInfo("Europe/Paris") : une journée compte 23, 24 ou
25 pas (changements d'heure), jamais un nombre supposé.

Une valeur n'est publiée que si tous les pas de la période sont valides pour
ce modèle (ou ce membre) : un cumul partiel sous-estimerait la pluie.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
# Créneaux opérationnels (heure locale). 06, 12 et 18 h ne sont jamais ambiguës à Paris
# (les changements d'heure ont lieu entre 02 h et 03 h).
HALF_DAYS = {"MORNING": (6, 12), "AFTERNOON": (12, 18)}
PRECEDING_WINDOWS_H = (24, 48, 72)
RAIN_HOUR_THRESHOLD_MM = 0.2      # « heure pluvieuse » pour hours_since_last_forecast_rain_ge_0_2mm

# Seuils des probabilités d'ensemble (inclusifs : cumul ≥ seuil, rafale ≥ seuil ; gel : min < 0 °C).
PRECIP_THRESHOLDS_MM = {"prob_ge_0_5mm": 0.5, "prob_ge_2mm": 2.0, "prob_ge_5mm": 5.0, "prob_ge_10mm": 10.0}
GUST_THRESHOLDS_KMH = {"prob_gust_ge_40kmh": 40.0, "prob_gust_ge_60kmh": 60.0}
FREEZE_C = 0.0
QUANTILES = {"p10_mm": 0.10, "p25_mm": 0.25, "median_mm": 0.50, "p75_mm": 0.75, "p90_mm": 0.90}


@dataclass(frozen=True)
class Period:
    kind: str          # "day" | "half_day"
    day: date          # date locale Europe/Paris
    slot: str          # "DAY" | "MORNING" | "AFTERNOON"
    start: datetime    # UTC, exclu
    end: datetime      # UTC, inclus

    @property
    def hours(self) -> int:
        return int((self.end - self.start) / timedelta(hours=1))

    def state(self, now: datetime) -> str:
        return "PAST" if self.end <= now else "ONGOING" if self.start < now else "FUTURE"

    def describe(self, now: datetime) -> dict:
        out = {"date": self.day.isoformat()}
        if self.kind == "half_day":
            h0, h1 = HALF_DAYS[self.slot]
            out.update(slot=self.slot, local_start=f"{h0:02d}:00", local_end=f"{h1:02d}:00")
        out.update(start_utc=self.start.isoformat(), end_utc=self.end.isoformat(), hours=self.hours,
                   period_state=self.state(now))
        return out


def local_to_utc(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), PARIS).astimezone(timezone.utc)


def to_local(moment: datetime) -> datetime:
    return moment.astimezone(PARIS)


def local_today(now: datetime) -> date:
    return to_local(now).date()


def day_period(day: date) -> Period:
    return Period("day", day, "DAY", local_to_utc(day, 0), local_to_utc(day + timedelta(days=1), 0))


def half_day_period(day: date, slot: str) -> Period:
    h0, h1 = HALF_DAYS[slot]
    return Period("half_day", day, slot, local_to_utc(day, h0), local_to_utc(day, h1))


def indices(times: list[datetime], start: datetime, end: datetime) -> list[int]:
    return [i for i, t in enumerate(times) if start < t <= end]


def window(values, idx, expected: int) -> list[float] | None:
    """Valeurs de la fenêtre, ou None si un pas manque (hors série ou null)."""
    if len(idx) != expected:
        return None
    selected = [values[i] for i in idx]
    return None if any(v is None for v in selected) else selected


def _round(value, digits):
    return None if value is None else round(value, digits)


def _reduce(values, idx, expected, how, digits=1):
    selected = window(values, idx, expected) if values is not None else None
    if selected is None:
        return None
    return _round({"sum": sum, "min": min, "max": max, "mean": lambda v: sum(v) / len(v)}[how](selected), digits)


def availability(result, period: Period) -> tuple[str, int]:
    """(disponibilité, pas valides) de la précipitation d'un modèle sur la période."""
    if not result.usable:
        return "MODEL_" + ("ERROR" if result.status == "ERROR" else "UNAVAILABLE"), 0
    series = result.series
    idx = indices(series.times, period.start, period.end)
    stacks = list(series.members.get("precipitation", {}).values()) if series.members else [series.values.get("precipitation")]
    stacks = [values for values in stacks if values]
    valid = max((sum(1 for i in idx if values[i] is not None) for values in stacks), default=0)
    if valid == period.hours:
        return "AVAILABLE", valid
    if not valid:
        return "OUT_OF_HORIZON", 0
    # Dernier pas présent : le manque est en début de période (série sans heures passées) ou un trou.
    ends_valid = len(idx) == period.hours and any(values[idx[-1]] is not None for values in stacks)
    return ("INCOMPLETE" if ends_valid else "PARTIAL_HORIZON"), valid


# Agrégats déterministes : (clé publiée, variable Open-Meteo, réduction, décimales).
DETERMINISTIC_METRICS = (
    ("precipitation_mm", "precipitation", "sum", 1), ("rain_mm", "rain", "sum", 1),
    ("snowfall_cm", "snowfall", "sum", 1), ("temperature_min_c", "temperature_2m", "min", 1),
    ("temperature_max_c", "temperature_2m", "max", 1), ("wind_gust_max_kmh", "wind_gusts_10m", "max", 0),
    ("wind_speed_max_kmh", "wind_speed_10m", "max", 0), ("relative_humidity_mean_pct", "relative_humidity_2m", "mean", 0),
    ("et0_mm", "et0_fao_evapotranspiration", "sum", 1), ("surface_temperature_min_c", "surface_temperature", "min", 1),
)


def deterministic_period(result, period: Period) -> dict:
    state, valid = availability(result, period)
    out = {"availability": state, "hours_valid": valid}
    series = result.series if result.usable else None
    idx = indices(series.times, period.start, period.end) if series else []
    for key, variable, how, digits in DETERMINISTIC_METRICS:
        values = series.values.get(variable) if series else None
        out[key] = _reduce(values, idx, period.hours, how, digits)
    return out


def preceding_precipitation(result, start: datetime, hours: int) -> float | None:
    """Cumul prévu (runs Open-Meteo, y compris heures passées) sur ]start − hours, start]."""
    if not result.usable:
        return None
    series = result.series
    return _reduce(series.values.get("precipitation"), indices(series.times, start - timedelta(hours=hours), start), hours, "sum")


def hours_since_rain(result, moment: datetime) -> dict:
    """Heures écoulées depuis la fin du dernier pas ≥ 0,2 mm, à l'instant `moment`.

    `lower_bound` : aucun pas pluvieux dans la série disponible, la valeur est un minimum."""
    empty = {"hours": None, "lower_bound": False}
    if not result.usable:
        return empty
    series, values = result.series, result.series.values.get("precipitation")
    past = [i for i, t in enumerate(series.times) if t <= moment]
    if not values or not past or series.times[past[-1]] != moment or values[past[-1]] is None:
        return empty  # instant hors de la série ou hors horizon
    for i in reversed(past):
        if values[i] is None:
            return empty  # trou : durée inconnue
        if values[i] >= RAIN_HOUR_THRESHOLD_MM:
            return {"hours": int((moment - series.times[i]) / timedelta(hours=1)), "lower_bound": False}
    return {"hours": int((moment - series.times[past[0]]) / timedelta(hours=1)) + 1, "lower_bound": True}


def quantile(sorted_values: list[float], q: float) -> float:
    """Quantile par interpolation linéaire (Hyndman & Fan type 7, défaut numpy/R)."""
    position = (len(sorted_values) - 1) * q
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def probability(values: list[float], predicate) -> float | None:
    return None if not values else round(sum(1 for v in values if predicate(v)) / len(values), 3)


def member_values(members: dict, idx, expected, how) -> list[float]:
    """Réduction par membre ; un membre incomplet ou null est exclu (jamais compté à 0)."""
    out = []
    for values in members.values():
        selected = window(values, idx, expected)
        if selected is not None:
            out.append({"sum": sum, "min": min, "max": max}[how](selected))
    return out


def precipitation_stats(values: list[float]) -> dict:
    ordered = sorted(values)
    out = {"members_valid": len(values)}
    for key, q in QUANTILES.items():
        out[key] = _round(quantile(ordered, q), 1) if ordered else None
    out.update(min_mm=_round(ordered[0], 1) if ordered else None, max_mm=_round(ordered[-1], 1) if ordered else None)
    for key, threshold in PRECIP_THRESHOLDS_MM.items():
        out[key] = probability(values, lambda v, t=threshold: v >= t)
    return out


def temperature_stats(minima: list[float]) -> dict:
    ordered = sorted(minima)
    return {"members_valid": len(minima), "prob_temperature_below_0c": probability(minima, lambda v: v < FREEZE_C),
            "min_p10_c": _round(quantile(ordered, 0.1), 1) if ordered else None,
            "min_median_c": _round(quantile(ordered, 0.5), 1) if ordered else None}


def wind_stats(maxima: list[float]) -> dict:
    ordered = sorted(maxima)
    out = {"members_valid": len(maxima)}
    for key, threshold in GUST_THRESHOLDS_KMH.items():
        out[key] = probability(maxima, lambda v, t=threshold: v >= t)
    out.update(gust_max_median_kmh=_round(quantile(ordered, 0.5), 0) if ordered else None,
               gust_max_p90_kmh=_round(quantile(ordered, 0.9), 0) if ordered else None)
    return out


def ensemble_period(result, period: Period) -> dict:
    """Statistiques d'UN système d'ensemble ; les membres d'un autre système n'y entrent jamais."""
    state, _ = availability(result, period)
    members = result.series.members if result.usable else {}
    idx = indices(result.series.times, period.start, period.end) if result.usable else []
    return {"availability": state, "members_expected": result.spec.members_expected,
            "precipitation": precipitation_stats(member_values(members.get("precipitation", {}), idx, period.hours, "sum")),
            "temperature": temperature_stats(member_values(members.get("temperature_2m", {}), idx, period.hours, "min")),
            "wind": wind_stats(member_values(members.get("wind_gusts_10m", {}), idx, period.hours, "max"))}
