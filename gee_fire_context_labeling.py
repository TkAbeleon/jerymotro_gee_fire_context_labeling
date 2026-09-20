#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JeryMotro — Enrichissement GEE des détections FIRMS
=====================================================

Ce script enrichit la table PostgreSQL `firms_fire_detections` (modèle
SQLAlchemy `FirmsFireDetection`) avec le contexte d'occupation du sol
(ESA WorldCover v200, 2021, résolution 10m) autour de chaque détection de feu,
via Google Earth Engine.

Conforme au dictionnaire de données JeryMotro :
  - clé primaire `id` (BigInteger)
  - coordonnées `latitude` / `longitude` (Float)
  - priorisation par `acq_datetime` (les détections récentes alimentent les
    alertes et les FireEvent actifs)
  - rafraîchissement de `updated_at` à chaque écriture
  - la colonne métier préexistante `landcover` n'est touchée que si
    SYNC_LANDCOVER_COLUMN=true, et jamais écrasée si déjà renseignée

Fonctionnement :
  1. Vérifie/ajoute les colonnes `fire_context_type` et `context_percentages`.
  2. Récupère les détections non encore labellisées (fire_context_type IS NULL).
  3. Construit des zones tampons de 500m autour de chaque point.
  4. Envoie les zones à GEE en LOT (FeatureCollection) via reduceRegions,
     avec retry/backoff exponentiel (tenacity) pour gérer les erreurs 429.
  5. Met à jour la base par lots de N enregistrements (commit régulier).
  6. Partitionne optionnellement le travail entre deux workers indépendants
     via WORK=1 ou WORK=2 : chaque worker traite une partition disjointe des IDs.
  7. Peut tourner une fois (CI/CD) ou en démon planifié (cron/intervalle).

Auteur : Généré pour le backend JeryMotro
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)

import ee

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("jerymotro.gee_labeling")

# ---------------------------------------------------------------------------
# Constantes métier
# ---------------------------------------------------------------------------
# Nom physique de la table en base. Le modèle SQLAlchemy s'appelle
# `FirmsFireDetection` mais la table est `firms_fire_detections`
# (cf. data_dictionary.md : Alert.detection_id -> firms_fire_detections.id).
TABLE_NAME = "firms_fire_detections"

# Colonnes existantes de la table, conformes au dictionnaire de données.
ID_COLUMN = "id"                       # BigInteger, clé primaire
LATITUDE_COLUMN = "latitude"           # Float
LONGITUDE_COLUMN = "longitude"         # Float
ACQ_DATETIME_COLUMN = "acq_datetime"   # DateTime — sert à prioriser le récent
REGION_COLUMN = "region"               # String — filtrage optionnel
UPDATED_AT_COLUMN = "updated_at"       # DateTime — à rafraîchir à chaque write
LANDCOVER_COLUMN = "landcover"         # String — champ métier déjà existant

GEE_DATASET = "ESA/WorldCover/v200/2021"
GEE_BAND = "Map"
BUFFER_RADIUS_METERS = 500
GEE_SCALE_METERS = 10

# Table de correspondance des classes ESA WorldCover -> libellé métier
WORLDCOVER_LABELS: Dict[str, str] = {
    "10": "Forêt",
    "20": "Arbustes",
    "30": "Prairie / Herbacé",
    "40": "Culture agricole",
    "50": "Zone bâtie",
    "60": "Sol nu / Végétation clairsemée",
    "70": "Neige / Glace",
    "80": "Eau permanente",
    "90": "Zone humide herbacée",
    "95": "Mangrove",
    "100": "Mousse / Lichen",
}

DEFAULT_DB_BATCH_SIZE = 500
DEFAULT_GEE_BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Configuration (.env)
# ---------------------------------------------------------------------------
@dataclass
class AppConfig:
    database_url: str
    gee_auth_mode: str
    gee_service_account_json_path: Optional[str]
    gee_project: Optional[str]
    enable_auto_labeling: bool
    labeling_cron_expression: Optional[str]
    interval_minutes: int
    db_batch_size: int
    gee_batch_size: int
    max_records_per_run: Optional[int]
    region_filter: Optional[str]
    sync_landcover_column: bool
    worker_id: int


