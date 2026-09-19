# Déploiement JeryMotro sur Render avec Docker

## Architecture

Render Cron Job (Docker)
→ Google Earth Engine
→ Layerbase PostgreSQL

Aucun tunnel, AlwaysData ou serveur intermédiaire n'est nécessaire.

## 1. Render

Le dépôt contient un `render.yaml` qui définit le Cron Job.

Render construit l'image depuis :

`Dockerfile`

Le conteneur démarre avec :

`docker-entrypoint.sh`

qui transforme le secret `GEE_SERVICE_ACCOUNT_JSON` en fichier temporaire
nécessaire à Earth Engine, puis lance :

`python gee_fire_context_labeling.py --once`

## 2. Variables d'environnement

À configurer dans Render :

`DATABASE_URL`
: URL PostgreSQL Layerbase complète, avec `sslmode=require`.

`GEE_SERVICE_ACCOUNT_JSON`
: contenu intégral du JSON du Service Account GEE.

`GEE_PROJECT`
: projet Google Cloud / Earth Engine utilisé par le Service Account.

Recommandées :

`GEE_AUTH_MODE=service_account`

`ENABLE_AUTO_LABELING=false`

`GEE_BATCH_SIZE=200`

`DB_BATCH_SIZE=200`

Le mode `--once` est volontairement utilisé par Render Cron : chaque exécution
traite les détections en attente puis se termine.

## 3. Secret GEE

Ne pas ajouter le fichier JSON dans Git.

Dans Render, ajouter le contenu JSON comme valeur secrète de
`GEE_SERVICE_ACCOUNT_JSON`. Le conteneur écrit ce secret uniquement dans
`/tmp/gee-service-account.json`, avec les permissions 600, pendant
l'exécution.

## 4. Planification

Le `render.yaml` planifie actuellement le job toutes les 6 heures :

`0 */6 * * *`

Render utilise l'UTC pour les horaires des Cron Jobs.

## 5. Coût Render

Le Cron Job n'a pas de plan Free : Render indique un minimum de 1 $US/mois par
Cron Job, puis facture le temps d'exécution. C'est différent d'un Web Service
Free qui peut se mettre en veille. Pour ce traitement périodique, le Cron Job
correspond au modèle d'exécution du programme.

## 6. Test manuel

Après le premier déploiement, utiliser **Trigger Run** dans Render pour lancer
une exécution immédiatement et consulter les logs.

Logs attendus :

`Connexion à la base de données...`

`Authentification GEE via Service Account...`

`Earth Engine initialisé avec succès.`

puis le traitement des lots.

## 7. Dépendances externes

La base de données reste hébergée sur Layerbase. L'application elle-même,
son image Docker et son exécution sont hébergées sur Render.
