"""
precip_extract_comephore.py
============================

Reconstitue l'historique de précipitations sur le terrain Yavannomë
(Commenailles, 39140) à partir des réanalyses COMEPHORE de Météo-France
(radar + pluviomètres fusionnés, résolution 1 km, horaire, depuis 1997).

Source des données : jeu de données data.gouv.fr
    https://www.data.gouv.fr/datasets/reanalyses-comephore
Une archive TAR mensuelle par mois (H_COMEPHORE_YYYYMM.tar), contenant des
GeoTIFF horaires. L'archive est mise à jour avec ~2 mois de retard
(le mois N-2 apparaît au mois N).

Fonctionnement
--------------
1. Interroge l'API data.gouv.fr du jeu de données pour obtenir la liste des
   ressources (URL de téléchargement réelles, pas de pattern d'URL supposé).
2. Pour chaque mois de la période demandée :
   a. Télécharge l'archive TAR (streaming, avec retries).
   b. L'extrait dans un dossier temporaire.
   c. Pour chaque GeoTIFF horaire trouvé, lit la valeur du pixel à la
      position du terrain (reprojection automatique EPSG:2154 -> CRS du
      raster, quel qu'il soit, via pyproj/rasterio).
   d. Agrège les valeurs horaires en cumul journalier (UTC).
   e. Supprime le TAR et les fichiers extraits (l'historique complet
      représente plusieurs dizaines de Go — on ne garde que le CSV résultat).
3. Append les cumuls journaliers dans precip_yavannome.csv (source=comephore),
   en sautant les dates déjà présentes (reprise possible après interruption).

Usage
-----
    python precip_extract_comephore.py --start 1997-01 --end 2026-06
    python precip_extract_comephore.py --start 2024-01   # jusqu'au mois courant-2

Dépendances : voir requirements.txt (rasterio, pyproj, requests, tqdm)

Avertissement : l'historique complet (1997 -> aujourd'hui) représente
~350 archives d'environ 200-260 Mo chacune (~70-90 Go de téléchargement
cumulé, traité mois par mois donc jamais stocké en totalité sur disque).
Prévoir une connexion stable et du temps ; le script est ré-exécutable
sans risque (reprise automatique).
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import tarfile
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests

from precip_common import TARGET_L93_X, TARGET_L93_Y, append_rows, existing_dates

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("comephore")

DATASET_API_URL = "https://www.data.gouv.fr/api/1/datasets/reanalyses-comephore/"
RESOURCE_TITLE_RE = re.compile(r"H_COMEPHORE_(\d{6})")

# Pattern usuel des noms de GeoTIFF horaires COMEPHORE à l'intérieur des TAR :
# on cherche une séquence de 10 chiffres (YYYYMMDDHH) n'importe où dans le nom,
# ce qui reste robuste même si la convention exacte de nommage évolue.
HOURLY_TS_RE = re.compile(r"(\d{10})")


def list_comephore_resources() -> dict[str, str]:
    """Interroge l'API data.gouv.fr et retourne {"YYYYMM": url_de_telechargement}."""
    resources: dict[str, str] = {}
    url = DATASET_API_URL
    session = requests.Session()
    while url:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for res in data.get("resources", []):
            title = res.get("title", "")
            m = RESOURCE_TITLE_RE.search(title)
            if m:
                resources[m.group(1)] = res["url"]
        # pagination éventuelle de l'API data.gouv.fr
        url = (data.get("next_page") or None)
    if not resources:
        raise RuntimeError(
            "Aucune ressource H_COMEPHORE_YYYYMM trouvée via l'API data.gouv.fr — "
            "le jeu de données a peut-être été renommé/restructuré. "
            "Vérifier manuellement https://www.data.gouv.fr/datasets/reanalyses-comephore"
        )
    return resources


def month_range(start: str, end: str | None) -> list[str]:
    """Génère la liste des mois "YYYYMM" entre start et end (inclus), au format
    'YYYY-MM'. Si end est None, s'arrête au mois courant - 2 (dernier mois
    normalement publié par COMEPHORE)."""
    y0, m0 = (int(p) for p in start.split("-"))
    if end:
        y1, m1 = (int(p) for p in end.split("-"))
    else:
        today = date.today()
        total = today.year * 12 + (today.month - 1) - 2  # N-2
        y1, m1 = divmod(total, 12)
        m1 += 1
    months = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        months.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def download_file(url: str, dest: Path, retries: int = 3) -> None:
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            return
        except requests.RequestException as exc:
            log.warning("Téléchargement échoué (tentative %d/%d) : %s", attempt, retries, exc)
            if dest.exists():
                dest.unlink(missing_ok=True)
            if attempt == retries:
                raise


def read_pixel_value(tif_path: Path) -> float | None:
    """Lit la valeur du pixel du raster `tif_path` à la position du terrain.
    Reprojette les coordonnées cibles (EPSG:2154) vers le CRS natif du
    raster, quel qu'il soit. Retourne None si hors-emprise ou nodata.

    Vérifié sur un fichier réel (juin 2026) : GeoTIFF uint16, échelle/offset
    à 1.0/0.0 (valeurs déjà en mm, pas de conversion à appliquer), mais SANS
    tag nodata déclaré alors que la grille dépasse la couverture radar
    (max brut = 65535 = valeur max uint16, sur une bonne partie du domaine).
    On traite donc explicitement cette valeur sentinelle comme nodata,
    en plus du tag nodata s'il est présent."""
    import rasterio
    from pyproj import Transformer

    with rasterio.open(tif_path) as src:
        transformer = Transformer.from_crs("EPSG:2154", src.crs, always_xy=True)
        x, y = transformer.transform(TARGET_L93_X, TARGET_L93_Y)
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return None
        band = src.read(1, window=((row, row + 1), (col, col + 1)))
        raw = band[0, 0]
        value = float(raw)
        nodata = src.nodata
        if nodata is not None and value == nodata:
            return None
        if nodata is None and str(raw.dtype).startswith("uint"):
            max_representable = float(2 ** (raw.dtype.itemsize * 8) - 1)
            if value == max_representable:
                return None
        return value