def _str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> AppConfig:
    """Charge et valide la configuration depuis le fichier .env / l'environnement."""
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("La variable d'environnement DATABASE_URL est obligatoire.")
        sys.exit(1)

    gee_auth_mode = os.environ.get("GEE_AUTH_MODE", "service_account").strip().lower()
    if gee_auth_mode not in {"service_account", "browser"}:
        logger.error(
            "GEE_AUTH_MODE invalide : %s. Valeurs autorisées : service_account, browser.",
            gee_auth_mode,
        )
        sys.exit(1)

    config = AppConfig(
        database_url=database_url,
        gee_auth_mode=gee_auth_mode,
        gee_service_account_json_path=os.environ.get("GEE_SERVICE_ACCOUNT_JSON_PATH") or None,
        gee_project=os.environ.get("GEE_PROJECT") or None,
        enable_auto_labeling=_str_to_bool(os.environ.get("ENABLE_AUTO_LABELING"), default=False),
        labeling_cron_expression=os.environ.get("LABELING_CRON_EXPRESSION") or None,
        interval_minutes=int(os.environ.get("INTERVAL_MINUTES", "360")),
        db_batch_size=int(os.environ.get("DB_BATCH_SIZE", DEFAULT_DB_BATCH_SIZE)),
        gee_batch_size=int(os.environ.get("GEE_BATCH_SIZE", DEFAULT_GEE_BATCH_SIZE)),
        max_records_per_run=(
            int(os.environ["MAX_RECORDS_PER_RUN"])
            if os.environ.get("MAX_RECORDS_PER_RUN")
            else None
        ),
        region_filter=os.environ.get("REGION_FILTER") or None,
        sync_landcover_column=_str_to_bool(
            os.environ.get("SYNC_LANDCOVER_COLUMN"), default=False
        ),
        worker_id=int(os.environ.get("WORK", os.environ.get("work", "0")) or "0"),
    )

    if config.worker_id not in {0, 1, 2}:
        logger.error(
            "WORK invalide : %d. Valeurs autorisées : 0 (pas de partition), 1 ou 2.",
            config.worker_id,
        )
        sys.exit(1)

    if config.worker_id:
        logger.info(
            "Partitionnement multi-worker activé : WORK=%d/2. "
            "Ce worker traite uniquement sa partition d'IDs.",
            config.worker_id,
        )
    logger.debug("Configuration chargée : %s", config)
    return config


# ---------------------------------------------------------------------------
# Base de données : gestion du schéma
# ---------------------------------------------------------------------------
def ensure_columns_exist(engine: Engine, table_name: str = TABLE_NAME) -> None:
    """
    Vérifie l'existence des colonnes `fire_context_type` et `context_percentages`.
    Les ajoute de façon idempotente et sécurisée si absentes (PostgreSQL).
    """
    inspector = inspect(engine)

    if table_name not in inspector.get_table_names():
        logger.error(
            "La table '%s' n'existe pas dans la base de données cible. Abandon.",
            table_name,
        )
        sys.exit(1)

    existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
    logger.debug("Colonnes actuelles de '%s' : %s", table_name, sorted(existing_columns))

    # Garde-fou : les colonnes sources décrites dans le dictionnaire de données
    # doivent être présentes, sinon le job ne peut pas fonctionner.
    required_columns = {ID_COLUMN, LATITUDE_COLUMN, LONGITUDE_COLUMN}
    missing_required = required_columns - existing_columns
    if missing_required:
        logger.error(
            "Colonnes sources manquantes dans '%s' : %s. Vérifiez le modèle SQLAlchemy.",
            table_name,
            sorted(missing_required),
        )
        sys.exit(1)

    statements: List[str] = []
    if "fire_context_type" not in existing_columns:
        statements.append(
            f'ALTER TABLE "{table_name}" '
            f'ADD COLUMN IF NOT EXISTS fire_context_type VARCHAR(100)'
        )
    if "context_percentages" not in existing_columns:
        statements.append(
            f'ALTER TABLE "{table_name}" '
            f'ADD COLUMN IF NOT EXISTS context_percentages JSONB'
        )

    if not statements:
        logger.info("Les colonnes de contexte existent déjà. Aucune migration nécessaire.")
        return

    logger.info("Ajout de %d colonne(s) manquante(s)...", len(statements))
    try:
        with engine.begin() as conn:
            for stmt in statements:
                logger.info("Exécution DDL : %s", stmt)
                conn.execute(text(stmt))
        logger.info("Migration de schéma terminée avec succès.")
    except SQLAlchemyError:
        logger.exception("Échec de la migration du schéma.")
        raise


