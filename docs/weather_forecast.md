# Prévision météo ZP1 — multi-modèles

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
