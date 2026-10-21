"""Consensus multi-modèles et niveau de confiance — règles explicites, sans moyenne.

Principe : on part de HIGH et chaque règle peut seulement plafonner le niveau
(HIGH → MEDIUM → LOW). Chaque plafond est publié avec sa raison. Le scénario
« sec / humide » est binaire au seuil SCENARIO_THRESHOLD_MM, le même que la
probabilité d'ensemble prob_ge_2mm, pour que déterministes et ensembles
soient comparés sur la même question.

Ces catégories sont purement météorologiques : la quantité de pluie qui rend
le terrain travaillable (ou non) n'est pas calibrée et n'est pas codée ici.
"""
from __future__ import annotations

from .specs import RELAYS

# Catégories du vote déterministe (mm sur la période) : bornes inférieures inclusives.
DRY_BELOW_MM = 0.5        # DRY       < 0,5 mm
LIGHT_BELOW_MM = 2.0      # LIGHT     0,5 à < 2 mm
WET_BELOW_MM = 10.0       # WET       2 à < 10 mm ; VERY_WET ≥ 10 mm
SCENARIO_THRESHOLD_MM = 2.0   # scénario WET si cumul ≥ 2 mm (WET, VERY_WET), sinon DRY (DRY, LIGHT)

MIN_MODELS_HIGH = 3            # HIGH exige au moins 3 modèles déterministes complets
AGREEMENT_HIGH_MIN = 0.75      # accord pondéré ≥ 75 % pour HIGH
AGREEMENT_LOW_BELOW = 0.60     # accord < 60 % (partage sec/humide) : LOW
ENSEMBLE_STRONG_WET = 0.70     # P(≥ 2 mm) ≥ 70 % : l'ensemble soutient WET
ENSEMBLE_STRONG_DRY = 0.30     # P(≥ 2 mm) ≤ 30 % : l'ensemble soutient DRY
ENSEMBLE_AMBIGUOUS = (0.40, 0.60)   # P(≥ 2 mm) dans cet intervalle : ensemble indécis → LOW
ENSEMBLE_MIN_MEMBER_FRACTION = 0.8  # moins de 80 % des membres valides : MEDIUM au plus
SPREAD_NOTABLE_MM = 5.0        # max − min ≥ 5 mm : dispersion notable → MEDIUM au plus
SPREAD_EXTREME_MM = 10.0       # max − min ≥ 10 mm et max ≥ 3 × max(min, 1 mm) : extrême → LOW
SPREAD_EXTREME_RATIO = 3.0
LONG_LEAD_DAY = 6              # J+6 et au-delà : MEDIUM au plus
# AROME (HD et 2,5 km) pèsent davantage dans le vote à très courte échéance, sans majorité seuls :
# 2 × 1,5 = 3 voix contre 3 pour ARPEGE + IFS + ICON → un désaccord AROME / autres donne 50 % → LOW.
HIGH_RES_MODELS = ("arome_hd", "arome")
HIGH_RES_WEIGHT = 1.5
HIGH_RES_MAX_LEAD_DAY = 2

LEVELS = ("LOW", "MEDIUM", "HIGH")
LONG_LEAD_SINGLE_ENSEMBLE = "échéance longue : un seul système d'ensemble disponible"


def category(mm: float) -> str:
    if mm < DRY_BELOW_MM:
        return "DRY"
    if mm < LIGHT_BELOW_MM:
        return "LIGHT"
    return "WET" if mm < WET_BELOW_MM else "VERY_WET"


def scenario(mm: float) -> str:
    return "WET" if mm >= SCENARIO_THRESHOLD_MM else "DRY"


def ensemble_reading(prob: float) -> str:
    if prob >= ENSEMBLE_STRONG_WET:
        return "WET"
    if prob <= ENSEMBLE_STRONG_DRY:
        return "DRY"
    if ENSEMBLE_AMBIGUOUS[0] <= prob <= ENSEMBLE_AMBIGUOUS[1]:
        return "AMBIGUOUS"
    return "LEANING_WET" if prob > 0.5 else "LEANING_DRY"


def weight(model: str, lead_day: int) -> float:
    return HIGH_RES_WEIGHT if model in HIGH_RES_MODELS and lead_day <= HIGH_RES_MAX_LEAD_DAY else 1.0


