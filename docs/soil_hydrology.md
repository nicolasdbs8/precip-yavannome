# Sol / Hydrologie — Rain

Module de suivi hydrologique local de la parcelle **ZP1** (terrain Yavannomë,
Commenailles). Documentation vérifiée le 20 octobre 2026.

> **Une maille SAFRAN de 8 km ou un pixel Copernicus de 1 km ne constitue pas
> une mesure physique effectuée sur la parcelle.** Ces modèles et observations
> servent à suivre la tendance locale. Le contrôle réel du sol sur place reste
> supérieur pour juger sa travaillabilité (tarière, plantation).

## En bref

```bash
python soil_hydrology_update.py                       # SIM + Copernicus + pluie Rain → JSON + page
python soil_hydrology_update.py --provider sim        # SIM seul
python soil_hydrology_update.py --provider copernicus # Copernicus seul
python soil_hydrology_update.py --provider era5       # ERA5-Land (compte CDS)
python soil_hydrology_update.py --refresh             # ignore le cache
python soil_hydrology_update.py --offline             # cache uniquement, aucun réseau
python soil_hydrology_update.py --render-only         # page HTML depuis le JSON, sans réseau
python soil_hydrology_update.py --validate            # valide le JSON (schéma + invariants)
python update_local.py                                # git pull + graphiques pluie + page Sol/Hydrologie
```

Sorties :

| Fichier | Rôle | Versionné |
|---|---|---|
| `hydrologie_yavannome.json` | JSON compact (≈ 50 ko), contrat stable `schema_version: 1` | oui, mis à jour chaque jour par l'Action |
| `etat_hydrique_yavannome.html` | page « Sol / Hydrologie » (Plotly embarqué) | non (`*.html` ignoré) |
| `.cache/soil_hydrology.sqlite` | cache de développement | non |
| `soil_hydrology/data/parcelle_39160000ZP0001.geojson` | géométrie cadastrale officielle (fixture) | oui |

Une source non relancée (`--provider sim` par exemple) est **reprise du JSON
précédent** avec un âge recalculé et le champ `carried_over_from` : elle ne
devient jamais « fraîche » par simple recopie.

