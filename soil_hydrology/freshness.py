"""Statut publié par source : fraîcheur de la donnée, jamais masquée par le cache."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import Enum


class Status(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    UNAVAILABLE_AUTH = "UNAVAILABLE_AUTH"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


# Âge maximal (h) avant STALE, mesuré depuis la fin de la fenêtre de validité
# de la dernière journée. None : âge affiché mais jamais considéré « frais ».
MAX_AGE_HOURS = {"sim": 48, "copernicus_swi": 72, "copernicus_ssm": 72, "era5_land": None,
                 "rain_precipitation": 48}

# Statuts ProviderResult (collecte) → statut publié quand aucune donnée n'existe.
_NO_DATA = {"not_configured": Status.DISABLED, "unavailable_auth": Status.UNAVAILABLE_AUTH,
            "temporary_error": Status.ERROR}


def valid_until(day: date, window_end_utc_hour: int, days_after: int = 1) -> datetime:
    """Fin de la fenêtre de validité : SIM ]J 06 UTC, J+1 06 UTC] → J+1 06:00."""
    return datetime.combine(day + timedelta(days=days_after), time(window_end_utc_hour), timezone.utc)


# Fin de validité de la journée D : (jours après D, heure UTC).
WINDOW_END = {"sim": (1, 6), "copernicus_swi": (0, 12), "copernicus_ssm": (1, 0), "era5_land": (1, 0),
              "rain_precipitation": (1, 0)}


def assess(source: str, fetch_status: str, latest: date | None, now: datetime, partial: bool = False) -> dict:
    limit = MAX_AGE_HOURS[source]
    if latest is None:
        status = _NO_DATA.get(fetch_status, Status.UNAVAILABLE)
        return {"status": status.value, "source_date": None, "valid_until": None, "age_hours": None, "max_age_hours": limit,
                "status_reason": {Status.DISABLED: "source non activée",
                                  Status.UNAVAILABLE_AUTH: "identifiants absents ou refusés",
                                  Status.ERROR: "erreur lors de la collecte, aucune donnée"}.get(status, "aucune donnée valide")}
    days_after, hour = WINDOW_END[source]
    until = valid_until(latest, hour, days_after)
    age = round((now - until).total_seconds() / 3600, 1)
    if limit is None:
        status, reason = Status.STALE, "réanalyse différée : jamais traitée comme temps réel"
    elif age > limit:
        status, reason = Status.STALE, f"dernière donnée âgée de {age:g} h (> {limit} h)"
    elif partial:
        status, reason = Status.PARTIAL, "données récentes mais incomplètes (maille, pixel ou variable manquant)"
    else:
        status, reason = Status.FRESH, f"dernière donnée âgée de {age:g} h (≤ {limit} h)"
    if fetch_status in {"stale", "temporary_error"}:
        reason += " ; collecte en erreur, valeurs issues du cache"
    return {"status": status.value, "source_date": latest.isoformat(), "valid_until": until.isoformat(),
            "age_hours": age, "max_age_hours": limit, "status_reason": reason}


def reassess(block: dict, source: str, now: datetime) -> dict:
    """Recalcule l'âge d'un bloc publié antérieurement (source non relancée)."""
    if not block.get("source_date"):
        return block
    fresh = assess(source, "available", date.fromisoformat(block["source_date"]), now,
                   partial=block.get("status") == Status.PARTIAL.value)
    return {**block, **fresh}
