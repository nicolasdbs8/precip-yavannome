# Historique de livraison — module Sol / Hydrologie

La documentation de référence est [soil_hydrology.md](soil_hydrology.md).

## 20 octobre 2026 — parcelle ZP1, SIM opérationnel, JSON publié, Action quotidienne

- Géométrie cadastrale officielle de ZP1 (API Carto IGN) conservée en fixture.
- Sélection SIM par **intersection** parcelle / mailles 8 km (au lieu du centre
  le plus proche) : ZP1 entièrement dans `8360_22090`, maille `8360_22010` à
  34 m conservée comme contexte.
- SIM : catalogue data.gouv par identifiant, API tabulaire vérifiée contre la
  version publiée, repli CSV.gz en flux avec ETag/SHA-256 ; `SSWI_10J` ajouté ;
  SWI conservé sur son échelle 0–1 publiée.
- Copernicus CLMS SWI 1 km v2 / SSM 1 km v1 : découverte OData, pixels de la
  parcelle, lecture fenêtrée des COG, décodage officiel ; `UNAVAILABLE_AUTH`
  sans identifiants CDSE (lecture authentifiée non testée faute d'identifiants).
- JSON compact `hydrologie_yavannome.json` + schéma, statuts de fraîcheur,
  comparaison pluie SIM / Rain, page rendue depuis le JSON, intégrée à
  `update_local.py`.
- Action `hydrology_daily.yml` (cron 10:40 UTC + manuel).
- Correction : `Cache.get(ttl=0)` pouvait renvoyer le cache à horloge identique
  (`--refresh` inopérant, test ERA5 instable sous Windows).
- Remplacé : page/export de septembre (`etat_hydrique_yavannome.json` 1 Mo,
  `select_grid` au centre le plus proche). `interpretation.py` n'est plus utilisé.

## 24 septembre 2026 — première version (non publiée)

Paquet `soil_hydrology/` : modèle `Observation`, cache SQLite, provider SIM
(centre de maille le plus proche du point `precip_common.py`), adaptateur
ERA5-Land CDS (testé hors ligne seulement), lecture du CSV pluie Rain,
convergence qualitative des tendances, page HTML Plotly. Copernicus,
CzechGlobe et sondes : interfaces désactivées.
