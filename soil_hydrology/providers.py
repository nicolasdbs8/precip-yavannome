"""Adaptateur du CSV existant et fournisseurs explicitement désactivés."""
import csv
from pathlib import Path

from precip_common import TARGET_LAT, TARGET_LON
from .models import Nature, Observation, ProviderResult, utc_now
from .variables import parse_value


class RainPrecipitationProvider:
    name = "Précipitations Rain"

    def __init__(self, path: Path):
        self.path = path

    def fetch(self, query):
        if abs(query.latitude - TARGET_LAT) > 1e-6 or abs(query.longitude - TARGET_LON) > 1e-6:
            return ProviderResult(self.name, "unavailable", messages=["Le CSV Rain correspond uniquement au site défini dans precip_common.py."])
        output = ProviderResult(self.name, messages=["CSV existant lu sans modification. Couverture intrajournalière et coordonnées des pixels non conservées par Rain."])
        definitions = {
            "comephore": ("COMÉPHORE", Nature.REANALYSIS, 1000),
            "antilope": ("LAME_D_EAU (alias historique antilope)", Nature.DERIVED, 500),
            "pluviometre_site": ("Pluviomètre du site", Nature.LOCAL_SENSOR, None),
        }
        fetched, seen = utc_now(), {}
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"date", "source", "cumul_mm"}.issubset(reader.fieldnames or []):
                raise ValueError("CSV Rain : date,source,cumul_mm requis")
            for row in reader:
                if not str(query.start) <= row["date"] <= str(query.end):
                    continue
                if row["source"] not in definitions:
                    output.messages.append(f"Source CSV inconnue ignorée : {row['source']}")
                    output.status = "partial"
                    continue
                key = (row["date"], row["source"])
                if key in seen:
                    if seen[key].metadata["raw_value"] != row["cumul_mm"]:
                        seen[key].value, seen[key].quality = None, "conflicting_duplicates"
                    continue
                product, nature, resolution = definitions[row["source"]]
                value, quality = parse_value(row["cumul_mm"], "precipitation")
                if quality == "valid":
                    quality = "coverage_unknown"
                seen[key] = Observation(
                    timestamp=f"{row['date']}T00:00:00+00:00", date=row["date"],
                    latitude=query.latitude, longitude=query.longitude, source=f"rain_{row['source']}",
                    product=product, provider=self.name, variable="precipitation", original_variable="cumul_mm",
                    value=value, unit="mm", nature=nature, retrieved_at=fetched,
                    spatial_resolution_m=resolution, temporal_window="00–24 UTC", quality=quality,
                    metadata={"raw_value": row["cumul_mm"], "file": str(self.path),
                              "grid_coordinates": "not retained in legacy CSV", "retrieved_at_meaning": "lecture CSV, téléchargement original inconnu"})
        output.observations = list(seen.values())
        if not seen:
            output.status = "unavailable"
        return output


class CopernicusSwiProvider:
    name = "Copernicus SWI"

    def fetch(self, query):
        return ProviderResult(self.name, "not_configured", messages=[
            "Accès officiel CDSE STAC/OData/S3 identifié ; adaptateur de téléchargement et décodage non activé.",
            "Configurer un accès CDSE, choisir version et constante T, valider les masques qualité avant activation.",
            "Produit satellitaire dérivé européen, 1 km, quotidien ; valeurs distinctes du SWI SIM."])


class CzechGlobeProvider:
    name = "CzechGlobe / InterSucho"

    def fetch(self, query):
        return ProviderResult(self.name, "not_configured", messages=[
            "Aucun accès officiel automatisable vérifié pour ce site ; fournisseur désactivé. Aucun scraping Windy."])


class LocalSensorProvider:
    name = "Capteurs locaux"

    def fetch(self, query):
        return ProviderResult(self.name, "not_configured", messages=[
            "Aucune sonde configurée. Les futurs adaptateurs doivent produire la nature LOCAL_SENSOR."])


def collect(providers, query):
    """Une panne est une information du fournisseur, pas une panne de Rain."""
    results = []
    for provider in providers:
        try:
            results.append(provider.fetch(query))
        except Exception as exc:
            results.append(ProviderResult(provider.name, "temporary_error", messages=[f"{type(exc).__name__} : {exc}"]))
    return results
