"""Smoke tests réseau, optionnels :
    RAIN_NETWORK_TESTS=1 python -m unittest discover -s tests -p "test_network_smoke.py" -v

Vérifient que les sources officielles répondent toujours au format attendu
(API Carto, catalogue SIM data.gouv, copie tabulaire, OData CDSE). Jamais
exécutés par défaut : les tests unitaires ne dépendent pas d'Internet.
"""
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from soil_hydrology import geo
from soil_hydrology.cache import Cache
from soil_hydrology.copernicus import SWI, CopernicusProvider, pixels_for_parcel
from soil_hydrology.models import Query
from soil_hydrology.parcel import ZP1, fetch_parcel, load_parcel
from soil_hydrology.sim import MeteoFranceSimProvider, parcel_grids
from soil_hydrology.transport import session


@unittest.skipUnless(os.environ.get("RAIN_NETWORK_TESTS") == "1", "RAIN_NETWORK_TESTS=1 pour les tests réseau")
class NetworkSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache, self.http = Cache(Path(self.tmp.name) / "c.sqlite"), session()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cadastre_matches_fixture(self):
        self.assertEqual(fetch_parcel(ZP1, self.http)["geometry"], load_parcel(ZP1).feature["geometry"])

    def test_sim_recent_local_data(self):
        grids = parcel_grids(self.http, self.cache, load_parcel(ZP1).polygons(geo.LAMBERT2E))
        today = date.today()
        result = MeteoFranceSimProvider(self.cache, grids, http=self.http).fetch(Query(46.8, 5.45, today - timedelta(days=10), today))
        self.assertIn(result.status, ("available", "partial"))
        latest = max(o.date for o in result.observations if o.variable == "swi_sim" and o.value is not None)
        self.assertGreaterEqual(date.fromisoformat(latest), today - timedelta(days=3))

    def test_copernicus_catalogue_is_current(self):
        parcel = load_parcel(ZP1)
        provider = CopernicusProvider(SWI, self.cache, pixels_for_parcel(parcel.polygons_wgs84, parcel.centroid_wgs84), http=self.http)
        found = provider.discover(date.today() - timedelta(days=7), date.today())
        self.assertTrue(found)
        self.assertGreaterEqual(max(found), date.today() - timedelta(days=4))


if __name__ == "__main__":
    unittest.main()
