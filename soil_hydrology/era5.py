"""ERA5-Land CDS : archives mensuelles limitées à une petite emprise.

Les états sont moyennés seulement avec 24 heures valides. Les flux à
00 UTC représentent le cumul du jour précédent : jamais somme des cumuls.
"""
from __future__ import annotations

import calendar
import json
import tempfile
import zipfile
from datetime import date, timedelta
from pathlib import Path

from .models import Nature, Observation, ProviderResult, days, utc_now, utc_today
from .variables import VARIABLES, convert, parse_value

DATASET = "reanalysis-era5-land"
STATES = {f"swvl{i}": (f"volumetric_soil_water_layer_{i}", "soil_water", depth)
          for i, depth in enumerate([(0, 7), (7, 28), (28, 100), (100, 289)], 1)}
STATES["t2m"] = ("2m_temperature", "temperature", (None, None))
FLUXES = {"tp": ("total_precipitation", "precipitation"),
          "e": ("total_evaporation", "actual_et"),
          "sro": ("surface_runoff", "runoff"), "ssro": ("sub_surface_runoff", "subsurface_runoff")}


def parse_dataset(ds, query, fetched):
    import pandas as pd

    if not {"latitude", "longitude"}.issubset(ds.coords):
        raise ValueError("ERA5 : coordonnées latitude/longitude absentes")
    if not set(ds.data_vars).intersection({**STATES, **FLUXES}):
        raise ValueError("ERA5 : aucune variable prise en charge dans le fichier")
    lon = query.longitude % 360 if float(ds.longitude.max()) > 180 else query.longitude
    point = ds.sel(latitude=query.latitude, longitude=lon, method="nearest")
    glat, glon = float(point.latitude), float(point.longitude)
    glon = (glon + 180) % 360 - 180
    if abs(glat - query.latitude) > .071 or abs((glon - query.longitude + 180) % 360 - 180) > .071:
        raise ValueError("ERA5 : site hors emprise du fichier")
    time_name = "valid_time" if "valid_time" in point.coords else "time"
    output = []
    for short, spec in {**STATES, **FLUXES}.items():
        if short not in point:
            continue
        data = point[short]
        extra = set(data.dims) - {time_name}
        if extra:
            raise ValueError(f"ERA5 : dimensions non prises en charge {extra} (expver/step à résoudre explicitement)")
        unit = data.attrs.get("units")
        variable = spec[1]
        accepted = {"m**3 m**-3", "m3 m-3", "m^3 m^-3", "m³/m³"} if variable == "soil_water" else ({"K"} if short == "t2m" else {"m"})
        if unit not in accepted:
            raise ValueError(f"ERA5 : unité inattendue pour {short} : {unit}")
        series = data.to_series()
        series.index = pd.to_datetime(series.index, utc=True)
        if series.index.has_duplicates:
            raise ValueError("ERA5 : horodatages dupliqués")
        for day in days(query.start, query.end):
            raw_values = []
            quality = "missing"
            value = None
            if short in STATES:
                start = pd.Timestamp(day, tz="UTC")
                index = pd.date_range(start, periods=24, freq="h")
                hourly = series.reindex(index)
                checked = [parse_value(v if short != "t2m" else float(v) - 273.15, variable) for v in hourly]
                valid = [v for v, _ in checked]
                if any(q == "invalid" for _, q in checked):
                    quality = "invalid"
                raw_values = [None if pd.isna(v) else float(v) for v in hourly]
                if all(v is not None for v in valid):
                    value, quality = sum(valid) / 24, "valid"
                depth = spec[2]
            else:
                stamp = pd.Timestamp(day + timedelta(days=1), tz="UTC")
                raw = series.get(stamp)
                depth = (None, None)
                if raw is not None and pd.notna(raw):
                    raw_values = [float(raw)]
                    converted = convert(float(raw), "m", "mm")
                    if short == "e":
                        converted = -converted  # ECMWF négatif vers l'atmosphère.
                    value, quality = parse_value(converted, variable)
            # Ne pas synthétiser des lignes en dehors de la couverture du fichier.
            if not raw_values or all(v is None for v in raw_values):
                continue
            output.append(Observation(
                timestamp=f"{day}T00:00:00+00:00", date=str(day), latitude=query.latitude,
                longitude=query.longitude, source="era5_land", provider="ECMWF / CDS",
                product="ERA5-Land", product_version="CDS (expver non fourni)" if "expver" not in ds else str(ds.expver.values),
                variable=variable, original_variable=short, value=value, unit=VARIABLES[variable].unit,
                nature=Nature.REANALYSIS, retrieved_at=fetched, grid_latitude=glat, grid_longitude=glon,
                grid_id=f"ERA5:{glat:.5f}:{glon:.5f}", depth_min_cm=depth[0], depth_max_cm=depth[1],
                spatial_resolution_m=9000, temporal_window="00–24 UTC", quality=quality,
                metadata={"source_unit": unit, "raw_values": raw_values,
                          "resource_url": "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land",
                          "variable_attrs": json.loads(json.dumps(dict(data.attrs), default=lambda v: v.tolist() if hasattr(v, "tolist") else str(v))),
                          "dataset_attrs": json.loads(json.dumps(dict(ds.attrs), default=lambda v: v.tolist() if hasattr(v, "tolist") else str(v))), "distributed_grid": "0.1° × 0.1°",
                          "calculation": "mean of 24 valid hourly states" if short in STATES else "00 UTC next day; m × 1000; evaporation sign reversed",
                          "provisional": day > utc_today() - timedelta(days=90)}))
    return output


