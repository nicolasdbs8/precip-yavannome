"""Synthèse qualitative des tendances d'humidité (comparisons.soil_moisture_trends)."""
import copy
import unittest

from soil_hydrology.export import build_report
from soil_hydrology.models import Nature, Observation, ProviderResult
from soil_hydrology.synthesis import moisture_synthesis
from test_export import FETCHED, NOW, PARCEL, rain_result, report

DAY = "2026-10-19"
D7 = "2026-10-12"
SWI_BANDS = ("SWI002", "SWI005", "SWI010", "SWI015", "SWI020", "SWI040", "SWI060", "SWI100")
# Données réelles du 19/10/2026 (pixel principal), J−7 et J.
SWI_NOW = {"SWI002": 40.0, "SWI005": 33.0, "SWI010": 26.5, "SWI015": 22.5, "SWI020": 20.0, "SWI040": 16.0, "SWI060": 16.5, "SWI100": 19.5}
SWI_D7 = {"SWI002": 37.0, "SWI005": 29.0, "SWI010": 21.0, "SWI015": 17.5, "SWI020": 15.5, "SWI040": 14.0, "SWI060": 15.0, "SWI100": 18.5}


def pixels(now: dict, before: dict, second=None):
    """Deux pixels ZP1 ; le second vaut le premier − 1 sauf valeurs explicites `second=(now, before)`."""
    now2, before2 = second or ({b: v - 1 for b, v in now.items() if v is not None},
                               {b: v - 1 for b, v in before.items() if v is not None})

    def one(pid, frac, values, prior):
        return {"id": pid, "overlap_fraction": frac,
                "series": {"date": [D7, DAY], **{b: [prior.get(b), values.get(b)] for b in now}}}
    return [one("r2821_c1842", .6874, now, before), one("r2822_c1842", .3126, now2, before2)]


def cop(now, before, status="FRESH", second=None, **extra):
    return {"status": status, "status_reason": "test", "source_date": DAY if status != "UNAVAILABLE" else None,
            "pixels": pixels(now, before, second), **extra}


def sim(swi=0.102, d7=0.098, trend="stable", sswi=-2.529, status="FRESH", **extra):
    grid = {"id": "8360_22090", "latest": {"date": DAY, "values": {"SWI": swi, "SSWI_10J": sswi}},
            "derived": {"swi": {"latest": swi, "latest_date": DAY, "d7": d7, "d7_date": D7,
                                "delta_7d": round(swi - d7, 4), "trend": trend}}}
    return {"status": status, "status_reason": "test", "source_date": DAY, "primary_grid": "8360_22090", "grids": [grid], **extra}


def real_sources():
    # SSM réel : r2821_c1842 46,0 % (J−7 25,5) ; r2822_c1842 42,0 % (J−7 25,5).
    return {"sim": sim(), "copernicus_swi": cop(SWI_NOW, SWI_D7),
            "copernicus_ssm": cop({"SSM": 46.0}, {"SSM": 25.5}, second=({"SSM": 42.0}, {"SSM": 25.5}))}


