"""Parcelle cadastrale de référence : API Carto IGN, conservée en fixture.

La fixture versionnée évite qu'une panne de l'API cadastrale bloque le
pipeline quotidien. En cas d'échec sans fixture, aucune géométrie n'est
fabriquée : l'erreur remonte et la sélection de maille devient impossible.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from . import geo
from .models import utc_now

API_CARTO = "https://apicarto.ign.fr/api/cadastre/parcelle"
DATA_DIR = Path(__file__).parent / "data"
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParcelRef:
    insee: str
    section: str
    number: str

    @property
    def idu(self) -> str:
        # Département (2) + commune (3) + préfixe/commune absorbée (3) + section (2) + numéro (4).
        return f"{self.insee}000{self.section.rjust(2, '0')}{self.number.rjust(4, '0')}"

    @property
    def fixture(self) -> Path:
        return DATA_DIR / f"parcelle_{self.idu}.geojson"


# Terrain Yavannomë : Commenailles (39), section ZP, parcelle 0001.
ZP1 = ParcelRef("39160", "ZP", "0001")


@dataclass(frozen=True)
class Parcel:
    ref: ParcelRef
    feature: dict  # Feature GeoJSON WGS84 telle que renvoyée par l'API, enrichie de "rain"

    @property
    def polygons_wgs84(self):
        return geo.geojson_polygons(self.feature["geometry"])

    def polygons(self, crs: str):
        return geo.project(self.polygons_wgs84, geo.WGS84, crs)

    @property
    def centroid_wgs84(self) -> tuple[float, float]:
        """(lon, lat) du centroïde surfacique calculé en Lambert-93."""
        x, y = geo.centroid(self.polygons(geo.LAMBERT93))
        return geo.transformer(geo.LAMBERT93, geo.WGS84).transform(x, y)

    def summary(self) -> dict:
        props, meta = self.feature["properties"], self.feature.get("rain", {})
        lon, lat = self.centroid_wgs84
        return {
            "insee": self.ref.insee, "section": self.ref.section, "number": self.ref.number,
            "idu": props.get("idu"), "commune": props.get("nom_com"),
            "area_m2": props.get("contenance"), "area_m2_geometry": round(geo.area(self.polygons(geo.LAMBERT93)), 1),
            "centroid": {"lat": round(lat, 7), "lon": round(lon, 7)},
            "bbox_wgs84": [round(v, 8) for v in geo.bounds(self.polygons_wgs84)],
            "geometry": geo.to_geojson(self.polygons_wgs84),
            "geometry_source": {k: meta.get(k) for k in ("provider", "url", "retrieved_at", "feature_id")},
        }


def parse_api_response(payload: dict, ref: ParcelRef, url: str) -> dict:
    features = payload.get("features") or []
    if len(features) != 1:
        raise ValueError(f"API Carto : {len(features)} parcelle(s) pour {ref.idu}, une seule attendue")
    feature = features[0]
    props = feature.get("properties", {})
    if props.get("idu") != ref.idu:
        raise ValueError(f"API Carto : IDU inattendu {props.get('idu')!r} (attendu {ref.idu})")
    geo.geojson_polygons(feature["geometry"])  # valide la géométrie
    feature["rain"] = {"provider": "IGN API Carto — module cadastre", "url": url,
                       "retrieved_at": utc_now(), "feature_id": feature.get("id")}
    return feature


def fetch_parcel(ref: ParcelRef, http) -> dict:
    params = {"code_insee": ref.insee, "section": ref.section, "numero": ref.number}
    url = f"{API_CARTO}?code_insee={ref.insee}&section={ref.section}&numero={ref.number}"
    log.info("Cadastre : interrogation API Carto pour %s", ref.idu)
    response = http.get(API_CARTO, params=params, timeout=(15, 60))
    if response.status_code != 200:
        raise RuntimeError(f"API Carto : HTTP {response.status_code} pour {url}")
    return parse_api_response(response.json(), ref, url)


def load_parcel(ref: ParcelRef = ZP1, http=None, refresh: bool = False) -> Parcel:
    """Fixture locale par défaut ; `refresh` réinterroge l'API et met la fixture à jour."""
    path = ref.fixture
    if refresh or not path.exists():
        if http is None:
            raise RuntimeError(f"Géométrie de {ref.idu} absente ({path}) et aucun accès réseau fourni")
        feature = fetch_parcel(ref, http)
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            if old["geometry"] != feature["geometry"]:
                log.warning("Cadastre : la géométrie de %s a changé depuis la fixture du %s",
                            ref.idu, old.get("rain", {}).get("retrieved_at"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(feature, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        log.info("Cadastre : fixture %s enregistrée", path.name)
    else:
        feature = json.loads(path.read_text(encoding="utf-8"))
        log.info("Cadastre : géométrie %s lue depuis la fixture (récupérée le %s)",
                 ref.idu, feature.get("rain", {}).get("retrieved_at"))
    return Parcel(ref, feature)