# ---------------------------------------------------------------------------
# Google Earth Engine : authentification
# ---------------------------------------------------------------------------
def initialize_gee(config: AppConfig) -> None:
    """
    Initialise Earth Engine.

    Par défaut, l'authentification utilise obligatoirement un Service Account.
    Le mode navigateur est volontairement opt-in : il faut définir explicitement
    GEE_AUTH_MODE=browser dans le fichier .env.
    """
    try:
        if config.gee_auth_mode == "service_account":
            if not config.gee_service_account_json_path:
                logger.error(
                    "Authentification GEE par défaut = Service Account, mais "
                    "GEE_SERVICE_ACCOUNT_JSON_PATH n'est pas défini."
                )
                logger.error(
                    "Définissez GEE_SERVICE_ACCOUNT_JSON_PATH, ou utilisez explicitement "
                    "GEE_AUTH_MODE=browser dans .env pour l'authentification par navigateur."
                )
                sys.exit(1)
            logger.info(
                "Authentification GEE via Service Account : %s",
                config.gee_service_account_json_path,
            )
            if not os.path.isfile(config.gee_service_account_json_path):
                logger.error(
                    "Fichier de credentials introuvable : %s",
                    config.gee_service_account_json_path,
                )
                sys.exit(1)

            with open(config.gee_service_account_json_path, "r", encoding="utf-8") as f:
                key_data = json.load(f)
            service_account_email = key_data.get("client_email")
            if not service_account_email:
                logger.error("Le JSON de Service Account ne contient pas 'client_email'.")
                sys.exit(1)

            credentials = ee.ServiceAccountCredentials(
                service_account_email, config.gee_service_account_json_path
            )
            if not config.gee_project:
                logger.error(
                    "GEE_PROJECT est obligatoire avec l'authentification Service Account."
                )
                logger.error(
                    "Utilisez le même projet que celui configuré dans Unet-jerymotro "
                    "(variable GEE_PROJECT)."
                )
                sys.exit(1)

            logger.info("Projet Earth Engine utilisé : %s", config.gee_project)
            ee.Initialize(credentials, project=config.gee_project)
        elif config.gee_auth_mode == "browser":
            logger.info(
                "Authentification GEE via navigateur demandée explicitement "
                "(GEE_AUTH_MODE=browser)."
            )
            ee.Authenticate()
            if config.gee_project:
                logger.info("Projet Earth Engine utilisé : %s", config.gee_project)
                ee.Initialize(project=config.gee_project)
            else:
                ee.Initialize()

        logger.info("Earth Engine initialisé avec succès.")
    except Exception:
        logger.exception("Échec de l'initialisation de Google Earth Engine.")
        raise


# ---------------------------------------------------------------------------
# Google Earth Engine : logique métier
# ---------------------------------------------------------------------------
def _is_rate_limit_error(exception: BaseException) -> bool:
    """Détecte les erreurs de type Rate Limit (429) ou erreurs transitoires GEE."""
    message = str(exception).lower()
    keywords = ("429", "rate limit", "too many requests", "quota", "user memory limit",
                "computation timed out", "deadline exceeded", "internal error")
    return any(kw in message for kw in keywords)


