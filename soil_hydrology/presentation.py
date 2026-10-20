"""Page « Sol / Hydrologie » de Rain, rendue depuis le JSON compact (Plotly, comme
precip_visualize.py). Aucune donnée n'est recalculée ici : la page affiche le JSON."""
from __future__ import annotations

from html import escape
import json

# Palette catégorielle (ordre fixe, validée CVD clair/sombre) et statuts.
SERIES = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"], "dark": ["#3987e5", "#d95926", "#199e70", "#c98500"]}
STATUS = {"FRESH": ("good", "✓", "À jour"), "STALE": ("warning", "⚠", "Périmée"), "PARTIAL": ("serious", "◐", "Partielle"),
          "UNAVAILABLE": ("critical", "✕", "Indisponible"), "UNAVAILABLE_AUTH": ("critical", "🔒", "Identifiants requis"),
          "ERROR": ("critical", "✕", "Erreur"), "DISABLED": ("neutral", "–", "Non activée")}
LABELS = {"sim": "Météo-France SIM (SAFRAN-ISBA, 8 km)", "copernicus_swi": "Copernicus SWI 1 km",
          "copernicus_ssm": "Copernicus SSM 1 km (surface)", "era5_land": "ERA5-Land (réanalyse ~9 km)",
          "rain_precipitation": "Pluie Rain (COMÉPHORE 1 km / LAME_D_EAU 500 m)"}
TREND = {"rising": "↗ hausse", "falling": "↘ baisse", "stable": "→ stable", None: "tendance inconnue"}


def esc(value) -> str:
    return escape("—" if value is None else str(value), quote=True)


def num(value, digits=1, unit="") -> str:
    if value is None:
        return "<span class='na'>n.d.</span>"
    return esc(f"{value:.{digits}f}{(' ' + unit) if unit else ''}")


def badge(status: str) -> str:
    tone, icon, label = STATUS.get(status, ("neutral", "?", status))
    return f"<span class='badge {tone}'><span aria-hidden='true'>{icon}</span> {esc(label)} <code>{esc(status)}</code></span>"


def summary_cards(report) -> str:
    cards = []
    for key, block in report["sources"].items():
        age = f"{block['age_hours']:g} h" if block.get("age_hours") is not None else "—"
        extra = f"<p class='muted'>Repris du JSON du {esc(block['carried_over_from'])}</p>" if block.get("carried_over_from") else ""
        msgs = "".join(f"<li>{esc(m)}</li>" for m in block.get("messages", []))
        cards.append(f"<article><h3>{esc(LABELS.get(key, key))}</h3>{badge(block['status'])}"
                     f"<p>Date des données : <b>{esc(block.get('source_date'))}</b> · âge {esc(age)}"
                     f"{' (seuil ' + str(block['max_age_hours']) + ' h)' if block.get('max_age_hours') else ''}</p>"
                     f"<p class='muted'>{esc(block['status_reason'])}</p>{extra}"
                     f"{'<details><summary>Messages</summary><ul>' + msgs + '</ul></details>' if msgs else ''}</article>")
    return "<div class='cards'>" + "".join(cards) + "</div>"


def sim_section(sim) -> str:
    out = []
    for grid in sim.get("grids", []):
        role = ("contient ZP1 (" + f"{100 * grid['overlap_fraction']:.0f} % de la parcelle)" if grid["role"] == "parcel"
                else f"maille voisine à {grid['distance_m']:g} m de ZP1 — contexte, ne contient pas la parcelle")
        head = (f"<h3>Maille {esc(grid['id'])} <small>LAMBX {grid['lambx_hm']} hm · LAMBY {grid['lamby_hm']} hm · "
                f"centre {grid['lat']:.4f} N, {grid['lon']:.4f} E</small></h3><p class='muted'>{esc(role)}</p>")
        d = grid.get("derived")
        if not d:
            out.append(f"<article class='wide'>{head}<p>Aucune donnée pour cette maille.</p></article>")
            continue
        swi, c = d["swi"], d["cumuls_mm"]
        rows = "".join(f"<tr><th>{esc(n)}</th>" + "".join(f"<td>{num(c[n][w], 1, 'mm')}</td>" for w in ("24h", "72h", "7d", "14d")) + "</tr>"
                       for n in ("PRELIQ", "PRENEI", "PRELIQ+PRENEI"))
        flux = "".join(f"<tr><th>{esc(n)}</th><td>{num(c[n]['7d'], 1, 'mm')}</td></tr>" for n in ("EVAP", "ETP", "PE", "DRAINC", "RUNC"))
        missing = d.get("missing_days") or {}
        out.append(
            f"<article class='wide'>{head}<div class='kpis'>"
            f"<div><span class='muted'>SWI au {esc(d['anchor_date'])}</span><b class='big'>{num(swi['latest'], 3)}</b><span class='muted'>échelle 0–1 publiée</span></div>"
            f"<div><span class='muted'>SWI J−7 ({esc(swi['d7_date'])})</span><b class='big'>{num(swi['d7'], 3)}</b></div>"
            f"<div><span class='muted'>Variation 7 j</span><b class='big'>{num(swi['delta_7d'], 3)}</b><span>{esc(TREND[swi['trend']])}</span></div>"
            f"<div><span class='muted'>SSWI 10 j</span><b class='big'>{num(grid['latest']['values'].get('SSWI_10J'), 2)}</b><span class='muted'>&lt; 0 : plus sec que la normale</span></div>"
            f"</div><div class='tables'><table><caption>Précipitations SIM (journées 06–06 UTC)</caption><thead><tr><th></th><th>24 h</th><th>72 h</th><th>7 j</th><th>14 j</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<table><caption>Bilan 7 jours</caption><tbody>{flux}</tbody></table></div>"
            + (f"<p class='warn'>Cumuls incomplets : {esc(', '.join(missing))}</p>" if missing else "") + "</article>")
    return "".join(out) or "<p>SIM indisponible.</p>"


