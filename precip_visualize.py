"""
precip_visualize.py
=====================

Génère les visualisations du cumul de précipitations sur le terrain
Yavannomë à partir de precip_yavannome.csv (colonnes : date, cumul_mm, source).

Sorties :
  - cumul_mensuel_yavannome.png   : barres empilées du cumul mensuel,
    distinguant visuellement comephore / antilope / pluviometre_site
    (couleur + hachure différente), avec courbe de cumul glissant 12 mois
    en second axe si assez de recul (>= 12 mois de données).
  - cumul_mensuel_yavannome.html  : version interactive (plotly), survol
    pour explorer les valeurs mensuelles et la source.

Le script est conçu pour accueillir sans modification une 3e série
source=pluviometre_site le jour où un pluviomètre physique est installé sur
le terrain, afin de visualiser l'écart de calibration modèle vs mesure réelle.

Usage :
    python precip_visualize.py
    python precip_visualize.py --csv precip_yavannome.csv --out-prefix cumul_mensuel_yavannome
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("visualize")

# Palette et hachures par source — cohérentes entre le PNG (matplotlib) et le HTML (plotly).
SOURCE_STYLE = {
    "comephore":        {"color": "#4C72B0", "hatch": "",   "label": "COMÉPHORE (rejoué)"},
    "antilope":         {"color": "#DD8452", "hatch": "//", "label": "ANTILOPE (temps réel)"},
    "pluviometre_site": {"color": "#55A868", "hatch": "xx", "label": "Pluviomètre terrain (mesuré)"},
}


def load_data(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(
            f"{csv_path} introuvable — exécuter d'abord precip_extract_comephore.py "
            "et/ou precip_daily_update.py pour générer des données."
        )
    df = pd.read_csv(csv_path, parse_dates=["date"])
    if df.empty:
        raise ValueError(f"{csv_path} est vide — aucune donnée à visualiser.")
    unknown = set(df["source"].unique()) - set(SOURCE_STYLE)
    if unknown:
        log.warning("Source(s) inconnue(s) dans le CSV, ignorée(s) du style dédié : %s", unknown)
    return df


def monthly_by_source(df: pd.DataFrame) -> pd.DataFrame:
    """Cumul mensuel par source. En cas de chevauchement de dates entre
    sources sur un même jour (ex: reprise antilope après comephore), on
    garde une seule valeur par (date, source) — le CSV ne devrait de toute
    façon jamais contenir de doublon date+source grâce à la dédup amont."""
    monthly = (
        df.assign(month=df["date"].dt.to_period("M").dt.to_timestamp())
        .groupby(["month", "source"], as_index=False)["cumul_mm"]
        .sum()
    )
    return monthly


def rolling_12m(df: pd.DataFrame) -> pd.Series:
    """Cumul glissant 12 mois, toutes sources confondues, indexé par mois."""
    monthly_total = (
        df.assign(month=df["date"].dt.to_period("M").dt.to_timestamp())
        .groupby("month")["cumul_mm"].sum()
        .asfreq("MS", fill_value=0.0)
    )
    return monthly_total.rolling(window=12, min_periods=12).sum()


def plot_png(monthly: pd.DataFrame, rolling: pd.Series, out_path: Path) -> None:
    import matplotlib.pyplot as plt

    months = sorted(monthly["month"].unique())
    fig, ax1 = plt.subplots(figsize=(max(10, len(months) * 0.25), 6))

    bottoms = {m: 0.0 for m in months}
    for source in ["comephore", "antilope", "pluviometre_site"]:
        sub = monthly[monthly["source"] == source]
        if sub.empty:
            continue
        style = SOURCE_STYLE[source]
        heights = [sub.loc[sub["month"] == m, "cumul_mm"].sum() for m in months]
        bottom_vals = [bottoms[m] for m in months]
        ax1.bar(
            months, heights, bottom=bottom_vals, width=20,
            color=style["color"], hatch=style["hatch"], label=style["label"],
            edgecolor="white", linewidth=0.5,
        )
        for m, h in zip(months, heights):
            bottoms[m] += h

    ax1.set_ylabel("Cumul mensuel (mm)")
    ax1.set_xlabel("Mois")
    ax1.set_title("Cumul de précipitations mensuel — terrain Yavannomë (Commenailles, 39140)")

    has_rolling = rolling.dropna().shape[0] > 0
    if has_rolling:
        ax2 = ax1.twinx()
        ax2.plot(rolling.index, rolling.values, color="black", linewidth=1.8, label="Cumul glissant 12 mois")
        ax2.set_ylabel("Cumul glissant 12 mois (mm)")
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)
    else:
        ax1.legend(loc="upper left", fontsize=8)
        log.info("Moins de 12 mois de données — courbe de cumul glissant 12 mois omise.")

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    log.info("PNG écrit : %s", out_path)


def plot_html(monthly: pd.DataFrame, rolling: pd.Series, out_path: Path) -> None:
    try:
        import plotly.graph_objects as go
    except ImportError:
        log.warning("plotly non installé — version interactive HTML non générée (pip install plotly).")
        return

    fig = go.Figure()
    pattern_map = {"": "", "//": "/", "xx": "x"}  # équivalents plotly des hachures matplotlib

    for source in ["comephore", "antilope", "pluviometre_site"]:
        sub = monthly[monthly["source"] == source].sort_values("month")
        if sub.empty:
            continue
        style = SOURCE_STYLE[source]
        fig.add_trace(go.Bar(
            x=sub["month"], y=sub["cumul_mm"],
            name=style["label"],
            marker=dict(
                color=style["color"],
                pattern_shape=pattern_map.get(style["hatch"], ""),
            ),
            hovertemplate="%{x|%Y-%m}<br>%{y:.1f} mm<br>" + style["label"] + "<extra></extra>",
        ))

    if rolling.dropna().shape[0] > 0:
        fig.add_trace(go.Scatter(
            x=rolling.index, y=rolling.values,
            name="Cumul glissant 12 mois", mode="lines",
            line=dict(color="black", width=2),
            yaxis="y2",
            hovertemplate="%{x|%Y-%m}<br>%{y:.1f} mm (12 mois)<extra></extra>",
        ))
        fig.update_layout(yaxis2=dict(title="Cumul glissant 12 mois (mm)", overlaying="y", side="right"))

    fig.update_layout(
        barmode="stack",
        title="Cumul de précipitations mensuel — terrain Yavannomë (Commenailles, 39140)",
        xaxis_title="Mois",
        yaxis_title="Cumul mensuel (mm)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        template="plotly_white",
    )
    fig.write_html(out_path, include_plotlyjs="cdn")
    log.info("HTML interactif écrit : %s", out_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default="precip_yavannome.csv", type=Path)
    parser.add_argument("--out-prefix", default="cumul_mensuel_yavannome")
    args = parser.parse_args()

    try:
        df = load_data(args.csv)
    except Exception as exc:
        log.error("%s", exc)
        return 1

    monthly = monthly_by_source(df)
    rolling = rolling_12m(df)

    plot_png(monthly, rolling, Path(f"{args.out_prefix}.png"))
    plot_html(monthly, rolling, Path(f"{args.out_prefix}.html"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
