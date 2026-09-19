# Déploiement JeryMotro sur Render avec Docker

## Architecture

Render Cron Job -> Docker -> Google Earth Engine -> Layerbase PostgreSQL

Aucun AlwaysData, tunnel ou WebSocket n'est nécessaire.

## Configuration Render

Le dépôt contient un render.yaml qui configure :

- type : Cron Job
- runtime : Docker
- plan : 0.5c-512mb
- Dockerfile : ./Dockerfile
- contexte Docker : .
- branche : main
- calendrier : 0 */6 * * *

Le Dockerfile contient l'ENTRYPOINT. Il n'est donc pas nécessaire de renseigner une commande de démarrage dans Render pour ce Cron Job Docker.

## Variables d'environnement

Dans Render -> Environment :

DATABASE_URL = URL complète PostgreSQL Layerbase, avec sslmode=require.

GEE_PROJECT = antigravitygcp-499308

GEE_AUTH_MODE = service_account

Les autres valeurs par défaut sont fournies par render.yaml.

## Compte de service Google Earth Engine

Dans Render -> Environment -> Secret Files :

Filename :
gee-service-account.json

Contents :
coller le JSON complet du compte de service Google Cloud/Earth Engine.

Le fichier est disponible dans le conteneur à :
/etc/secrets/gee-service-account.json

docker-entrypoint.sh définit automatiquement :
GEE_SERVICE_ACCOUNT_JSON_PATH=/etc/secrets/gee-service-account.json

Le JSON ne doit jamais être ajouté au dépôt Git.

## Docker

Le conteneur :
1. vérifie le Secret File GEE ;
2. utilise l'authentification Service Account ;
3. lance python gee_fire_context_labeling.py --once ;
4. termine lorsque le traitement est fini.

## Premier test

Après création du service, utiliser Trigger Run dans Render et vérifier les logs.

## Calendrier

0 */6 * * * = toutes les 6 heures, en UTC.

Pour Madagascar (UTC+3) : 03:00, 09:00, 15:00 et 21:00.
