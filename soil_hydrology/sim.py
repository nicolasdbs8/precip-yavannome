"""Météo-France SIM2 quotidien (SAFRAN-ISBA) : mailles de la parcelle, extraction ciblée.

Source de référence : jeu data.gouv.fr DATASET_ID, ressources découvertes par
l'API (jamais d'URL de fichier codée en dur). Deux chemins techniques vers le
MÊME fichier officiel :

- « tabular » : API tabulaire data.gouv, filtre serveur LAMBX/LAMBY. Utilisée
  seulement si data.gouv a ingéré exactement la version actuellement publiée
  par Météo-France (Last-Modified du fichier = analysis:last-modified-at) ;
- « csv » : téléchargement du .csv.gz officiel, lu en flux, requête
  conditionnelle ETag/Last-Modified, seules les lignes des mailles retenues.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import re
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from . import geo
from .models import Nature, Observation, ProviderResult, Query, utc_now, utc_today
from .transport import cached_document, session
from .variables import SIM_FIELDS, VARIABLES, parse_value

DATASET_ID = "6569b27598256cc583c917a7"
CATALOG = f"https://www.data.gouv.fr/api/1/datasets/{DATASET_ID}/"
TABULAR = "https://tabular-api.data.gouv.fr/api/resources/{rid}/data/"
GRID_TITLE = "coordonnees_grille_safran_lambert-2-etendu.csv"
PARAMETERS_TITLE = "SIM_QUOTIDIENNE_parametres.pdf"
LATEST = re.compile(r"^QUOT_SIM2_latest")
CELL_M = 8000  # « grille régulière de 8 km », coordonnées = centre de maille
REQUIRED = {"LAMBX", "LAMBY", "DATE"}
log = logging.getLogger(__name__)


class SimFormatError(ValueError):
    """Fichier SIM tronqué, corrompu ou au schéma inattendu."""


@dataclass(frozen=True)
class SimGrid:
    lambx_hm: int
    lamby_hm: int
    lat: float  # centre tel que publié par Météo-France
    lon: float
    role: str  # "parcel" : intersecte la parcelle ; "adjacent" : voisine, contexte seulement
    overlap_m2: float
    overlap_fraction: float
    distance_m: float

    @property
    def id(self) -> str:
        return f"{self.lambx_hm}_{self.lamby_hm}"

    @property
    def box(self):
        x, y, h = self.lambx_hm * 100, self.lamby_hm * 100, CELL_M / 2
        return x - h, y - h, x + h, y + h

    def to_dict(self):
        return {"id": self.id, **asdict(self)}


def cell_box(lambx_hm: float, lamby_hm: float):
    x, y, h = lambx_hm * 100, lamby_hm * 100, CELL_M / 2
    return x - h, y - h, x + h, y + h


def locate_parcel(grid_text: str, polygons_l2e, neighbor_margin_m: float = 100) -> list[SimGrid]:
    """Mailles intersectant la parcelle (polygones en EPSG:27572), puis mailles
    voisines à moins de `neighbor_margin_m` (contexte, jamais substituées)."""
    xmin, ymin, xmax, ymax = geo.bounds(polygons_l2e)
    parcel_area = geo.area(polygons_l2e)
    reach = CELL_M / 2 + neighbor_margin_m
    found = []
    for row in csv.DictReader(io.StringIO(grid_text), delimiter=";"):
        gx, gy = int(row["LAMBX (hm)"]), int(row["LAMBY (hm)"])
        if gx * 100 < xmin - reach or gx * 100 > xmax + reach or gy * 100 < ymin - reach or gy * 100 > ymax + reach:
            continue
        box = cell_box(gx, gy)
        overlap = geo.intersection_area(polygons_l2e, box)
        distance = 0.0 if overlap > 0 else geo.box_distance(polygons_l2e, box)
        if overlap <= 0 and distance > neighbor_margin_m:
            continue
        found.append(SimGrid(gx, gy, float(row["LAT_DG"].replace(",", ".")), float(row["LON_DG"].replace(",", ".")),
                             "parcel" if overlap > 0 else "adjacent", round(overlap, 1),
                             round(overlap / parcel_area, 4), round(distance, 1)))
    if not any(g.role == "parcel" for g in found):
        raise ValueError("Parcelle hors de la grille SIM : aucune maille ne l'intersecte")
    return sorted(found, key=lambda g: (g.role != "parcel", -g.overlap_m2, g.distance_m))


def parcel_grids(http, cache, polygons_l2e, offline=False, refresh=False, neighbor_margin_m: float = 100):
    """Grille officielle (ressource du jeu SIM, cache 7 jours) puis intersection."""
    text, _ = cached_document(http, cache, CATALOG, offline, refresh, ttl=3600)
    grid = MeteoFranceSimProvider.resource(json.loads(text), GRID_TITLE)
    if grid is None:
        raise RuntimeError(f"Ressource {GRID_TITLE} absente du jeu {DATASET_ID}")
    grid_text, _ = cached_document(http, cache, grid["url"], offline, refresh, ttl=7 * 86400)
    grids = locate_parcel(grid_text, polygons_l2e, neighbor_margin_m)
    for g in grids:
        log.info("SIM : maille %s (%s) — recouvrement %.1f %%, distance %.1f m, centre %.5f N %.5f E",
                 g.id, g.role, 100 * g.overlap_fraction, g.distance_m, g.lat, g.lon)
    return grids


def extract_rows(stream, grids) -> tuple[dict[str, list[dict]], dict]:
    """Lecture ligne à ligne d'un flux texte/binaire CSV ; mémoire bornée."""
    wanted = {(str(g.lambx_hm), str(g.lamby_hm)): g.id for g in grids}
    text = io.TextIOWrapper(stream, encoding="utf-8", newline="") if not isinstance(stream, io.TextIOBase) else stream
    rows = {g.id: [] for g in grids}
    stats = {"lines_read": 0, "lines_matched": 0, "malformed_local_lines": 0}
    try:
        header = text.readline().strip().split(";")
        if not REQUIRED.issubset(header):
            raise SimFormatError(f"Schéma SIM inattendu : {sorted(REQUIRED - set(header))} absent(s) de l'entête")
        ix, iy = header.index("LAMBX"), header.index("LAMBY")
        for line in text:
            stats["lines_read"] += 1
            fields = line.rstrip("\r\n").split(";")
            if len(fields) <= max(ix, iy):
                continue
            grid_id = wanted.get((fields[ix].strip(), fields[iy].strip()))
            if grid_id is None:
                continue
            if len(fields) != len(header):
                stats["malformed_local_lines"] += 1
                continue
            rows[grid_id].append(dict(zip(header, fields)))
            stats["lines_matched"] += 1
    except (EOFError, gzip.BadGzipFile, zlib.error, UnicodeDecodeError) as exc:
        raise SimFormatError(f"Fichier SIM incomplet ou corrompu ({type(exc).__name__} : {exc})") from exc
    stats["header"] = header
    return rows, stats


