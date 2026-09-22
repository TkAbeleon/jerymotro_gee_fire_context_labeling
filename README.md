# JeryMotro — Enrichissement contextuel GEE des détections FIRMS

Script Python robuste et automatisable qui enrichit la table PostgreSQL
`FirmsFireDetection` avec le **contexte d'occupation du sol** autour de chaque
détection de feu, en s'appuyant sur **Google Earth Engine** et le dataset
**ESA WorldCover v200 (2021)** à 10 m de résolution.

Pour chaque détection, le script calcule l'histogramme des classes d'occupation
du sol dans un rayon de **500 mètres**, en déduit la classe dominante
(`fire_context_type`) et la répartition complète en pourcentages
(`context_percentages`).

---

## Sommaire

- [Ce que fait le script](#ce-que-fait-le-script)
- [Architecture du dépôt](#architecture-du-dépôt)
- [Prérequis](#prérequis)
- [Installation](#installation)
- [Configuration (`.env`)](#configuration-env)
- [Authentification Google Earth Engine](#authentification-google-earth-engine)
- [Utilisation](#utilisation)
- [Schéma de la base de données](#schéma-de-la-base-de-données)
- [Classes ESA WorldCover](#classes-esa-worldcover)
- [Automatisation GitHub Actions](#automatisation-github-actions)
- [Résilience et performance](#résilience-et-performance)
- [Dépannage](#dépannage)

---

## Ce que fait le script

À chaque exécution, le script déroule les étapes suivantes :

1. **Migration de schéma** — vérifie l'existence des colonnes
   `fire_context_type` (VARCHAR) et `context_percentages` (JSONB) dans la table
   `FirmsFireDetection`, et les ajoute si elles sont absentes.
2. **Sélection idempotente** — récupère uniquement les détections où
   `fire_context_type IS NULL`. Relancer le script ne retraite jamais ce qui a
   déjà été labellisé.
3. **Construction du lot GEE** — génère une `FeatureCollection` contenant une
   zone tampon de 500 m autour de chaque point.
4. **Appel GEE en lot** — un **seul** appel réseau `reduceRegions()` +
   `getInfo()` pour tout le lot, au lieu de N requêtes HTTP individuelles.
5. **Calcul du contexte** — normalise l'histogramme en pourcentages, identifie
   la classe dominante.
6. **Écriture par lots** — met à jour la base et effectue un `COMMIT` tous les
   500 enregistrements (configurable), pour ne pas tenir de transaction longue.

---

## Architecture du dépôt

```
.
├── gee_fire_context_labeling.py    # Script principal
├── requirements.txt                # Dépendances Python
├── setup.sh                        # Script de déploiement / lancement
├── .env.example                    # Modèle de configuration
├── .gitignore
├── README.md
└── .github/
    └── workflows/
        └── gee_labeling.yml        # Automatisation CI/CD
```

---

## Prérequis

| Composant | Version / Détail |
|---|---|
| Python | 3.9 ou supérieur (3.11 recommandé) |
| PostgreSQL | 12+ (support JSONB requis) |
| Compte Google Earth Engine | Projet GEE enregistré et activé |
| Table existante | `firms_fire_detections` (modèle `FirmsFireDetection`) |

Le script valide au démarrage la présence des colonnes sources `id`,
`latitude` et `longitude`, et s'arrête avec un message explicite si l'une
d'elles manque. Les noms de colonnes sont centralisés en constantes en haut de
`gee_fire_context_labeling.py` si votre schéma diverge.

---

## Installation

### Méthode automatisée (recommandée)

```bash
chmod +x setup.sh
./setup.sh --once
```

Le script crée l'environnement virtuel s'il n'existe pas, l'active, installe
les dépendances, puis lance le job.

### Méthode manuelle

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # puis éditez .env
python gee_fire_context_labeling.py --once
```

---

## Configuration (`.env`)

Copiez `.env.example` vers `.env` et renseignez les valeurs :

| Variable | Obligatoire | Description |
|---|:---:|---|
| `DATABASE_URL` | ✅ | URL SQLAlchemy PostgreSQL, ex. `postgresql://user:pass@host:5432/db` |
| `GEE_AUTH_MODE` | ❌ | Mode GEE : `service_account` par défaut. Mettre explicitement `browser` dans `.env` pour utiliser l'authentification par navigateur. |
| `ENABLE_AUTO_LABELING` | ❌ | `true` active le mode démon planifié. `false` → exécution unique puis arrêt poli. Défaut : `false` |
| `LABELING_CRON_EXPRESSION` | ❌ | Expression CRON 5 champs, ex. `0 */6 * * *`. Prioritaire sur `INTERVAL_MINUTES` |
| `INTERVAL_MINUTES` | ❌ | Intervalle en minutes si pas de CRON. Défaut : `360` |
| `DB_BATCH_SIZE` | ❌ | Taille des lots de lecture/commit. Défaut : `500` |
| `GEE_BATCH_SIZE` | ❌ | Taille des lots envoyés à GEE. Défaut : `500` |
| `MAX_RECORDS_PER_RUN` | ❌ | Plafond de sécurité par exécution. Vide = illimité |
| `REGION_FILTER` | ❌ | Ne traite que les détections d'une `region` donnée. Vide = toutes |
| `SYNC_LANDCOVER_COLUMN` | ❌ | Si `true`, renseigne aussi la colonne métier `landcover` quand elle est `NULL`. Défaut : `false` |
| `WORK` | ❌ | Répartition entre 2 workers : `0` = désactivé, `1` = IDs impairs, `2` = IDs pairs. Défaut : `0` |
| `LOG_LEVEL` | ❌ | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Défaut : `INFO` |

---

## Authentification Google Earth Engine

### Par défaut : Service Account

Le mode d'authentification par défaut est désormais **Service Account**. Définissez
`GEE_AUTH_MODE=service_account` et renseignez `GEE_SERVICE_ACCOUNT_JSON_PATH`
dans `.env`.

### Authentification par navigateur (opt-in)

L'authentification interactive par navigateur est **désactivée par défaut**.
Pour l'utiliser volontairement en local, définissez explicitement dans `.env` :

```env
GEE_AUTH_MODE=browser
```

Puis le script lancera `ee.Authenticate()`.

### En CI/CD ou serveur (Service Account)

1. Dans la console Google Cloud, créez un **Service Account** sur le projet lié
   à Earth Engine.
2. Générez une **clé JSON** et téléchargez-la.
3. Enregistrez l'adresse e-mail du Service Account sur
   [code.earthengine.google.com](https://code.earthengine.google.com) (menu
   *Assets* → partage), ou via la page d'enregistrement des Service Accounts GEE.
4. Renseignez le chemin du fichier dans `GEE_SERVICE_ACCOUNT_JSON_PATH`.

> ⚠️ Ne commitez **jamais** ce fichier JSON. Le `.gitignore` fourni bloque déjà
> les motifs usuels (`gee_key.json`, `service-account*.json`, `credentials*.json`).

---

## Utilisation

### Exécution unique

```bash
python gee_fire_context_labeling.py --once
```

Traite toutes les détections en attente, puis se termine. C'est le mode utilisé
par GitHub Actions.

### Mode démon planifié

Avec `ENABLE_AUTO_LABELING=true` dans le `.env` :

```bash
python gee_fire_context_labeling.py
```

APScheduler prend la main et relance le job selon `LABELING_CRON_EXPRESSION`
(ou `INTERVAL_MINUTES`). `Ctrl+C` provoque un arrêt propre.

### Désactivation

Avec `ENABLE_AUTO_LABELING=false`, le script effectue une passe unique puis
s'arrête poliment avec un code de sortie `0`.

---

## Schéma de la base de données

Le script cible la table physique **`firms_fire_detections`** (modèle SQLAlchemy
`FirmsFireDetection`), conformément au dictionnaire de données.

### Colonnes lues (existantes)

| Colonne | Type | Usage |
|---|---|---|
| `id` | `BigInteger` | Clé primaire, cible du `UPDATE` |
| `latitude` / `longitude` | `Float` | Centre de la zone tampon de 500 m |
| `acq_datetime` | `DateTime` | Tri : les détections **récentes** sont traitées en priorité |
| `region` | `String` | Filtrage optionnel via `REGION_FILTER` |

### Colonnes écrites

| Colonne | Type | Statut | Description |
|---|---|---|---|
| `fire_context_type` | `VARCHAR(100)` | **ajoutée** | Classe d'occupation du sol dominante dans les 500 m |
| `context_percentages` | `JSONB` | **ajoutée** | Répartition complète des classes, en pourcentage |
| `updated_at` | `DateTime` | existante | Rafraîchie à `NOW()` à chaque écriture |
| `landcover` | `String` | existante | Renseignée **uniquement** si `SYNC_LANDCOVER_COLUMN=true` **et** si la valeur est `NULL` |

> **Cohabitation avec `landcover`.** Le dictionnaire décrit déjà une colonne
> `landcover` (type de couverture terrestre). Le script ne l'écrase jamais : par
> défaut il ne la touche pas du tout, et avec `SYNC_LANDCOVER_COLUMN=true` il
> n'écrit que via `COALESCE`, donc seulement là où la valeur est absente.
> `fire_context_type` reste la source de vérité du contexte calculé par GEE.

Exemple de valeur pour `context_percentages` :

```json
{
  "Forêt": 62.41,
  "Prairie / Herbacé": 24.18,
  "Culture agricole": 11.05,
  "Zone bâtie": 2.36
}
```

Requêtes utiles :

```sql
-- Répartition des contextes de feu
SELECT fire_context_type, COUNT(*)
FROM firms_fire_detections
GROUP BY fire_context_type
ORDER BY COUNT(*) DESC;

-- Détections à risque élevé situées majoritairement en forêt
SELECT id, region, frp, risk_score, fire_context_type
FROM firms_fire_detections
WHERE (context_percentages->>'Forêt')::numeric > 50
  AND risk_score >= 0.60
ORDER BY acq_datetime DESC;

-- Contexte dominant par niveau de risque (seuils compute_risk_level)
SELECT CASE
         WHEN risk_score >= 0.80 OR frp > 50 THEN 'CRITICAL'
         WHEN risk_score >= 0.60 THEN 'HIGH'
         WHEN risk_score >= 0.40 THEN 'MEDIUM'
         ELSE 'LOW'
       END AS risk_level,
       fire_context_type,
       COUNT(*)
FROM firms_fire_detections
WHERE fire_context_type IS NOT NULL
GROUP BY 1, 2
ORDER BY 1, 3 DESC;

-- Contexte agrégé d'un événement de feu (jointure FireEvent)
SELECT fe.fire_id, d.fire_context_type, COUNT(*)
FROM firms_fire_detections d
JOIN fire_events fe ON fe.id = d.fire_event_id
WHERE fe.cluster_status = 'ACTIVE'
GROUP BY fe.fire_id, d.fire_context_type;

-- Suivi de l'avancement de la labellisation
SELECT COUNT(*) FILTER (WHERE fire_context_type IS NULL) AS en_attente,
       COUNT(*) FILTER (WHERE fire_context_type IS NOT NULL) AS traitees
FROM firms_fire_detections;
```

Pour **relancer une labellisation complète**, il suffit de remettre la colonne à
`NULL` :

```sql
UPDATE firms_fire_detections SET fire_context_type = NULL;
```

---

## Classes ESA WorldCover

| Code | Libellé utilisé |
|:---:|---|
| 10 | Forêt |
| 20 | Arbustes |
| 30 | Prairie / Herbacé |
| 40 | Culture agricole |
| 50 | Zone bâtie |
| 60 | Sol nu / Végétation clairsemée |
| 70 | Neige / Glace |
| 80 | Eau permanente |
| 90 | Zone humide herbacée |
| 95 | Mangrove |
| 100 | Mousse / Lichen |

Ces libellés sont définis dans le dictionnaire `WORLDCOVER_LABELS` du script et
peuvent être adaptés à votre vocabulaire métier.

---

## Automatisation GitHub Actions

Le workflow `.github/workflows/gee_labeling.yml` se déclenche :

- **automatiquement** toutes les 6 heures (`cron: "0 */6 * * *"`) ;
- **manuellement** via l'onglet *Actions* (`workflow_dispatch`), avec un champ
  optionnel pour limiter le nombre de détections traitées.

### Secrets à configurer

Dans *Settings → Secrets and variables → Actions* :

| Secret | Contenu |
|---|---|
| `DATABASE_URL` | URL de connexion PostgreSQL complète |
| `GEE_SERVICE_ACCOUNT_JSON` | Contenu **intégral** du fichier JSON du Service Account |

La clé est écrite dans un fichier temporaire au début du job, puis supprimée
systématiquement en fin d'exécution (`if: always()`).

> Si votre base n'est pas accessible publiquement, utilisez un *self-hosted
> runner* ou un tunnel, les runners GitHub hébergés ayant des IP variables.

---

## Résilience et performance

**Traitement par lot côté GEE.** Toutes les zones tampons d'un lot sont
regroupées dans une `FeatureCollection` et traitées par un unique appel
`reduceRegions()`. Un lot de 500 détections = **1 requête HTTP**, pas 500.

**Backoff exponentiel.** La librairie `tenacity` protège l'appel GEE :
jusqu'à 6 tentatives, avec une attente croissante de 2 s à 120 s. Le retry ne se
déclenche que sur les erreurs pertinentes (429, quota, timeout de calcul,
erreur interne), détectées par `_is_rate_limit_error()`.

**Commits par lots.** Chaque lot de 500 enregistrements est commité
séparément. En cas d'interruption, le travail déjà effectué est conservé et la
reprise est automatique grâce au filtre `fire_context_type IS NULL`.

**Dégradation gracieuse.** Si un lot échoue définitivement après épuisement du
backoff, l'erreur est journalisée et le script poursuit avec le lot suivant
plutôt que de tout interrompre.

### Traitement parallèle sur deux Web Services

Pour utiliser deux Web Services différents sur la même base PostgreSQL sans qu'ils sélectionnent les mêmes lignes :

**Web Service 1**

```env
WORK=1
```

**Web Service 2**

```env
WORK=2
```

Le partitionnement est déterministe sur la clé primaire `id` : le worker 1 commence par traiter les IDs impairs et le worker 2 les IDs pairs. Chaque lot est réservé atomiquement dans la table légère `gee_labeling_claims`, ce qui empêche les deux workers de prendre le même lot. Lorsqu'un worker termine sa partition native avant l'autre, il passe automatiquement en **mode secours** et récupère les lignes libres restantes, y compris dans l'autre partition. Après un redémarrage, les réservations abandonnées sont récupérées automatiquement après `WORK_CLAIM_TTL_MINUTES` et les lignes déjà labellisées restent exclues par `fire_context_type IS NULL`. Les deux services peuvent donc fonctionner en parallèle sur la même base. Après un redémarrage, chaque worker reprend uniquement sa partition et les lignes déjà labellisées restent exclues par `fire_context_type IS NULL`.

Avec `WORK=0`, le partitionnement est désactivé (mode historique).

### Réglage pour le Free Tier

Le Free Tier de GEE impose des limites de mémoire et de temps de calcul. Si vous
rencontrez des erreurs *User memory limit exceeded* :

```env
GEE_BATCH_SIZE=200      # voire 100
DB_BATCH_SIZE=200
```

---

## Dépannage

**`La table 'firms_fire_detections' n'existe pas`**
Le script cible le nom **physique** de la table, pas le nom de la classe
SQLAlchemy. Vérifiez le `__tablename__` de votre modèle `FirmsFireDetection` et
ajustez la constante `TABLE_NAME` si nécessaire.

**`Colonnes sources manquantes dans 'firms_fire_detections'`**
Les colonnes `id`, `latitude` ou `longitude` sont absentes ou nommées
autrement. Ajustez `ID_COLUMN`, `LATITUDE_COLUMN`, `LONGITUDE_COLUMN`.

**`ee.ee_exception.EEException: Not signed up for Earth Engine`**
Le Service Account n'est pas enregistré auprès de GEE. Enregistrez son adresse
e-mail sur la page dédiée de Earth Engine.

**`User memory limit exceeded`**
Réduisez `GEE_BATCH_SIZE` (voir la section précédente).

**`Aucune donnée WorldCover exploitable pour la détection id=...`**
La zone est hors couverture du dataset ou entièrement masquée (océan profond,
par exemple). La détection reste à `NULL` et sera retentée à la prochaine
exécution — c'est le comportement attendu.

**Trop de logs / pas assez de détail**
Ajustez `LOG_LEVEL` dans le `.env` (`DEBUG` pour le détail complet).

---

## Licence

Projet interne JeryMotro. Les données ESA WorldCover sont distribuées sous
licence CC BY 4.0 et requièrent l'attribution suivante :
*ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021)
processed by ESA WorldCover consortium.*
