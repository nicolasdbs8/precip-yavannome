"""Contrat indépendant des formats des producteurs."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import Any, Protocol


class Nature(str, Enum):
    OBSERVATION = "OBSERVATION"
    SATELLITE_DERIVED = "SATELLITE_DERIVED"
    DERIVED = "DERIVED"
    MODEL = "MODEL"
    REANALYSIS = "REANALYSIS"
    LOCAL_SENSOR = "LOCAL_SENSOR"
    RAIN_ESTIMATE = "RAIN_ESTIMATE"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class Query:
    latitude: float
    longitude: float
    start: date
    end: date

    def __post_init__(self):
        if not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise ValueError("Coordonnées WGS84 invalides")
        if self.start > self.end:
            raise ValueError("La date de début suit la date de fin")


@dataclass
class Observation:
    timestamp: str
    date: str
    latitude: float
    longitude: float
    source: str
    product: str
    provider: str
    variable: str
    original_variable: str
    value: float | None
    unit: str
    nature: Nature
    retrieved_at: str
    grid_latitude: float | None = None
    grid_longitude: float | None = None
    grid_id: str | None = None
    depth_min_cm: float | None = None
    depth_max_cm: float | None = None
    spatial_resolution_m: float | None = None
    temporal_resolution: str = "P1D"
    temporal_window: str = "unknown"
    quality: str = "unknown"
    published_at: str | None = None
    product_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.nature = Nature(self.nature)
        date.fromisoformat(self.date)
        if datetime.fromisoformat(self.timestamp).tzinfo is None:
            raise ValueError("Horodatage sans fuseau")
        if datetime.fromisoformat(self.retrieved_at).tzinfo is None:
            raise ValueError("Date de récupération sans fuseau")
        if self.value is not None and not isfinite(self.value):
            raise ValueError("Valeur non finie")

    def to_dict(self):
        return asdict(self)

    @property
    def series_key(self):
        return (self.source, self.product, self.product_version, self.variable, self.unit,
                self.depth_min_cm, self.depth_max_cm, self.grid_id, self.temporal_window)


@dataclass
class ProviderResult:
    provider: str
    status: str = "available"
    observations: list[Observation] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)  # provenance technique (ressource, transport, cache)


class SoilMoistureProvider(Protocol):
    name: str

    def fetch(self, query: Query) -> ProviderResult: ...


def days(start: date, end: date):
    while start <= end:
        yield start
        start += timedelta(days=1)
