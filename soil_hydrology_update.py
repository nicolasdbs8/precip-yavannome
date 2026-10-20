"""Mise à jour hydrologique de la parcelle ZP1 (Rain) : JSON compact + page HTML.

Usage :
    python soil_hydrology_update.py                      # SIM + Copernicus (+ pluie Rain locale)
    python soil_hydrology_update.py --provider sim       # SIM seul (autres sources reprises du JSON précédent)
    python soil_hydrology_update.py --provider copernicus
    python soil_hydrology_update.py --provider era5      # ERA5-Land (compte CDS requis)
    python soil_hydrology_update.py --refresh            # ignore le cache
    python soil_hydrology_update.py --offline            # cache uniquement, aucun réseau
    python soil_hydrology_update.py --render-only        # régénère la page depuis le JSON
    python soil_hydrology_update.py --validate           # valide le JSON existant

Code retour : 0 OK ; 1 si SIM était demandé et n'a fourni aucune donnée
locale valide ; 3 si le JSON produit ne respecte pas le schéma.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from precip_common import CSV_PATH, TARGET_LAT, TARGET_LON
from soil_hydrology import geo
from soil_hydrology.cache import Cache
from soil_hydrology.copernicus import SSM, SWI, CopernicusProvider, pixels_for_parcel
from soil_hydrology.era5 import Era5LandProvider
from soil_hydrology.export import build_report, dumps, validate
from soil_hydrology.models import ProviderResult, Query
from soil_hydrology.parcel import load_parcel
from soil_hydrology.presentation import render
from soil_hydrology.providers import RainPrecipitationProvider, collect
from soil_hydrology.sim import MeteoFranceSimProvider, parcel_grids
from soil_hydrology.transport import session

ROOT = Path(__file__).parent
log = logging.getLogger("hydrology")


def cds_configured() -> bool:
    return bool(os.environ.get("CDSAPI_KEY")) or (Path.home() / ".cdsapirc").exists()


def summarize(report: dict) -> None:
    for name, block in report["sources"].items():
        log.info("%-18s %-16s date=%s âge=%s h — %s", name, block["status"], block["source_date"],
                 block["age_hours"], block["status_reason"])
    sim = report["sources"]["sim"]
    for grid in sim.get("grids", []):
        derived = grid.get("derived")
        if not derived:
            log.warning("SIM maille %s : aucune donnée", grid["id"])
            continue
        swi, c = derived["swi"], derived["cumuls_mm"]
        log.info("SIM %s (%s) %s : SWI %s (J−7 %s, Δ %s, %s) ; PRELIQ 24h/72h/7j/14j = %s/%s/%s/%s mm ; "
                 "EVAP 7j %s ; PE 7j %s ; DRAINC 7j %s ; RUNC 7j %s mm", grid["id"], grid["role"], derived["anchor_date"],
                 swi["latest"], swi["d7"], swi["delta_7d"], swi["trend"], *c["PRELIQ"].values(),
                 c["EVAP"]["7d"], c["PE"]["7d"], c["DRAINC"]["7d"], c["RUNC"]["7d"])
    comparison = report["comparisons"]["precipitation_sim_vs_rain"]
    log.info("Pluie SIM vs Rain (au %s) : %s %s", comparison["anchor_date"], comparison["status"],
             {k: (w["sim_mm"], w["rain_mm"], w["flag"]) for k, w in comparison["windows"].items()})


def step_summary(report: dict, path: Path) -> None:
    """Résumé Markdown pour l'onglet Actions (GITHUB_STEP_SUMMARY)."""
    lines = [f"## Hydrologie ZP1 — {report['generated_at']}", "", "| Source | Statut | Date | Âge (h) | Raison |", "|---|---|---|---|---|"]
    lines += [f"| {k} | {b['status']} | {b['source_date'] or '—'} | {b['age_hours'] if b['age_hours'] is not None else '—'} | {b['status_reason']} |"
              for k, b in report["sources"].items()]
    for grid in report["sources"]["sim"].get("grids", []):
        d = grid.get("derived")
        if d:
            c = d["cumuls_mm"]
            lines += ["", f"**SIM {grid['id']}** ({grid['role']}, {d['anchor_date']}) : SWI {d['swi']['latest']} "
                      f"(J−7 {d['swi']['d7']}, Δ {d['swi']['delta_7d']}, {d['swi']['trend']}) · pluie 24 h/72 h/7 j "
                      f"{c['PRELIQ']['24h']}/{c['PRELIQ']['72h']}/{c['PRELIQ']['7d']} mm · EVAP 7 j {c['EVAP']['7d']} · "
                      f"DRAINC 7 j {c['DRAINC']['7d']} · RUNC 7 j {c['RUNC']['7d']} mm"]
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=["all", "sim", "copernicus", "era5"], default="all")
    parser.add_argument("--days", type=int, default=60, help="Historique conservé (jours, défaut 60)")
    parser.add_argument("--refresh", action="store_true", help="Ignore le cache (téléchargements forcés)")
    parser.add_argument("--offline", action="store_true", help="Aucun accès réseau : cache uniquement")
    parser.add_argument("--refresh-parcel", action="store_true", help="Réinterroge l'API Carto et met à jour la fixture")
    parser.add_argument("--sim-transport", choices=["auto", "tabular", "csv"], default="auto")
    parser.add_argument("--neighbor-margin-m", type=float, default=100,
                        help="Mailles SIM voisines conservées comme contexte si à moins de N m (0 : aucune)")
    parser.add_argument("--era5", action="store_true", help="Inclut ERA5-Land dans --provider all")
    parser.add_argument("--json", type=Path, default=ROOT / "hydrologie_yavannome.json")
    parser.add_argument("--html", type=Path, default=ROOT / "etat_hydrique_yavannome.html")
    parser.add_argument("--no-html", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("HYDRO_CACHE_PATH") or ROOT / ".cache/soil_hydrology.sqlite"))
    parser.add_argument("--csv", type=Path, default=CSV_PATH, help="CSV de précipitations Rain")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.days < 15:
        parser.error("--days doit être ≥ 15 (cumuls 14 jours et tendance J−7)")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("rasterio").setLevel(logging.WARNING)

    if args.validate or args.render_only:
        report = json.loads(args.json.read_text(encoding="utf-8"))
        errors = validate(report)
        for error in errors:
            log.error("Schéma : %s", error)
        if args.render_only and not errors:
            render(report, args.html)
            log.info("Page régénérée : %s", args.html)
        else:
            log.info("%s : %s", args.json.name, "valide" if not errors else f"{len(errors)} erreur(s)")
        return 3 if errors else 0

    now = datetime.now(timezone.utc)
    http, cache = session(), Cache(args.cache)
    parcel = load_parcel(http=None if args.offline else http, refresh=args.refresh_parcel)
    lon, lat = parcel.centroid_wgs84
    # J (aujourd'hui) n'est jamais publié : `days` journées de données jusqu'à J−1.
    query = Query(lat, lon, now.date() - timedelta(days=args.days), now.date())
    run = {"all": {"sim", "copernicus"} | ({"era5"} if args.era5 else set()), "sim": {"sim"},
           "copernicus": {"copernicus"}, "era5": {"era5"}}[args.provider]
    results = {}

    if "sim" in run:
        try:
            grids = parcel_grids(http, cache, parcel.polygons(geo.LAMBERT2E), args.offline, args.refresh, args.neighbor_margin_m)
            results["sim"] = collect([MeteoFranceSimProvider(cache, grids, args.offline, args.refresh, args.sim_transport, http)], query)[0]
            results["sim"].details.setdefault("grids", [g.to_dict() for g in grids])
        except Exception as exc:  # grille introuvable : erreur explicite, jamais de maille supposée
            log.error("SIM : %s : %s", type(exc).__name__, exc)
            results["sim"] = ProviderResult("Météo-France SIM", "temporary_error", messages=[f"{type(exc).__name__} : {exc}"])
    if "copernicus" in run:
        pixels = pixels_for_parcel(parcel.polygons_wgs84, (lon, lat))
        for product in (SWI, SSM):
            results[product.key] = collect([CopernicusProvider(product, cache, pixels, args.offline, args.refresh, http)], query)[0]
    if "era5" in run:
        if cds_configured() or args.offline:
            results["era5_land"] = collect([Era5LandProvider(cache, True, args.offline, args.refresh)], query)[0]
        else:
            results["era5_land"] = ProviderResult("ERA5-Land", "unavailable_auth", messages=[
                "CDSAPI_KEY (ou ~/.cdsapirc) absent : compte CDS et licence ERA5-Land requis."])
    # Pluie Rain : CSV local, toujours relu (indépendant de SIM, jamais remplacé par lui).
    results["rain_precipitation"] = collect([RainPrecipitationProvider(args.csv)],
                                            Query(TARGET_LAT, TARGET_LON, query.start, query.end))[0]

    previous = json.loads(args.json.read_text(encoding="utf-8")) if args.json.exists() else None
    report = build_report(parcel.summary(), results, now, previous)
    errors = validate(report)
    if errors:
        for error in errors:
            log.error("Schéma : %s", error)
        log.error("JSON non écrit : %d violation(s) du schéma", len(errors))
        return 3
    args.json.write_text(dumps(report), encoding="utf-8")
    log.info("JSON écrit : %s (%.1f ko)", args.json, args.json.stat().st_size / 1000)
    summarize(report)
    if not args.no_html:
        render(report, args.html)
        log.info("Page : %s", args.html)

    sim = report["sources"]["sim"]
    if "sim" in run and sim["source_date"] is None:
        log.error("SIM n'a fourni aucune donnée locale valide (%s)", sim["status"])
        return 1
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        step_summary(report, Path(os.environ["GITHUB_STEP_SUMMARY"]))
    if os.environ.get("GITHUB_ACTIONS"):
        for name, block in report["sources"].items():
            if block["status"] in ("STALE", "PARTIAL", "ERROR", "UNAVAILABLE_AUTH"):
                print(f"::warning title={name}::{block['status']} — {block['status_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
