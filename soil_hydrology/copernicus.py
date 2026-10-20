"""Copernicus CLMS 1 km Europe : SWI v2 (ASCAT + Sentinel-1) et SSM v1 (Sentinel-1).

Découverte : catalogue OData public du Copernicus Data Space Ecosystem (le
STAC CDSE accusait ~5 semaines de retard en octobre 2026). Lecture : COG par
bande, fenêtre de quelques pixels seulement (requêtes HTTP Range de GDAL),
jamais le raster européen complet. Authentification obligatoire :

- CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY : S3 EODATA (recommandé), via une
  session boto3 enveloppée dans rasterio.session.AWSSession — jamais d'options
  GDAL AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (refusées par rasterio >= 1.4) ;
- CDSE_USERNAME / CDSE_PASSWORD : jeton OIDC + téléchargement OData.

Un preflight unique (HEAD S3 ou requête Range OData) sur le produit le plus
récent distingue identifiants refusés (UNAVAILABLE_AUTH), panne globale
(ERROR, un seul message) et erreurs propres à un produit.

Encodage (PUM SWI1km I1.50 du 08/07/2025, PUM SSM1km I1.30 du 19/04/2019) :
octet non signé, valeur physique = DN × 0,5 (%) pour DN ∈ [0, 200] ;
241/242/251–254 = drapeaux, 255 = pas de donnée. Aucun drapeau n'est
converti en valeur.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

from . import geo
from .models import Nature, Observation, ProviderResult, Query, utc_now

ODATA = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
DOWNLOAD = "https://download.dataspace.copernicus.eu/odata/v1/Products({id})/Nodes({product})/Nodes({file})/$value"
# S3 EODATA : boto3 attend une URL, GDAL (AWS_S3_ENDPOINT) un nom d'hôte sans schéma.
S3_ENDPOINT_URL = "https://eodata.dataspace.copernicus.eu"
S3_ENDPOINT_HOST = "eodata.dataspace.copernicus.eu"
S3_BUCKET, S3_REGION = "eodata", "default"
AUTH_ERROR_CODES = {"401", "403", "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken",
                    "Forbidden", "Unauthorized", "ExpiredToken"}
# Au-delà de N produits consécutifs en échec sans aucune réussite, l'erreur est
# considérée comme globale : arrêt du fournisseur, un seul message de synthèse.
MAX_CONSECUTIVE_FAILURES = 3

# Grille commune SWI1km/SSM1km : EPSG:4326, origine (-11°, 72°), pas 1/112°.
ORIGIN_LON, ORIGIN_LAT, PIXEL_DEG = -11.0, 72.0, 1 / 112
FLAGS = {241: "ExceedingMin", 242: "ExceedingMax", 251: "WaterMask", 252: "SensitivityMask",
         253: "SlopeMask", 254: "LowQFLAG", 255: "NoData"}
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Product:
    key: str  # "copernicus_swi" | "copernicus_ssm"
    label: str
    prefix: str  # début du nom de produit CDSE
    collection: str  # identifiant STAC équivalent, pour la traçabilité
    variable: str
    bands: tuple[str, ...]
    qflags: tuple[str, ...]
    extra: tuple[str, ...]
    nominal_hour: int  # heure UTC représentée par le produit (12 pour SWI, 0 pour SSM)


SWI_T = ("002", "005", "010", "015", "020", "040", "060", "100")
SWI = Product("copernicus_swi", "CLMS Soil Water Index Europe 1 km daily v2", "c_gls_SWI1km_",
              "clms_swi_europe_1km_daily_v2_cog", "swi_copernicus",
              tuple(f"SWI{t}" for t in SWI_T), tuple(f"QFLAG{t}" for t in SWI_T), ("SSF",), 12)
SSM = Product("copernicus_ssm", "CLMS Surface Soil Moisture Europe 1 km daily v1", "c_gls_SSM1km_",
              "clms_ssm_europe_1km_daily_v1_cog", "ssm_copernicus", ("SSM",), (), ("NOISE",), 0)
NAME = re.compile(r"^(?P<prefix>c_gls_\w+?1km)_(?P<stamp>\d{12})_(?P<region>\w+?)_(?P<sensor>\w+?)_(?P<version>V\d+\.\d+\.\d+)_cog$")


def decode(dn) -> tuple[float | None, str]:
    """DN → (valeur physique %, qualité). Un drapeau ne devient jamais une valeur."""
    if dn is None:
        return None, "missing"
    dn = int(dn)
    if 0 <= dn <= 200:
        return dn * 0.5, "valid"
    return None, FLAGS.get(dn, f"invalid_dn_{dn}")


@dataclass(frozen=True)
class Pixel:
    row: int
    col: int
    lat: float  # centre
    lon: float
    overlap_fraction: float  # part de la parcelle dans ce pixel
    distance_to_centroid_m: float

    @property
    def id(self):
        return f"r{self.row}_c{self.col}"

    @property
    def bounds(self):
        west = ORIGIN_LON + self.col * PIXEL_DEG
        north = ORIGIN_LAT - self.row * PIXEL_DEG
        return west, north - PIXEL_DEG, west + PIXEL_DEG, north

    def to_dict(self):
        return {"id": self.id, **asdict(self), "bounds_wgs84": [round(v, 7) for v in self.bounds]}


def pixels_for_parcel(polygons_wgs84, centroid_lonlat) -> list[Pixel]:
    """Pixels de la grille 1/112° intersectant la parcelle (calcul en degrés :
    l'erreur de forme est négligeable à l'échelle d'un pixel)."""
    west, south, east, north = geo.bounds(polygons_wgs84)
    total = geo.area(polygons_wgs84)
    geod = geo.transformer(geo.WGS84, geo.LAMBERT93)
    cx, cy = geod.transform(*centroid_lonlat)
    result = []
    for row in range(math.floor((ORIGIN_LAT - north) / PIXEL_DEG), math.floor((ORIGIN_LAT - south) / PIXEL_DEG) + 1):
        for col in range(math.floor((west - ORIGIN_LON) / PIXEL_DEG), math.floor((east - ORIGIN_LON) / PIXEL_DEG) + 1):
            px = Pixel(row, col, 0, 0, 0, 0)
            overlap = geo.intersection_area(polygons_wgs84, px.bounds)
            if overlap <= 0:
                continue
            lon = ORIGIN_LON + (col + .5) * PIXEL_DEG
            lat = ORIGIN_LAT - (row + .5) * PIXEL_DEG
            x, y = geod.transform(lon, lat)
            result.append(Pixel(row, col, round(lat, 7), round(lon, 7), round(overlap / total, 4),
                                round(math.hypot(x - cx, y - cy), 1)))
    return sorted(result, key=lambda p: -p.overlap_fraction)


def read_pixels(dataset, pixels) -> dict[str, int | None]:
    """DN bruts d'un raster ouvert (rasterio), lecture fenêtrée minimale."""
    from rasterio.windows import Window

    a, _, c, _, e, f = tuple(dataset.transform)[:6]
    if not (math.isclose(a, PIXEL_DEG, rel_tol=1e-6) and math.isclose(-e, PIXEL_DEG, rel_tol=1e-6)
            and math.isclose(c, ORIGIN_LON, abs_tol=1e-6) and math.isclose(f, ORIGIN_LAT, abs_tol=1e-6)):
        raise ValueError(f"Grille Copernicus inattendue : {dataset.transform}")
    r0, r1 = min(p.row for p in pixels), max(p.row for p in pixels)
    c0, c1 = min(p.col for p in pixels), max(p.col for p in pixels)
    block = dataset.read(1, window=Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1), masked=False)
    return {p.id: int(block[p.row - r0, p.col - c0]) for p in pixels}