def tabular_rows(http, rid: str, grids) -> tuple[dict[str, list[dict]], dict]:
    """Rend les lignes au format du CSV officiel (DATE AAAAMMJJ, valeurs texte)."""
    rows, stats = {}, {"lines_matched": 0, "requests": 0}
    for grid in grids:
        url = TABULAR.format(rid=rid)
        params = {"LAMBX__exact": grid.lambx_hm, "LAMBY__exact": grid.lamby_hm, "DATE__sort": "asc", "page_size": 200}
        found, total = [], None
        while url:
            response = http.get(url, params=params, timeout=(15, 60))
            stats["requests"] += 1
            if response.status_code != 200:
                raise RuntimeError(f"API tabulaire : HTTP {response.status_code}")
            payload = response.json()
            total = payload.get("meta", {}).get("total")
            found.extend(payload.get("data", []))
            url, params = payload.get("links", {}).get("next"), None
        if total is not None and total != len(found):
            raise RuntimeError(f"API tabulaire : {len(found)} lignes reçues pour {total} annoncées")
        converted = []
        for item in found:
            row = {k: "" if v is None else str(v) for k, v in item.items() if k != "__id"}
            if not REQUIRED.issubset(row):
                raise SimFormatError("API tabulaire : colonnes LAMBX/LAMBY/DATE absentes")
            row["DATE"] = row["DATE"].replace("-", "")
            converted.append(row)
        rows[grid.id] = converted
        stats["lines_matched"] += len(converted)
    stats["header"] = list(next((r for v in rows.values() for r in v), {}).keys())
    return rows, stats