def comparison_section(comp) -> str:
    rows = "".join(f"<tr><th>{esc(k)}</th><td>{num(w['sim_mm'], 1, 'mm')}</td><td>{num(w['rain_mm'], 1, 'mm')}</td>"
                   f"<td>{num(w['diff_mm'], 1, 'mm')}</td><td>{badge_flag(w['flag'])}</td></tr>" for k, w in comp["windows"].items())
    return (f"<article class='wide'><h3>Pluie SIM vs pluie Rain au {esc(comp['anchor_date'])} {badge_flag(comp['status'])}</h3>"
            f"<p class='muted'>{esc(comp['caveat'])} {esc(comp['method'])}.</p>"
            f"<table><thead><tr><th>Fenêtre</th><th>SIM (PRELIQ+PRENEI)</th><th>Rain</th><th>Écart SIM − Rain</th><th></th></tr></thead><tbody>{rows}</tbody></table></article>")


def badge_flag(flag) -> str:
    tone = {"CONSISTENT": ("good", "✓", "cohérent"), "DIVERGENT": ("serious", "⚠", "divergent"),
            "INCOMPLETE": ("neutral", "…", "incomplet"), "PARTIAL": ("neutral", "◐", "partiel"),
            "INSUFFICIENT": ("neutral", "–", "insuffisant")}.get(flag, ("neutral", "?", flag))
    return f"<span class='badge {tone[0]}'><span aria-hidden='true'>{tone[1]}</span> {esc(tone[2])}</span>"


def raster_section(key, block) -> str:
    pixels = [p for p in block.get("pixels", []) if p.get("latest")]
    if not pixels:
        latest = block.get("details", {}).get("catalog_latest")
        hint = f"Dernier produit catalogué : <b>{esc(latest)}</b>. " if latest else ""
        return (f"<article class='wide'><h3>{esc(LABELS[key])}</h3>{badge(block['status'])}<p>{hint}{esc(block['status_reason'])}</p>"
                "<p class='muted'>Voir docs/soil_hydrology.md § Copernicus pour configurer les identifiants CDSE.</p></article>"
                if key != "era5_land" else f"<article class='wide'><h3>{esc(LABELS[key])}</h3>{badge(block['status'])}<p>{esc(block['status_reason'])}</p></article>")
    out = []
    for p in pixels:
        lat = p["latest"]
        share = f" · {100 * p['overlap_fraction']:.0f} % de ZP1" if p.get("overlap_fraction") is not None else ""
        rows = "".join(
            f"<tr><th>{esc(n)}</th><td>{num(v, 1, block['unit'])}</td><td>{esc(lat['quality'].get(n))}</td>"
            f"<td>{num(lat['metadata'].get(n, {}).get('qflag_pct'), 0, '%')}</td>"
            f"<td>{esc(TREND[p['derived']['trends'][n]['trend']])} ({num(p['derived']['trends'][n]['delta_7d'], 1)})</td></tr>"
            for n, v in lat["values"].items())
        out.append(f"<article class='wide'><h3>{esc(LABELS[key])} — pixel {esc(p['id'])}<small>{share}</small></h3>"
                   f"<p class='muted'>Date satellite : {esc(lat['date'])} · {esc(block.get('band_meaning'))}</p>"
                   f"<table><thead><tr><th>Bande</th><th>Valeur</th><th>Qualité</th><th>QFLAG</th><th>Tendance 7 j</th></tr></thead><tbody>{rows}</tbody></table></article>")
    return "".join(out)