def process_month(month: str, url: str, seen_dates: set[str]) -> list[tuple[str, float, str]]:
    """Télécharge, traite et nettoie un mois d'archive COMEPHORE.
    Retourne les lignes (date, cumul_mm, 'comephore') à ajouter au CSV."""
    with tempfile.TemporaryDirectory(prefix=f"comephore_{month}_") as tmpdir:
        tmp = Path(tmpdir)
        tar_path = tmp / f"H_COMEPHORE_{month}.tar"
        log.info("Téléchargement %s ...", url)
        download_file(url, tar_path)

        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        try:
            with tarfile.open(tar_path) as tar:
                tar.extractall(extract_dir)
        except tarfile.TarError as exc:
            log.error("Archive %s corrompue ou illisible : %s", tar_path.name, exc)
            return []
        finally:
            tar_path.unlink(missing_ok=True)  # libère l'espace disque au plus vite

        # Chaque heure a 3 GeoTIFF : _RR (hauteur de précipitation, la donnée
        # voulue), _ERR (champ d'erreur d'estimation) et _QUALIF (indicateur
        # de qualité). Vérifié sur un fichier réel (juin 2026) — ne pas
        # filtrer sur "_RR" fait sommer les trois valeurs ensemble, ce qui
        # gonfle massivement le cumul (bug corrigé le 25/08/2026).
        tif_files = sorted(
            p for p in extract_dir.rglob("*")
            if p.suffix.lower() in (".tif", ".tiff", ".gtif") and p.stem.upper().endswith("_RR")
        )
        if not tif_files:
            log.warning("Aucun GeoTIFF _RR trouvé dans l'archive du mois %s — format inattendu ?", month)
            return []

        daily_sums: dict[str, float] = defaultdict(float)
        daily_missing: dict[str, int] = defaultdict(int)
        for tif in tif_files:
            m = HOURLY_TS_RE.search(tif.stem)
            if not m:
                log.debug("Nom de fichier sans horodatage reconnu, ignoré : %s", tif.name)
                continue
            ts = m.group(1)
            try:
                dt = datetime.strptime(ts, "%Y%m%d%H")
            except ValueError:
                continue
            day_str = dt.strftime("%Y-%m-%d")
            if day_str in seen_dates:
                continue
            try:
                value = read_pixel_value(tif)
            except Exception as exc:  # défensif : un fichier corrompu ne doit pas arrêter le mois
                log.error("Erreur lecture %s : %s", tif.name, exc)
                daily_missing[day_str] += 1
                continue
            if value is None:
                daily_missing[day_str] += 1
                continue
            daily_sums[day_str] += value

        rows = []
        for day_str, total in sorted(daily_sums.items()):
            if daily_missing.get(day_str, 0) > 0:
                log.warning(
                    "%s : %d heure(s) manquante(s)/nodata — cumul journalier partiel (%.2f mm)",
                    day_str, daily_missing[day_str], total,
                )
            rows.append((day_str, total, "comephore"))
        return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="1997-01", help="Mois de début (YYYY-MM), défaut 1997-01")
    parser.add_argument("--end", default=None, help="Mois de fin (YYYY-MM), défaut = mois courant - 2")
    args = parser.parse_args()

    try:
        resources = list_comephore_resources()
    except Exception as exc:
        log.error("Impossible de récupérer le catalogue COMEPHORE : %s", exc)
        return 1

    months = month_range(args.start, args.end)
    seen = existing_dates(source="comephore")
    log.info("%d mois à traiter (%s -> %s), %d dates déjà présentes en cache.",
              len(months), months[0], months[-1], len(seen))

    total_written = 0
    for month in months:
        url = resources.get(month)
        if url is None:
            log.warning("Mois %s absent du catalogue data.gouv.fr (pas encore publié ?) — ignoré.", month)
            continue
        # Reprise : si toutes les dates du mois sont déjà en cache, on saute le téléchargement.
        y, m = int(month[:4]), int(month[4:])
        import calendar
        n_days = calendar.monthrange(y, m)[1]
        month_dates = {f"{y:04d}-{m:02d}-{d:02d}" for d in range(1, n_days + 1)}
        if month_dates.issubset(seen):
            log.info("Mois %s déjà entièrement présent dans le CSV — ignoré.", month)
            continue
        try:
            rows = process_month(month, url, seen)
        except Exception as exc:
            log.error("Échec du traitement du mois %s : %s — poursuite avec le mois suivant.", month, exc)
            continue
        written = append_rows(rows)
        seen.update(r[0] for r in rows)
        total_written += written
        log.info("Mois %s : %d jour(s) ajouté(s).", month, written)

    log.info("Terminé. %d jour(s) ajouté(s) au total dans le CSV.", total_written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
