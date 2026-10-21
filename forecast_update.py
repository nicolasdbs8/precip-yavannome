"""Prévision météo multi-modèles au point ZP1 (Open-Meteo) : JSON + page HTML.

Usage :
    python forecast_update.py                 # collecte (cache 2 h) → forecast_zp1.json + forecast_zp1.html
    python forecast_update.py --refresh       # ignore le cache
    python forecast_update.py --offline       # cache uniquement, aucun réseau
    python forecast_update.py --render-only   # régénère la page depuis le JSON
    python forecast_update.py --validate      # valide le JSON existant

Ne produit aucune décision (GO / WAIT / NO-GO) : seulement météo locale,
incertitude, consensus et créneaux. Code retour : 0 OK (y compris modèles
partiellement en erreur) ; 1 si aucun modèle n'est exploitable (le JSON
précédent est conservé, marqué carried_over_from) ; 3 si le JSON viole le schéma.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from soil_hydrology.cache import Cache
from weather_forecast.export import ForecastUnavailable, build_report, carry_over, dumps, refresh_freshness, validate
from weather_forecast.open_meteo import fetch_all, session
from weather_forecast.presentation import SHORT, render
from weather_forecast.specs import ALL_MODELS

ROOT = Path(__file__).parent
log = logging.getLogger("forecast")


def fmt(value, digits=1):
    return "—" if value is None else f"{value:.{digits}f}"


def rows(report: dict) -> list[list[str]]:
    """Tableau compact J0–J7 (journal et résumé GitHub)."""
    out = []
    for d in report["daily"]:
        c, ens = d["consensus"], d["ensemble"]

        def quant(key):
            b = ens.get(key)
            if not b or b["availability"] != "AVAILABLE":
                return "—"
            p = b["precipitation"]
            return f"{fmt(p['median_mm'])} [{fmt(p['p10_mm'])}–{fmt(p['p90_mm'])}]"

        voters = [d["deterministic"][k] for k in c["deterministic_models"]]
        tmin = [b["temperature_min_c"] for b in voters if b["temperature_min_c"] is not None]
        gust = [b["wind_gust_max_kmh"] for b in voters if b["wind_gust_max_kmh"] is not None]
        probs = " / ".join("—" if v is None else f"{v:.0%}" for v in c["ensemble_prob_ge_2mm"].values())
        out.append([d["date"], ",".join(SHORT[k] for k in c["deterministic_models"]) or "—",
                    f"{fmt(c['deterministic_min_mm'])}–{fmt(c['deterministic_max_mm'])}", quant("ecmwf_ens"),
                    quant("icon_eu_eps"), probs,
                    f"{fmt(min(tmin))}–{fmt(max(tmin))}" if tmin else "—",
                    f"{fmt(min(gust), 0)}–{fmt(max(gust), 0)}" if gust else "—", c["confidence"]])
    return out


HEADER = ["date", "modèles dét.", "pluie dét. mm", "IFS ENS p50 [p10–p90]", "ICON-EU-EPS p50 [p10–p90]",
          "P(≥2 mm) IFS/ICON", "Tmin °C", "rafales km/h", "confiance"]


def summarize(report: dict) -> None:
    for group in ("deterministic", "ensemble"):
        for key, b in report["providers"][group].items():
            log.info("%-12s %-28s %-8s run=%s fin=%s — %s", key, b["model_id"], b["status"], b["model_run"],
                     b["last_forecast_time"], b["status_reason"])
    for r in rows(report):
        log.info(" | ".join(r))


def step_summary(report: dict, path: Path) -> None:
    lines = [f"## Prévision ZP1 — {report['generated_at']} ({report['status']})", "",
             "| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    lines += ["| " + " | ".join(r) + " |" for r in rows(report)]
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="Ignore le cache (appels forcés)")
    parser.add_argument("--offline", action="store_true", help="Aucun accès réseau : cache uniquement")
    parser.add_argument("--render-only", action="store_true", help="Page HTML depuis le JSON existant")
    parser.add_argument("--validate", action="store_true", help="Valide le JSON existant")
    parser.add_argument("--json", type=Path, default=ROOT / "forecast_zp1.json")
    parser.add_argument("--html", type=Path, default=ROOT / "forecast_zp1.html")
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("FORECAST_CACHE_PATH") or ROOT / ".cache/weather_forecast.sqlite"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    now = datetime.now(timezone.utc)

    if args.validate or args.render_only:
        report = json.loads(args.json.read_text(encoding="utf-8"))
        errors = validate(report)
        for error in errors:
            log.error("Schéma : %s", error)
        if args.render_only and not errors:
            render(report, args.html, now)
            log.info("Page régénérée : %s", args.html)
        else:
            current = refresh_freshness(report, now) if not errors else report
            log.info("%s : %s ; statut à l'instant %s (âge %s h)", args.json.name,
                     "valide" if not errors else f"{len(errors)} erreur(s)", current["status"], current["age_hours"])
        return 3 if errors else 0

    results = fetch_all(ALL_MODELS, None if args.offline else session(), Cache(args.cache), args.offline, args.refresh)
    now = datetime.now(timezone.utc)  # après la collecte : generated_at ≥ data_fetched_at
    previous = json.loads(args.json.read_text(encoding="utf-8")) if args.json.exists() else None
    status = 0
    try:
        report = build_report(results, now)
    except ForecastUnavailable as exc:
        log.error("%s", exc)
        if previous is None:
            log.error("Aucun JSON précédent : rien n'est écrit.")
            return 1
        report = carry_over(previous, now, str(exc))
        log.warning("Prévision précédente conservée (carried_over_from=%s, statut %s, âge %s h)",
                    report["carried_over_from"], report["status"], report["age_hours"])
        status = 1
    errors = validate(report)
    if errors:
        for error in errors:
            log.error("Schéma : %s", error)
        log.error("JSON non écrit : %d violation(s)", len(errors))
        return 3
    args.json.write_text(dumps(report), encoding="utf-8")
    log.info("JSON écrit : %s (%.0f ko)", args.json, args.json.stat().st_size / 1000)
    summarize(report)
    if not args.no_html:
        render(report, args.html, now)
        log.info("Page : %s", args.html)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        step_summary(report, Path(os.environ["GITHUB_STEP_SUMMARY"]))
    if os.environ.get("GITHUB_ACTIONS"):
        for group in ("deterministic", "ensemble"):
            for key, b in report["providers"][group].items():
                if b["status"] != "FRESH":
                    print(f"::warning title={key}::{b['status']} — {b['status_reason']}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