class Synthesis(unittest.TestCase):
    def test_all_fresh_sources_are_used(self):
        out = moisture_synthesis(real_sources())
        self.assertNotEqual(out["category"], "INSUFFICIENT_DATA")
        self.assertEqual((out["completeness"], out["sources_used"], out["sources_excluded"]),
                         ("COMPLETE", ["sim", "copernicus_ssm", "copernicus_swi"], {}))

    def test_real_case_is_surface_rewetting_with_deep_dryness(self):
        out = moisture_synthesis(real_sources())
        self.assertEqual(out["category"], "SURFACE_REWETTING_DEEP_DRYNESS")
        self.assertEqual(out["label"], "réhumidification superficielle récente, sécheresse de fond persistante")
        ssm = out["signals"]["copernicus_ssm"]
        self.assertEqual((ssm["value"], ssm["d7"], ssm["delta_7d"], ssm["trend"]), (44.75, 25.5, 19.25, "rising"))
        swi = out["signals"]["copernicus_swi"]
        self.assertEqual([t["trend"] for t in swi["short_t"]], ["rising"] * 3)
        self.assertGreaterEqual(swi["short_minus_long_pts"], 10)
        self.assertTrue(any("SSWI_10J = -2.529" in e for e in out["evidence"]))
        self.assertTrue(any(e.startswith("Copernicus SSM pondéré ZP1 = 44.75 %") and "forte hausse" in e for e in out["evidence"]))

    def test_carried_over_fresh_sim_with_new_copernicus_run(self):
        """Bug réel : --provider copernicus, SIM repris du JSON précédent mais FRESH."""
        first = report()  # SIM exécuté (série synthétique en hausse)

        def obs(pixel, band, day, value, source="copernicus_swi", variable="swi_copernicus"):
            return Observation(timestamp=f"{day}T12:00:00+00:00", date=day, latitude=46.8, longitude=5.45, source=source,
                               product="CLMS", provider="CLMS", variable=variable, original_variable=band, value=value,
                               unit="%", nature=Nature.SATELLITE_DERIVED, retrieved_at=FETCHED, grid_id=pixel, quality="valid")
        px = [{"id": "r2821_c1842", "overlap_fraction": .6874}, {"id": "r2822_c1842", "overlap_fraction": .3126}]
        swi_obs = [obs(p["id"], b, d, v) for p in px for b in SWI_BANDS for d, v in ((D7, SWI_D7[b]), (DAY, SWI_NOW[b]))]
        ssm_obs = [obs(p["id"], "SSM", d, v, "copernicus_ssm", "ssm_copernicus") for p in px for d, v in ((D7, 25.5), (DAY, 46.0))]
        later = build_report(PARCEL, {"sim": None, "rain_precipitation": rain_result({}),
                                      "copernicus_swi": ProviderResult("SWI", observations=swi_obs, details={"pixels": px}),
                                      "copernicus_ssm": ProviderResult("SSM", observations=ssm_obs, details={"pixels": px})},
                             NOW, previous=first)
        self.assertIn("carried_over_from", later["sources"]["sim"])
        self.assertEqual(later["sources"]["sim"]["status"], "FRESH")
        out = later["comparisons"]["soil_moisture_trends"]
        self.assertEqual(out["sources_used"], ["sim", "copernicus_ssm", "copernicus_swi"])
        self.assertNotEqual(out["category"], "INSUFFICIENT_DATA")

    def test_no_sources_is_insufficient(self):
        out = moisture_synthesis({})
        self.assertEqual((out["category"], out["label"], out["completeness"], out["sources_used"]),
                         ("INSUFFICIENT_DATA", "données insuffisantes", "NONE", []))
        self.assertEqual(len(out["sources_excluded"]), 3)

    def test_stale_copernicus_is_not_current_evidence(self):
        sources = real_sources()
        sources["copernicus_ssm"]["status"] = "STALE"
        out = moisture_synthesis(sources)
        self.assertNotIn("copernicus_ssm", out["sources_used"])
        self.assertNotIn("copernicus_ssm", out["signals"])
        self.assertTrue(out["sources_excluded"]["copernicus_ssm"].startswith("STALE"))
        self.assertEqual(out["completeness"], "PARTIAL")
        self.assertTrue(any("copernicus_ssm non utilisé" in e for e in out["evidence"]))
        sources["copernicus_swi"]["status"] = "ERROR"
        only_sim = moisture_synthesis(sources)
        self.assertEqual(only_sim["category"], "INSUFFICIENT_DATA")  # pas de surface : pas de convergence simulée
        self.assertEqual(only_sim["sources_used"], ["sim"])

    def test_levels_are_never_compared_across_sources(self):
        base = moisture_synthesis(real_sources())
        scaled = real_sources()
        scaled["sim"] = sim(swi=10.2, d7=9.8, trend="stable")  # SIM ×100 : niveau « comparable » au %
        shifted = real_sources()
        shifted["copernicus_swi"] = cop({b: v + 30 for b, v in SWI_NOW.items()}, {b: v + 30 for b, v in SWI_D7.items()})
        for variant in (moisture_synthesis(scaled), moisture_synthesis(shifted)):
            self.assertEqual(variant["category"], base["category"])
        self.assertEqual(moisture_synthesis(scaled)["signals"]["copernicus_swi"], base["signals"]["copernicus_swi"])
        self.assertEqual(set(base["signals"]), {"sim", "copernicus_ssm", "copernicus_swi"})  # aucun signal inter-sources

    def test_other_categories(self):
        wet = real_sources()
        wet["sim"] = sim(swi=0.40, d7=0.30, trend="rising", sswi=0.5)
        wet["copernicus_swi"] = cop({b: 50.0 for b in SWI_BANDS}, {b: 40.0 for b in SWI_BANDS})
        self.assertEqual(moisture_synthesis(wet)["category"], "GENERAL_REWETTING")
        dry = real_sources()
        dry["copernicus_ssm"] = cop({"SSM": 20.0}, {"SSM": 40.0})
        dry["copernicus_swi"] = cop({b: 20.0 for b in SWI_BANDS}, {b: 30.0 for b in SWI_BANDS})
        dry["sim"] = sim(swi=0.10, d7=0.20, trend="falling", sswi=0.2)
        self.assertEqual(moisture_synthesis(dry)["category"], "DRYING")
        flat = real_sources()
        flat["copernicus_ssm"] = cop({"SSM": 30.0}, {"SSM": 29.0})
        flat["copernicus_swi"] = cop({b: 25.0 for b in SWI_BANDS}, {b: 25.0 for b in SWI_BANDS})
        flat["sim"] = sim(sswi=0.1)
        self.assertEqual(moisture_synthesis(flat)["category"], "STABLE")

    def test_missing_d7_and_flagged_pixel(self):
        sources = real_sources()
        sources["copernicus_ssm"] = cop({"SSM": 46.0}, {"SSM": None}, second=({"SSM": 42.0}, {}))  # pas de passage S1 à J−7
        out = moisture_synthesis(sources)
        self.assertIsNone(out["signals"]["copernicus_ssm"]["trend"])
        self.assertTrue(any("tendance non calculable" in e for e in out["evidence"]))
        flagged = copy.deepcopy(real_sources())
        flagged["copernicus_ssm"]["pixels"][1]["series"]["SSM"] = [None, None]
        ssm = moisture_synthesis(flagged)["signals"]["copernicus_ssm"]
        self.assertEqual((ssm["value"], ssm["covered_fraction"]), (46.0, .6874))  # pixel exclu, pas compté 0


if __name__ == "__main__":
    unittest.main()
