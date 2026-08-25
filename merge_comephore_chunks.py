"""
merge_comephore_chunks.py
============================

Fusionne les CSV produits par les jobs parallèles du workflow GitHub
Actions "Backfill COMEPHORE (parallèle)" (un fichier par bloc d'années)
dans le CSV partagé precip_yavannome.csv, sans dupliquer de dates déjà
présentes (ex: lignes source=antilope déjà écrites par le suivi continu,
ou un chevauchement entre deux blocs).

Usage :
    python merge_comephore_chunks.py <dossier_contenant_les_chunks> <csv_partagé>

Le dossier est parcouru récursivement (structure produite par
actions/download-artifact@v4 : un sous-dossier par artefact).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from precip_common import CSV_COLUMNS


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage : python merge_comephore_chunks.py <dossier_chunks> <csv_partagé>", file=sys.stderr)
        return 1

    chunks_dir = Path(sys.argv[1])
    target_csv = Path(sys.argv[2])

    frames: list[pd.DataFrame] = []

    if target_csv.exists():
        existing = pd.read_csv(target_csv, dtype={"date": str, "source": str})
        if not existing.empty:
            frames.append(existing)

    chunk_files = sorted(chunks_dir.rglob("*.csv"))
    if not chunk_files:
        print(f"Aucun fichier CSV trouvé sous {chunks_dir} — rien à fusionner.", file=sys.stderr)
        return 1

    for chunk_path in chunk_files:
        df = pd.read_csv(chunk_path, dtype={"date": str, "source": str})
        if not df.empty:
            frames.append(df)
        print(f"{chunk_path} : {len(df)} ligne(s)")

    merged = pd.concat(frames, ignore_index=True)
    before = len(merged)
    merged = merged.drop_duplicates(subset=["date", "source"], keep="first")
    merged = merged.sort_values(["date", "source"]).reset_index(drop=True)
    merged = merged[CSV_COLUMNS]
    after = len(merged)

    merged.to_csv(target_csv, index=False, float_format="%.2f")
    print(f"Fusion terminée : {before} lignes brutes -> {after} après déduplication -> {target_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