@retry(
    reraise=True,
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=2, max=120),
    retry=retry_if_exception(_is_rate_limit_error),
    before_sleep=before_sleep_log(logger, logging.WARNING),
)
def _get_worldcover_histograms(feature_collection: "ee.FeatureCollection") -> List[dict]:
    """
    Appelle GEE (reduceRegions en lot) pour calculer l'histogramme des classes
    WorldCover sur chaque zone tampon, avec retry/backoff exponentiel en cas
    d'erreur de Rate Limit (HTTP 429) ou d'erreur transitoire.
    """
    worldcover = ee.Image(GEE_DATASET).select(GEE_BAND)

    reduced = worldcover.reduceRegions(
        collection=feature_collection,
        reducer=ee.Reducer.frequencyHistogram(),
        scale=GEE_SCALE_METERS,
    )

    # Un seul appel réseau (.getInfo()) pour tout le lot : c'est le coeur du
    # traitement par lot demandé (au lieu de N requêtes individuelles).
    result = reduced.getInfo()
    return result.get("features", [])


def _build_feature_collection(rows: List[dict]) -> "ee.FeatureCollection":
    """Construit une FeatureCollection GEE de zones tampons de 500m autour de chaque point."""
    features = []
    for row in rows:
        point = ee.Geometry.Point([row["longitude"], row["latitude"]])
        buffered = point.buffer(BUFFER_RADIUS_METERS)
        feature = ee.Feature(buffered, {"detection_id": row["id"]})
        features.append(feature)
    return ee.FeatureCollection(features)


def _compute_context_from_histogram(histogram: Dict[str, float]) -> Optional[tuple]:
    """
    À partir d'un histogramme brut {code_classe: nb_pixels}, calcule :
      - le pourcentage par classe (libellé lisible)
      - la classe dominante (fire_context_type)
    """
    if not histogram:
        return None

    total_pixels = sum(histogram.values())
    if total_pixels <= 0:
        return None

    percentages: Dict[str, float] = {}
    for class_code, pixel_count in histogram.items():
        label = WORLDCOVER_LABELS.get(str(class_code), f"Classe inconnue ({class_code})")
        percentages[label] = round((pixel_count / total_pixels) * 100, 2)

    dominant_label = max(percentages, key=percentages.get)
    return dominant_label, percentages


def label_batch_via_gee(rows: List[dict]) -> Dict[int, dict]:
    """
    Envoie un lot de détections à GEE et retourne un mapping
    {detection_id: {"fire_context_type": ..., "context_percentages": {...}}}
    """
    logger.info("Envoi d'un lot de %d détections à Google Earth Engine...", len(rows))
    feature_collection = _build_feature_collection(rows)

    try:
        features = _get_worldcover_histograms(feature_collection)
    except Exception:
        logger.exception(
            "Échec définitif de l'appel GEE après plusieurs tentatives (backoff épuisé)."
        )
        return {}

    updates: Dict[int, dict] = {}
    for feature in features:
        properties = feature.get("properties", {})
        detection_id = properties.get("detection_id")
        histogram = properties.get("histogram", {})

        context = _compute_context_from_histogram(histogram)
        if context is None:
            logger.warning(
                "Aucune donnée WorldCover exploitable pour la détection id=%s (zone probablement "
                "hors couverture ou entièrement masquée).",
                detection_id,
            )
            continue

        dominant_label, percentages = context
        updates[detection_id] = {
            "fire_context_type": dominant_label,
            "context_percentages": percentages,
        }

    logger.info("Lot traité : %d/%d détections labellisées avec succès.", len(updates), len(rows))
    return updates


# ---------------------------------------------------------------------------
# Base de données : lecture / écriture par lots
# ---------------------------------------------------------------------------
DB_RETRY_ATTEMPTS = 6
DB_RETRY_BASE_SECONDS = 2
DB_POOL_RECYCLE_SECONDS = 300


