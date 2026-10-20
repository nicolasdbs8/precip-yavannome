"""Géospatial hors réseau : transformations, parcelle ZP1, mailles SAFRAN, pixels Copernicus."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from soil_hydrology import geo
from soil_hydrology.copernicus import PIXEL_DEG, pixels_for_parcel
from soil_hydrology.parcel import Parcel, ParcelRef, ZP1, load_parcel, parse_api_response
from soil_hydrology.sim import cell_box, locate_parcel

FIXTURES = Path(__file__).parent / "fixtures"
GRID = (FIXTURES / "safran_grid_sample.csv").read_text(encoding="utf-8")


def square(x, y, half):
    return [[[(x - half, y - half), (x + half, y - half), (x + half, y + half), (x - half, y + half), (x - half, y - half)]]]


class Transforms(unittest.TestCase):
    def test_wgs84_lambert2e_roundtrip_and_published_centres(self):
        to_l2e, to_wgs = geo.transformer(geo.WGS84, geo.LAMBERT2E), geo.transformer(geo.LAMBERT2E, geo.WGS84)
        x, y = to_l2e.transform(5.4501605, 46.8038979)
        lon, lat = to_wgs.transform(x, y)
        self.assertAlmostEqual(lon, 5.4501605, places=7)  # ~1 cm
        self.assertAlmostEqual(lat, 46.8038979, places=7)
        # Centre A publié 46.8392 N 5.43194 E : écart connu ~42 m, surtout est-ouest.
        lon, lat = to_wgs.transform(836000, 2209000)
        self.assertAlmostEqual(lat, 46.8392, delta=0.0002)
        self.assertAlmostEqual(lon, 5.43194, delta=0.001)

    def test_area_centroid_and_clip(self):
        poly = square(0, 0, 10)
        self.assertEqual(geo.area(poly), 400)
        self.assertEqual(geo.centroid(poly), (0, 0))
        self.assertAlmostEqual(geo.intersection_area(poly, (0, -100, 100, 100)), 200)
        self.assertEqual(geo.intersection_area(poly, (20, 20, 30, 30)), 0)
        self.assertAlmostEqual(geo.box_distance(poly, (20, -5, 30, 5)), 10)
        with_hole = [poly[0] + square(0, 0, 5)[0]]
        self.assertEqual(geo.area(with_hole), 300)
        concave = [[[(0, 0), (10, 0), (10, 10), (5, 2), (0, 10), (0, 0)]]]
        self.assertAlmostEqual(geo.intersection_area(concave, (-1, -1, 11, 11)), geo.area(concave))


class ParcelTests(unittest.TestCase):
    def test_api_response_parsing_and_summary(self):
        payload = json.loads((FIXTURES / "apicarto_zp1.json").read_text(encoding="utf-8"))
        feature = parse_api_response(payload, ZP1, "https://apicarto.ign.fr/api/cadastre/parcelle?…")
        summary = Parcel(ZP1, feature).summary()
        self.assertEqual(summary["idu"], "39160000ZP0001")
        self.assertEqual(summary["area_m2"], 4634)
        self.assertAlmostEqual(summary["area_m2_geometry"], 4634, delta=50)
        self.assertAlmostEqual(summary["centroid"]["lat"], 46.80390, places=4)
        self.assertAlmostEqual(summary["centroid"]["lon"], 5.45016, places=4)
        west, south, east, north = summary["bbox_wgs84"]
        self.assertTrue(west < summary["centroid"]["lon"] < east and south < summary["centroid"]["lat"] < north)
        self.assertEqual(summary["geometry_source"]["provider"], "IGN API Carto — module cadastre")

    def test_wrong_or_missing_parcel_is_an_error(self):
        payload = json.loads((FIXTURES / "apicarto_zp1.json").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            parse_api_response(payload, ParcelRef("39160", "ZP", "0002"), "url")
        with self.assertRaises(ValueError):
            parse_api_response({"features": []}, ZP1, "url")

    def test_no_fixture_no_network_never_fabricates(self):
        ref = ParcelRef("39160", "ZZ", "9999")
        with tempfile.TemporaryDirectory() as tmp, patch("soil_hydrology.parcel.DATA_DIR", Path(tmp)):
            with self.assertRaises(RuntimeError):
                load_parcel(ref, http=None)
            http = Mock()
            http.get.return_value = Mock(status_code=503)
            with self.assertRaises(RuntimeError):
                load_parcel(ref, http=http)
            self.assertFalse(list(Path(tmp).iterdir()))

    def test_committed_fixture_is_official_zp1(self):
        parcel = load_parcel(ZP1)
        self.assertEqual(parcel.feature["properties"]["idu"], "39160000ZP0001")
        self.assertIn("apicarto.ign.fr", parcel.feature["rain"]["url"])


class SafranGrid(unittest.TestCase):
    def test_zp1_is_entirely_in_cell_a_and_b_is_adjacent(self):
        grids = locate_parcel(GRID, load_parcel(ZP1).polygons(geo.LAMBERT2E))
        self.assertEqual([(g.id, g.role) for g in grids], [("8360_22090", "parcel"), ("8360_22010", "adjacent")])
        self.assertEqual(grids[0].overlap_fraction, 1.0)
        self.assertAlmostEqual(grids[1].distance_m, 33.9, delta=1)
        self.assertEqual((grids[0].lat, grids[0].lon), (46.8392, 5.43194))
        self.assertEqual(len(locate_parcel(GRID, load_parcel(ZP1).polygons(geo.LAMBERT2E), neighbor_margin_m=0)), 1)

    def test_parcel_straddling_two_cells_keeps_both(self):
        # Polygone à cheval sur la frontière y = 2 205 000 m, 70 % au nord (A).
        poly = [[[(837000, 2204970), (837100, 2204970), (837100, 2205070), (837000, 2205070), (837000, 2204970)]]]
        grids = locate_parcel(GRID, poly, neighbor_margin_m=0)
        self.assertEqual([g.id for g in grids], ["8360_22090", "8360_22010"])
        self.assertEqual({g.role for g in grids}, {"parcel"})
        self.assertAlmostEqual(grids[0].overlap_fraction, .7)
        self.assertAlmostEqual(sum(g.overlap_fraction for g in grids), 1)

    def test_not_nearest_centre_but_intersection(self):
        # Centroïde dans B, mais une pointe entre dans A : les deux sont gardées.
        poly = [[[(837000, 2203000), (837010, 2203000), (837010, 2205050), (837000, 2203000)]]]
        self.assertEqual({g.role for g in locate_parcel(GRID, poly, 0)}, {"parcel"})
        self.assertEqual(len(locate_parcel(GRID, poly, 0)), 2)

    def test_cells_are_8km_squares_and_outside_grid_fails(self):
        self.assertEqual(cell_box(8360, 22090), (832000, 2205000, 840000, 2213000))
        with self.assertRaises(ValueError):
            locate_parcel(GRID, square(100000, 100000, 10))


class CopernicusPixels(unittest.TestCase):
    def test_zp1_intersects_two_1km_pixels(self):
        parcel = load_parcel(ZP1)
        pixels = pixels_for_parcel(parcel.polygons_wgs84, parcel.centroid_wgs84)
        self.assertEqual([p.id for p in pixels], ["r2821_c1842", "r2822_c1842"])
        self.assertAlmostEqual(sum(p.overlap_fraction for p in pixels), 1, places=3)
        self.assertGreater(pixels[0].overlap_fraction, .6)
        west, south, east, north = pixels[0].bounds
        self.assertAlmostEqual(east - west, PIXEL_DEG)
        self.assertAlmostEqual(south, 72 - 2822 / 112)

    def test_single_pixel_when_parcel_inside(self):
        lon, lat = 5.45, 46.808
        poly = [[[(lon, lat), (lon + .001, lat), (lon + .001, lat + .001), (lon, lat + .001), (lon, lat)]]]
        pixels = pixels_for_parcel(poly, (lon + .0005, lat + .0005))
        self.assertEqual(len(pixels), 1)
        self.assertEqual(pixels[0].overlap_fraction, 1.0)


if __name__ == "__main__":
    unittest.main()
