"""Copernicus CLMS 1 km Europe : SWI v2 (ASCAT + Sentinel-1) et SSM v1 (Sentinel-1).

Découverte : catalogue OData public du Copernicus Data Space Ecosystem (le
STAC CDSE accusait ~5 semaines de retard en octobre 2026). Lecture : COG par
bande, fenêtre de quelques pixels seulement (requêtes HTTP Range de GDAL),
jamais le raster européen complet. Authentification obligatoire :

- CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY : S3 EODATA (recommandé) ;
- CDSE_USERNAME / CDSE_PASSWORD : jeton OIDC + téléchargement OData.

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
S3_ENDPOINT = "eodata.dataspace.copernicus.eu"

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


class Credentials:
    def __init__(self, env=os.environ):
        self.s3 = (env.get("CDSE_S3_ACCESS_KEY"), env.get("CDSE_S3_SECRET_KEY"))
        self.login = (env.get("CDSE_USERNAME"), env.get("CDSE_PASSWORD"))

    @property
    def method(self) -> str | None:
        return "s3" if all(self.s3) else "odata" if all(self.login) else None


class CopernicusProvider:
    def __init__(self, product: Product, cache, pixels, offline=False, refresh=False, http=None, env=os.environ,
                 opener=None):
        from .transport import session

        self.product, self.cache, self.pixels = product, cache, list(pixels)
        self.offline, self.refresh, self.http = offline, refresh, http or session()
        self.credentials, self.opener, self._token = Credentials(env), opener, None
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

    def token(self) -> str:
        if self._token is None:
            user, password = self.credentials.login
            response = self.http.post(TOKEN_URL, data={"grant_type": "password", "username": user, "password": password,
                                                        "client_id": "cdse-public"}, timeout=(15, 60))
            if response.status_code in (400, 401, 403):
                raise PermissionError(f"Authentification CDSE refusée (HTTP {response.status_code})")
            response.raise_for_status()
            self._token = response.json()["access_token"]
        return self._token

    def open(self, item: dict, band: str):
        """Ouvre un COG de bande (rasterio) via S3 ou OData, sans téléchargement complet."""
        if self.opener:
            return self.opener(item, band)
        import rasterio

        filename = item["stem"].format(band=band)
        if self.credentials.method == "s3":
            access, secret = self.credentials.s3
            env = rasterio.Env(AWS_ACCESS_KEY_ID=access, AWS_SECRET_ACCESS_KEY=secret, AWS_S3_ENDPOINT=S3_ENDPOINT,
                               AWS_VIRTUAL_HOSTING="FALSE", AWS_HTTPS="YES", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR")
            path = f"/vsis3{item['s3_path']}/{filename}"
        else:
            env = rasterio.Env(GDAL_HTTP_HEADERS=f"Authorization: Bearer {self.token()}",
                               GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS="")
            path = "/vsicurl/" + DOWNLOAD.format(id=item["id"], product=item["name"], file=filename)
        return env, rasterio.open(path)

    def extract(self, item: dict) -> dict:
        values = {}
        for band in self.product.bands + self.product.qflags + self.product.extra:
            opened = self.open(item, band)
            env, dataset = opened if isinstance(opened, tuple) else (None, opened)
            try:
                if env:
                    with env:
                        values[band] = read_pixels(dataset, self.pixels)
                else:
                    values[band] = read_pixels(dataset, self.pixels)
            finally:
                dataset.close()
        return {"product": item["name"], "version": item["version"], "published": item["published"],
                "nominal": item["nominal"].isoformat(), "retrieved_at": utc_now(), "dn": values}

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

    def fetch(self, query: Query) -> ProviderResult:
        result = ProviderResult(self.name)
        result.details = {"collection": self.product.collection, "discovery": ODATA,
                          "auth_method": self.credentials.method, "pixels": [p.to_dict() for p in self.pixels]}
        if self.offline:
            catalog = {}
        else:
            catalog = self.discover(query.start, query.end)
            result.details["catalog_latest"] = str(max(catalog)) if catalog else None
        if self.credentials.method is None and not self.offline:
            result.status = "unavailable_auth"
            result.messages.append("Identifiants CDSE absents (CDSE_S3_ACCESS_KEY/CDSE_S3_SECRET_KEY ou "
                                   "CDSE_USERNAME/CDSE_PASSWORD) : lecture des COG impossible.")
            if catalog:
                result.messages.append(f"Produit le plus récent catalogué : {max(catalog)} (non lu).")
        reads = hits = 0
        cached_days = {}
        for day in sorted(set(catalog) | {query.start + timedelta(days=i) for i in range((query.end - query.start).days + 1)}):
            item = catalog.get(day)
            key = self.cache.key("copernicus-v1", self.product.key, str(day), [p.id for p in self.pixels])
            payload = self.cache.get(key)
            if payload and item and payload["product"] != item["name"] and not self.offline:
                payload = None  # nouveau run publié : remplace l'ancien
            if payload and not self.refresh:
                hits += 1
                cached_days[day] = payload
                continue
            if not item or result.status == "unavailable_auth":
                continue
            try:
                payload = self.extract(item)
                reads += 1
                self.cache.put(key, payload)
                cached_days[day] = payload
            except PermissionError as exc:
                result.status = "unavailable_auth"
                result.messages.append(str(exc))
                break
            except Exception as exc:
                log.error("%s : échec lecture %s : %s", self.product.key, item["name"], exc)
                result.messages.append(f"{item['name']} : {type(exc).__name__} : {exc}")
                if result.status == "available":
                    result.status = "partial"
        for payload in cached_days.values():
            result.observations.extend(self.observations(payload))
        log.info("%s : %d jour(s) lus, %d depuis le cache, %d pixel(s)", self.product.key, reads, hits, len(self.pixels))
        if not result.observations and result.status in ("available", "partial"):
            result.status = "temporary_error" if result.messages else "unavailable"
        return result
