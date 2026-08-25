# Suivi des précipitations — terrain Yavannomë (Commenailles, 39140)

Reconstitution d'un historique fiable de cumul de précipitations sur le
terrain agroforestier de 4600 m² (Commenailles, Jura), en croisant deux
produits Météo-France plus fiables que le radar grand public (Météociel) :

- **COMÉPHORE** : réanalyse horaire radar+pluvio, 1 km, depuis 1997, mise à
  jour avec ~2 mois de retard → reconstitue l'historique.
- **ANTILOPE** : produit radar+pluvio temps réel/J+1 (API Météo-France),
  cumuls 1h à 96h, 1 km → suivi continu à partir d'aujourd'hui.

Les deux sources écrivent dans un CSV unique, `precip_yavannome.csv`
(`date, cumul_mm, source`), pour permettre plus tard la comparaison directe
avec un pluviomètre physique installé sur site.

## Coordonnées du terrain

Point d'extraction : centroïde du polygone terrain (8 bornes du géomètre,
récupérées dans le projet `concepteur_haies/haie_optimizer/engine/terrain_grid.py`,
converties EPSG:3947 → EPSG:2154).

| Repère | Valeur |
|---|---|
| Lambert-93 (EPSG:2154) | X = 886824.91, Y = 6636650.21 |
| WGS84 | lon = 5.450160°, lat = 46.803906° |

La résolution 1 km des deux produits fait qu'un seul pixel couvre
l'intégralité du terrain — le centroïde est donc suffisant, pas besoin d'un
point par parcelle.

## Installation

```bash
pip install -r requirements.txt
```

`rasterio` nécessite GDAL — sous Windows, privilégier un environnement
`conda`/`mamba` si l'installation via `pip` échoue :

```bash
conda install -c conda-forge rasterio pyproj
```

## Étape 1 — Historique COMÉPHORE

```bash
python precip_extract_comephore.py --start 1997-01
```

- Télécharge, traite puis **supprime immédiatement** chaque archive
  mensuelle (H_COMEPHORE_YYYYMM.tar, ~200-260 Mo) — rien n'est conservé sur
  disque en dehors du CSV final. L'historique complet représente ~70-90 Go de
  téléchargement cumulé au total : prévoir une connexion stable et du temps.
- Reprise automatique : les mois déjà entièrement présents dans le CSV sont
  sautés, on peut donc interrompre (Ctrl+C) et relancer sans perte ni doublon.
- `--start`/`--end` au format `YYYY-MM` ; `--end` par défaut = mois courant − 2
  (dernier mois normalement publié par COMÉPHORE).

## Étape 2 — Suivi quasi-continu ANTILOPE / LAME_D_EAU

### Pourquoi ce n'est pas une simple requête quotidienne

Vérification faite en conditions réelles : le catalogue de l'API "données
radar" (DPRadar) n'expose pas de cumul pré-calculé sur 24h/96h — seuls
`LAME_D_EAU` (mosaïque radar 5 min) et `REFLECTIVITE` existent, et la doc
précise que **seul le dernier instantané est disponible** (pas
d'historique interrogeable à la demande). Le script doit donc être
exécuté **toutes les 5 minutes** pour ne pas perdre de pluie tombée entre
deux passages, et additionner les instantanés en un cumul journalier
persistant (`precip_antilope_state.json`).

Comme le suivi doit tourner 24h/24 et que le PC ne doit pas rester allumé
en continu, l'exécution se fait dans le cloud via **GitHub Actions**
(gratuit sur dépôt public), pas sur votre machine.

### Inscription API Météo-France (gratuite)

1. Créer un compte sur https://portail-api.meteofrance.fr/
2. Une fois connecté : **Mes API** → s'abonner à l'API **"données radar"**
   (DPRadar).
3. Sur la page "Configurer l'API" de cette API, choisir le mode
   **"API Key"** (pas OAuth2), indiquer une durée de validité longue en
   secondes (essayer `31536000` = 1 an ; réduire si le formulaire la
   refuse), cliquer **Générer Token**, révéler la valeur (icône œil) et la
   copier.

Cette clé est envoyée telle quelle dans l'en-tête `apikey` à chaque
requête — plus simple qu'un échange OAuth2, mais elle **expire à la durée
choisie à sa génération** et devra être régénérée manuellement (et le
secret GitHub mis à jour, voir plus bas) à ce moment-là.

> **Point d'attention** : le nom exact du produit (identifiant, format du
> fichier renvoyé) dépend du catalogue réellement accessible à votre
> abonnement, qui évolue. Le script interroge donc le catalogue en direct
> pour détecter automatiquement un produit `ANTILOPE`/`LAME_D_EAU`. Si la
> détection échoue, il affiche la liste des produits disponibles ; fixez
> alors `METEOFRANCE_OBSERVATION` (variable d'env) avec le nom exact
> trouvé. Si le format du fichier reçu n'est ni GeoTIFF ni GeoTIFF gzippé,
> la fonction à adapter est `extract_raster_value()` dans
> `precip_daily_update.py` — se référer au "Descriptif technique des
> produits" (onglet documentation du portail des données publiques
> Météo-France).