# Fenêtres temporelles du dictionnaire SIM (31/03/2026).
WINDOWS = {"T": ("00–00 UTC", "00"), "HU": ("00–00 UTC", "00"), "TINF_H": ("18–18 UTC", "18"),
           "WG_RACINE": ("instant 06 UTC", "06"), "WGI_RACINE": ("instant 06 UTC", "06"),
           "SSWI_10J": ("10 jours glissants (J−9 → J)", "06"), "ETP": ("quotidien (bornes non précisées)", "06")}


def normalize_rows(rows, query: Query, grid: SimGrid, fetched: str, resource: dict):
    result = []
    for row in rows:
        day = datetime.strptime(row["DATE"], "%Y%m%d").date()
        if not query.start <= day <= query.end:
            continue
        current = {}
        for field, variable in SIM_FIELDS.items():
            # Noms actuels sans suffixe ; anciens fichiers avec suffixe _Q.
            original = field if field in row else f"{field}_Q"
            if original not in row:
                continue
            value, quality = parse_value(row[original], variable)
            window, hour = WINDOWS.get(field, ("06–06 UTC", "06"))
            current[variable] = Observation(
                timestamp=f"{day}T{hour}:00:00+00:00", date=str(day), latitude=grid.lat, longitude=grid.lon,
                source="meteo_france_sim", product="SIM2 quotidien", provider="Météo-France",
                variable=variable, original_variable=original, value=value, unit=VARIABLES[variable].unit,
                nature=Nature.MODEL, retrieved_at=fetched, grid_latitude=grid.lat, grid_longitude=grid.lon,
                grid_id=grid.id, spatial_resolution_m=CELL_M, temporal_window=window, quality=quality,
                published_at=resource.get("last_modified"), product_version="SIM2",
                metadata={"raw_value": row[original], "resource_url": resource.get("url")})
        result.extend(current.values())
        # Total calculé dans une même source, jamais entre fournisseurs.
        a, b = current.get("rainfall"), current.get("snowfall")
        if a and b:
            data = a.to_dict()
            both = a.value is not None and b.value is not None
            data.update(variable="precipitation", original_variable="PRELIQ+PRENEI",
                        value=a.value + b.value if both else None, quality="valid" if both else "missing",
                        nature=Nature.DERIVED, metadata={**a.metadata, "calculation": "PRELIQ + PRENEI"})
            result.append(Observation(**data))
    return result


def _http_date(value: str | None):
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