Code retour : `0` OK ; `1` si SIM était demandé et n'a fourni **aucune donnée
locale valide** ; `3` si le JSON produit viole le schéma (il n'est alors pas écrit).

## Architecture

Le module étend le paquet `soil_hydrology/` existant (protocole
`fetch(Query) -> ProviderResult`, `Observation` normalisée, cache SQLite,
transport HTTP avec retries) : pas de seconde architecture.

| Fichier | Responsabilité |
|---|---|
| `parcel.py` | ZP1 : API Carto IGN → fixture GeoJSON ; centroïde, emprise, surface |
| `geo.py` | projection (pyproj), aire, centroïde, découpage polygone/rectangle, distance |
| `sim.py` | SIM : catalogue data.gouv, mailles intersectant la parcelle, transports tabulaire / CSV.gz, parsing, normalisation |
| `copernicus.py` | CLMS SWI 1 km v2 et SSM 1 km v1 : découverte OData, pixels de la parcelle, lecture fenêtrée de COG, décodage |
| `era5.py` | ERA5-Land (CDS), inchangé, activé par `--provider era5` ou `--era5` |
| `providers.py` | pluie Rain (`precip_yavannome.csv`, lecture seule), isolation des pannes |
| `freshness.py` | statuts `FRESH`/`STALE`/`PARTIAL`/`UNAVAILABLE`/`UNAVAILABLE_AUTH`/`ERROR`/`DISABLED` |
| `calculations.py` | cumuls sur fenêtre ; une journée absente rend le cumul `null` |
| `comparison.py` | convergence qualitative des tendances d'humidité (un vote par source) |
| `export.py` | construction et validation du JSON compact |
| `schema/hydrologie.schema.json` | schéma JSON (draft 2020-12) |
| `presentation.py` | page HTML depuis le JSON (aucun recalcul) |
| `soil_hydrology_update.py` | CLI (argparse, comme les autres scripts Rain) |

Chaîne : parcelle → maille(s)/pixel(s) → téléchargement ciblé → extraction
locale → `Observation` → JSON compact → page.

## Parcelle ZP1

| Champ | Valeur |
|---|---|
| Source | [API Carto IGN, module cadastre](https://apicarto.ign.fr/api/cadastre/parcelle?code_insee=39160&section=ZP&numero=0001) |
| IDU | `39160000ZP0001` (feature `parcelle.3329810`) |
| Contenance cadastrale | 4 634 m² (surface géométrique Lambert-93 : 4 606,5 m²) |
| Centroïde WGS84 | 46,8038979 N ; 5,4501605 E (calculé en Lambert-93) |
| Emprise WGS84 | 5,44955812 / 46,80292294 → 5,45080225 / 46,80480487 |
| Récupération | 20/10/2026, fixture `soil_hydrology/data/parcelle_39160000ZP0001.geojson` |

Le pipeline lit la fixture ; `--refresh-parcel` réinterroge l'API et signale
toute modification de géométrie. Sans fixture ni API : erreur explicite, aucune
géométrie approchée. Le centroïde coïncide (à 0,1 m près) avec le point
d'extraction historique de `precip_common.py`.

## Météo-France SIM2 (source de référence)

Jeu data.gouv.fr [`6569b27598256cc583c917a7`](https://www.data.gouv.fr/api/1/datasets/6569b27598256cc583c917a7/)
(« Données changement climatique - SIM quotidienne »), accès public sans clé.
Ressources découvertes par l'API : `QUOT_SIM2_latest` (glissant 60 jours,
mis à jour chaque jour vers 08:00 UTC, ~20 Mo gzip / ~90 Mo CSV, ~593 000
lignes) ; archives `QUOT_SIM2_AAAA` au-delà de 60 jours ; grille
`coordonnees_grille_safran_lambert-2-etendu.csv` ; dictionnaire PDF du 31/03/2026.

### Mailles

La grille est « régulière de 8 km », coordonnées « au centre de la maille »
(présentation du modèle, 02/06/2026), Lambert II étendu (EPSG:27572), en hm.
Les ressources officielles (CSV, shapefiles, KML) ne publient **que les
centres** : la maille est donc le carré centre ± 4 km, et tous les centres sont
vérifiés sur le réseau (x ≡ 40, y ≡ 10 mod 80 hm). La parcelle est projetée en
EPSG:27572 et **intersectée** avec les carrés (pas de centre le plus proche) :

| Maille | Centre (publié) | Rôle | Recouvrement | Distance |
|---|---|---|---|---|
| A `8360_22090` | 46,8392 N 5,43194 E | contient ZP1 | 100 % | 0 |
| B `8360_22010` | 46,7673 N 5,42782 E | voisine (contexte) | 0 % | 33,9 m |

ZP1 est **entièrement dans A**, à 28–34 m de la frontière sud (y = 2 205 000 m).
Le résultat est robuste à la transformation : pyproj (Helmert NTF→WGS84,
précision 2 m) donne 33,9 m ; un ajustement affine sur les centres publiés par
Météo-France (décalés de ~42 m vers l'est par rapport à pyproj, résidus 3 m)
donne 27,6 m. Vu cette proximité, B est extraite comme **maille de contexte**
(`role: "adjacent"`, `--neighbor-margin-m`, défaut 100 m ; 0 pour la retirer).
Elle n'est jamais substituée à A. Si une parcelle intersectait deux mailles,
toutes deux auraient `role: "parcel"` et leur fraction de recouvrement.

### Transports (même fichier officiel)

1. **API tabulaire data.gouv** (filtre serveur `LAMBX__exact`/`LAMBY__exact`,
   ~2 requêtes). Utilisée seulement si data.gouv a ingéré **exactement** la
   version publiée : `HEAD` sur le CSV officiel (`Last-Modified`) =
   `analysis:last-modified-at` de la ressource, ingestion terminée, nombre de
   lignes reçues = `meta.total`.
2. **CSV.gz officiel** en flux (lecture ligne à ligne, mémoire bornée), requête
   conditionnelle `If-None-Match`/`If-Modified-Since` (S3 renvoie 304), contrôle
   `Content-Length`, SHA-256 calculé, seules les lignes des mailles retenues.

`--sim-transport auto|tabular|csv`. Le 20/10/2026, les deux voies ont donné
**0 différence sur 2 240 valeurs** (2 mailles × 70 jours). La raison d'un
abandon de la voie tabulaire est conservée dans `provenance[].fallback`.
GéoBretagne WFS et SAFRAN Fairy (RiverLy-INRAE) ne sont pas implémentés :
ce sont d'autres chemins vers les mêmes données, à ajouter comme transports
dans `MeteoFranceSimProvider.rows()` si nécessaire.

### Variables et unités (entête réelle du 20/10/2026)

`LAMBX;LAMBY;DATE;PRENEI;PRELIQ;T;FF;Q;DLI;SSI;HU;EVAP;ETP;PE;SWI;SSWI_10J;DRAINC;RUNC;RESR_NEIGE;RESR_NEIGE6;HTEURNEIGE;HTEURNEIGE6;HTEURNEIGEX;SNOW_FRAC;ECOULEMENT;WG_RACINE;WGI_RACINE;TINF_H;TSUP_H`

| Champ (alias `_Q` accepté) | Signification | Unité publiée | Fenêtre |
|---|---|---|---|
| `SWI` | indice d'humidité des sols | **0–1** (voir ci-dessous) | moyenne 06–06 UTC |
| `SSWI_10J` | indice standardisé de sécheresse d'humidité des sols, 10 jours | sans unité | J−9 → J |
| `WG_RACINE`, `WGI_RACINE` | eau liquide / gelée, couche racinaire | m³/m³ | instant 06 UTC |
| `PRELIQ`, `PRENEI` | pluie / neige (équivalent eau) | mm | ]06 UTC, 06 UTC] |
| `PE` | pluie efficace (peut être négative) | mm | 06–06 UTC |
| `EVAP`, `ETP` | évapotranspiration réelle / potentielle (FAO-56) | mm | 06–06 UTC |
| `DRAINC`, `RUNC` | drainage, ruissellement | mm | 06–06 UTC |
| `T`, `HU` ; `TINF_H` ; `TSUP_H` | température, humidité de l'air | °C, % | 00–00 ; 18–18 ; 06–06 UTC |

**SWI SIM** : le dictionnaire 2026 indique « % », mais les fichiers publient
une échelle 0–1 (ex. 0,102 le 19/10/2026). La valeur est conservée **telle
quelle** (`unit: "1"`), jamais multipliée par 100, et n'est jamais comparée en
niveau au SWI Copernicus (en %, satellite, paramètre T). `PRELIQ+PRENEI` est un
total calculé à l'intérieur de SIM (`null` si un membre manque).

### Dérivés (par maille)

Ancrés sur la dernière date où `SWI` est valide (DATE J = ]J 06 UTC, J+1 06 UTC]) :
SWI J et J−7 (date exacte, jamais interpolée), variation, tendance
(`rising`/`falling` si |Δ| > 0,02, sinon `stable` — convention Rain, pas un
seuil scientifique) ; cumuls `PRELIQ`, `PRENEI`, `PRELIQ+PRENEI` sur 24 h
(= 1 journée SIM), 72 h, 7 j, 14 j ; `EVAP`, `ETP`, `PE`, `DRAINC`, `RUNC` sur
7 j. Une journée absente rend le cumul `null` et est listée dans `missing_days`.