def _is_transient_db_disconnect(exception: BaseException) -> bool:
    """Détermine si une erreur DB correspond probablement à une coupure/reconnexion."""
    if isinstance(exception, OperationalError):
        message = str(exception).lower()
        markers = (
            "ssl syscall error",
            "eof detected",
            "connection reset",
            "connection refused",
            "server closed the connection",
            "connection not open",
            "connection already closed",
            "could not receive data",
            "could not send data",
        )
        return any(marker in message for marker in markers) or bool(
            getattr(exception, "connection_invalidated", False)
        )
    if isinstance(exception, DBAPIError):
        return bool(getattr(exception, "connection_invalidated", False))
    return False


def _db_retry_sleep(attempt: int) -> None:
    """Backoff court pour laisser le réseau/serveur PostgreSQL se reconnecter."""
    delay = min(DB_RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 30)
    logger.warning(
        "Nouvelle tentative DB dans %d seconde(s) (tentative %d/%d).",
        delay,
        attempt + 1,
        DB_RETRY_ATTEMPTS,
    )
    time.sleep(delay)


def fetch_pending_detections(
    session: Session,
    limit: int,
    region_filter: Optional[str] = None,
    worker_id: int = 0,
) -> List[dict]:
    """
    Récupère les détections non encore labellisées (fire_context_type IS NULL).

    Quand WORK=1 ou WORK=2, la table est partitionnée de façon déterministe
    selon l'ID :
      - WORK=1 -> IDs impairs
      - WORK=2 -> IDs pairs

    Les deux workers peuvent donc travailler simultanément sur la même base
    sans sélectionner la même ligne. WORK=0 désactive le partitionnement.
    Les détections les plus récentes (`acq_datetime`) restent prioritaires dans
    chaque partition.
    """
    params: dict = {"limit": limit}
    region_clause = ""
    worker_clause = ""

    if region_filter:
        region_clause = f'AND "{REGION_COLUMN}" = :region '
        params["region"] = region_filter

    if worker_id in {1, 2}:
        # Partition disjointe et stable : worker 1 = IDs impairs,
        # worker 2 = IDs pairs. Après un redémarrage, le même worker reprend
        # naturellement sa partition sans retraiter les lignes déjà labellisées.
        worker_clause = f'AND MOD("{ID_COLUMN}" - 1, 2) = :worker_partition '
        params["worker_partition"] = worker_id - 1

    query = text(
        f'SELECT "{ID_COLUMN}", "{LATITUDE_COLUMN}", "{LONGITUDE_COLUMN}" '
        f'FROM "{TABLE_NAME}" '
        f'WHERE fire_context_type IS NULL '
        f'AND "{LATITUDE_COLUMN}" IS NOT NULL AND "{LONGITUDE_COLUMN}" IS NOT NULL '
        f'{worker_clause}'
        f'{region_clause}'
        f'ORDER BY "{ACQ_DATETIME_COLUMN}" DESC NULLS LAST, "{ID_COLUMN}" DESC '
        f'LIMIT :limit'
    )
    result = session.execute(query, params)
    rows = [
        {"id": int(r[0]), "latitude": float(r[1]), "longitude": float(r[2])}
        for r in result.fetchall()
    ]
    return rows