class MeteoFranceSimProvider:
    name = "Météo-France SIM"

    def __init__(self, cache, grids, offline=False, refresh=False, transport="auto", http=None):
        if transport not in {"auto", "tabular", "csv"}:
            raise ValueError(f"Transport SIM inconnu : {transport}")
        self.cache, self.grids, self.offline, self.refresh = cache, list(grids), offline, refresh
        self.transport, self.http = transport, http or session()

    def catalog(self) -> dict:
        text, stale = cached_document(self.http, self.cache, CATALOG, self.offline, self.refresh, ttl=3600)
        if stale and not self.offline:
            log.warning("SIM : catalogue data.gouv injoignable, copie en cache utilisée")
        return json.loads(text)

    @staticmethod
    def resource(catalog: dict, title: str | re.Pattern):
        for r in catalog.get("resources", []):
            if (title.match(r["title"]) if isinstance(title, re.Pattern) else r["title"] == title):
                return r
        return None

    def _grid_key(self, *parts):
        return self.cache.key(*parts, [g.id for g in self.grids])

    def _tabular_is_current(self, resource) -> tuple[bool, str]:
        extras = resource.get("extras", {})
        ingested = extras.get("analysis:last-modified-at")
        if not extras.get("analysis:parsing:finished_at") or not ingested:
            return False, "aucune ingestion tabulaire terminée"
        head = self.http.head(resource["url"], timeout=(15, 30), allow_redirects=True)
        published = _http_date(head.headers.get("Last-Modified"))
        if head.status_code != 200 or published is None:
            return False, f"version du fichier officiel non vérifiable (HEAD HTTP {head.status_code})"
        if datetime.fromisoformat(ingested) != published:
            return False, f"copie tabulaire du {ingested} ≠ fichier publié le {published.isoformat()}"
        return True, f"copie tabulaire = version publiée le {published.isoformat()}"

    def _via_tabular(self, resource):
        ingested = resource["extras"]["analysis:last-modified-at"]
        key = self._grid_key("sim-tabular-v1", resource["id"], ingested)
        payload = None if self.refresh else self.cache.get(key)
        if payload:
            log.info("SIM : cache HIT (tabulaire, version %s)", ingested)
            return payload, True
        log.info("SIM : cache MISS → API tabulaire data.gouv, ressource %s", resource["id"])
        rows, stats = tabular_rows(self.http, resource["id"], self.grids)
        payload = {"rows": rows, "stats": stats, "retrieved_at": utc_now(), "transport": "tabular",
                   "source_version": ingested, "url": TABULAR.format(rid=resource["id"])}
        self.cache.put(key, payload)
        return payload, False

    def _via_csv(self, resource):
        key = self._grid_key("sim-csv-v1", resource["url"])
        cached = self.cache.get(key)
        headers = {}
        if cached and not self.refresh:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
        log.info("SIM : téléchargement en flux %s", resource["url"])
        with self.http.get(resource["url"], stream=True, headers=headers, timeout=(15, 180)) as response:
            if response.status_code == 304 and cached:
                log.info("SIM : cache HIT (HTTP 304, ETag %s inchangé)", cached.get("etag"))
                return cached, True
            if response.status_code != 200:
                raise RuntimeError(f"CSV SIM : HTTP {response.status_code} pour {resource['url']}")
            digest, size = hashlib.sha256(), 0

            class Hashing(io.RawIOBase):
                def readable(self):
                    return True

                def readinto(self, buffer):
                    nonlocal size
                    chunk = response.raw.read(len(buffer))
                    digest.update(chunk)
                    size += len(chunk)
                    buffer[:len(chunk)] = chunk
                    return len(chunk)

            with gzip.GzipFile(fileobj=io.BufferedReader(Hashing(), 1 << 20)) as stream:
                rows, stats = extract_rows(stream, self.grids)
            expected = response.headers.get("Content-Length")
            if expected and int(expected) != size:
                raise SimFormatError(f"CSV SIM tronqué : {size} octets reçus pour {expected} annoncés")
            payload = {"rows": rows, "stats": stats, "retrieved_at": utc_now(), "transport": "csv.gz",
                       "url": resource["url"], "etag": response.headers.get("ETag"),
                       "last_modified": response.headers.get("Last-Modified"), "size_bytes": size,
                       "sha256": digest.hexdigest(), "source_version": resource.get("last_modified")}
        log.info("SIM : %d lignes lues, %d retenues (%s), %.1f Mo, sha256 %s…", stats["lines_read"],
                 stats["lines_matched"], ", ".join(self.grids_ids), size / 1e6, payload["sha256"][:12])
        if stats["malformed_local_lines"]:
            log.warning("SIM : %d ligne(s) locales malformées ignorées", stats["malformed_local_lines"])
        self.cache.put(key, payload)
        return payload, False

    @property
    def grids_ids(self):
        return [g.id for g in self.grids]

    def _offline_payload(self, resource):
        for key in (self._grid_key("sim-csv-v1", resource["url"]),
                    self._grid_key("sim-tabular-v1", resource["id"], resource.get("extras", {}).get("analysis:last-modified-at"))):
            payload = self.cache.get(key)
            if payload:
                return payload
        return None

    def rows(self, resource, output: ProviderResult):
        """Lignes locales d'une ressource ; jamais d'erreur masquée."""
        if self.offline:
            payload = self._offline_payload(resource)
            output.messages.append("Mode hors ligne : extraction en cache du " + (payload or {}).get("retrieved_at", "—"))
            return payload, True, ["hors ligne"]
        errors = []
        order = {"auto": ["tabular", "csv"], "tabular": ["tabular"], "csv": ["csv"]}[self.transport]
        for transport in order:
            try:
                if transport == "tabular":
                    current, reason = self._tabular_is_current(resource)
                    log.info("SIM : API tabulaire %s (%s)", "retenue" if current else "écartée", reason)
                    if not current:
                        errors.append(f"tabulaire écartée : {reason}")
                        continue
                    return (*self._via_tabular(resource), errors)
                return (*self._via_csv(resource), errors)
            except Exception as exc:
                message = f"{transport} : {type(exc).__name__} : {exc}"
                log.error("SIM : échec %s", message)
                errors.append(message)
        output.messages.extend(errors)
        # Dernier recours : extraction précédente, signalée comme telle.
        payload = self._offline_payload(resource)
        output.status = "stale" if payload else "temporary_error"
        if payload:
            log.warning("SIM : repli sur l'extraction en cache du %s", payload["retrieved_at"])
            output.messages.append(f"Repli sur l'extraction en cache du {payload['retrieved_at']}")
        return payload, True, errors

    def fetch(self, query: Query) -> ProviderResult:
        output = ProviderResult(self.name)
        catalog = self.catalog()
        latest = self.resource(catalog, LATEST)
        if latest is None:
            raise RuntimeError(f"Ressource QUOT_SIM2_latest absente du jeu {DATASET_ID}")
        log.info("SIM : jeu %s, ressource %s (modifiée %s)", DATASET_ID, latest["title"], latest.get("last_modified"))
        selected = [latest]
        # Au-delà du glissant (~60 jours) : archives annuelles officielles.
        if query.start < utc_today() - timedelta(days=58):
            annual = [self.resource(catalog, f"QUOT_SIM2_{y}") for y in range(query.start.year, query.end.year + 1)]
            selected.extend(r for r in annual if r)
        seen, details = {}, []
        for resource in selected:
            payload, from_cache, fallback = self.rows(resource, output)
            if not payload:
                output.messages.append(f"{resource['title']} : aucune donnée locale disponible")
                continue
            details.append({"resource": resource["title"], "resource_id": resource["id"], "transport": payload["transport"],
                            "url": payload["url"], "source_version": payload.get("source_version"),
                            "retrieved_at": payload["retrieved_at"], "cache_hit": from_cache, "fallback": fallback,
                            **{k: payload.get(k) for k in ("etag", "size_bytes", "sha256") if payload.get(k)},
                            "lines_matched": payload["stats"].get("lines_matched"),
                            "lines_read": payload["stats"].get("lines_read")})
            for grid in self.grids:
                for obs in normalize_rows(payload["rows"].get(grid.id, []), query, grid, payload["retrieved_at"], resource):
                    identity = (obs.grid_id, obs.date, obs.variable)
                    previous = seen.get(identity)
                    # La publication la plus récente prime sur un chevauchement.
                    if previous is None or (obs.published_at or "") > (previous.published_at or ""):
                        seen[identity] = obs
            if resource is latest and seen:
                first = min(o.date for o in seen.values())
                if str(query.start) >= first:
                    break
        output.observations = sorted(seen.values(), key=lambda o: (o.grid_id, o.date, o.variable))
        output.details = {"dataset_id": DATASET_ID, "catalog": CATALOG, "extractions": details,
                          "grids": [g.to_dict() for g in self.grids]}
        for grid in self.grids:
            count = len({o.date for o in output.observations if o.grid_id == grid.id})
            log.info("SIM : maille %s (%s) → %d jours", grid.id, grid.role, count)
            if count == 0:
                output.messages.append(f"Maille {grid.id} absente des données")
                if output.status == "available":
                    output.status = "partial"
        if not output.observations and output.status == "available":
            output.status = "unavailable"
        return output
