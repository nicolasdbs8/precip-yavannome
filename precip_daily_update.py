r"""
precip_daily_update.py
========================

Suivi quasi-continu du cumul de précipitations sur le terrain Yavannomë
(Commenailles, 39140) via l'API Météo-France "données radar" (portail-api.
meteofrance.fr, API DPRadar v1).

Architecture — pourquoi ce n'est PAS une requête quotidienne unique
---------------------------------------------------------------------
Vérification faite en conditions réelles (25/08/2026) : le catalogue de
cette API n'expose PAS de produit "ANTILOPE" ni de cumul pré-calculé sur
1h/24h/96h comme espéré initialement. Seuls deux produits existent :

  - LAME_D_EAU    : mosaïque radar de lame d'eau, résolution 1 km/500 m,
                    mise à jour toutes les 5 minutes.
  - REFLECTIVITE  : réflectivité radar brute (non utilisée ici).

Et surtout : la doc de l'observation LAME_D_EAU précise explicitement
"pour la date (TU) la plus récente" — seul le DERNIER instantané 5 min est
téléchargeable, pas d'historique interrogeable à la demande (cohérent avec
la rétention de 20h annoncée mais la limitation "seul le produit le plus
récent est disponible" documentée dans le PDF officiel de l'API).

Ce script doit donc être exécuté **toutes les 5 minutes** (tâche planifiée,
voir plus bas) : à chaque passage, il récupère le dernier instantané
disponible, l'ajoute à un accumulateur journalier persistant (fichier JSON
d'état, à côté du CSV), et n'écrit une ligne dans precip_yavannome.csv
qu'au moment où un nouvel instantané appartenant au jour SUIVANT est
détecté (flush du cumul de la veille). Un instantané déjà comptabilisé
(même validity_time) n'est jamais recompté, ce qui rend le script
idempotent même s'il tourne plus souvent que toutes les 5 minutes ou est
relancé après un crash.

Hypothèse retenue (à valider) : chaque valeur LAME_D_EAU représente un
cumul en mm sur le pas de 5 minutes ; la somme des instantanés d'une
journée UTC donne donc le cumul journalier. À confirmer/recaler le jour où
un pluviomètre physique sera installé sur site (comparaison directe via
source=pluviometre_site, cf. precip_visualize.py).

Authentification (à faire une fois, gratuit)
---------------------------------------------
1. Compte sur https://portail-api.meteofrance.fr/, s'abonner à l'API
   "données radar" (DPRadar).
2. Sur la page "Configurer l'API" de cette API : choisir le mode
   **"API Key"** (pas OAuth2), renseigner une durée de validité longue en
   secondes (essayer 31536000 = 1 an ; réduire si refusé par le
   formulaire), cliquer "Générer Token", révéler la valeur (icône œil) et
   la copier.
3. Stocker cette clé en variable d'environnement (jamais en dur) :

     setx METEOFRANCE_APPLICATION_ID "votre_cle_api_ici"

Cette clé est envoyée telle quelle dans l'en-tête `apikey` à chaque
requête (pas d'échange OAuth2 côté script — plus simple, mais la clé
expire à la durée choisie et devra être régénérée manuellement à ce
moment-là, contrairement à un flux OAuth2 client_credentials classique).

Exécution — GitHub Actions toutes les 5 minutes, pas sur votre PC
--------------------------------------------------------------------
Voir .github/workflows/precip_antilope.yml et README.md : le suivi tourne
dans le cloud (dépôt GitHub public, gratuit) pour ne pas avoir à laisser
le PC allumé en continu. Le workflow committe le CSV et l'état persistant
à chaque instantané traité.

Format du produit téléchargé — attention BUFR vs HDF5
---------------------------------------------------------
D'après la doc officielle de l'API : à la maille 1000 m, le produit est au
format **BUFR** (binaire météo spécialisé, nécessite ecCodes + tables de
descripteurs propriétaires Météo-France — non géré ici). À la maille
500 m, il est au format **HDF5** (probablement ODIM_H5, le standard
EUMETNET/OPERA) — c'est celui utilisé par ce script (MAILLE=500), avec
lecture générique via h5py. Si la structure interne du fichier diffère de
ce qui est supposé dans read_odim_h5_value(), le script journalise un
diagnostic (octets magiques, groupes HDF5 trouvés) pour ajuster le
parsing.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from precip_common import CSV_PATH, TARGET_L93_X, TARGET_L93_Y, append_rows, existing_dates

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("antilope")

API_BASE = "https://public-api.meteofrance.fr/public/DPRadar/v1"
ZONE = "METROPOLE"
MAILLE = 500  # 500 m -> HDF5 (ODIM), lu via h5py ; 1000 m est en BUFR (non géré ici, cf. docstring)

# Nombre attendu d'instantanés 5 min par jour, pour signaler un cumul incomplet.
EXPECTED_SLOTS_PER_DAY = 24 * 60 // 5  # 288

STATE_PATH = Path(os.environ.get("PRECIP_STATE_PATH", CSV_PATH.parent / "precip_antilope_state.json"))

# Si la détection automatique du produit échoue (catalogue modifié côté
# Météo-France), fixer ici son nom exact, ou via la variable d'env
# METEOFRANCE_OBSERVATION.
OBSERVATION_OVERRIDE = os.environ.get("METEOFRANCE_OBSERVATION")


def get_auth_headers() -> dict[str, str]:
    """Clé API statique (mode "API Key" du portail, header `apikey`) —
    plus simple qu'un échange OAuth2, mais expire à la durée choisie lors
    de sa génération sur le portail (à régénérer manuellement à ce
    moment-là)."""
    api_key = os.environ.get("METEOFRANCE_APPLICATION_ID")
    if not api_key:
        raise RuntimeError(
            "Variable d'environnement METEOFRANCE_APPLICATION_ID absente. "
            "Voir la documentation en tête de fichier / README.md pour l'obtenir."
        )
    return {"apikey": api_key}


def discover_observation_name(headers: dict[str, str]) -> str:
    """Retourne le nom de l'observation à utiliser (ex: 'LAME_D_EAU'),
    détecté dans le catalogue en direct plutôt que figé en dur."""
    if OBSERVATION_OVERRIDE:
        return OBSERVATION_OVERRIDE
    headers = {**headers, "accept": "application/json"}
    resp = requests.get(f"{API_BASE}/mosaiques/{ZONE}/observations", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    links = data.get("links", data if isinstance(data, list) else [])
    names = []
    for item in links:
        title = item.get("title", "")
        href = item.get("href", "")
        name = href.rstrip("/").rsplit("/", 1)[-1]
        names.append(name)
        if "ANTILOPE" in title.upper() or "ANTILOPE" in name.upper():
            log.info("Produit ANTILOPE détecté : %s", name)
            return name
    if "LAME_D_EAU" in names:
        log.info("Pas de produit ANTILOPE explicite — utilisation de LAME_D_EAU (radar+pluvio 5 min).")
        return "LAME_D_EAU"
    raise RuntimeError(
        f"Aucun produit ANTILOPE/LAME_D_EAU trouvé dans le catalogue. Produits disponibles : {names}. "
        "Fixez METEOFRANCE_OBSERVATION avec le nom exact souhaité."
    )


def get_latest_snapshot(headers: dict[str, str], observation: str) -> tuple[str, str]:
    """Retourne (validity_time ISO8601, url de téléchargement) pour la
    maille configurée, à partir du dernier instantané disponible."""
    headers = {**headers, "accept": "application/json"}
    resp = requests.get(f"{API_BASE}/mosaiques/{ZONE}/observations/{observation}", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    links = data.get("links", data if isinstance(data, list) else [])
    for item in links:
        href = item.get("href", "")
        if f"maille={MAILLE}" in href:
            validity_time = item.get("validity_time")
            if not validity_time:
                raise RuntimeError(f"Pas de validity_time dans la réponse : {item}")
            return validity_time, href
    raise RuntimeError(f"Aucun produit à la maille {MAILLE} trouvé pour {observation} : {links}")


def download_product(headers: dict[str, str], href: str) -> bytes:
    headers = {**headers, "accept": "*/*"}
    resp = requests.get(href, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content


def diagnose_format(raw_bytes: bytes) -> str:
    """Renvoie une description courte des octets reçus (magiques connus +
    aperçu hexadécimal), pour diagnostiquer un format inattendu sans accès
    direct à l'API."""
    magic_map = {
        b"\x89HDF\r\n\x1a\n": "HDF5",
        b"BUFR": "BUFR",
        b"GRIB": "GRIB",
        b"\x1f\x8b": "gzip",
        b"II*\x00": "TIFF (little-endian)",
        b"MM\x00*": "TIFF (big-endian)",
        b"PK\x03\x04": "ZIP",
    }
    detected = next((name for magic, name in magic_map.items() if raw_bytes.startswith(magic)), "inconnu")
    preview = raw_bytes[:16].hex(" ")
    return f"format détecté={detected}, taille={len(raw_bytes)} octets, 16 premiers octets=[{preview}]"