def apply_updates(
    session: Session, updates: Dict[int, dict], sync_landcover: bool = False
) -> None:
    """
    Applique les mises à jour en base pour un lot donné, puis commit.

    - `updated_at` est systématiquement rafraîchi (convention du modèle).
    - `landcover` (colonne métier préexistante) n'est renseignée que si
      SYNC_LANDCOVER_COLUMN=true, et uniquement lorsqu'elle est encore NULL,
      afin de ne jamais écraser une valeur issue d'une autre source.
    """
    if not updates:
        return

    landcover_clause = ""
    if sync_landcover:
        landcover_clause = (
            f'    "{LANDCOVER_COLUMN}" = COALESCE("{LANDCOVER_COLUMN}", :fire_context_type), '
        )

    update_stmt = text(
        f'UPDATE "{TABLE_NAME}" '
        f'SET fire_context_type = :fire_context_type, '
        f'    context_percentages = CAST(:context_percentages AS JSONB), '
        f'{landcover_clause}'
        f'    "{UPDATED_AT_COLUMN}" = NOW() '
        f'WHERE "{ID_COLUMN}" = :detection_id'
    )

    try:
        for detection_id, payload in updates.items():
            session.execute(
                update_stmt,
                {
                    "fire_context_type": payload["fire_context_type"],
                    "context_percentages": json.dumps(payload["context_percentages"]),
                    "detection_id": detection_id,
                },
            )
        session.commit()
        logger.info("Commit effectué : %d enregistrement(s) mis à jour.", len(updates))
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Échec de l'écriture en base, rollback effectué.")
        raise


# ---------------------------------------------------------------------------
# Orchestration du job
# ---------------------------------------------------------------------------
def run_labeling_job(engine: Engine, config: AppConfig) -> None:
    """
    Boucle principale résiliente :
      - une session PostgreSQL courte pour chaque lecture ;
      - une session PostgreSQL courte pour chaque écriture ;
      - aucune connexion DB n'est conservée pendant les appels GEE ;
      - reconnexion automatique en cas de coupure réseau/SSL.
    """
    logger.info("=== Démarrage du job de labellisation contextuelle GEE ===")
    session_factory = sessionmaker(bind=engine, future=True)
    total_processed = 0
    total_labeled = 0

    while True:
        if config.max_records_per_run and total_processed >= config.max_records_per_run:
            logger.info(
                "Limite MAX_RECORDS_PER_RUN=%d atteinte pour cette exécution.",
                config.max_records_per_run,
            )
            break

        # ------------------------------------------------------------------
        # Lecture DB avec connexion courte et retry de reconnexion.
        # La connexion est libérée avant l'appel GEE.
        # ------------------------------------------------------------------
        rows = None
        last_db_error = None

        for attempt in range(1, DB_RETRY_ATTEMPTS + 1):
            try:
                with session_factory() as session:
                    rows = fetch_pending_detections(
                        session,
                        limit=config.db_batch_size,
                        region_filter=config.region_filter,
                        worker_id=config.worker_id,
                    )
                last_db_error = None
                break
            except SQLAlchemyError as exc:
                last_db_error = exc
                if not _is_transient_db_disconnect(exc) or attempt >= DB_RETRY_ATTEMPTS:
                    raise
                logger.warning(
                    "Connexion PostgreSQL perdue pendant la lecture du lot "
                    "(tentative %d/%d) : %s",
                    attempt,
                    DB_RETRY_ATTEMPTS,
                    exc,
                )
                engine.dispose()
                _db_retry_sleep(attempt)

        if last_db_error is not None:
            raise last_db_error

        if not rows:
            logger.info("Aucune détection en attente de labellisation. Job terminé.")
            break

        logger.info("Lot récupéré : %d détection(s) à traiter.", len(rows))
        labeled_in_this_round = 0

        # ------------------------------------------------------------------
        # GEE : aucun lien DB n'est maintenu pendant ce calcul.
        # ------------------------------------------------------------------
        for i in range(0, len(rows), config.gee_batch_size):
            sub_batch = rows[i : i + config.gee_batch_size]
            updates = label_batch_via_gee(sub_batch)

            if updates:
                # ----------------------------------------------------------
                # Écriture DB avec une nouvelle connexion et retry.
                # La mise à jour est idempotente : si le COMMIT a réussi
                # mais que son accusé de réception a été perdu, la répétition
                # de la même valeur ne corrompt pas les données.
                # ----------------------------------------------------------
                last_db_error = None
                for attempt in range(1, DB_RETRY_ATTEMPTS + 1):
                    try:
                        with session_factory() as write_session:
                            apply_updates(
                                write_session,
                                updates,
                                sync_landcover=config.sync_landcover_column,
                            )
                        last_db_error = None
                        break
                    except SQLAlchemyError as exc:
                        last_db_error = exc
                        if not _is_transient_db_disconnect(exc) or attempt >= DB_RETRY_ATTEMPTS:
                            raise
                        logger.warning(
                            "Connexion PostgreSQL perdue pendant l'écriture du lot "
                            "(tentative %d/%d) : %s",
                            attempt,
                            DB_RETRY_ATTEMPTS,
                            exc,
                        )
                        engine.dispose()
                        _db_retry_sleep(attempt)

                if last_db_error is not None:
                    raise last_db_error

            labeled_in_this_round += len(updates)

        total_labeled += labeled_in_this_round
        total_processed += len(rows)

        # Garde-fou anti-boucle-infinie.
        if labeled_in_this_round == 0:
            logger.warning(
                "Aucun enregistrement labellisé sur ce lot de %d détection(s). "
                "Arrêt du job pour éviter une boucle infinie — vérifiez les logs "
                "GEE ci-dessus. Le traitement reprendra à la prochaine exécution.",
                len(rows),
            )
            break

        logger.info(
            "Progression cumulée : %d détection(s) traitées, %d labellisées avec succès.",
            total_processed,
            total_labeled,
        )

    logger.info(
        "=== Job terminé. Total traité=%d | Total labellisé=%d ===",
        total_processed,
        total_labeled,
    )


