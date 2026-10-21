"""Consensus : règles explicites, plafonds publiés, jamais de moyenne."""
import unittest

from weather_forecast import confidence as conf


def det(**values):
    """{modèle: cumul mm} → blocs déterministes ; None = hors horizon."""
    return {k: {"availability": "OUT_OF_HORIZON" if v is None else "AVAILABLE", "precipitation_mm": v}
            for k, v in values.items()}


def ens(ecmwf=None, icon=None, members=(51, 40)):
    out = {}
    for (name, p), (expected, valid) in zip((("ecmwf_ens", ecmwf), ("icon_eu_eps", icon)),
                                            ((51, members[0]), (40, members[1]))):
        out[name] = {"availability": "OUT_OF_HORIZON" if p is None else "AVAILABLE", "members_expected": expected,
                     "precipitation": {"prob_ge_2mm": p, "members_valid": 0 if p is None else valid}}
    return out


ALL_DRY = det(arome_hd=0.0, arome=0.1, arpege=0.0, ecmwf=0.2, icon=0.0)
ALL_WET = det(arome_hd=6.5, arome=7.0, arpege=5.8, ecmwf=8.1, icon=6.9)


class Categories(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual([conf.category(v) for v in (0, 0.49, 0.5, 1.99, 2.0, 9.99, 10.0, 30)],
                         ["DRY", "DRY", "LIGHT", "LIGHT", "WET", "WET", "VERY_WET", "VERY_WET"])
        self.assertEqual((conf.scenario(1.99), conf.scenario(2.0)), ("DRY", "WET"))

    def test_no_agronomic_naming(self):
        self.assertFalse([name for name in dir(conf) if "AUGER" in name.upper() or "USEFUL" in name.upper()])


class Confidence(unittest.TestCase):
    def test_all_dry_short_range_is_high_dry(self):
        c = conf.assess(ALL_DRY, ens(0.02, 0.05), lead_day=1)
        self.assertEqual((c["confidence"], c["majority_scenario"], c["confidence_cap_reason"]), ("HIGH", "DRY", None))
        self.assertEqual(c["deterministic_wet_fraction"], 0.0)

    def test_all_wet_with_wet_ensembles_is_high_wet(self):
        c = conf.assess(ALL_WET, ens(0.92, 0.88), lead_day=1)
        self.assertEqual((c["confidence"], c["majority_scenario"]), ("HIGH", "WET"))
        self.assertEqual((c["deterministic_min_mm"], c["deterministic_max_mm"]), (5.8, 8.1))
        self.assertEqual(c["ensemble_prob_ge_2mm"], {"ecmwf_ens": 0.92, "icon_eu_eps": 0.88})

    def test_half_split_models_is_low(self):
        c = conf.assess(det(arpege=0.0, ecmwf=4.0, icon=0.3, arome=3.0), ens(0.5, 0.45), lead_day=3)
        self.assertEqual((c["confidence"], c["majority_scenario"]), ("LOW", "SPLIT"))

    def test_converging_models_but_dispersed_ensemble(self):
        ambiguous = conf.assess(ALL_DRY, ens(0.5, 0.2), lead_day=2)
        self.assertEqual(ambiguous["confidence"], "LOW")
        weak = conf.assess(ALL_DRY, ens(0.35, 0.1), lead_day=2)
        self.assertEqual(weak["confidence"], "MEDIUM")
        contradicted = conf.assess(ALL_DRY, ens(0.85, 0.1), lead_day=2)
        self.assertEqual(contradicted["confidence"], "LOW")

    def test_day_7_with_single_ensemble_is_never_high(self):
        c = conf.assess(det(arome_hd=None, arome=None, arpege=None, ecmwf=0.0, icon=None, icon_global=0.1),
                        ens(0.05, None), lead_day=7)
        self.assertNotEqual(c["confidence"], "HIGH")
        self.assertEqual(c["confidence"], "MEDIUM")
        self.assertEqual(c["confidence_cap_reason"], "échéance longue : un seul système d'ensemble disponible")
        self.assertEqual(c["ensemble_systems_available"], 1)
        many = conf.assess(ALL_DRY, ens(0.01, None), lead_day=7)
        self.assertEqual(many["confidence"], "MEDIUM")

    def test_two_models_or_one_ensemble_caps_medium(self):
        self.assertEqual(conf.assess(det(ecmwf=0.0, icon=0.0), ens(0.01, 0.02), lead_day=4)["confidence"], "MEDIUM")
        self.assertEqual(conf.assess(ALL_DRY, ens(0.01, None), lead_day=1)["confidence"], "MEDIUM")
        self.assertEqual(conf.assess(ALL_DRY, ens(None, None), lead_day=1)["confidence"], "MEDIUM")
        self.assertEqual(conf.assess(det(ecmwf=0.0), ens(0.01, 0.02), lead_day=1)["confidence"], "LOW")
        self.assertEqual(conf.assess({}, ens(0.01, 0.02), lead_day=1)["confidence"], "LOW")

    def test_dispersion(self):
        notable = conf.assess(det(arpege=12.0, ecmwf=18.0, icon=15.0), ens(0.95, 0.9), lead_day=3)
        self.assertEqual(notable["confidence"], "MEDIUM")
        extreme = conf.assess(det(arpege=2.5, ecmwf=16.0, icon=3.0), ens(0.95, 0.9), lead_day=3)
        self.assertEqual(extreme["confidence"], "LOW")
        self.assertIn("fort écart", extreme["confidence_cap_reason"])

    def test_arome_weight_without_overriding_others(self):
        c = conf.assess(det(arome_hd=4.0, arome=5.0, arpege=0.0, ecmwf=0.1, icon=0.3), ens(0.2, 0.1), lead_day=1)
        self.assertEqual(c["vote_weights"]["arome"], 1.5)
        self.assertEqual((c["majority_scenario"], c["confidence"]), ("SPLIT", "LOW"))
        self.assertEqual(conf.weight("arome", 3), 1.0)
        one = conf.assess(det(arome_hd=4.0, arome=0.0, arpege=0.0, ecmwf=0.1, icon=0.3), ens(0.1, 0.1), lead_day=1)
        self.assertEqual(one["majority_scenario"], "DRY")  # AROME HD seul ne renverse pas le vote

    def test_relay_votes_only_when_target_missing(self):
        both = conf.assess(det(ecmwf=0.0, icon=0.0, icon_global=0.0, arpege=0.0), ens(0.1, 0.1), lead_day=3)
        self.assertNotIn("icon_global", both["deterministic_models"])
        alone = conf.assess(det(ecmwf=0.0, icon=None, icon_global=0.0), ens(0.1, None), lead_day=6)
        self.assertIn("icon_global", alone["deterministic_models"])
        self.assertTrue(any("relaie" in r for r in alone["reasons"]))

    def test_failed_model_and_missing_members_cap_medium(self):
        failed = conf.assess(ALL_DRY, ens(0.01, 0.02), lead_day=1, failed_models=["arpege"])
        self.assertEqual(failed["confidence"], "MEDIUM")
        thin = conf.assess(ALL_DRY, ens(0.01, 0.02, members=(30, 40)), lead_day=1)
        self.assertEqual(thin["confidence"], "MEDIUM")

    def test_reasons_are_published_and_no_mean(self):
        c = conf.assess(ALL_WET, ens(0.92, 0.88), lead_day=1)
        self.assertTrue(c["reason"])
        self.assertFalse([k for k in c if "average" in k or "mean" in k])


if __name__ == "__main__":
    unittest.main()
