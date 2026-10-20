"""Définitions, unités et validations explicites, sans unité devinée."""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Variable:
    label: str
    unit: str
    definition: str
    limits: str
    minimum: float | None = None
    maximum: float | None = None


VARIABLES = {
    # Le dictionnaire 2026 annonce « % » mais le fichier publie une échelle 0–1
    # (ex. 0.102) : valeur conservée telle quelle, jamais ×100 (≠ SWI Copernicus %).
    "swi_sim": Variable("SWI SIM", "1", "Indice d'humidité des sols SIM (ISBA), moyenne 06–06 UTC, échelle 0–1 telle que publiée",
                        "Dictionnaire 2026 : unité « % », valeurs publiées sur 0–1. Non comparable en niveau au SWI Copernicus (%, satellite, T)."),
    "sswi_sim": Variable("SSWI 10 j SIM", "1", "Indice standardisé de sécheresse d'humidité des sols intégré sur 10 jours (J−9 → J)",
                         "Sans unité ; négatif = plus sec que la normale du modèle. Période de référence non précisée par le dictionnaire."),
    "swi_copernicus": Variable("SWI Copernicus", "% indice", "Indice dérivé satellitaire filtré selon une constante T", "T n'est pas une profondeur ; non interchangeable avec SWI SIM.", 0, 100),
    "ssm_copernicus": Variable("SSM Copernicus", "%", "Humidité de surface relative (degré de saturation), premiers centimètres, Sentinel-1",
                               "Surface uniquement ; non moyennable avec un SWI (profondeurs et définitions différentes).", 0, 100),
    "root_liquid": Variable("Eau liquide racinaire SIM", "m³/m³", "Contenu volumique liquide à 06 UTC", "Profondeur racinaire variable, non fournie par le CSV.", 0, 1),
    "root_ice": Variable("Eau gelée racinaire SIM", "m³/m³", "Contenu volumique gelé à 06 UTC", "Distinct de l'eau disponible aux plantes.", 0, 1),
    "soil_water": Variable("Humidité volumique", "m³/m³", "Volume d'eau par volume de sol", "Interprétation dépendante du sol et de la profondeur.", 0, 1),
    "precipitation": Variable("Précipitations", "mm", "Hauteur d'eau cumulée", "Conserver fenêtre temporelle et couverture.", 0),
    "rainfall": Variable("Pluie liquide SIM", "mm", "Précipitations liquides", "Ne comprend pas la neige.", 0),
    "snowfall": Variable("Précipitations solides SIM", "mm", "Équivalent en eau des précipitations solides", "Pas une épaisseur de neige.", 0),
    "effective_precipitation": Variable("Pluie efficace SIM", "mm", "Bilan précipitations et évapotranspiration du producteur", "Peut être négative ; ne pas écrêter."),
    "actual_et": Variable("Évapotranspiration réelle", "mm", "Flux réel calculé par le modèle", "Convention de signe documentée par source."),
    "potential_et": Variable("ETP", "mm", "Évapotranspiration potentielle", "SIM : Penman-Monteith FAO-56 ; non équivalente à un besoin d'arrosage."),
    "drainage": Variable("Drainage", "mm", "Flux de drainage", "Sortie de modèle régional.", 0),
    "runoff": Variable("Ruissellement", "mm", "Flux de ruissellement", "Sortie de modèle régional.", 0),
    "subsurface_runoff": Variable("Écoulement souterrain ERA5", "mm", "Sub-surface runoff ECMWF", "Conservé séparément du drainage SIM : définitions différentes.", 0),
    "temperature": Variable("Température moyenne", "°C", "Température de l'air", "Fenêtre et hauteur propres au produit.", -100, 70),
    "temperature_min": Variable("Température minimale", "°C", "Minimum quotidien", "SIM : fenêtre 18–18 UTC.", -100, 70),
    "temperature_max": Variable("Température maximale", "°C", "Maximum quotidien", "SIM : fenêtre 06–06 UTC.", -100, 70),
    "relative_humidity": Variable("Humidité relative de l'air", "%", "Humidité relative atmosphérique", "N'est pas une humidité du sol.", 0, 100),
    "relative_saturation": Variable("Saturation relative", "%", "Fraction de saturation du sol", "Référence et porosité à préciser.", 0, 100),
    "available_water": Variable("Réserve utile", "mm", "Réserve entre capacité au champ et flétrissement", "Nécessite pédologie et épaisseur de sol.", 0),
    "water_deficit": Variable("Déficit hydrique", "mm", "Écart à une réserve de référence", "Référence obligatoire dans les métadonnées."),
    "anomaly": Variable("Anomalie", "1", "Écart à une climatologie définie", "Période de référence et méthode obligatoires."),
}

# PDF SIM_QUOTIDIENNE_parametres, 31 mars 2026. La précision 1/10
# n'est PAS un facteur d'échelle : les CSV contiennent déjà des décimales.
SIM_FIELDS = {
    "SWI": "swi_sim", "SSWI_10J": "sswi_sim", "WG_RACINE": "root_liquid", "WGI_RACINE": "root_ice",
    "PRELIQ": "rainfall", "PRENEI": "snowfall", "PE": "effective_precipitation",
    "EVAP": "actual_et", "ETP": "potential_et", "DRAINC": "drainage",
    "RUNC": "runoff", "T": "temperature", "TINF_H": "temperature_min",
    "TSUP_H": "temperature_max", "HU": "relative_humidity",
}


def parse_value(raw, variable):
    if raw is None or str(raw).strip().lower() in {"", "nan", "na", "null", "mq"}:
        return None, "missing"
    try:
        value = float(str(raw).replace(",", "."))
    except ValueError:
        return None, "invalid"
    spec = VARIABLES[variable]
    if not isfinite(value) or (spec.minimum is not None and value < spec.minimum) or (spec.maximum is not None and value > spec.maximum):
        return None, "invalid"
    return value, "valid"


def convert(value: float, source_unit: str, target_unit: str) -> float:
    if not isfinite(value):
        raise ValueError("Valeur non finie")
    if source_unit == target_unit:
        return value
    conversions = {("kg/m²", "mm"): (1, 0), ("m", "mm"): (1000, 0),
                   ("K", "°C"): (1, -273.15), ("°C", "K"): (1, 273.15)}
    if (source_unit, target_unit) not in conversions:
        raise ValueError(f"Conversion non autorisée : {source_unit} → {target_unit}")
    scale, offset = conversions[source_unit, target_unit]
    return value * scale + offset