def assess(deterministic: dict, ensembles: dict, lead_day: int, failed_models: list[str] = (),
           relays: dict = RELAYS) -> dict:
    """`deterministic` : {modèle: bloc période} ; `ensembles` : {système: bloc période} ;
    `failed_models` : modèles en erreur alors qu'ils auraient dû couvrir l'échéance ;
    `relays` : {relais: modèle relayé} — un relais ne vote que si le modèle relayé est absent."""
    caps: list[tuple[str, str]] = []   # (niveau maximal, raison)
    notes: list[str] = []
    used = {k: b["precipitation_mm"] for k, b in deterministic.items()
            if b["availability"] == "AVAILABLE" and b["precipitation_mm"] is not None}
    for relay, target in relays.items():
        if relay in used and target in used:
            del used[relay]  # même système : jamais deux voix
        elif relay in used:
            notes.append(f"{relay} relaie {target} (hors horizon ou indisponible)")
    partial = sorted(k for k, b in deterministic.items() if b["availability"] in ("PARTIAL_HORIZON", "INCOMPLETE"))
    outside = sorted(k for k, b in deterministic.items() if b["availability"] == "OUT_OF_HORIZON")
    if partial:
        notes.append("couverture incomplète de la période, exclus du vote : " + ", ".join(partial))
    if outside:
        notes.append("hors horizon : " + ", ".join(outside))
    if failed_models:
        caps.append(("MEDIUM", "données partielles : " + ", ".join(failed_models) + " en erreur"))

    weights = {k: weight(k, lead_day) for k in used}
    total = sum(weights.values())
    wet_w = sum(w for k, w in weights.items() if scenario(used[k]) == "WET")
    majority = None
    agreement = None
    if total:
        agreement = round(max(wet_w, total - wet_w) / total, 3)
        majority = "SPLIT" if wet_w == total - wet_w else "WET" if wet_w > total - wet_w else "DRY"
    values = sorted(used.values())
    spread = round(values[-1] - values[0], 1) if values else None

    n = len(used)
    if n == 0:
        caps.append(("LOW", "aucun modèle déterministe complet sur la période"))
    elif n == 1:
        caps.append(("LOW", "un seul modèle déterministe disponible"))
    elif n < MIN_MODELS_HIGH:
        caps.append(("MEDIUM", f"seulement {n} modèles déterministes"))
    if majority == "SPLIT" or (agreement is not None and agreement < AGREEMENT_LOW_BELOW):
        caps.append(("LOW", f"déterministes partagés entre sec et humide (accord {agreement:.0%})"))
    elif agreement is not None and agreement < AGREEMENT_HIGH_MIN:
        caps.append(("MEDIUM", f"accord déterministe {agreement:.0%} < {AGREEMENT_HIGH_MIN:.0%}"))
    elif agreement is not None:
        notes.append(f"{n} modèle{'s' if n > 1 else ''}, accord pondéré {agreement:.0%} sur le scénario {majority}")
    if spread is not None and n >= 2:
        extreme = spread >= SPREAD_EXTREME_MM and values[-1] >= SPREAD_EXTREME_RATIO * max(values[0], 1.0)
        if extreme:
            caps.append(("LOW", f"fort écart entre modèles ({values[0]:g}–{values[-1]:g} mm)"))
        elif spread >= SPREAD_NOTABLE_MM:
            caps.append(("MEDIUM", f"dispersion quantitative notable ({values[0]:g}–{values[-1]:g} mm)"))

    probs, readings, available = {}, {}, 0
    for name, block in ensembles.items():
        p = block["precipitation"]["prob_ge_2mm"] if block["availability"] == "AVAILABLE" else None
        probs[name] = p
        if p is None:
            readings[name] = None
            continue
        available += 1
        readings[name] = reading = ensemble_reading(p)
        valid, expected = block["precipitation"]["members_valid"], block["members_expected"]
        if expected and valid < ENSEMBLE_MIN_MEMBER_FRACTION * expected:
            caps.append(("MEDIUM", f"{name} : {valid}/{expected} membres valides"))
        if reading == "AMBIGUOUS":
            caps.append(("LOW", f"{name} : P(≥ 2 mm) = {p:.0%}, proche de 50 %"))
        elif majority in ("WET", "DRY") and reading in ("WET", "DRY") and reading != majority:
            caps.append(("LOW", f"{name} contredit les déterministes (P(≥ 2 mm) = {p:.0%})"))
        elif majority in ("WET", "DRY") and reading != majority:
            caps.append(("MEDIUM", f"{name} : soutien faible du scénario {majority} (P(≥ 2 mm) = {p:.0%})"))
        else:
            notes.append(f"{name} : P(≥ 2 mm) = {p:.0%}")
    if available == 0:
        caps.append(("MEDIUM", "aucun système d'ensemble disponible"))
    elif available == 1:
        caps.append(("MEDIUM", LONG_LEAD_SINGLE_ENSEMBLE if lead_day >= LONG_LEAD_DAY
                     else "un seul système d'ensemble disponible"))
    if lead_day >= LONG_LEAD_DAY:
        caps.append(("MEDIUM", f"échéance J+{lead_day} ≥ J+{LONG_LEAD_DAY}"))

    level = min((LEVELS.index(c) for c, _ in caps), default=2)
    binding = [reason for c, reason in caps if LEVELS.index(c) == level]
    # Le cas « J+6/J+7, un seul ensemble » est toujours nommé explicitement quand il s'applique.
    cap_reason = (LONG_LEAD_SINGLE_ENSEMBLE if level == 1 and any(r == LONG_LEAD_SINGLE_ENSEMBLE for _, r in caps)
                  else binding[0] if caps else None)
    reasons = [reason for _, reason in caps] + notes
    return {
        "scenario_threshold_mm": SCENARIO_THRESHOLD_MM, "deterministic_models": sorted(used),
        "deterministic_categories": {k: category(v) for k, v in sorted(used.items())},
        "deterministic_wet_fraction": round(sum(1 for v in values if v >= SCENARIO_THRESHOLD_MM) / n, 3) if n else None,
        "deterministic_rain_fraction": round(sum(1 for v in values if v >= DRY_BELOW_MM) / n, 3) if n else None,
        "vote_weights": weights, "weighted_agreement": agreement, "majority_scenario": majority,
        "deterministic_min_mm": values[0] if values else None, "deterministic_max_mm": values[-1] if values else None,
        "deterministic_spread_mm": spread, "ensemble_prob_ge_2mm": probs, "ensemble_readings": readings,
        "ensemble_systems_available": available, "confidence": LEVELS[level], "confidence_cap_reason": cap_reason,
        "reasons": reasons, "reason": " ; ".join(reasons) or "aucune réserve",
    }


