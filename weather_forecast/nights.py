"""Gel de l’air à 2 m ; minima par membre, aucune recommandation agronomique."""
from . import aggregation as agg

# Seuils de lecture uniquement, configurables ici ; aucune validation agronomique.
FROST_LOW = 0.2
FROST_HIGH = 0.7
MIN_MEMBER_FRACTION = 0.8


def night_temperature(result, period):
    series = result.series if result.usable else None
    # Température instantanée : inclure les deux bornes 18 h et 06 h.
    idx = [i for i, t in enumerate(series.times) if period.start <= t <= period.end] if series else []
    expected = period.hours + 1
    if result.spec.kind == "ensemble":
        members = series.members.get("temperature_2m", {}) if series else {}
        minima = agg.member_values(members, idx, expected, "min")
        stats = agg.temperature_stats(minima)
        stats["min_p90_c"] = round(agg.quantile(sorted(minima), .9), 1) if minima else None
        valid = len(minima)
        state = "AVAILABLE" if valid == result.spec.members_expected else "PARTIAL_MEMBERS" if valid else "INCOMPLETE"
        prob = stats["prob_temperature_below_0c"]
        sufficient = valid >= result.spec.members_expected * MIN_MEMBER_FRACTION
        level = "INSUFFICIENT" if not sufficient else "LOW" if prob < FROST_LOW else "HIGH" if prob >= FROST_HIGH else "UNCERTAIN"
        return {"availability": state if result.usable else "MODEL_UNAVAILABLE", "samples_expected": expected,
                "members_expected": result.spec.members_expected, **stats, "reading": level}
    values = series.values.get("temperature_2m") if series else None
    selected = agg.window(values, idx, expected) if values is not None else None
    minimum = round(min(selected), 1) if selected is not None else None
    return {"availability": "AVAILABLE" if minimum is not None else "INCOMPLETE" if result.usable else "MODEL_UNAVAILABLE",
            "samples_expected": expected, "temperature_min_c": minimum,
            "below_0c": None if selected is None else min(selected) < 0}


def night_block(period, lead, det, ens, now):
    return {**period.describe(now), "lead_day": lead,
            "start_local": period.start.astimezone(agg.PARIS).isoformat(),
            "end_local": period.end.astimezone(agg.PARIS).isoformat(),
            "deterministic": {k: night_temperature(r, period) for k, r in det.items()},
            "ensemble": {k: night_temperature(r, period) for k, r in ens.items()},
            "reading_thresholds": {"low_below": FROST_LOW, "high_at_least": FROST_HIGH,
                                   "min_member_fraction": MIN_MEMBER_FRACTION}}
