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

## Étape 2 — Suivi quotidien ANTILOPE

### Inscription API Météo-France (gratuite)

1. Créer un compte sur https://portail-api.meteofrance.fr/
2. Une fois connecté : **Mes API** → s'abonner à l'API donnant accès aux
   cumuls de précipitations radar (catalogue "API Ciblée Radar" / "Données
   radars" au moment de la rédaction — le catalogue évolue, vérifier la
   présence éventuelle d'une API dédiée "ANTILOPE"/"Précipitations").
3. Sur cette API, cliquer **Générer Token** : la commande `curl` affichée
   contient, après `Authorization: Basic `, votre `APPLICATION_ID`.
4. Stocker cet identifiant en variable d'environnement (jamais en dur dans
   le code — voir `.env.example`) :

   ```powershell
   setx METEOFRANCE_APPLICATION_ID "votre_application_id_ici"
   ```

   (rouvrir le terminal pour que la variable soit prise en compte par les
   futures sessions ; `$env:METEOFRANCE_APPLICATION_ID = "..."` pour la
   session courante uniquement)

Le script échange cet identifiant contre un token Bearer à chaque exécution
(`POST https://portail-api.meteofrance.fr/token`) — c'est le flux OAuth2
officiel documenté par Météo-France, pas une clé API statique.

> **Point d'attention** : le nom exact du produit ANTILOPE (identifiant,
> périodes de cumul disponibles, format du fichier renvoyé) dépend du
> catalogue réellement accessible à votre abonnement, qui évolue. Le script
> interroge donc le catalogue en direct pour détecter automatiquement un
> produit `ANTILOPE`/`LAME_D_EAU`. Si la détection échoue, il affiche la
> liste des produits disponibles ; fixez alors `METEOFRANCE_OBSERVATION`
> (variable d'env) avec le nom exact trouvé. Si le format du fichier reçu
> n'est ni GeoTIFF ni GeoTIFF gzippé, la fonction à adapter est
> `extract_raster_value()` dans `precip_daily_update.py` — se référer au
> "Descriptif technique des produits" (onglet documentation du portail des
> données publiques Météo-France).

### Exécution manuelle

```bash
python precip_daily_update.py
```

Ajoute le cumul 24h du jour courant au CSV partagé (`source=antilope`),
sans doublon si déjà exécuté le même jour. Une erreur de quota (HTTP 429)
ou de disponibilité produit est journalisée sans faire planter le script —
le prochain passage planifié réessaiera.

### Tâche planifiée

**Windows** (Planificateur de tâches), exécution quotidienne à 07h00 :

```powershell
schtasks /create /tn "Precip Yavannome Daily" /tr "python C:\chemin\vers\precip_daily_update.py" /sc daily /st 07:00
```

Adapter le chemin vers `python.exe` si un environnement virtuel est utilisé :

```powershell
schtasks /create /tn "Precip Yavannome Daily" /tr "C:\chemin\venv\Scripts\python.exe C:\chemin\vers\precip_daily_update.py" /sc daily /st 07:00
```

**Linux/macOS** (cron), équivalent :

```cron
0 7 * * * /usr/bin/env METEOFRANCE_APPLICATION_ID=... python3 /chemin/precip_daily_update.py >> /chemin/precip_daily_update.log 2>&1
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
