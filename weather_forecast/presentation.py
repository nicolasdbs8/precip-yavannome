"""Page `forecast_zp1.html`, rendue depuis le JSON sans réseau ni recalcul météo.

Elle décrit la météo prévue et son incertitude, pas une décision : aucune
couleur « favorable / défavorable » (pas de vert), seulement l'intensité de
la pluie et le niveau de confiance en tons neutres.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from html import escape

from .export import refresh_freshness

SHORT = {"arome_hd": "AROME HD", "arome": "AROME", "arpege": "ARPEGE", "ecmwf": "IFS", "icon": "ICON-EU", "icon_global": "ICON-G",
         "ecmwf_ens": "IFS ENS", "icon_eu_eps": "ICON-EU-EPS"}
DAYS_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
SLOT_FR = {"MORNING": "matin 06–12 h", "AFTERNOON": "après-midi 12–18 h"}
STATE_FR = {"PAST": "écoulé", "ONGOING": "en cours", "FUTURE": ""}
CONF_FR = {"HIGH": "élevée", "MEDIUM": "moyenne", "LOW": "faible"}
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#deded8;--rain1:#e3edf9;--rain2:#bcd3f0;--rain3:#87b0e3;
--hi:#3b3f8f;--hi-ink:#fff;--md:#c9cbe8;--md-ink:#23264f;--lo:transparent;--lo-ink:#55554f;--warn:#9a5b00;--warn-bg:#fdf0d9}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--ink:#ecece8;--muted:#a3a39c;--line:#3a3a36;
--rain1:#1d2a3b;--rain2:#24406a;--rain3:#2f5f9e;--hi:#9da3ec;--hi-ink:#14163a;--md:#3c4074;--md-ink:#e4e5fb;--lo-ink:#b9b9b1;
--warn:#f0b75a;--warn-bg:#3a2c12}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1280px;margin:0 auto;padding:24px 16px 48px}h1{margin:.2em 0;font-size:1.7rem}h2{margin:1.6em 0 .5em;font-size:1.2rem}
header p,.note,.warn{overflow-wrap:anywhere}.eyebrow{letter-spacing:.12em;font-size:.75rem;color:var(--muted);margin:0}.muted{color:var(--muted)}small{color:var(--muted)}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:10px 14px;font-weight:600}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:7px 9px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}thead th{font-size:.8rem;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--card)}
tbody tr.day th{white-space:nowrap}td.r1{background:var(--rain1)}td.r2{background:var(--rain2)}td.r3{background:var(--rain3)}
.badge{display:inline-block;border-radius:999px;padding:1px 9px;font-size:.8rem;font-weight:600;border:1px solid var(--line)}
.HIGH{background:var(--hi);color:var(--hi-ink);border-color:var(--hi)}.MEDIUM{background:var(--md);color:var(--md-ink);border-color:var(--md)}
.LOW{background:var(--lo);color:var(--lo-ink)}.FRESH{background:var(--md);color:var(--md-ink)}.STALE{background:var(--warn-bg);color:var(--warn)}
.na{color:var(--muted)}.models{font-size:.8rem;color:var(--muted)}.reason{font-size:.8rem;color:var(--muted);max-width:340px}
footer{margin-top:2em;font-size:.8rem;color:var(--muted)}a{color:inherit}
"""


def esc(value) -> str:
    return escape("—" if value is None else str(value), quote=True)


def num(value, digits=1, unit="") -> str:
    if value is None:
        return "<span class='na'>n.d.</span>"
    return esc(f"{value:.{digits}f}{(' ' + unit) if unit else ''}").replace(".", ",")


def pct(value) -> str:
    return "<span class='na'>n.d.</span>" if value is None else f"{round(100 * value)} %"


def day_label(period) -> str:
    d = date.fromisoformat(period["date"])
    state = STATE_FR[period["period_state"]]
    return (f"J+{period['lead_day']} · {DAYS_FR[d.weekday()]} {d:%d/%m}"
            + (f" <small>({state})</small>" if state else ""))


def rain_class(mm) -> str:
    if mm is None or mm < 0.5:
        return ""
    return " class='r1'" if mm < 2 else " class='r2'" if mm < 10 else " class='r3'"


def det_range(period, key, digits=1, unit="") -> str:
    """Plage sur les modèles qui votent (un relais non compté n'élargit pas la plage)."""
    voters = period["consensus"]["deterministic_models"]
    values = [b[key] for k, b in period["deterministic"].items()
              if k in voters and b["availability"] == "AVAILABLE" and b[key] is not None]
    if not values:
        return "<span class='na'>n.d.</span>"
    low, high = min(values), max(values)
    return num(low, digits, unit) if low == high else f"{num(low, digits)} – {num(high, digits, unit)}"


