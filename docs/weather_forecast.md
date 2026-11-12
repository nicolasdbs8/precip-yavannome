# Prévision météo ZP1 — multi-modèles

## Extension AIFS et nuits — 12 novembre 2026

### Produits vérifiés

Documentation officielle et appels réels vérifiés le **12 novembre 2026**, au
centroïde cadastral ZP1, sans compte ni clé supplémentaire :

| Clé JSON | Identifiant Open-Meteo explicite | Endpoint | Produit |
|---|---|---|---|
| `aifs_single` | `ecmwf_aifs025_single` | `https://api.open-meteo.com/v1/forecast` | déterministe, 0,25° (~28 km), 15 jours nominaux |
| `aifs_ens` | `ecmwf_aifs025_ensemble` | `https://ensemble-api.open-meteo.com/v1/ensemble` | 51 membres, 0,25° (~28 km), 15 jours nominaux |

Sources : [documentation ECMWF](https://open-meteo.com/en/docs/ecmwf-api),
[documentation ensembles](https://open-meteo.com/en/docs/ensemble-api),
[identifiants dans le code officiel](https://github.com/open-meteo/open-meteo-website/blob/main/src/routes/en/docs/ensemble-api/options.ts).
L’API propose aussi `ecmwf_aifs_europe_ensemble` sur grille N320 (~31 km) ; ce
produit distinct n’est pas demandé ici. Aucun produit seamless ni best_match.

Les variables **vérifiées et demandées pour cette intégration** sont
`precipitation` (mm, cumul sur l’heure précédente) et `temperature_2m` (°C,
instantanée). Les autres métriques déterministes/rafales AIFS restent `null` :
leur disponibilité n’est pas supposée. La résolution native AIFS est **6 h** ;
Open-Meteo sert une série **interpolée à 1 h**, ce qui ne permet pas de résoudre
un minimum bref de température ou la répartition réelle d’une averse dans ces
six heures. `native_step_hours`, `returned_step_hours`, `temporal_note` et
`experimental` sont publiés dans les métadonnées du modèle.

Les cumuls horaires servis sont additionnés **une seule fois**, sans multiplier
par six. Une comparaison réelle avec `temporal_resolution=hourly_6` a vérifié
11 blocs Single et 561 blocs ENS : écart maximal **0,3 mm par bloc de 6 h**.
L’API arrondit les précipitations à 0,1 mm ; la somme de six valeurs arrondies
peut différer du cumul arrondi. Les tests hors réseau conservent les réponses
des deux résolutions, avec tolérance de 0,35 mm (six arrondis horaires et un
arrondi six-horaire). Ce contrôle ne prétend pas valider les cumuls GRIB natifs
du producteur. L’agrégation locale conserve exactement la somme des valeurs
horaires reçues, avant l’arrondi final à 0,1 mm.

Les appels `meta.json` AIFS ont aussi répondu : `model_run` n’est publié que
si les lectures avant/après sont identiques et plausibles selon les contrôles
existants. Il reste `null` si l’heure de production n’est pas vérifiable.
Chaque modèle conserve son statut de collecte, sa date et sa propre
`freshness` (`FRESH` jusqu’à 12 h, puis `STALE`), recalculée lors du rendu.
`ERROR` est un échec de collecte même si sa tentative est récente.

### Précipitations et consensus

`daily` et `half_days` accueillent les deux nouveaux modèles dans leurs
sections déterministe et ensemble existantes. Les quantiles type 7, probabilités
`≥ 2/5/10 mm` et nombres de membres réutilisent les calculs existants : somme
complète par membre, puis quantiles et proportions parmi les membres valides.
Un membre incomplet est exclu ; zéro membre valide donne `null`, jamais 0 mm.
Les seuils de précipitation restent inclusifs comme dans le module existant.

La section IA affiche les cumuls de chaque modèle, les quantiles, probabilités,
membres valides et l’écart signé AIFS Single − IFS. La dispersion P10–P90 et
les statuts restent visibles, sans produire une moyenne entre modèles.

**AIFS ne vote pas** : il est filtré avant l’appel au calcul de consensus,
y compris dans la liste des modèles en panne. Poids, plafonds et règles de
`confidence.py` sont inchangés ; les tests comparent tous les champs du
consensus avant/après ajout d’AIFS contradictoires ou en erreur. IFS et AIFS
appartiennent à l’ECMWF et ne sont pas supposés statistiquement indépendants.
La fraîcheur globale et la condition de réussite de collecte reposent sur les
modèles conventionnels ; une collecte AIFS ancienne ne périme pas leurs
prévisions, et AIFS seul ne remplace pas les modèles conventionnels en panne.

### Huit nuits et probabilité de gel

`nights[8]` correspond à D=J0…J+7, **D 18 h → D+1 6 h Europe/Paris**.
La dernière nuit finit donc le matin suivant J+7, sans ajouter de journée J+8.
Les requêtes existantes à neuf jours couvrent cette fin de nuit. `DAYS=8` reste
la seule convention de couverture ; le nominal AIFS de 360 h permettra une
extension ultérieure, qui nécessitera aussi une révision du contrat de schéma.

Chaque nuit publie `start_utc`, `end_utc`, `start_local`, `end_local`, sa durée
et son état. Les températures sont instantanées : les échantillons aux deux
bornes sont inclus. Une nuit ordinaire compte 12 intervalles/13 échantillons ;
la nuit du 28 au 29 mars 2026 compte 11 h/12 échantillons, celle du 24 au
25 octobre 13 h/14 échantillons. Les bornes locales sont converties séparément
en UTC ; aucune hypothèse de douze pas fixes.

Pour chaque déterministe : minimum à 2 m et `below_0c`. Pour chaque ensemble
séparément : minimum de **chaque membre sur toute la nuit**, P10/P50/P90 des
minima, nombre de membres complets et fraction dont le minimum est strictement
inférieur à 0 °C. Ce n’est jamais la moyenne des probabilités horaires.
Un échantillon manquant exclut le membre entier. `PARTIAL_MEMBERS` signale un
ensemble incomplet, `INCOMPLETE` l’absence de membre complet, avec probabilité
`null`. La probabilité partielle reste conditionnelle aux membres complets,
avec son dénominateur visible, jamais présentée comme un ensemble complet.

Seuils de **lecture**, configurables dans `weather_forecast/nights.py` et
publiés dans `reading_thresholds` :

- faible : P(gel) < 20 % ;
- incertaine : 20 % ≤ P(gel) < 70 % ;
- élevée : P(gel) ≥ 70 % ;
- données insuffisantes : moins de 80 % des membres attendus complets.

Ces seuils n’ont **aucune validation agronomique** et ne constituent pas la
confiance météorologique HIGH/MEDIUM/LOW. Les prévisions concernent **l’air à
2 m, ni le sol ni les bourgeons**. Le gel des plantes peut différer par nuit
calme et dégagée. Aucune décision automatique de protection n’est produite.
Les cartes signalent les divergences déterministes et détaillent les minima
par modèle, les membres, les bornes et les disponibilités dans des panneaux
dépliables. Les modèles anciens sont explicitement marqués comme tels.

### Compatibilité, automatisation et contrôle

Le schéma garde `schema_version: 1` avec des ajouts optionnels : `nights`,
nouvelles clés de modèles et nouvelles métadonnées. Les anciens JSON restent
valides ; leur page indique qu’une collecte est nécessaire pour obtenir les
nuits. Les lecteurs du dépôt ont été adaptés. Un lecteur externe qui énumère
strictement les anciens modèles doit accepter les clés supplémentaires et
consulter `experimental` ; un ancien validateur doit charger le nouveau schéma.
Les champs et conventions des journées/demi-journées existantes restent présents.

Le workflow `forecast_daily.yml` est réutilisé **sans changement de cadence,
de secrets ni d’infrastructure** : il collecte automatiquement les dix modèles,
valide et exporte le JSON, puis génère la page en artifact. Une panne AIFS
renvoie un résultat `ERROR` isolé, sans faire échouer la collecte conventionnelle.
Les modules pluie, hydrologie et tâches externes ne sont pas modifiés.

```powershell
python -m unittest discover -s tests
python forecast_update.py --refresh
python forecast_update.py --validate
python forecast_update.py --render-only
# Intégration réelle facultative, distincte des tests hors réseau :
$env:RAIN_NETWORK_TESTS='1'
python -m unittest discover -s tests -p 'test_forecast_network_smoke.py' -v
Remove-Item Env:RAIN_NETWORK_TESTS
```

Dans les logs et l’artifact du workflow, vérifier `aifs_single` / `aifs_ens`,
`fetched_at`, `freshness`, `model_run_note`, le nombre de membres et les huit
nuits. Un statut partiel doit se lire avec sa raison : lors de la validation
du 12 novembre, IFS ENS ne couvrait que jusqu’au 18 novembre à 06 h UTC ;
les périodes suivantes sont restées manquantes. La réussite du workflow
n’implique donc pas que tous les modèles couvrent toutes les échéances.

Exemple de **prévisions réellement collectées le 12 novembre 2026 à 16:24 UTC** :
pour le 15 novembre, AIFS Single 9,3 mm, AIFS ENS P50 6,3 mm [P10 3,7 ; P90
10,8], 51 membres valides, P(≥2/5/10 mm) 94,1 / 72,5 / 19,6 %. Pour la nuit
du 13 au 14 novembre : P(gel) IFS ENS 90,2 %, ICON-EU-EPS 60,0 %, AIFS ENS
58,8 % ; minimum AIFS Single −0,7 °C. Ces valeurs datées sont des prévisions,
pas des observations ni une évaluation de performance.

Après intégration de ces changements sur GitHub par le propriétaire, aucune
action manuelle récurrente n’est nécessaire. Aucun push n’a été effectué lors
de l’implémentation locale.

---

Module de prévision ponctuelle au point de la parcelle **ZP1** (terrain
Yavannomë, Commenailles). Documentation vérifiée le 21 octobre 2026.

> **Ce module décrit la météo prévue et son incertitude. Il ne dit pas si le
> sol est travaillable.** La décision tarière / plantation croise
> `forecast_zp1.json`, `hydrologie_yavannome.json`, les observations terrain,
> l'agenda et les échéances de plants. Aucun GO / WAIT / NO-GO n'est codé ici.

## En bref

```bash
python forecast_update.py                 # collecte Open-Meteo (cache 2 h) → forecast_zp1.json + forecast_zp1.html
python forecast_update.py --refresh       # ignore le cache
python forecast_update.py --offline       # cache uniquement, aucun réseau
python forecast_update.py --render-only   # page HTML depuis le JSON, sans réseau
python forecast_update.py --validate      # schéma + invariants ; affiche la fraîcheur à l'instant
python update_local.py                    # git pull + graphiques pluie + Sol/Hydrologie + Prévision
```

| Fichier | Rôle | Versionné |
|---|---|---|
| `forecast_zp1.json` | prévision J0–J+7, `schema_version: 1` (≈ 170 ko) | oui, mis à jour chaque jour par l'Action |
| `forecast_zp1.html` | page locale | non (`*.html` ignoré) ; aussi dans l'artifact du workflow |
| `.cache/weather_forecast.sqlite` | cache des réponses (TTL 2 h) | non |
| `weather_forecast/schema/forecast.schema.json` | contrat JSON Schema 2020-12 | oui |

Dépendances : `pip install -r requirements-forecast.txt` (`requests`,
`jsonschema`, `tzdata` — ce dernier est indispensable à `zoneinfo` sous Windows).

Code retour : `0` OK (même si certains modèles sont en erreur) ; `1` aucun
modèle exploitable (le JSON précédent est conservé, voir *Fraîcheur*) ; `3`
JSON non conforme au schéma (il n'est alors pas écrit).

## Point de référence

Centroïde cadastral déjà utilisé par le module hydrologie, sans géocodage :
`46.8039042 N, 5.450162 E`, parcelle `39160000ZP0001`, fuseau `Europe/Paris`.
Open-Meteo renvoie la maille la plus proche de chaque modèle ; sa position et sa
distance au point sont publiées dans `providers.*.grid_point` (21/10/2026 :
0,4 km AROME / AROME HD, 1,4 km ICON-EU, 3,8 km ARPEGE, 4,9 km ICON-EU-EPS,
5,8 km IFS et IFS ENS, 7,1 km ICON Global).

## Pourquoi plusieurs modèles

Un seul modèle donne une seule trajectoire possible, sans mesure de sa fiabilité.
Plusieurs modèles indépendants (Météo-France, ECMWF, DWD) et deux ensembles
permettent de savoir si la pluie annoncée est **robuste** (tous d'accord) ou
**incertaine** (désaccord). Cette divergence est une information : elle est
publiée telle quelle.

Chaque modèle est demandé **explicitement** (`models=<identifiant>`) et
conservé séparément. `best_match`, `*_seamless`, une ville voisine ou une
prévision départementale ne sont jamais utilisés (un test le garantit).

## Modèles et horizons

Identifiants vérifiés dans le code de la documentation Open-Meteo
(`open-meteo-website/src/routes/en/docs/*/options.ts`) et par appels réels.
Horizons observés lors de la collecte du 21/10/2026 à 10:01 UTC.

| Clé | Modèle | `model_id` | Résolution | Horizon nominal | Observé | Rôle |
|---|---|---|---|---|---|---|
| `arome_hd` | AROME France HD | `meteofrance_arome_france_hd` | 1,5 km | 48 h | 44 h | très court terme J0–J2 |
| `arome` | AROME France | `meteofrance_arome_france` | 2,5 km | 48 h | 44 h | très court terme J0–J2 |
| `arpege` | ARPEGE Europe | `meteofrance_arpege_europe` | 0,1° (~11 km) | 96 h | 92 h | court terme J0–J4 |
| `ecmwf` | IFS HRES 9 km (O1280) | `ecmwf_ifs` | 9 km | 15 j | > J+8 | contrôle indépendant J0–J7 |
| `icon` | ICON-EU | `dwd_icon_eu` | ~7 km | 120 h | 110 h | contrôle indépendant J0–J5 |
| `icon_global` | ICON Global | `dwd_icon_global` | ~13 km | 180 h | 170 h | **relais** d'ICON-EU au-delà de son horizon |
| `ecmwf_ens` | IFS ENS 9 km Europe, 51 membres | `ecmwf_ifs_europe_ensemble` | 9 km | 15 j | > J+8 | ensemble principal |
| `icon_eu_eps` | ICON-EU-EPS, 40 membres | `dwd_icon_eu_eps_native` | ~6,5 km* | 120 h | 116 h | second ensemble J0–J5 |

\* résolution indiquée par la documentation Open-Meteo.

Notes de collecte :

- **AROME HD** ne fournit ni `rain`, ni `snowfall`, ni `et0_fao_evapotranspiration`,
  ni `surface_temperature` (séries nulles) : c'est déclaré dans
  `weather_forecast/specs.py` et le modèle reste `FRESH`. `surface_temperature`
  n'existe pas non plus pour AROME et ARPEGE. Une variable **inattendue** manquante
  rend le modèle `PARTIAL`.
- **ICON Global** est le modèle pilote d'ICON-EU (même système) : publié
  séparément, il **ne vote que lorsque ICON-EU ne couvre pas la période**, ce qui
  est écrit dans les raisons du consensus (`icon_global relaie icon`). Sans lui,
  J+5–J+7 n'auraient qu'un seul modèle déterministe.
- **ICON-EU-EPS** : variante « native » (grille icosaédrique d'origine). Elle
  renvoie la même maille que `dwd_icon_eu_eps`, mais le `meta.json` de ce dernier
  annonçait un run du 06/10/2026 alors que les données étaient du jour.
- **IFS ENS 9 km** ne sert pas d'heures passées : la journée J0 (commencée à
  minuit) est incomplète et publiée `INCOMPLETE` (valeurs `null`).
- Au-delà de 90 h, IFS passe à des pas tri- puis hexa-horaires ; la répartition
  horaire est celle d'Open-Meteo, les demi-journées lointaines sont approximatives.
- Hors horizon, un modèle est `OUT_OF_HORIZON` (fin d'horizon dans la période :
  `PARTIAL_HORIZON`) et ses valeurs sont `null` : rien n'est extrapolé.

### Heure du run (`model_run`)

Lue dans `https://{api|ensemble-api}.open-meteo.com/data/<meta_id>/static/meta.json`
(`last_run_initialisation_time`) **avant et après** la requête ; publiée seulement si
les deux lectures sont identiques et datent de moins de 36 h. Sinon `null` avec
la raison dans `model_run_note`. Aucun `meta.json` n'existe pour IFS ENS 9 km :
son run n'est pas publié plutôt que deviné.

## Déterministe et ensemble

- **Déterministe** : une seule simulation par modèle, à la meilleure résolution.
  Elle dit *ce que prévoit ce modèle*, pas avec quelle probabilité.
- **Ensemble** : 51 (IFS) ou 40 (ICON-EU-EPS) simulations légèrement perturbées.
  La répartition des membres mesure l'incertitude.
- Les deux systèmes d'ensemble ne sont **jamais mélangés** (pas de pseudo-ensemble
  de 91 membres) : chacun garde ses membres valides, ses quantiles, ses
  probabilités et sa provenance.

### Lire p10 / p50 / p90

Pour le cumul d'une période, triés sur les membres valides (interpolation
linéaire, type 7 de Hyndman & Fan, comme numpy/R) :

- `median_mm` (p50) : la moitié des membres prévoit moins, l'autre moitié plus ;
- `p10_mm` : 10 % des membres prévoient moins (scénario plutôt sec) ;
- `p90_mm` : 10 % des membres prévoient plus (scénario plutôt humide).

`prob_ge_2mm = 0.82` : 82 % des membres valides prévoient au moins 2 mm. Le
dénominateur est toujours `members_valid` (un membre `null` est exclu, jamais
compté à 0) ; zéro membre valide → `null`. Gel : proportion de membres dont le
minimum de la période est **< 0 °C**. Rafales : ≥ 40 et ≥ 60 km/h.

## Pas de moyenne inter-modèles

Aucune valeur « moyenne des modèles » n'est publiée (une telle clé est même
refusée par la validation) : une moyenne entre 0 mm et 10 mm produirait 5 mm,
un scénario qu'aucun modèle ne prévoit. Le JSON donne à la place :
la valeur de **chaque** modèle, le min–max déterministe, et des métriques de
consensus explicites.

## Temps : UTC et Europe/Paris

- Requêtes en `timezone=GMT`, `timeformat=unixtime` ; horodatages stockés en UTC.
- Convention Open-Meteo : un cumul ou une rafale horodaté T couvre ]T − 1 h, T].
  Une période locale [début, fin[ retient donc les pas T tels que
  `début < T ≤ fin`, bornes converties avec `zoneinfo.ZoneInfo("Europe/Paris")`.
- Une journée compte **23, 24 ou 25 pas** : le 25/10/2026 (passage à l'heure
  d'hiver) en compte 25 (testé, y compris sur les données réelles).
- Demi-journées : `MORNING` 06:00–12:00 et `AFTERNOON` 12:00–18:00 heure de Paris
  (06, 12 et 18 h ne sont jamais ambiguës : les changements d'heure ont lieu
  entre 02 et 03 h).
- Une valeur n'est publiée que si **tous** les pas de la période sont présents.

## Contenu de `forecast_zp1.json`

```text
schema_version, generated_at, status (FRESH|STALE), age_hours, max_age_hours=12,
fresh_until, data_fetched_at, carried_over_from, last_attempt{at,outcome,reason},
point, attribution, conventions (règles, seuils, unités),
providers.deterministic.<clé> / providers.ensemble.<clé> :
    provider, model_name, model_id, region, nominal_resolution_km, forecast_horizon,
    fetched_at, status (FRESH|PARTIAL|UNAVAILABLE|ERROR), status_reason,
    first_forecast_time, last_forecast_time, model_run, grid_point, …
daily[8]     : J0..J+7 — deterministic.<modèle>, ensemble.<système>, horizon,
               preceding_{24,48,72}h_forecast_precip_mm_by_model, consensus
half_days[16]: idem par créneau + hours_since_last_forecast_rain_ge_0_2mm
limits
```

Par modèle déterministe et par période : `precipitation_mm`, `rain_mm`,
`snowfall_cm` (cm de neige fraîche ; convention Open-Meteo 7 cm ≈ 10 mm d'eau),
`temperature_min_c`, `temperature_max_c`, `wind_gust_max_kmh`,
`wind_speed_max_kmh`, `relative_humidity_mean_pct`, `et0_mm`,
`surface_temperature_min_c`, `availability`, `hours_valid`.

Indicateurs factuels pour raisonner le ressuyage (aucun score de travaillabilité) :

- `preceding_24h/48h/72h_forecast_precip_mm_by_model` : cumul prévu sur les
  24–72 h précédant le début de la journée. Pour les heures déjà passées, ce sont
  des valeurs des runs Open-Meteo antérieurs (`past_days=3`), **pas des mesures** :
  la pluie réellement tombée est dans Rain (ANTILOPE / COMÉPHORE).
- `hours_since_last_forecast_rain_ge_0_2mm` : heures entre la fin du dernier pas
  ≥ 0,2 mm et le début du créneau ; `lower_bound: true` = aucun pas pluvieux dans
  la série disponible, la valeur est un minimum.

Un créneau sans pluie n'est pas pour autant exploitable : les pluies des
24–72 h précédentes et l'état hydrologique comptent.

## Consensus et confiance

Implémentation : `weather_forecast/confidence.py` (seuils en constantes,
recopiés dans `conventions.confidence_rules`).

Catégories du vote (cumul de la période, purement météorologiques — la quantité
de pluie qui rend le terrain travaillable n'est pas calibrée) :

| Catégorie | Cumul |
|---|---|
| `DRY` | < 0,5 mm |
| `LIGHT` | 0,5 à < 2 mm |
| `WET` | 2 à < 10 mm |
| `VERY_WET` | ≥ 10 mm |

Le **scénario** est binaire au seuil de 2 mm (`WET`/`VERY_WET` → `WET`,
`DRY`/`LIGHT` → `DRY`), le même seuil que `prob_ge_2mm` des ensembles.

Le niveau part de `HIGH` ; chaque règle peut seulement le plafonner :

| Règle | Plafond |
|---|---|
| aucun ou un seul modèle déterministe complet | LOW |
| 2 modèles déterministes | MEDIUM |
| accord pondéré < 60 % ou égalité sec/humide | LOW |
| accord pondéré < 75 % | MEDIUM |
| écart max − min ≥ 10 mm et max ≥ 3 × max(min, 1 mm) | LOW |
| écart max − min ≥ 5 mm | MEDIUM |
| un ensemble à P(≥ 2 mm) entre 40 et 60 % | LOW |
| un ensemble contredit nettement le scénario (≥ 70 % contre ≤ 30 %) | LOW |
| un ensemble soutient faiblement le scénario (entre 30 et 70 %) | MEDIUM |
| un seul système d'ensemble, ou aucun | MEDIUM |
| moins de 80 % des membres valides | MEDIUM |
| modèle en erreur alors qu'il aurait dû couvrir l'échéance | MEDIUM |
| échéance ≥ J+6 | MEDIUM |

`HIGH` exige donc ≥ 3 modèles, ≥ 75 % d'accord, les deux ensembles d'accord
(≥ 70 % ou ≤ 30 %), pas de dispersion notable, échéance ≤ J+5.

Poids du vote : AROME HD et AROME comptent 1,5 jusqu'à J+2 (1 ensuite), les
autres 1. À deux, ils pèsent 3 voix contre 3 pour ARPEGE + IFS + ICON : un
désaccord AROME / autres donne une égalité → `LOW`, sans que AROME l'emporte seul.
Un modèle à couverture partielle de la période ne vote pas.

À J+6/J+7, quand ICON-EU-EPS est hors horizon, la confiance plafonne à `MEDIUM`
avec `confidence_cap_reason = "échéance longue : un seul système d'ensemble disponible"`.
Toutes les raisons sont dans `consensus.reasons` / `consensus.reason`.

## Fraîcheur et pannes

- `status` = `FRESH` si `age_hours` ≤ 12 depuis `data_fetched_at` (plus ancienne
  collecte utilisée), sinon `STALE`. Le JSON versionné fige l'âge au moment de sa
  génération : **un lecteur doit recalculer l'âge depuis `data_fetched_at`**
  (`--validate` et `--render-only` le font).
- Un modèle en panne (HTTP 500, réseau, séries incohérentes) passe `ERROR` ; les
  autres sont conservés. Ses valeurs sont `null` (jamais reprises d'un run ancien).
- Si **aucun** modèle n'est exploitable : le JSON précédent est conservé, avec
  `carried_over_from` (date de la prévision d'origine), `last_attempt.outcome =
  "FAILED"` et un statut recalculé (`STALE` au-delà de 12 h). Il n'est jamais
  remplacé par un objet vide ; sans JSON précédent, rien n'est écrit.
- Réseau : session `requests` du dépôt (`soil_hydrology.transport`), 2 réessais
  maximum sur 429/5xx avec backoff, timeout (10 s, 60 s), User-Agent
  `precip-yavannome/1.0`. Séries validées : pas horaire strict, UTC, longueurs
  identiques, valeurs numériques finies, unités attendues.
- Cache SQLite, TTL 2 h : une relance immédiate ne refait pas les appels. En
  `--offline`, l'heure de collecte d'origine est conservée, donc l'âge reste vrai.

## Prévision ≠ observation

| | Prévision (ce module) | Rain |
|---|---|---|
| Nature | simulations numériques | lame d'eau radar (ANTILOPE 500 m) et réanalyse COMÉPHORE 1 km |
| Période | J0 → J+7 | passé → maintenant |
| Usage | anticiper un créneau | savoir ce qui est réellement tombé |

Les mailles de 1,5 à 13 km ne résolvent ni la haie, ni le micro-relief, ni une
averse isolée : un orage peut toucher la parcelle sans apparaître au point, et
inversement. La précision locale reste celle d'un modèle, pas d'une mesure.

## Workflow GitHub

`.github/workflows/forecast_daily.yml` — **« Prévision météo ZP1 — multi-modèles »**

- tous les jours à **15:30 UTC** (17:30 heure d'été, 16:30 heure d'hiver : avant le
  rapport du dimanche 18:30 Europe/Paris), et `workflow_dispatch` (option
  `refresh`) ;
- Python 3.12, `requirements-forecast.txt`, tests `test_forecast*.py` hors réseau,
  génération, validation, page HTML, artifact (JSON + HTML + log, 30 jours) ;
- commit de `forecast_zp1.json` seul, push avec jusqu'à 5 essais et
  `git pull --no-rebase --no-edit origin main` en cas de commit concurrent
  (bot ANTILOPE), groupe de concurrence `forecast-daily` ;
- en cas d'échec total de collecte, le JSON reporté est committé puis le run est
  signalé en échec.

## Tests

```bash
python -m unittest discover -s tests -p "test_forecast*.py"          # hors réseau
RAIN_NETWORK_TESTS=1 python -m unittest discover -s tests -p "test_forecast_network_smoke.py" -v
```

Fixtures : réponses Open-Meteo réelles capturées au point ZP1 le 21/10/2026
(`tests/fixtures/open_meteo/*.json.gz`) et générateur synthétique au format exact
de l'API (`tests/forecast_fixtures.py`).

## Licence et attribution

- Données : [Weather data by Open-Meteo.com](https://open-meteo.com/), licence
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) ; API gratuite sans
  clé, usage personnel non commercial (moins de 10 000 appels/jour ; ce module en
  fait une vingtaine par exécution).
- Producteurs des modèles : **Météo-France** (AROME France HD, AROME France,
  ARPEGE Europe — Licence Ouverte Etalab 2.0), **ECMWF** (IFS HRES 9 km,
  IFS ENS 9 km — données ouvertes CC BY 4.0), **Deutscher Wetterdienst** (ICON-EU,
  ICON Global, ICON-EU-EPS — données ouvertes, « Quelle : Deutscher Wetterdienst »).
- Modifications : séries horaires agrégées par période locale, sans moyenne
  inter-modèles. Ces mentions figurent aussi dans `attribution` du JSON et en pied
  de page HTML.