# ---------------------------------------------------------------------------
# Planification (mode démon)
# ---------------------------------------------------------------------------
def run_scheduler(engine: Engine, config: AppConfig) -> None:
    """Lance le job en tâche de fond planifiée (cron ou intervalle simple)."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BlockingScheduler(timezone="UTC")

    if config.labeling_cron_expression:
        trigger = CronTrigger.from_crontab(config.labeling_cron_expression, timezone="UTC")
        logger.info(
            "Mode démon activé — planification CRON : '%s'", config.labeling_cron_expression
        )
    else:
        trigger = IntervalTrigger(minutes=config.interval_minutes)
        logger.info(
            "Mode démon activé — planification par intervalle : toutes les %d minute(s).",
            config.interval_minutes,
        )

    scheduler.add_job(
        run_labeling_job,
        trigger=trigger,
        args=[engine, config],
        id="jerymotro_gee_labeling_job",
        max_instances=1,
        coalesce=True,
    )

    try:
        logger.info("Scheduler démarré. En attente des prochaines exécutions (Ctrl+C pour arrêter).")
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Arrêt du scheduler demandé. Fermeture propre.")


# ---------------------------------------------------------------------------
# Entrée du script
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JeryMotro — Enrichissement contextuel GEE des détections FIRMS."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Force une exécution unique du job, même si ENABLE_AUTO_LABELING=true "
        "(utile pour le mode CI/CD, ex: GitHub Actions).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    logger.info("Connexion à la base de données...")
    if config.worker_id:
        logger.info("Worker configuré : WORK=%d/2", config.worker_id)
    else:
        logger.info("Worker configuré : WORK=0 (partitionnement désactivé)")
    engine = create_engine(
        config.database_url,
        pool_pre_ping=True,
        pool_recycle=DB_POOL_RECYCLE_SECONDS,
        future=True,
    )

    ensure_columns_exist(engine)
    initialize_gee(config)

    if args.once or not config.enable_auto_labeling:
        if not config.enable_auto_labeling:
            logger.info(
                "ENABLE_AUTO_LABELING=false : exécution unique, puis arrêt poli du script."
            )
        run_labeling_job(engine, config)
        logger.info("Script terminé (mode exécution unique).")
        sys.exit(0)

    # Mode démon : exécution planifiée récurrente
    run_scheduler(engine, config)


if __name__ == "__main__":
    main()