def parse_product(item: dict) -> dict | None:
    match = NAME.match(item["Name"])
    if not match:
        return None
    stamp = datetime.strptime(match["stamp"], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    return {"id": item["Id"], "name": item["Name"], "s3_path": item.get("S3Path"), "nominal": stamp,
            "date": stamp.date(), "version": match["version"], "published": item.get("PublicationDate"),
            "online": item.get("Online", True), "stem": f"{match['prefix']}-{{band}}_{match['stamp']}_{match['region']}_{match['sensor']}_{match['version']}.tiff"}


def _version_key(version: str):
    return tuple(int(v) for v in version[1:].split("."))


def latest_per_day(items: list[dict]) -> dict[date, dict]:
    """Une seule version par date nominale : la plus récente (run supérieur)."""
    chosen = {}
    for item in filter(None, map(parse_product, items)):
        if item["online"] and (item["date"] not in chosen or _version_key(item["version"]) > _version_key(chosen[item["date"]]["version"])):
            chosen[item["date"]] = item
    return chosen


class AuthRefused(PermissionError):
    """Identifiants présents mais refusés (401/403…)."""


class GlobalAccessError(RuntimeError):
    """Panne commune à tous les produits (session, endpoint, dépendance) : arrêt immédiat."""


def _error_code(exc) -> tuple[str | None, int | None]:
    """Code d'une botocore ClientError (sans importer botocore)."""
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code"), response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def s3_key(item: dict, band: str) -> str:
    bucket, _, prefix = (item.get("s3_path") or "").lstrip("/").partition("/")
    if bucket != S3_BUCKET or not prefix:
        raise GlobalAccessError(f"Chemin S3 inattendu dans le catalogue : {item.get('s3_path')!r}")
    return f"{prefix}/{item['stem'].format(band=band)}"


class Credentials:
    def __init__(self, env=os.environ):
        self.s3 = (env.get("CDSE_S3_ACCESS_KEY"), env.get("CDSE_S3_SECRET_KEY"))
        self.login = (env.get("CDSE_USERNAME"), env.get("CDSE_PASSWORD"))

    @property
    def method(self) -> str | None:
        return "s3" if all(self.s3) else "odata" if all(self.login) else None


class S3Reader:
    """COG sur S3 EODATA : boto3 pour les identifiants, rasterio pour la lecture fenêtrée."""

    method = "s3"

    def __init__(self, access_key: str, secret_key: str, boto3_module=None):
        self._access, self._secret, self._boto3 = access_key, secret_key, boto3_module

    def boto3_session(self):
        if self._boto3 is None:
            try:
                import boto3
            except ImportError as exc:
                raise GlobalAccessError("boto3 absent : pip install boto3 (requis par rasterio pour S3)") from exc
            self._boto3 = boto3
        return self._boto3.Session(aws_access_key_id=self._access, aws_secret_access_key=self._secret,
                                   region_name=S3_REGION)

    def path(self, item: dict, band: str) -> str:
        return f"s3://{S3_BUCKET}/{s3_key(item, band)}"

    def preflight(self, item: dict, band: str) -> str:
        key = s3_key(item, band)
        client = self.boto3_session().client("s3", endpoint_url=S3_ENDPOINT_URL, region_name=S3_REGION)
        try:
            head = client.head_object(Bucket=S3_BUCKET, Key=key)
        except Exception as exc:
            code, status = _error_code(exc)
            if code in AUTH_ERROR_CODES or status in (401, 403):
                raise AuthRefused(f"Clés S3 CDSE refusées (HEAD s3://{S3_BUCKET}/{key} : {code or status})") from exc
            detail = " ".join(str(v) for v in (code, status) if v)
            raise GlobalAccessError(f"HEAD s3://{S3_BUCKET}/{key} : {type(exc).__name__} {detail}".strip()) from exc
        return f"HEAD s3://{S3_BUCKET}/{key} → {head.get('ContentLength', '?')} octets"

    def env(self):
        import rasterio
        from rasterio.session import AWSSession

        session = AWSSession(session=self.boto3_session(), endpoint_url=S3_ENDPOINT_HOST)
        return rasterio.Env(session=session, AWS_S3_ENDPOINT=S3_ENDPOINT_HOST, AWS_VIRTUAL_HOSTING="FALSE",
                            AWS_HTTPS="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR")


class ODataReader:
    """COG via le téléchargement OData authentifié (jeton OIDC), lecture par plages HTTP."""

    method = "odata"

    def __init__(self, username: str, password: str, http):
        self._login, self.http, self._token = (username, password), http, None

    def token(self) -> str:
        if self._token is None:
            user, password = self._login
            response = self.http.post(TOKEN_URL, data={"grant_type": "password", "username": user, "password": password,
                                                        "client_id": "cdse-public"}, timeout=(15, 60))
            if response.status_code in (400, 401, 403):
                raise AuthRefused(f"Authentification CDSE refusée (HTTP {response.status_code})")
            if response.status_code != 200:
                raise GlobalAccessError(f"Jeton CDSE : HTTP {response.status_code}")
            self._token = response.json()["access_token"]
        return self._token

    def url(self, item: dict, band: str) -> str:
        return DOWNLOAD.format(id=item["id"], product=item["name"], file=item["stem"].format(band=band))

    def path(self, item: dict, band: str) -> str:
        return "/vsicurl/" + self.url(item, band)

    def preflight(self, item: dict, band: str) -> str:
        response = self.http.get(self.url(item, band), headers={"Authorization": f"Bearer {self.token()}",
                                                                "Range": "bytes=0-1023"}, timeout=(15, 60), stream=True)
        if response.status_code in (401, 403):
            raise AuthRefused(f"Téléchargement OData refusé (HTTP {response.status_code})")
        if response.status_code not in (200, 206):
            raise GlobalAccessError(f"Téléchargement OData : HTTP {response.status_code}")
        return f"GET Range OData → HTTP {response.status_code}"

    def env(self):
        import rasterio

        return rasterio.Env(GDAL_HTTP_HEADERS=f"Authorization: Bearer {self.token()}",
                            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS="")


class CopernicusProvider:
    def __init__(self, product: Product, cache, pixels, offline=False, refresh=False, http=None, env=os.environ,
                 reader=None, opener=None):
        from .transport import session

        self.product, self.cache, self.pixels = product, cache, list(pixels)
        self.offline, self.refresh, self.http = offline, refresh, http or session()
        self.credentials = Credentials(env)
        if reader is None and self.credentials.method == "s3":
            reader = S3Reader(*self.credentials.s3)
        elif reader is None and self.credentials.method == "odata":
            reader = ODataReader(*self.credentials.login, self.http)
        self.reader, self.opener = reader, opener  # opener(path) : ouverture rasterio, injectable en test
        self.name = product.label

    def discover(self, start: date, end: date) -> dict[date, dict]:
        params = {"$filter": (f"startswith(Name,'{self.product.prefix}') and endswith(Name,'_cog') "
                              f"and ContentDate/Start ge {start - timedelta(days=1)}T00:00:00.000Z "
                              f"and ContentDate/Start le {end + timedelta(days=1)}T00:00:00.000Z"),
                  "$orderby": "ContentDate/Start desc", "$top": 200}
        response = self.http.get(ODATA, params=params, timeout=(15, 60))
        if response.status_code != 200:
            raise RuntimeError(f"OData CDSE : HTTP {response.status_code}")
        found = {d: p for d, p in latest_per_day(response.json().get("value", [])).items() if start <= d <= end}
        log.info("%s : %d produit(s) catalogués du %s au %s (dernier : %s)", self.product.key, len(found), start, end,
                 max(found) if found else "—")
        return found

    def _open(self, path: str):
        if self.opener:
            return self.opener(path)
        import rasterio

        return rasterio.open(path)

    def extract(self, item: dict) -> dict:
        """À appeler dans l'environnement rasterio du lecteur (self.reader.env())."""
        values = {}
        for band in self.product.bands + self.product.qflags + self.product.extra:
            with self._open(self.reader.path(item, band)) as dataset:
                values[band] = read_pixels(dataset, self.pixels)
        return {"product": item["name"], "version": item["version"], "published": item["published"],
                "nominal": item["nominal"].isoformat(), "retrieved_at": utc_now(), "dn": values}

    def _read_all(self, todo: list[dict], catalog: dict, result: ProviderResult, cached_days: dict) -> int:
        """Preflight unique, un seul environnement rasterio, arrêt dès qu'une panne est globale."""
        latest = catalog[max(catalog)]
        try:
            outcome = self.reader.preflight(latest, self.product.bands[0])
        except AuthRefused as exc:
            log.error("%s : S3 preflight : KO (%s)", self.product.key, exc)
            result.status, result.details["s3_preflight"] = "unavailable_auth", f"KO : {exc}"
            result.messages.append(str(exc))
            return 0
        except Exception as exc:
            log.error("%s : S3 preflight : KO (%s : %s)", self.product.key, type(exc).__name__, exc)
            result.status, result.details["s3_preflight"] = "temporary_error", f"KO : {type(exc).__name__} : {exc}"
            result.messages.append(f"Accès {self.reader.method} indisponible ({type(exc).__name__} : {exc}) ; aucun produit tenté.")
            return 0
        log.info("%s : S3 preflight : OK (%s)", self.product.key, outcome)
        result.details["s3_preflight"] = f"OK : {outcome}"
        try:
            environment = self.reader.env()
        except Exception as exc:
            log.error("%s : session rasterio impossible : %s", self.product.key, exc)
            result.status = "temporary_error"
            result.messages.append(f"Session rasterio/GDAL impossible ({type(exc).__name__} : {exc}) ; aucun produit tenté.")
            return 0
        reads, consecutive, failures = 0, 0, {}
        with environment:
            for item in todo:
                key = self.cache.key("copernicus-v1", self.product.key, str(item["date"]), [p.id for p in self.pixels])
                try:
                    payload = self.extract(item)
                except Exception as exc:
                    consecutive += 1
                    failures.setdefault(f"{type(exc).__name__} : {exc}", []).append(item["name"])
                    if reads == 0 and consecutive >= MAX_CONSECUTIVE_FAILURES:
                        log.error("%s : %d échecs consécutifs sans réussite, arrêt (%s)", self.product.key, consecutive, exc)
                        result.status = "temporary_error"
                        result.messages.append(f"Lecture COG en échec sur les {consecutive} premiers produits "
                                               f"({type(exc).__name__} : {exc}) ; {len(todo) - consecutive} produit(s) non tenté(s).")
                        failures = {}
                        break
                    continue
                consecutive, reads = 0, reads + 1
                self.cache.put(key, payload)
                cached_days[item["date"]] = payload
        for message, names in failures.items():
            log.warning("%s : %d produit(s) en échec : %s", self.product.key, len(names), message)
            result.messages.append(f"{len(names)} produit(s) en échec ({message}), ex. {names[0]}")
            if result.status == "available":
                result.status = "partial"
        return reads

    def fetch(self, query: Query) -> ProviderResult:
        result = ProviderResult(self.name)
        result.details = {"collection": self.product.collection, "discovery": ODATA,
                          "auth_method": self.reader.method if self.reader else None,
                          "pixels": [p.to_dict() for p in self.pixels], "s3_preflight": "non tenté"}
        catalog = {} if self.offline else self.discover(query.start, query.end)
        if not self.offline:
            result.details["catalog_latest"] = str(max(catalog)) if catalog else None
        if self.reader is None and not self.offline:
            result.status = "unavailable_auth"
            result.messages.append("Identifiants CDSE absents (CDSE_S3_ACCESS_KEY/CDSE_S3_SECRET_KEY ou "
                                   "CDSE_USERNAME/CDSE_PASSWORD) : lecture des COG impossible.")
            if catalog:
                result.messages.append(f"Produit le plus récent catalogué : {max(catalog)} (non lu).")
        hits, cached_days, todo = 0, {}, []
        for day in sorted(set(catalog) | {query.start + timedelta(days=i) for i in range((query.end - query.start).days + 1)}):
            item = catalog.get(day)
            key = self.cache.key("copernicus-v1", self.product.key, str(day), [p.id for p in self.pixels])
            payload = self.cache.get(key)
            if payload and item and payload["product"] != item["name"] and not self.offline:
                payload = None  # nouveau run publié : remplace l'ancien
            if payload and not self.refresh:
                hits += 1
                cached_days[day] = payload
            elif item:
                todo.append(item)
        reads = self._read_all(todo, catalog, result, cached_days) if todo and self.reader and not self.offline else 0
        for payload in cached_days.values():
            result.observations.extend(self.observations(payload))
        result.details.update(days_read=reads, days_from_cache=hits)
        log.info("%s : %d jour(s) lus, %d depuis le cache, %d pixel(s)", self.product.key, reads, hits, len(self.pixels))
        if not result.observations and result.status in ("available", "partial"):
            result.status = "temporary_error" if result.messages else "unavailable"
        return result

    def observations(self, payload: dict) -> list[Observation]:
        day = payload["nominal"][:10]
        output = []
        for band in self.product.bands:
            t = band[3:] if band.startswith("SWI") else None
            qflag = f"QFLAG{t}" if t else None
            for px in self.pixels:
                dn = payload["dn"][band].get(px.id)
                value, quality = decode(dn)
                meta = {"band": band, "raw_dn": dn, "encoding": "DN×0.5 (%) si DN∈[0,200]"}
                if qflag and qflag in payload["dn"]:
                    meta["qflag_pct"] = decode(payload["dn"][qflag].get(px.id))[0]
                for extra in self.product.extra:
                    raw = payload["dn"].get(extra, {}).get(px.id)
                    meta[extra.lower()] = decode(raw)[0] if extra == "NOISE" else raw
                output.append(Observation(
                    timestamp=payload["nominal"], date=day, latitude=px.lat, longitude=px.lon, source=self.product.key,
                    product=self.product.label, provider="Copernicus Land Monitoring Service (CDSE)",
                    variable=self.product.variable, original_variable=band, value=value, unit="%",
                    nature=Nature.SATELLITE_DERIVED, retrieved_at=payload["retrieved_at"], grid_latitude=px.lat,
                    grid_longitude=px.lon, grid_id=px.id, spatial_resolution_m=1000,
                    temporal_window=f"T={int(t)}" if t else "journalier", quality=quality,
                    published_at=payload["published"], product_version=payload["version"], metadata=meta))
        return output