def read_odim_h5_value(content: bytes) -> float | None:
    """Lit la valeur du pixel à la position du terrain dans un fichier
    HDF5 de mosaïque radar (structure attendue : convention ODIM_H5,
    standard EUMETNET/OPERA — /where pour la géoréférence, /dataset1/data1
    pour les valeurs). Structure sondée dynamiquement (pas de chemin figé)
    pour rester robuste à de légères variantes."""
    import io

    import h5py
    from pyproj import CRS, Transformer

    with h5py.File(io.BytesIO(content), "r") as f:
        where = f.get("where")
        if where is None:
            raise RuntimeError(f"Pas de groupe /where — groupes racine trouvés : {list(f.keys())}")
        attrs = where.attrs

        # Cherche le premier dataset 2D de valeurs (convention ODIM: datasetN/data1/data)
        data_ds = None
        data_group = None
        for key in f.keys():
            if key.startswith("dataset"):
                candidate = f[key].get("data1", {}).get("data") if "data1" in f[key] else None
                if candidate is not None:
                    data_ds = candidate
                    data_group = f[key]["data1"]
                    break
        if data_ds is None:
            raise RuntimeError(f"Aucun dataset de type datasetN/data1/data trouvé — groupes : {list(f.keys())}")

        array = data_ds[()]
        height, width = array.shape[-2], array.shape[-1]

        # Géoréférence : proj4 + bbox (convention ODIM standard)
        proj4 = attrs.get("projdef")
        has_projected_bbox = proj4 is not None and "LL_x" in attrs
        if has_projected_bbox:
            if isinstance(proj4, bytes):
                proj4 = proj4.decode()
            crs = CRS.from_proj4(proj4)
            transformer = Transformer.from_crs("EPSG:2154", crs, always_xy=True)
            x, y = transformer.transform(TARGET_L93_X, TARGET_L93_Y)
            ll_x, ll_y = float(attrs["LL_x"]), float(attrs["LL_y"])

        if not has_projected_bbox:
            # Repli : bbox en lon/lat (LL_lon/LL_lat/UR_lon/UR_lat), mapping linéaire simple
            transformer = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
            lon, lat = transformer.transform(TARGET_L93_X, TARGET_L93_Y)
            ll_lon, ll_lat = float(attrs["LL_lon"]), float(attrs["LL_lat"])
            ur_lon, ur_lat = float(attrs["UR_lon"]), float(attrs["UR_lat"])
            if not (ll_lon <= lon <= ur_lon and ll_lat <= lat <= ur_lat):
                return None
            col = int((lon - ll_lon) / (ur_lon - ll_lon) * width)
            row = int((ur_lat - lat) / (ur_lat - ll_lat) * height)
        else:
            xscale, yscale = float(attrs["xscale"]), float(attrs["yscale"])
            col = int((x - ll_x) / xscale)
            row = int((ll_y + height * yscale - y) / yscale)

        if not (0 <= row < height and 0 <= col < width):
            return None

        raw_value = float(array[..., row, col].reshape(-1)[0])

        what = data_group.get("what")
        gain, offset, nodata, undetect = 1.0, 0.0, None, None
        if what is not None:
            gain = float(what.attrs.get("gain", 1.0))
            offset = float(what.attrs.get("offset", 0.0))
            nodata = what.attrs.get("nodata")
            undetect = what.attrs.get("undetect")
        if nodata is not None and raw_value == float(nodata):
            return None
        if undetect is not None and raw_value == float(undetect):
            return 0.0
        return raw_value * gain + offset