## Pluie Rain vs SIM

La pluie Rain (`precip_yavannome.csv` : COMÉPHORE 1 km, puis LAME_D_EAU 500 m
sous l'étiquette `antilope`) reste **indépendante** et n'est jamais remplacée
par SIM. Par jour, COMÉPHORE prime sur le temps réel ; la journée UTC en cours
est exclue des cumuls. `comparisons.precipitation_sim_vs_rain` compare 7/14/30 j
(maille principale) : `DIVERGENT` si |Δ| > 5 mm **et** > 30 % du plus grand
cumul, `INCOMPLETE` si un jour manque. Les journées diffèrent (SIM 06–06 UTC,
Rain 00–24 UTC) : seuls les cumuls pluri-journaliers ont un sens.

## Copernicus CLMS (pixels 1 km)

| Produit | Collection CDSE | Bandes | Encodage | Date |
|---|---|---|---|---|
| SWI Europe 1 km daily **v2** (ASCAT + Sentinel-1, 2025→) | `clms_swi_europe_1km_daily_v2_cog` | `SWI002…SWI100` (T = 2, 5, 10, 15, 20, 40, 60, 100 j), `QFLAG*`, `SSF` | uint8, % = DN × 0,5 (DN 0–200) | nominale J 12:00 UTC |
| SSM Europe 1 km daily **v1** (Sentinel-1) | `clms_ssm_europe_1km_daily_v1_cog` | `SSM`, `NOISE` | idem, % de saturation | J 00–24 UTC |

Drapeaux (jamais convertis en valeur) : 241 ExceedingMin, 242 ExceedingMax,
251 WaterMask, 252 SensitivityMask, 253 SlopeMask, 254 LowQFLAG, 255 NoData.
Sources : PUM SWI1km I1.50 (08/07/2025, v2.0.1) et PUM SSM1km I1.30 (19/04/2019).
`T` est un temps caractéristique du filtre, **pas une profondeur**. SSM
(premiers centimètres) et SWI restent séparés ; aucune moyenne.

**Accès choisi** : découverte par le **catalogue OData** public
(`catalogue.dataspace.copernicus.eu/odata/v1/Products`) — le STAC CDSE
s'arrêtait au 15/09/2026 alors que l'OData contenait les produits du 19/10.
Lecture fenêtrée des COG par bande (GDAL, requêtes HTTP Range : quelques
dizaines de ko au lieu de ~8,5 Mo par bande et ~76 Mo par produit).

Pixels (grille EPSG:4326, origine −11° / 72°, pas 1/112°) intersectant ZP1 :

| Pixel | Centre | Part de ZP1 | Distance au centroïde |
|---|---|---|---|
| `r2821_c1842` | 46,80804 N 5,45089 E | 68,7 % | 463 m |
| `r2822_c1842` | 46,79911 N 5,45089 E | 31,3 % | 535 m |

### Identifiants (secrets)

Sans identifiants : statut `UNAVAILABLE_AUTH`, SIM continue normalement, la
page indique le dernier produit catalogué. Créer un compte gratuit sur
[dataspace.copernicus.eu](https://dataspace.copernicus.eu/), puis **une** des deux options :

| Variable | Usage |
|---|---|
| `CDSE_S3_ACCESS_KEY`, `CDSE_S3_SECRET_KEY` | **recommandé** : clés S3 générées sur [eodata-s3keysmanager.dataspace.copernicus.eu](https://eodata-s3keysmanager.dataspace.copernicus.eu/) ; lecture `/vsis3/eodata/…` (endpoint `eodata.dataspace.copernicus.eu`) |
| `CDSE_USERNAME`, `CDSE_PASSWORD` | alternative : jeton OIDC (`client_id=cdse-public`) puis `/vsicurl/` sur `download.dataspace.copernicus.eu` |

En local : variables d'environnement (le `.env` n'est pas chargé automatiquement,
comme pour les autres scripts Rain). Dans GitHub : *Settings → Secrets and
variables → Actions*, mêmes noms. Les clés S3 CDSE ont une date d'expiration :
à renouveler comme la clé Météo-France.

**Limite connue** : la lecture authentifiée n'a pas pu être testée (aucun
identifiant disponible lors du développement). Découverte, calcul des pixels,
décodage et lecture fenêtrée sont testés sur des GeoTIFF reproduisant la grille
officielle. La voie S3 suit la configuration GDAL documentée par CDSE ; la voie
OData dépend du support des requêtes Range par `download.dataspace.copernicus.eu`.

## ERA5-Land

Adaptateur existant (`era5.py`) : couches `swvl1..4` (0–7, 7–28, 28–100,
100–289 cm, m³/m³), point 0,1° le plus proche (~9 km), latence ~5 jours.
Activer : `CDSAPI_KEY` (ou `~/.cdsapirc`), licence acceptée sur le portail
CDS, `pip install -r requirements-hydrology.txt`, puis `--provider era5`.
Statut toujours `STALE` (« réanalyse différée, jamais temps réel ») avec son âge
affiché : contrôle indépendant/historique, jamais pilote du diagnostic quotidien.
Non intégré à l'Action quotidienne (validation CDS authentifiée à faire).

## Fraîcheur et statuts

| Source | Fin de validité de la journée J | `STALE` au-delà de |
|---|---|---|
| SIM | J+1 06:00 UTC | 48 h |
| Copernicus SWI | J 12:00 UTC | 72 h |
| Copernicus SSM | J+1 00:00 UTC | 72 h |
| Pluie Rain | J+1 00:00 UTC | 48 h |
| ERA5-Land | J+1 00:00 UTC | toujours `STALE` (âge affiché) |

`PARTIAL` : données récentes mais maille/pixel manquant. `UNAVAILABLE` :
aucune donnée. `UNAVAILABLE_AUTH` : identifiants absents/refusés. `ERROR` :
collecte en échec sans donnée. `DISABLED` : source non activée. Une collecte
en échec avec repli sur le cache garde le statut issu de la **date des
données** et l'indique dans `status_reason` et `messages`.

Garanties : `0` = valeur réelle nulle, `null` = absente (ou drapeau, listé dans
`flags`), source indisponible = statut sans valeurs ; aucun NaN publié ; aucune
moyenne nationale ni inter-sources ; pas de SWI fabriqué.

## Format JSON (extrait)

```json
{"schema_version": 1, "generated_at": "…", "parcel": {"idu": "39160000ZP0001", "area_m2": 4634, "centroid": {…}, "geometry": {…}},
 "limits": "Une maille SAFRAN de 8 km …",
 "sources": {
  "sim": {"status": "FRESH", "source_date": "2026-10-19", "valid_until": "2026-10-20T06:00:00+00:00", "age_hours": 9.8,
          "units": {"SWI": "1", "PRELIQ": "mm", …}, "primary_grid": "8360_22090",
          "grids": [{"id": "8360_22090", "role": "parcel", "overlap_fraction": 1.0,
                     "latest": {"date": "2026-10-19", "values": {"SWI": 0.102, …}},
                     "derived": {"swi": {"latest": 0.102, "d7": 0.098, "delta_7d": 0.004, "trend": "stable"},
                                 "cumuls_mm": {"PRELIQ": {"24h": 1.6, "72h": 5.9, "7d": 6.8, "14d": 29.7}, "EVAP": {"7d": 5.1}, …}},
                     "series": {"date": [… 60 jours …], "SWI": […], "PRELIQ": […], …}}, …],
          "provenance": [{"transport": "tabular", "source_version": "2026-10-20T08:03:26+00:00", "cache_hit": false, …}]},
  "copernicus_swi": {"status": "UNAVAILABLE_AUTH", "pixels": [{"id": "r2821_c1842", "overlap_fraction": 0.6874, …}], …},
  "copernicus_ssm": {…}, "era5_land": {…}, "rain_precipitation": {…}},
 "comparisons": {"precipitation_sim_vs_rain": {…}, "soil_moisture_trends": {…}}}
```

Le schéma complet est `soil_hydrology/schema/hydrologie.schema.json`.

## Cache

SQLite `.cache/soil_hydrology.sqlite` (`HYDRO_CACHE_PATH`). Catalogue SIM 1 h ;
grille SIM 7 jours ; extraction tabulaire indexée par version publiée ;
extraction CSV avec URL, ETag, Last-Modified, taille, SHA-256 et date de
récupération (304 → réutilisée) ; pixels Copernicus par produit/jour (remplacés
si un nouveau run du produit est publié). `--refresh` ignore le cache,
`--offline` n'utilise que lui. Le cache ne masque jamais l'âge : la fraîcheur
est calculée sur la date des données, pas sur la date de téléchargement.

## GitHub Action

[`.github/workflows/hydrology_daily.yml`](../.github/workflows/hydrology_daily.yml) :
quotidien à 10:40 UTC (après la publication SIM ~08:00 et son ingestion
tabulaire ~09:30), déclenchable à la main (`provider`, `refresh`). Étapes :
dépendances minimales → cache SQLite restauré (`actions/cache`) → tests
unitaires hors réseau → mise à jour (échec si SIM sans donnée locale) →
validation du schéma → artifact (JSON + logs, 30 jours) → commit de
`hydrologie_yavannome.json` sur `main` avec réessai. Avertissements Actions
pour `STALE`/`PARTIAL`/`ERROR`/`UNAVAILABLE_AUTH`, résumé dans l'onglet du run.

**Publication** : Rain n'a ni GitHub Pages ni release ; ses données vivent
déjà sur `main`, alimentées par les bots (≈ 288 commits/jour ANTILOPE) et
récupérées par `update_local.py` (`git pull`). Un commit quotidien de ~50 ko
réutilise ce mécanisme sans nouvelle infrastructure. Si un jour la page doit
être consultable en ligne, l'étape suivante serait un déploiement GitHub Pages
de la page générée (non fait).

## Débogage

- `-v` : logs détaillés. Les logs indiquent source, ressource et version,
  transport retenu ou écarté (et pourquoi), lignes lues / retenues, mailles,
  cache HIT/MISS, âge et raison de chaque statut.
- SIM `ERROR` : vérifier le catalogue (`curl https://www.data.gouv.fr/api/1/datasets/6569b27598256cc583c917a7/`),
  forcer le CSV (`--sim-transport csv --refresh`), lire `messages` et `provenance`.
- `SimFormatError` : fichier tronqué/corrompu ou entête changée (colonnes `LAMBX`,
  `LAMBY`, `DATE` requises) ; relancer plus tard, Météo-France republie le fichier.
- Copernicus `UNAVAILABLE_AUTH` : variables `CDSE_*` absentes ou refusées.
- Tests réseau optionnels : `RAIN_NETWORK_TESTS=1 python -m unittest discover -s tests -p "test_network_smoke.py" -v`.
- `git pull` refusé à cause de `hydrologie_yavannome.json` modifié par un
  lancement local : `git checkout -- hydrologie_yavannome.json` (fichier généré).

## Tests

```bash
python -m unittest discover -s tests -v
```

Hors réseau, fixtures réduites issues des vrais formats (`tests/fixtures/` :
lignes du `QUOT_SIM2_latest` du 20/10/2026, réponse de l'API tabulaire, extrait
de la grille officielle, réponse API Carto). Couvre : transformations, mailles
et intersection, pixels Copernicus, lecture `.csv.gz`, entête réelle, filtre,
alias `_Q`, valeurs manquantes, fichier tronqué/corrompu, choix du transport,
304/ETag, repli visible sur cache, cumuls, J−7, fraîcheur, 0 ≠ null, absence de
moyenne inter-sources, décodage DN/drapeaux, lecture fenêtrée de COG, schéma,
page HTML. Les tests ERA5 nécessitent `requirements-hydrology.txt`.

## Limites scientifiques

- Maille SIM 8 km et pixels 1 km : grandeurs régionales/modélisées, pas de
  mesure in situ. Topographie, sol et drainage de la parcelle ne sont pas représentés.
- SIM est une réanalyse SAFRAN-ISBA forcée par des observations et un modèle ;
  Copernicus SWI dérive de radars satellitaires (ASCAT, Sentinel-1) filtrés
  exponentiellement ; SSM ne voit que les premiers centimètres ; ERA5-Land est
  une réanalyse ~9 km. Ces grandeurs ne sont pas interchangeables : on compare
  leurs **tendances** (`comparisons.soil_moisture_trends`), jamais leurs niveaux.
- Unité SWI SIM incohérente entre dictionnaire et fichiers (échelle 0–1 conservée).
- Seuils de tendance (0,02 SWI SIM ; 2 points SWI Copernicus ; 5 points SSM) et
  de divergence de pluie : conventions internes, pas des seuils agronomiques.
- Aucune recommandation de travail du sol n'est calculée : la décision reste
  terrain (test à la bêche/tarière, consistance du sol en profondeur).
