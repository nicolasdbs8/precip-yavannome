"""Synthèse qualitative des tendances d'humidité du sol, calculée sur le JSON final.

Chaque source n'est lue qu'à travers ses propres tendances, dans sa propre unité :
SSM Copernicus (surface, %), SWI Copernicus (indice %, par temps caractéristique T,
qui n'est PAS une profondeur), SIM (SWI 0–1 et SSWI_10J standardisé). Aucun
niveau n'est comparé d'une source à l'autre ; le seul écart de niveau calculé
(temps courts vs temps longs) l'est à l'intérieur du même produit SWI.

Les blocs repris d'un run précédent (`carried_over_from`) sont utilisables tant
que leur statut recalculé est FRESH ou PARTIAL ; STALE, ERROR, UNAVAILABLE… sont
exclus et signalés.
"""
from __future__ import annotations

from datetime import date, timedelta

USABLE = {"FRESH", "PARTIAL"}
SHORT_T = ("SWI002", "SWI005", "SWI010")  # réagissent en quelques jours
LONG_T = ("SWI040", "SWI060", "SWI100")   # intègrent plusieurs semaines
# Conventions internes Rain (pas des seuils scientifiques publiés), unité de chaque produit.
SSM_TREND_PTS = 5.0         # Δ 7 j SSM (points de %) au-delà duquel la surface varie
SSM_STRONG_PTS = 10.0       # Δ 7 j SSM « forte »
SWI_TREND_PTS = 2.0         # Δ 7 j d'une bande SWI Copernicus
SHORT_LONG_GAP_PTS = 10.0   # temps longs nettement sous les temps courts (même produit)
SSWI_DRY = -1.0             # SSWI_10J ≤ −1 : plus sec que la normale du modèle SIM
LABELS = {
    "INSUFFICIENT_DATA": "données insuffisantes",
    "SURFACE_REWETTING_DEEP_DRYNESS": "réhumidification superficielle récente, sécheresse de fond persistante",
    "GENERAL_REWETTING": "réhumidification générale (surface et fond)",
    "DRYING": "assèchement",
    "STABLE": "état stable",
    "MIXED": "signaux mixtes",
}


def weighted_at(pixels: list[dict], band: str, day: str | None) -> tuple[float | None, float]:
    """Moyenne des pixels d'un même produit pondérée par leur part de parcelle ;
    un pixel sans valeur valide est exclu (jamais 0). Retourne (valeur, part couverte)."""
    if day is None:
        return None, 0.0
    valid = []
    for p in pixels:
        value = dict(zip(p["series"]["date"], p["series"].get(band, []))).get(day)
        if value is not None and p.get("overlap_fraction"):
            valid.append((p["overlap_fraction"], value))
    weight = sum(w for w, _ in valid)
    return (round(sum(w * v for w, v in valid) / weight, 2) if weight > 0 else None), round(weight, 4)


def _direction(delta, threshold):
    if delta is None:
        return None
    return "rising" if delta > threshold else "falling" if delta < -threshold else "stable"


def band_trend(block: dict, band: str, threshold: float) -> dict:
    anchor = block.get("source_date")
    prior = (date.fromisoformat(anchor) - timedelta(days=7)).isoformat() if anchor else None
    value, covered = weighted_at(block.get("pixels", []), band, anchor)
    before, _ = weighted_at(block.get("pixels", []), band, prior)
    delta = round(value - before, 2) if value is not None and before is not None else None
    return {"band": band, "date": anchor, "value": value, "d7_date": prior, "d7": before, "delta_7d": delta,
            "trend": _direction(delta, threshold), "covered_fraction": covered}


def _fmt(value, digits=2):
    return "n.d." if value is None else f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _signed(value, digits=2):
    return "n.d." if value is None else f"{value:+.{digits}f}".rstrip("0").rstrip(".")


