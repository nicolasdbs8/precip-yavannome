"""Interprétation propre à Rain, distincte des valeurs du producteur."""
from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class ClimatologyReference:
    source: str
    variable: str
    baseline_start: int
    baseline_end: int
    depth_min_cm: float | None
    depth_max_cm: float | None
    method: str


class ClimatologyProvider(Protocol):
    def anomaly(self, observation, reference: ClimatologyReference): ...


def interpret(observation, as_of: date):
    if observation is None or observation.value is None:
        return "Donnée indisponible"
    age = (as_of - date.fromisoformat(observation.date)).days
    if age > 7:
        return f"Donnée trop ancienne pour une situation actuelle ({age} jours)"
    if observation.variable == "swi_sim":
        return "Indice régional brut ; unité à clarifier, catégorie non attribuée"
    return "Valeur régionale ; catégorie sec/humide non attribuée sans référence pédologique ou climatologique"