### Mise en place GitHub Actions (une fois)

1. Créer un dépôt **public** sur https://github.com/new (ex. nom :
   `precip-yavannome`), sans README/gitignore auto-générés.
2. Pousser ce projet :

   ```bash
   git remote add origin https://github.com/VOTRE_USER/precip-yavannome.git
   git branch -M main
   git push -u origin main
   ```

3. Dans le dépôt GitHub : **Settings → Secrets and variables → Actions →
   New repository secret**, nom `METEOFRANCE_APPLICATION_ID`, valeur = la
   clé API obtenue ci-dessus.
4. Onglet **Actions** du dépôt : le workflow *"Suivi précipitations
   ANTILOPE (5 min)"* ([.github/workflows/precip_antilope.yml](.github/workflows/precip_antilope.yml))
   apparaît (`workflow_dispatch` permet un déclenchement manuel immédiat
   pour tester).

**Cadence régulière — cron-job.org** : le `schedule:` natif de GitHub
Actions n'a aucune garantie de ponctualité (retards fréquents de
plusieurs minutes en cas de forte charge sur les workflows planifiés
publics). Pour un vrai passage toutes les 5 minutes, un cron externe
gratuit ([cron-job.org](https://cron-job.org)) appelle l'API GitHub pour
déclencher `workflow_dispatch` :

- URL : `https://api.github.com/repos/VOTRE_USER/precip-yavannome/actions/workflows/precip_antilope.yml/dispatches`
- Méthode : `POST`, toutes les 5 minutes
- En-têtes : `Authorization: Bearer VOTRE_TOKEN_GITHUB` (token *fine-grained*,
  scopé au seul dépôt, permission Actions "Read and write"), `Accept:
  application/vnd.github+json`, `Content-Type: application/json`
- Corps : `{"ref":"main"}`

Le `schedule:` interne (toutes les 30 min) reste actif en secours si
cron-job.org tombe en panne — ce n'est pas la source de la cadence
régulière, juste un filet de sécurité redondant.

Le workflow committe et pousse `precip_yavannome.csv` et
`precip_antilope_state.json` à chaque instantané traité — un commit
toutes les ~5 minutes est normal et attendu (gratuit sur dépôt public,
et ça évite la désactivation automatique du workflow après 60 jours
d'inactivité du dépôt).

Un instantané déjà comptabilisé n'est jamais recompté (déduplication par
`validity_time` dans l'état persistant) : le script est donc idempotent
même relancé manuellement ou après une interruption. Une erreur de quota
(HTTP 429) ou de format inattendu est journalisée dans les logs Actions
sans faire planter les exécutions suivantes.

### Exécution manuelle (test local, optionnel)

```bash
python precip_daily_update.py
```

## Étape 3 — Structure du CSV partagé

`precip_yavannome.csv` :

| colonne | description |
|---|---|
| `date` | `YYYY-MM-DD`, cumul journalier |
| `cumul_mm` | cumul de précipitations en mm |
| `source` | `comephore`, `antilope`, ou (futur) `pluviometre_site` |

Le jour où un pluviomètre physique est installé sur le terrain, il suffit
d'append des lignes `source=pluviometre_site` au même CSV (même schéma) —
`precip_visualize.py` les affichera automatiquement en 3e série superposable
pour visualiser l'écart de calibration modèle vs mesure réelle.

## Étape 4 — Visualisation

```bash
python precip_visualize.py
```

Génère :
- `cumul_mensuel_yavannome.png` — barres empilées du cumul mensuel, une
  couleur/hachure par source (COMÉPHORE plein, ANTILOPE hachuré `//`,
  pluviomètre site hachuré `xx`), + courbe de cumul glissant 12 mois si
  ≥ 12 mois de données disponibles.
- `cumul_mensuel_yavannome.html` — version interactive (plotly), survol
  pour explorer les valeurs mois par mois.

## Contraintes respectées

- Aucune clé API en dur dans le code (variables d'environnement uniquement,
  voir `.env.example`).
- Gestion d'erreurs : retries sur téléchargement, striping des archives
  volumineuses au fur et à mesure (COMÉPHORE), gestion explicite du quota
  API (429) et des données manquantes/nodata (ANTILOPE), sans interrompre
  le traitement des mois/jours suivants.
- Reprise possible sans doublon (déduplication par `date`+`source` avant
  chaque écriture).
