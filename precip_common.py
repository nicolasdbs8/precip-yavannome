"""
Constantes et utilitaires partagés par les scripts de suivi pluviométrique
du terrain Yavannomë (Commenailles, Jura, 39140).

Coordonnées du terrain
-----------------------
Reprises du polygone terrain défini dans le projet haie_optimizer
(concepteur_haies/haie_optimizer/engine/terrain_grid.py, VERTICES_CC46),
8 bornes officielles du géomètre en EPSG:3947 (CC47, malgré le nom
"CC46" donné par le géomètre — vérifié : Commenailles est à ~46.81°N,
dans la zone CC47 46.25°-47.75°N).

Le centroïde du polygone a été recalculé ici (pyproj, EPSG:3947 -> EPSG:2154)
et validé par l'utilisateur comme point d'extraction unique — la résolution
1 km de COMEPHORE/ANTILOPE fait qu'un seul pixel couvre de toute façon
l'intégralité des 4600 m² du terrain.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────
# Coordonnées du point d'extraction (centroïde du terrain)
# ─────────────────────────────────────────────────────────────────────────
TARGET_L93_X: float = 886824.91
TARGET_L93_Y: float = 6636650.21
TARGET_LON: float = 5.450160
TARGET_LAT: float = 46.803906

CRS_L93 = "EPSG:2154"
CRS_WGS84 = "EPSG:4326"

# ─────────────────────────────────────────────────────────────────────────
# CSV partagé
# ─────────────────────────────────────────────────────────────────────────
CSV_PATH = Path(os.environ.get("PRECIP_CSV_PATH", Path(__file__).parent / "precip_yavannome.csv"))
CSV_COLUMNS = ["date", "cumul_mm", "source"]

# Sources valides — "pluviometre_site" réservé au futur pluviomètre physique
# (cf. précip_visualize.py, superposable pour calibration modèle vs mesure réelle).
VALID_SOURCES = {"comephore", "antilope", "pluviometre_site"}


def ensure_csv() -> None:
    """Crée le CSV avec l'en-tête s'il n'existe pas encore."""
    if not CSV_PATH.exists():
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)


def existing_dates(source: str | None = None) -> set[str]:
    """Retourne l'ensemble des dates (str, YYYY-MM-DD) déjà présentes dans le CSV,
    optionnellement filtrées par source. Utilisé pour dédupliquer / reprendre
    un traitement interrompu sans retélécharger ce qui est déjà acquis."""
    ensure_csv()
    dates: set[str] = set()
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if source is None or row.get("source") == source:
                dates.add(row["date"])
    return dates


def append_rows(rows: list[tuple[str, float, str]]) -> int:
    """Ajoute des lignes (date, cumul_mm, source) au CSV partagé.
    Ne fait aucune vérification de doublon ici (à faire en amont via
    existing_dates()) — retourne le nombre de lignes écrites."""
    if not rows:
        return 0
    ensure_csv()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for date, cumul_mm, source in rows:
            if source not in VALID_SOURCES:
                raise ValueError(f"Source invalide : {source!r} (attendu parmi {VALID_SOURCES})")
            writer.writerow([date, f"{cumul_mm:.2f}", source])
    return len(rows)