def ens_rain(period, key) -> str:
    block = period["ensemble"].get(key)
    if not block or block["availability"] != "AVAILABLE":
        reason = {"OUT_OF_HORIZON": "hors horizon", "PARTIAL_HORIZON": "fin d'horizon",
                  "INCOMPLETE": "période incomplète"}.get(block["availability"] if block else None, "indisponible")
        return f"<span class='na'>{reason}</span>"
    p = block["precipitation"]
    return (f"<b>{num(p['median_mm'])}</b> <small>[{num(p['p10_mm'])} – {num(p['p90_mm'])}] · "
            f"{p['members_valid']}/{block['members_expected']} m.</small>")


def ens_prob(period, part, key) -> str:
    return " / ".join(pct(period["ensemble"][k][part][key]) if period["ensemble"][k]["availability"] == "AVAILABLE"
                      else "<span class='na'>–</span>" for k in period["ensemble"])


def models_cell(period) -> str:
    c, h = period["consensus"], period["horizon"]
    used = ", ".join(SHORT[k] for k in c["deterministic_models"]) or "aucun"
    per_model = " · ".join(f"{SHORT[k]} {num(b['precipitation_mm'])}"
                           + ("" if k in c["deterministic_models"] else " (relais non compté)")
                           for k, b in period["deterministic"].items() if b["availability"] == "AVAILABLE")
    extra = []
    if h["out_of_horizon"]:
        extra.append("hors horizon : " + ", ".join(SHORT[k] for k in h["out_of_horizon"]))
    if h["partial_horizon"]:
        extra.append("fin d'horizon : " + ", ".join(SHORT[k] for k in h["partial_horizon"]))
    if h["incomplete"]:
        extra.append("période incomplète : " + ", ".join(SHORT[k] for k in h["incomplete"]))
    if h["in_error"]:
        extra.append("en erreur : " + ", ".join(SHORT[k] for k in h["in_error"]))
    return (f"{len(c['deterministic_models'])} dét. <span class='models'>({esc(used)})</span>"
            f"<div class='models'>{per_model}</div>" + "".join(f"<div class='models'>{esc(e)}</div>" for e in extra))


def confidence_cell(period) -> str:
    c = period["consensus"]
    scen = {"DRY": f"sec (< {c['scenario_threshold_mm']:g} mm)", "WET": f"humide (≥ {c['scenario_threshold_mm']:g} mm)",
            "SPLIT": "partagé", None: "—"}[c["majority_scenario"]]
    cap = f"<div class='reason'><b>Plafond :</b> {esc(c['confidence_cap_reason'])}</div>" if c["confidence_cap_reason"] else ""
    return (f"<span class='badge {c['confidence']}'>{CONF_FR[c['confidence']]}</span> <small>{esc(scen)}</small>{cap}"
            f"<details class='reason'><summary>raisons</summary>{esc(c['reason'])}</details>")


def since_rain(period) -> str:
    values = [v for v in period["hours_since_last_forecast_rain_ge_0_2mm"].values() if v["hours"] is not None]
    if not values:
        return "<span class='na'>n.d.</span>"
    text = sorted({f"{'≥ ' if v['lower_bound'] else ''}{v['hours']} h" for v in values}, key=lambda s: int(s.strip("≥ h")))
    return esc(text[0] if len(text) == 1 else f"{text[0]} – {text[-1]}")


HEAD = ("<thead><tr><th>Période</th><th>Modèles déterministes</th><th>Pluie dét. min–max</th><th>IFS ENS p50 [p10–p90]</th>"
        "<th>ICON-EU-EPS p50 [p10–p90]</th><th>P(≥ 2 mm)<br><small>IFS / ICON</small></th><th>Tmin dét.</th>"
        "<th>P(gel)<br><small>IFS / ICON</small></th><th>Rafales dét.</th><th>P(rafale ≥ 40)<br><small>IFS / ICON</small></th>"
        "{extra}<th>Confiance</th></tr></thead>")


def row(period, label, extra="") -> str:
    rain_max = period["consensus"]["deterministic_max_mm"]
    return (f"<tr class='day'><th>{label}</th><td>{models_cell(period)}</td>"
            f"<td{rain_class(rain_max)}>{det_range(period, 'precipitation_mm', 1, 'mm')}</td>"
            f"<td>{ens_rain(period, 'ecmwf_ens')}</td><td>{ens_rain(period, 'icon_eu_eps')}</td>"
            f"<td>{ens_prob(period, 'precipitation', 'prob_ge_2mm')}</td><td>{det_range(period, 'temperature_min_c', 1, '°C')}</td>"
            f"<td>{ens_prob(period, 'temperature', 'prob_temperature_below_0c')}</td>"
            f"<td>{det_range(period, 'wind_gust_max_kmh', 0, 'km/h')}</td><td>{ens_prob(period, 'wind', 'prob_gust_ge_40kmh')}</td>"
            f"{extra}<td>{confidence_cell(period)}</td></tr>")