def extract_raster_value(raw_bytes: bytes) -> float | None:
    """Lit la valeur du pixel à la position du terrain dans le produit
    téléchargé. Tente HDF5 (ODIM) en premier — format attendu à la maille
    500 m configurée — puis GeoTIFF brut/gzippé en repli.

    NB : si le format exact renvoyé diffère de tout ce qui est tenté ici,
    l'erreur inclut un diagnostic des octets reçus (diagnose_format) —
    consultez le "Descriptif technique des produits" (onglet documentation
    du portail des données publiques Météo-France) pour l'identifier.
    """
    errors: list[str] = []

    try:
        return read_odim_h5_value(raw_bytes)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"HDF5/ODIM : {exc}")

    import rasterio
    from pyproj import Transformer

    candidates: list[bytes] = [raw_bytes]
    try:
        candidates.insert(0, gzip.decompress(raw_bytes))
    except OSError:
        pass  # pas gzippé, on garde le contenu brut

    for content in candidates:
        try:
            with rasterio.MemoryFile(content) as memfile:
                with memfile.open() as src:
                    transformer = Transformer.from_crs("EPSG:2154", src.crs, always_xy=True)
                    x, y = transformer.transform(TARGET_L93_X, TARGET_L93_Y)
                    row, col = src.index(x, y)
                    if not (0 <= row < src.height and 0 <= col < src.width):
                        return None
                    band = src.read(1, window=((row, row + 1), (col, col + 1)))
                    value = float(band[0, 0])
                    if src.nodata is not None and value == src.nodata:
                        return None
                    return value
        except Exception as exc:  # noqa: BLE001
            errors.append(f"GeoTIFF : {exc}")
            continue

    raise RuntimeError(
        f"Impossible de décoder le raster reçu. Tentatives échouées : {' | '.join(errors)}. "
        f"Diagnostic : {diagnose_format(raw_bytes)}"
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("État illisible (%s) — redémarrage à zéro.", exc)
    return {"day": None, "sum_mm": 0.0, "processed": []}


def save_state(state: dict) -> None:
    # borne la liste des instantanés mémorisés pour ne pas grossir indéfiniment
    state["processed"] = state["processed"][-EXPECTED_SLOTS_PER_DAY * 2:]
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def flush_day(day: str, sum_mm: float, n_slots: int) -> None:
    seen = existing_dates(source="antilope")
    if day in seen:
        log.info("%s déjà présent dans le CSV (source=antilope) — pas de doublon écrit.", day)
        return
    if n_slots < EXPECTED_SLOTS_PER_DAY:
        log.warning(
            "%s : cumul basé sur %d/%d instantanés 5 min (%.1f%% de couverture) — valeur possiblement sous-estimée.",
            day, n_slots, EXPECTED_SLOTS_PER_DAY, 100 * n_slots / EXPECTED_SLOTS_PER_DAY,
        )
    append_rows([(day, sum_mm, "antilope")])
    log.info("Cumul du %s écrit : %.2f mm (%d instantanés, source=antilope).", day, sum_mm, n_slots)


def main() -> int:
    try:
        headers = get_auth_headers()
    except Exception as exc:
        log.error("Authentification échouée : %s", exc)
        return 1

    try:
        observation = discover_observation_name(headers)
        validity_time, href = get_latest_snapshot(headers, observation)
    except Exception as exc:
        log.error("Découverte du produit/instantané échouée : %s", exc)
        return 1

    try:
        dt = datetime.fromisoformat(validity_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        log.error("validity_time inattendu : %r", validity_time)
        return 1
    day_str = dt.date().isoformat()

    state = load_state()
    if validity_time in state["processed"]:
        log.info("Instantané %s déjà comptabilisé — rien à faire (prochain passage dans ~5 min).", validity_time)
        return 0

    # Changement de jour : on flush le cumul de la veille avant de démarrer le nouvel accumulateur.
    if state["day"] is not None and state["day"] != day_str:
        flush_day(state["day"], state["sum_mm"], len(state["processed"]))
        state = {"day": day_str, "sum_mm": 0.0, "processed": []}
    elif state["day"] is None:
        state["day"] = day_str

    try:
        raw = download_product(headers, href)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 429:
            log.error("Quota API dépassé (429). Nouvelle tentative au prochain passage planifié.")
        else:
            log.error("Téléchargement échoué : %s", exc)
        return 1
    except Exception as exc:
        log.error("Téléchargement échoué : %s", exc)
        return 1

    try:
        value = extract_raster_value(raw)
    except Exception as exc:
        log.error("Lecture du raster échouée : %s", exc)
        return 1

    if value is None:
        log.warning("Terrain hors emprise ou pixel nodata pour cet instantané — ignoré (comptera comme trou).")
        return 0

    state["sum_mm"] += value
    state["processed"].append(validity_time)
    save_state(state)
    log.info(
        "Instantané %s : +%.3f mm -> cumul %s en cours = %.2f mm (%d instantanés).",
        validity_time, value, day_str, state["sum_mm"], len(state["processed"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