def moisture_synthesis(sources: dict) -> dict:
    used, excluded, evidence, signals = [], {}, [], {}
    for key in ("sim", "copernicus_ssm", "copernicus_swi"):
        block = sources.get(key) or {}
        if block.get("status") in USABLE and block.get("source_date"):
            used.append(key)
        else:
            excluded[key] = f"{block.get('status', 'ABSENT')} : {block.get('status_reason', 'source absente')}"
            evidence.append(f"{key} non utilisé comme preuve actuelle ({excluded[key]})")

    surface_up = surface_down = deep_dry = deep_up = False
    if "copernicus_ssm" in used:
        ssm = band_trend(sources["copernicus_ssm"], "SSM", SSM_TREND_PTS)
        signals["copernicus_ssm"] = ssm
        strong = ssm["delta_7d"] is not None and abs(ssm["delta_7d"]) > SSM_STRONG_PTS
        surface_up, surface_down = ssm["trend"] == "rising", ssm["trend"] == "falling"
        word = {"rising": "forte hausse" if strong else "hausse", "falling": "forte baisse" if strong else "baisse",
                "stable": "stable", None: "tendance non calculable (J−7 absent)"}[ssm["trend"]]
        evidence.append(f"Copernicus SSM pondéré ZP1 = {_fmt(ssm['value'])} % au {ssm['date']} (J−7 {_fmt(ssm['d7'])} %, "
                        f"Δ {_signed(ssm['delta_7d'])} pts) : {word} de l'humidité de surface")
    if "copernicus_swi" in used:
        block = sources["copernicus_swi"]
        short = [band_trend(block, b, SWI_TREND_PTS) for b in SHORT_T]
        long_ = [band_trend(block, b, SWI_TREND_PTS) for b in LONG_T]
        short_levels = [t["value"] for t in short if t["value"] is not None]
        long_levels = [t["value"] for t in long_ if t["value"] is not None]
        gap = round(sum(short_levels) / len(short_levels) - sum(long_levels) / len(long_levels), 2) \
            if short_levels and long_levels else None
        rising = sum(t["trend"] == "rising" for t in short)
        falling = sum(t["trend"] == "falling" for t in short)
        long_rising = sum(t["trend"] == "rising" for t in long_)
        signals["copernicus_swi"] = {"short_t": short, "long_t": long_, "short_minus_long_pts": gap}
        surface_up = surface_up or rising >= 2
        surface_down = surface_down or falling >= 2
        deep_dry = deep_dry or (gap is not None and gap >= SHORT_LONG_GAP_PTS)
        deep_up = deep_up or long_rising >= 2
        deltas = [t["delta_7d"] for t in short if t["delta_7d"] is not None]
        evidence.append(f"Copernicus SWI temps courts ({', '.join(SHORT_T)}) : {rising}/{len(SHORT_T)} en hausse, "
                        f"{falling} en baisse sur 7 jours"
                        + (f" (Δ {_signed(min(deltas))} à {_signed(max(deltas))} pts)" if deltas else ""))
        if long_levels:
            evidence.append(f"Copernicus SWI temps longs ({LONG_T[0]}–{LONG_T[-1]}) : {_fmt(min(long_levels))}–{_fmt(max(long_levels))} %, "
                            f"{long_rising}/{len(LONG_T)} en hausse"
                            + (f" ; {_fmt(gap)} pts sous les temps courts du même produit" if gap is not None else ""))
    if "sim" in used:
        sim = sources["sim"]
        grid = next((g for g in sim.get("grids", []) if g["id"] == sim.get("primary_grid")), None)
        if grid and grid.get("derived"):
            swi = grid["derived"]["swi"]
            sswi = grid["latest"]["values"].get("SSWI_10J")
            signals["sim"] = {"grid": grid["id"], "date": swi["latest_date"], "swi": swi["latest"], "swi_d7": swi["d7"],
                              "swi_delta_7d": swi["delta_7d"], "swi_trend": swi["trend"], "sswi_10j": sswi}
            dry = sswi is not None and sswi <= SSWI_DRY
            deep_dry = deep_dry or (dry and swi["trend"] in ("stable", "falling"))
            deep_up = deep_up or swi["trend"] == "rising"
            evidence.append(f"SIM SWI = {_fmt(swi['latest'], 3)} au {swi['latest_date']} (J−7 {_fmt(swi['d7'], 3)}, "
                            f"Δ {_signed(swi['delta_7d'], 3)}) : {({'rising': 'hausse', 'falling': 'baisse', 'stable': 'stable'}).get(swi['trend'], 'tendance inconnue')} sur 7 jours")
            if sswi is not None:
                evidence.append(f"SIM SSWI_10J = {_fmt(sswi, 3)}"
                                + (f" (≤ {SSWI_DRY:g} : plus sec que la normale du modèle)" if dry else ""))

    surface_known = bool({"copernicus_ssm", "copernicus_swi"} & set(used))
    if not surface_known:
        category = "INSUFFICIENT_DATA"  # sans Copernicus, aucune information de surface récente
    elif surface_up and deep_dry:
        category = "SURFACE_REWETTING_DEEP_DRYNESS"
    elif surface_up and deep_up:
        category = "GENERAL_REWETTING"
    elif surface_down and not deep_up:
        category = "DRYING"
    elif not (surface_up or surface_down or deep_up):
        category = "STABLE"
    else:
        category = "MIXED"
    missing = [k for k in ("sim", "copernicus_ssm", "copernicus_swi") if k not in used]
    return {
        "category": category, "label": LABELS[category],
        "completeness": "COMPLETE" if not missing else ("NONE" if not used else "PARTIAL"),
        "sources_used": used, "sources_excluded": excluded, "evidence": evidence, "signals": signals,
        "method": (f"Tendances 7 j propres à chaque source : SSM Δ > {SSM_TREND_PTS:g} pts ; SWI Copernicus par bande "
                   f"Δ > {SWI_TREND_PTS:g} pts (temps courts {', '.join(SHORT_T)} majoritaires) ; écart temps courts − longs "
                   f"≥ {SHORT_LONG_GAP_PTS:g} pts dans le même produit = fond en retard ; SIM : tendance SWI et "
                   f"SSWI_10J ≤ {SSWI_DRY:g}. Valeurs Copernicus pondérées par la part de ZP1 de chaque pixel."),
        "limits": ("Synthèse qualitative : aucun niveau comparé entre SIM et Copernicus, T n'est pas une profondeur, "
                   "aucune probabilité. Le contrôle du sol sur place reste supérieur."),
    }