def charts(report) -> list[str]:
    import plotly.graph_objects as go

    figures = []
    sim = report["sources"]["sim"]
    rain = report["sources"]["rain_precipitation"]

    def line_chart(title, ytitle, traces):
        fig = go.Figure()
        for i, (name, x, y) in enumerate(traces):
            fig.add_trace(go.Scatter(x=x, y=y, name=name, mode="lines+markers", connectgaps=False, meta=i,
                                     line=dict(width=2, color=SERIES["light"][i]), marker=dict(size=5),
                                     hovertemplate="%{y}<extra>" + escape(name) + "</extra>"))
            last = next(((a, b) for a, b in zip(reversed(x), reversed(y)) if b is not None), None)
            if last and len(traces) <= 4:
                fig.add_annotation(x=last[0], y=last[1], text=escape(name.split(" (")[0]), showarrow=False,
                                   xanchor="left", xshift=6, font=dict(size=11))
        return title, ytitle, fig

    grids = sim.get("grids", [])
    if grids:
        figures.append(line_chart("SWI SIM (échelle 0–1)", "SWI", [
            (f"Maille {g['id']} ({'ZP1' if g['role'] == 'parcel' else 'voisine'})", g["series"]["date"], g["series"].get("SWI", []))
            for g in grids]))
        primary = next((g for g in grids if g["id"] == sim.get("primary_grid")), grids[0])
        fig = go.Figure()
        fig.add_trace(go.Bar(x=primary["series"]["date"], y=primary["series"].get("PRELIQ+PRENEI", []), meta=0,
                             name=f"SIM maille {primary['id']} (06–06 UTC)", marker_color=SERIES["light"][0],
                             hovertemplate="%{y} mm<extra>SIM</extra>"))
        if rain.get("series", {}).get("date"):
            fig.add_trace(go.Bar(x=rain["series"]["date"], y=rain["series"]["mm"], meta=2, name="Rain COMÉPHORE/ANTILOPE (00–24 UTC)",
                                 marker_color=SERIES["light"][2], customdata=rain["series"]["source"],
                                 hovertemplate="%{y} mm (%{customdata})<extra>Rain</extra>"))
        fig.update_layout(barmode="group", bargap=.25, bargroupgap=.08)
        figures.append(("Pluie quotidienne : SIM vs Rain", "mm", fig))
    for key, wanted in (("copernicus_swi", ("SWI002", "SWI010", "SWI040", "SWI100")), ("copernicus_ssm", ("SSM",)),
                        ("era5_land", ("swvl1", "swvl2", "swvl3", "swvl4"))):
        block = report["sources"][key]
        pixels = [p for p in block.get("pixels", []) if p.get("latest")]
        if not pixels:
            continue
        if len(wanted) == 1:  # une bande : une série par pixel
            traces = [(f"Pixel {p['id']}", p["series"]["date"], p["series"].get(wanted[0], [])) for p in pixels[:4]]
        else:  # plusieurs bandes : pixel principal seulement
            p = pixels[0]
            traces = [(f"{b} ({p['id']})", p["series"]["date"], p["series"][b]) for b in wanted if b in p["series"]]
        figures.append(line_chart(LABELS[key], block["unit"], traces))
    html = []
    for i, (title, ytitle, fig) in enumerate(figures):
        fig.update_layout(template="plotly_white", title=title, yaxis_title=ytitle, height=380, hovermode="x unified",
                          margin=dict(l=55, r=90, t=60, b=40), legend=dict(orientation="h", y=-.18),
                          paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
        html.append(fig.to_html(full_html=False, include_plotlyjs=(i == 0), div_id=f"chart{i}",
                                config={"responsive": True, "displaylogo": False}))
    return html


CSS = """
:root{--surface:#fcfcfb;--page:#f4f5f3;--card:#ffffff;--border:#e3e2dd;--ink:#0b0b0b;--ink2:#52514e;--accent:#2a78d6;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--neutral:#8a8983}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--surface:#1a1a19;--page:#121211;--card:#1a1a19;--border:#383835;--ink:#f0efec;--ink2:#c3c2b7;--accent:#3987e5}}
:root[data-theme="dark"]{--surface:#1a1a19;--page:#121211;--card:#1a1a19;--border:#383835;--ink:#f0efec;--ink2:#c3c2b7;--accent:#3987e5}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.55 system-ui,sans-serif}
main{max-width:1200px;margin:auto;padding:28px 16px}header{border-bottom:2px solid var(--accent);padding-bottom:16px}
h1{font-size:32px;margin:4px 0}h2{margin-top:36px}h3{margin:0 0 6px;font-size:16px}h3 small{font-weight:400;color:var(--ink2);margin-left:6px}
.eyebrow{letter-spacing:3px;color:var(--accent);margin:0}.muted{color:var(--ink2)}.na{color:var(--ink2);font-style:italic}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
article{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin:0 0 14px;min-width:0}
.limits{background:var(--card);border-left:4px solid var(--warning);padding:12px 16px;border-radius:6px}
.badge{display:inline-flex;gap:6px;align-items:center;border-radius:999px;padding:2px 10px;font-size:13px;border:1px solid currentColor}
.badge code{font-size:11px;opacity:.75}.good{color:var(--good)}.warning{color:#a36b00}.serious{color:#c4561f}.critical{color:var(--critical)}.neutral{color:var(--neutral)}
@media (prefers-color-scheme:dark){.warning{color:var(--warning)}.serious{color:var(--serious)}}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:10px 0}.kpis div{display:flex;flex-direction:column}
.big{font-size:26px;font-variant-numeric:tabular-nums}.tables{display:flex;flex-wrap:wrap;gap:16px}
table{border-collapse:collapse;font-variant-numeric:tabular-nums;max-width:100%}caption{text-align:left;color:var(--ink2);padding:4px 0}
th,td{padding:6px 10px;border-bottom:1px solid var(--border);text-align:right}th{text-align:left;font-weight:600}
article{overflow-x:auto}.warn{color:#a36b00}details{margin-top:6px}summary{cursor:pointer;color:var(--accent)}footer{margin:30px 0;color:var(--ink2)}
"""

# Mode sombre des graphiques : bascule vers SERIES["dark"] (palette validée sur la surface sombre).
DARK_JS = """<script>(function(){var m=window.matchMedia('(prefers-color-scheme: dark)');
var dark=%s;function apply(){if(!window.Plotly)return;var on=m.matches;document.querySelectorAll('.plotly-graph-div').forEach(function(g){
Plotly.relayout(g,{'font.color':on?'#f0efec':'#0b0b0b','xaxis.gridcolor':on?'#383835':'#e3e2dd','yaxis.gridcolor':on?'#383835':'#e3e2dd'});
g.data.forEach(function(t,i){if(t.meta!==undefined){var c=on?dark[t.meta]:%s[t.meta];Plotly.restyle(g,t.type==='bar'?{'marker.color':c}:{'line.color':c,'marker.color':c},[i]);}});});}
m.addEventListener('change',apply);window.addEventListener('load',apply);})();</script>"""


def render(report: dict, path) -> None:
    parcel = report["parcel"]
    sources = report["sources"]
    blocks = [
        f"<header><p class='eyebrow'>RAIN · YAVANNOMË</p><h1>Sol / Hydrologie</h1>"
        f"<p>Parcelle {esc(parcel['section'])} {esc(parcel['number'])} — {esc(parcel.get('commune'))} ({esc(parcel['insee'])}), "
        f"{esc(parcel['area_m2'])} m² · centroïde {parcel['centroid']['lat']:.5f} N, {parcel['centroid']['lon']:.5f} E · "
        f"mise à jour {esc(report['generated_at'])}</p></header>",
        f"<p class='limits'>{esc(report['limits'])}</p>",
        "<h2>Résumé des sources</h2>", summary_cards(report),
        "<h2>SIM local</h2>", sim_section(sources["sim"]),
        comparison_section(report["comparisons"]["precipitation_sim_vs_rain"]),
        "<h2>Copernicus</h2>", raster_section("copernicus_ssm", sources["copernicus_ssm"]),
        raster_section("copernicus_swi", sources["copernicus_swi"]),
        "<h2>ERA5-Land</h2>", raster_section("era5_land", sources["era5_land"]),
        "<h2>Historique</h2><p class='muted'>Chaque source garde son unité et son graphique ; aucune moyenne entre sources. "
        "Les trous restent visibles (pas d'interpolation).</p>",
    ]
    blocks.extend(f"<article class='wide'>{chart}</article>" for chart in charts(report))
    blocks.append("<footer>Sources : Météo-France (SIM2, COMÉPHORE, ANTILOPE), Copernicus Land Monitoring Service, "
                  "ECMWF/C3S, IGN (cadastre). Données complètes : hydrologie_yavannome.json.</footer>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<!doctype html><html lang='fr'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
                    f"<title>Rain · Sol / Hydrologie</title><style>{CSS}</style></head><body><main>" + "".join(blocks) + "</main>"
                    + DARK_JS % (json.dumps(SERIES["dark"]), json.dumps(SERIES["light"])) + "</body></html>", encoding="utf-8")