def providers_table(report) -> str:
    rows = []
    for group in ("deterministic", "ensemble"):
        for key, b in report["providers"][group].items():
            horizon = b["forecast_horizon"]
            members = f" · {b['members_received']}/{b['members_expected']} membres" if group == "ensemble" else ""
            grid = b["grid_point"] or {}
            rows.append(f"<tr><th>{esc(b['model_name'])}<br><small><code>{esc(b['model_id'])}</code></small></th>"
                        f"<td>{esc(b['provider'])}</td><td>{num(b['nominal_resolution_km'], 1, 'km')}</td>"
                        f"<td>{esc(b['model_run'] or 'non publié')}</td>"
                        f"<td>{esc(b['last_forecast_time'])}<br><small>{num(horizon['observed_hours_after_fetch'], 0, 'h')} après collecte "
                        f"(nominal {horizon['nominal_hours']} h) · dernier jour complet {esc(horizon['last_complete_local_day'])}</small></td>"
                        f"<td>{num(grid.get('distance_km'), 1, 'km')}</td>"
                        f"<td><span class='badge'>{esc(b['status'])}</span>{members}<div class='reason'>{esc(b['status_reason'])}</div></td></tr>")
    return ("<div class='scroll'><table><thead><tr><th>Modèle</th><th>Producteur</th><th>Résolution</th><th>Run (UTC)</th>"
            "<th>Dernière échéance (UTC)</th><th>Distance maille</th><th>Statut</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def render(report: dict, path, now: datetime | None = None) -> None:
    report = refresh_freshness(report, now or datetime.now(timezone.utc))
    point, att = report["point"], report["attribution"]
    banner = ""
    if report["status"] != "FRESH":
        banner = f"<p class='warn'>Prévision périmée : âge {report['age_hours']:g} h (&gt; {report['max_age_hours']} h).</p>"
    if report["carried_over_from"]:
        banner += (f"<p class='warn'>Dernière collecte en échec ({esc(report['last_attempt']['reason'])}) : prévision "
                   f"reprise du JSON du {esc(report['carried_over_from'])}.</p>")
    days = "".join(row(d, day_label(d)) for d in report["daily"])
    halves = "".join(row(h, f"{day_label(h)}<br><small>{SLOT_FR[h['slot']]}</small>", f"<td>{since_rain(h)}</td>")
                     for h in report["half_days"])
    blocks = [
        f"<header><p class='eyebrow'>RAIN · YAVANNOMË</p><h1>Prévision météo ZP1 — multi-modèles</h1>"
        f"<p>Parcelle {esc(point['parcel_id'])} ({esc(point.get('commune'))}) · point {point['lat']} N, {point['lon']} E · "
        f"généré le {esc(report['generated_at'])} · données collectées le {esc(report['data_fetched_at'])} · "
        f"<span class='badge {report['status']}'>{esc(report['status'])}</span> âge {report['age_hours']:g} h "
        f"(seuil {report['max_age_hours']} h)</p></header>", banner,
        "<p class='note'>Cette page décrit la météo prévue et son incertitude ; elle ne dit pas si le sol est travaillable. "
        "Les cumuls sont ceux de chaque modèle, <b>sans moyenne entre modèles</b>. Une période sèche ne suffit pas : "
        "tenir compte des pluies des 24–72 h précédentes, de la page Sol / Hydrologie et du contrôle sur place.</p>",
        "<h2>Journées J0 – J+7 (heure de Paris)</h2>",
        f"<div class='scroll'><table>{HEAD.format(extra='')}<tbody>{days}</tbody></table></div>",
        "<h2>Demi-journées</h2>",
        f"<div class='scroll'><table>{HEAD.format(extra='<th>Depuis dernière pluie ≥ 0,2 mm</th>')}<tbody>{halves}</tbody></table></div>",
        "<h2>Modèles</h2>", providers_table(report),
        "<h2>Limites</h2><ul>" + "".join(f"<li>{esc(x)}</li>" for x in report["limits"]) + "</ul>",
        f"<footer><a href='{esc(att['open_meteo']['url'])}'>{esc(att['open_meteo']['text'])}</a> "
        f"({esc(att['open_meteo']['licence'])}). Modèles : "
        + " ; ".join(f"{esc(p['name'])} — {esc(', '.join(p['models']))} ({esc(p['licence'])})" for p in att["producers"])
        + f". {esc(att['changes'])}. Données complètes : forecast_zp1.json.</footer>",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                    f"<title>Rain · Prévision ZP1</title><style>{CSS}</style></head><body><main>"
                    + "".join(blocks) + "</main></body></html>", encoding="utf-8")
