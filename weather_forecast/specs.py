"""Point de référence et modèles demandés explicitement à Open-Meteo.

Identifiants vérifiés le 21/10/2026 dans le code de la documentation Open-Meteo
(open-meteo-website, `src/routes/en/docs/*/options.ts`) puis par appels réels
au point ZP1. `meta_id` : dossier `/data/{meta_id}/static/meta.json` (heure du
dernier run) ; il diffère parfois de l'identifiant API et n'existe pas pour
l'ensemble IFS 9 km (le run n'est alors pas publié plutôt que deviné).
"""
from __future__ import annotations

from dataclasses import dataclass

# Centroïde cadastral de la parcelle ZP1, identique à hydrologie_yavannome.json
# (parcel.centroid, calculé en Lambert-93 par soil_hydrology.parcel). Pas de géocodage.
POINT = {"parcel_id": "39160000ZP0001", "commune": "Commenailles", "lat": 46.8039042, "lon": 5.450162,
         "timezone": "Europe/Paris", "source": "centroïde cadastral du module Sol / Hydrologie (IGN API Carto)"}

# Variables horaires. Les secondaires ne font jamais échouer un modèle.
DETERMINISTIC_VARIABLES = ("temperature_2m", "precipitation", "rain", "snowfall", "wind_speed_10m",
                           "wind_gusts_10m", "relative_humidity_2m")
SECONDARY_VARIABLES = ("et0_fao_evapotranspiration", "surface_temperature")
ENSEMBLE_VARIABLES = ("precipitation", "temperature_2m", "wind_gusts_10m")

# Modèles réputés ne jamais servir certaines variables (constaté par appels réels) :
# l'absence est alors normale et documentée, pas une dégradation du modèle.
NOT_PROVIDED = {
    "meteofrance_arome_france_hd": ("rain", "snowfall", "et0_fao_evapotranspiration", "surface_temperature"),
    "meteofrance_arome_france": ("surface_temperature",),
    "meteofrance_arpege_europe": ("surface_temperature",),
}


@dataclass(frozen=True)
class ModelSpec:
    key: str                     # clé publiée dans le JSON
    kind: str                    # "deterministic" | "ensemble"
    model_id: str                # identifiant API Open-Meteo, toujours explicite
    meta_id: str | None
    provider: str
    model_name: str
    region: str
    nominal_resolution_km: float
    nominal_horizon_hours: int   # horizon nominal du producteur (documentation Open-Meteo)
    role: str
    members_expected: int | None = None
    relay_for: str | None = None   # ne vote que si ce modèle (même système, domaine réduit) est hors horizon

    @property
    def variables(self) -> tuple[str, ...]:
        if self.kind == "ensemble":
            return ENSEMBLE_VARIABLES
        return DETERMINISTIC_VARIABLES + SECONDARY_VARIABLES

    @property
    def not_provided(self) -> tuple[str, ...]:
        return NOT_PROVIDED.get(self.model_id, ())


DETERMINISTIC = (
    ModelSpec("arome_hd", "deterministic", "meteofrance_arome_france_hd", "meteofrance_arome_france_hd", "Météo-France",
              "AROME France HD", "France", 1.5, 48, "très court terme J0–J2, poids interprétatif renforcé"),
    ModelSpec("arome", "deterministic", "meteofrance_arome_france", "meteofrance_arome_france0025", "Météo-France",
              "AROME France", "France", 2.5, 48, "très court terme J0–J2, poids interprétatif renforcé"),
    ModelSpec("arpege", "deterministic", "meteofrance_arpege_europe", "meteofrance_arpege_europe", "Météo-France",
              "ARPEGE Europe", "Europe", 11.0, 96, "court terme J0–J4"),
    ModelSpec("ecmwf", "deterministic", "ecmwf_ifs", "ecmwf_ifs", "ECMWF",
              "IFS HRES 9 km (grille native O1280)", "Global (grille native 9 km)", 9.0, 360, "contrôle indépendant J0–J7"),
    ModelSpec("icon", "deterministic", "dwd_icon_eu", "dwd_icon_eu", "DWD",
              "ICON-EU", "Europe", 7.0, 120, "contrôle indépendant J0–J5"),
    # ICON-EU s'arrête à +120 h : sans relais, J+5–J+7 n'auraient qu'IFS. Le global est le modèle pilote
    # d'ICON-EU (même système) : publié séparément, il ne vote qu'en l'absence d'ICON-EU (signalé).
    ModelSpec("icon_global", "deterministic", "dwd_icon_global", "dwd_icon", "DWD",
              "ICON Global", "Global", 13.0, 180, "relais explicite d'ICON-EU au-delà de son horizon (J+5–J+7)",
              relay_for="icon"),
)
ENSEMBLES = (
    ModelSpec("ecmwf_ens", "ensemble", "ecmwf_ifs_europe_ensemble", None, "ECMWF",
              "IFS ENS 9 km (O1280) Europe", "Europe", 9.0, 360, "ensemble principal J0–J7", members_expected=51),
    # Variante « native » (grille icosaédrique, sans réinterpolation) : même maille servie que
    # dwd_icon_eu_eps, et seul meta.json à jour (celui de dwd_icon_eu_eps affichait un run du 06/10/2026).
    ModelSpec("icon_eu_eps", "ensemble", "dwd_icon_eu_eps_native", "dwd_icon_eu_eps_native", "DWD",
              "ICON-EU-EPS (grille native)", "Europe", 6.5, 120, "second ensemble indépendant J0–J5", members_expected=40),
)
ALL_MODELS = DETERMINISTIC + ENSEMBLES
RELAYS = {s.key: s.relay_for for s in DETERMINISTIC if s.relay_for}

# Agrégateurs implicites refusés : le modèle exact doit toujours être demandé.
FORBIDDEN_MODEL_TOKENS = ("best_match", "seamless")

ATTRIBUTION = {
    "open_meteo": {"text": "Weather data by Open-Meteo.com", "url": "https://open-meteo.com/",
                   "licence": "CC BY 4.0", "licence_url": "https://creativecommons.org/licenses/by/4.0/",
                   "usage": "usage personnel non commercial, API gratuite sans clé"},
    "producers": [
        {"name": "Météo-France", "models": ["AROME France HD", "AROME France", "ARPEGE Europe"],
         "licence": "Licence Ouverte / Open Licence Etalab 2.0"},
        {"name": "ECMWF", "models": ["IFS HRES 9 km", "IFS ENS 9 km Europe"],
         "licence": "ECMWF open data, CC BY 4.0"},
        {"name": "Deutscher Wetterdienst (DWD)", "models": ["ICON-EU", "ICON Global", "ICON-EU-EPS"],
         "licence": "DWD open data, attribution « Quelle : Deutscher Wetterdienst »"},
    ],
    "changes": "séries horaires agrégées par période locale ; aucune moyenne inter-modèles",
}