RULES = {
    "categories_mm": {"DRY": f"< {DRY_BELOW_MM}", "LIGHT": f"{DRY_BELOW_MM} à < {LIGHT_BELOW_MM}",
                      "WET": f"{LIGHT_BELOW_MM} à < {WET_BELOW_MM}", "VERY_WET": f">= {WET_BELOW_MM}"},
    "scenario_threshold_mm": SCENARIO_THRESHOLD_MM, "min_models_high": MIN_MODELS_HIGH,
    "agreement_high_min": AGREEMENT_HIGH_MIN, "agreement_low_below": AGREEMENT_LOW_BELOW,
    "ensemble_strong_wet": ENSEMBLE_STRONG_WET, "ensemble_strong_dry": ENSEMBLE_STRONG_DRY,
    "ensemble_ambiguous": list(ENSEMBLE_AMBIGUOUS), "ensemble_min_member_fraction": ENSEMBLE_MIN_MEMBER_FRACTION,
    "spread_notable_mm": SPREAD_NOTABLE_MM, "spread_extreme_mm": SPREAD_EXTREME_MM, "spread_extreme_ratio": SPREAD_EXTREME_RATIO,
    "long_lead_day": LONG_LEAD_DAY, "high_res_models": list(HIGH_RES_MODELS), "high_res_weight": HIGH_RES_WEIGHT,
    "high_res_max_lead_day": HIGH_RES_MAX_LEAD_DAY, "relays": RELAYS,
    "principle": "niveau initial HIGH, chaque règle ne peut que plafonner ; raisons publiées",
}