def read_download(path, query, fetched):
    import xarray as xr

    output = []
    # Le CDS peut renvoyer un ZIP de NetCDF séparés par stepType.
    with tempfile.TemporaryDirectory(prefix="rain-era5-read-") as temp:
        paths = [path]
        if zipfile.is_zipfile(path):
            paths = []
            with zipfile.ZipFile(path) as archive:
                for i, name in enumerate(archive.namelist()):
                    if name.endswith(".nc"):
                        target = Path(temp) / f"part-{i}.nc"
                        target.write_bytes(archive.read(name))
                        paths.append(target)
        if not paths:
            raise ValueError("ERA5 : aucun NetCDF dans le téléchargement")
        for part in paths:
            with xr.open_dataset(part) as ds:
                output.extend(parse_dataset(ds, query, fetched))
    return output


class Era5LandProvider:
    name = "ERA5-Land"

    def __init__(self, cache, enabled=False, offline=False, refresh=False, client=None):
        self.cache, self.enabled, self.offline, self.refresh = cache, enabled, offline, refresh
        self.client = client

    def fetch(self, query):
        if not self.enabled:
            return ProviderResult(self.name, "not_configured", messages=[
                "Activer --era5 après configuration CDSAPI_URL / CDSAPI_KEY (ou ~/.cdsapirc) et acceptation de la licence CDS.",
                "Réanalyse ~9 km, grille distribuée 0,1°, quatre couches 0–7 / 7–28 / 28–100 / 100–289 cm."])
        result = ProviderResult(self.name)
        if self.offline:
            result.messages.append("Mode hors ligne : dates de récupération originales conservées.")
        month = query.start.replace(day=1)
        while month <= query.end:
            end = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
            available_end = min(end, utc_today() - timedelta(days=6))
            if available_end < month:
                result.messages.append(f"{month:%Y-%m} : période trop récente pour ERA5-Land")
                result.status = "partial" if result.observations else "unavailable"
                break
            key = self.cache.key("era5-month-v1", query.latitude, query.longitude, str(month))
            ttl = None if end < utc_today() - timedelta(days=120) else 86400
            payload = self.cache.get(key, None if self.offline else (0 if self.refresh else ttl))
            if payload is None and not self.offline:
                try:
                    if self.client is None:
                        import cdsapi
                        self.client = cdsapi.Client(timeout=120, retry_max=2, sleep_max=10, quiet=True)
                    all_observations = []
                    # Chaque lot est borné au mois, plus 00 UTC le premier jour
                    # suivant pour le dernier cumul journalier.
                    for start, finish in [(month, available_end), (available_end + timedelta(days=1), available_end + timedelta(days=1))]:
                        req = {"variable": [v[0] for v in STATES.values()] + [v[0] for v in FLUXES.values()],
                               "year": str(start.year), "month": f"{start.month:02d}",
                               "day": [f"{d.day:02d}" for d in days(start, finish)],
                               "time": [f"{h:02d}:00" for h in range(24)] if start == month else ["00:00"],
                               "area": [min(90, query.latitude + .15), max(-180, query.longitude - .15),
                                        max(-90, query.latitude - .15), min(180, query.longitude + .15)],
                               "data_format": "netcdf", "download_format": "unarchived"}
                        from .models import Query
                        with tempfile.TemporaryDirectory(prefix="rain-era5-") as temp:
                            path = Path(temp) / "era5.nc"
                            self.client.retrieve(DATASET, req, str(path))
                            all_observations.extend(read_download(path, Query(query.latitude, query.longitude, month, end), utc_now()))
                    # Le second lot ne remplace pas les états complets du premier.
                    merged = {}
                    for obs in all_observations:
                        identity = (obs.date, obs.series_key)
                        if identity not in merged or obs.quality == "valid":
                            merged[identity] = obs
                    payload = {"observations": [o.to_dict() for o in merged.values()],
                               "source": self.name, "period": [str(month), str(end)], "version": DATASET}
                    self.cache.put(key, payload)
                except Exception as exc:
                    payload = self.cache.get(key)
                    result.status = "stale" if payload else "temporary_error"
                    # Ne pas exposer une URL de requête qui pourrait contenir des identifiants.
                    result.messages.append(f"{month:%Y-%m} : échec CDS ({type(exc).__name__}), vérifier compte, licence et disponibilité.")
            if payload:
                result.observations.extend(Observation(**o) for o in payload["observations"]
                                           if str(query.start) <= o["date"] <= str(query.end))
            else:
                result.messages.append(f"{month:%Y-%m} : aucune archive en cache")
                if result.status == "available":
                    result.status = "unavailable"
            month = end + timedelta(days=1)
        return result
