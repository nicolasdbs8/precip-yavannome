"""Convergence de tendances, pas une probabilité ni une moyenne d'indices."""
from datetime import date, timedelta

from .calculations import group_series

# Conventions internes Rain : seuils de variation sur 7 jours, pas des
# seuils scientifiques de sécheresse. Un seul vote par fournisseur.
THRESHOLDS = {"root_liquid": 0.005, "soil_water": 0.005, "swi_copernicus": 2.0}


def compare(observations, end: date):
    start = end - timedelta(days=6)
    candidates = {}
    for key, values in group_series(observations).items():
        sample = values[0]
        if sample.variable not in THRESHOLDS:
            continue
        if sample.variable == "soil_water" and (sample.depth_min_cm, sample.depth_max_cm) != (0, 7):
            continue
        usable = {o.date: o for o in values if o.value is not None and o.quality == "valid"
                  and str(start) <= o.date <= str(end)}
        if len(usable) >= 3:
            # Ne pas mélanger versions / mailles : préférence déterministe.
            existing = candidates.get(sample.source)
            if existing is None or (len(usable), str(key)) > (len(existing), str(next(iter(existing.values())).series_key)):
                candidates[sample.source] = usable
    common = set.intersection(*(set(v) for v in candidates.values())) if candidates else set()
    result = {"category": "données insuffisantes", "evidence": [], "period": [str(start), str(end)],
              "method": "Rain v1 : signe de la variation entre dates communes, seuils internes documentés ; un vote par source.",
              "limits": "Profondeurs, fenêtres et définitions différentes ; convergence qualitative, aucune probabilité. Les modèles peuvent partager des observations d'entrée."}
    if len(candidates) < 2 or len(common) < 3:
        return result
    first, last = min(common), max(common)
    if (date.fromisoformat(last) - date.fromisoformat(first)).days < 2 or (end - date.fromisoformat(last)).days > 2:
        return result
    for source, values in sorted(candidates.items()):
        a, b = values[first], values[last]
        delta, threshold = b.value - a.value, THRESHOLDS[a.variable]
        direction = "hausse" if delta > threshold else "baisse" if delta < -threshold else "stable"
        result["evidence"].append({"source": source, "variable": a.variable, "first": a.to_dict(),
                                   "last": b.to_dict(), "delta": delta, "unit": a.unit,
                                   "threshold": threshold, "direction": direction})
    directions = {e["direction"] for e in result["evidence"]}
    if {"hausse", "baisse"}.issubset(directions):
        result["category"] = "données contradictoires"
    elif len(directions) == 1 and len(candidates) >= 3:
        result["category"] = "convergence forte"
    else:
        result["category"] = "convergence modérée"
    result["period"] = [first, last]
    return result
