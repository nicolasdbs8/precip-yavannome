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

Tâche planifiée — toutes les 5 minutes, en continu
-----------------------------------------------------
Windows (Planificateur de tâches) :

    schtasks /create /tn "Precip Yavannome 5min" ^
      /tr "python C:\chemin\vers\precip_daily_update.py" ^
      /sc minute /mo 5

(adapter le chemin vers python.exe si un venv est utilisé)

Linux/macOS (cron), équivalent :

    */5 * * * * /usr/bin/env METEOFRANCE_APPLICATION_ID=... python3 /chemin/precip_daily_update.py >> /chemin/precip_daily_update.log 2>&1
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
MAILLE = 1000  # 1000 m -> format gzip/GeoTIFF géré par rasterio ; 500 m est en HDF5 (non géré ici)

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


def extract_raster_value(raw_bytes: bytes) -> float | None:
    """Lit la valeur du pixel à la position du terrain dans le produit
    téléchargé (GeoTIFF brut ou gzippé selon la réponse de l'API).

    NB : si le format exact renvoyé diffère (ex: format propriétaire
    Météo-France non documenté publiquement), c'est ICI qu'il faudra
    adapter le parsing — consultez le "Descriptif technique des produits"
    (onglet documentation du portail des données publiques Météo-France).
    """
    import rasterio
    from pyproj import Transformer

    candidates: list[bytes] = [raw_bytes]
    try:
        candidates.insert(0, gzip.decompress(raw_bytes))
    except OSError:
        pass  # pas gzippé, on garde le contenu brut

    last_error: Exception | None = None
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
        except Exception as exc:  # noqa: BLE001 - on essaie le candidat suivant
            last_error = exc
            continue

    raise RuntimeError(
        f"Impossible de décoder le raster reçu (GeoTIFF/gzip essayés). "
        f"Format probablement différent de celui attendu — voir la docstring "
        f"de extract_raster_value(). Dernière erreur : {last_error}"
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
