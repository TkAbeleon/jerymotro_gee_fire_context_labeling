#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Serveur HTTP Render + worker de labellisation GEE."""

from __future__ import annotations

import logging
import os
import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine

from gee_fire_context_labeling import (
    load_config,
    ensure_columns_exist,
    initialize_gee,
    run_labeling_job,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("jerymotro.render")

app = FastAPI(title="JeryMotro GEE Fire Context Labeling")
_worker_started = False
_worker_lock = threading.Lock()


@app.get("/")
def root() -> dict:
    return {
        "service": "jerymotro-gee-fire-context-labeling",
        "status": "running",
        "worker": "background",
    }


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": "jerymotro-gee-fire-context-labeling"},
    )


def labeling_worker() -> None:
    startup_delay = int(os.environ.get("LABELING_START_DELAY_SECONDS", "20"))
    interval_minutes = int(os.environ.get("INTERVAL_MINUTES", "360"))

    logger.info(
        "Serveur HTTP prêt. Le worker GEE commencera dans %d seconde(s).",
        startup_delay,
    )
    time.sleep(startup_delay)

    while True:
        try:
            config = load_config()

            if not config.enable_auto_labeling:
                logger.info("ENABLE_AUTO_LABELING=false : worker arrêté.")
                return

            logger.info("Initialisation de la connexion PostgreSQL du worker...")
            engine = create_engine(config.database_url, pool_pre_ping=True, future=True)

            logger.info("Vérification du schéma PostgreSQL...")
            ensure_columns_exist(engine)

            logger.info("Initialisation de Google Earth Engine...")
            initialize_gee(config)

            logger.info("=== Worker GEE démarré : collecte/labellisation ===")
            run_labeling_job(engine, config)
            logger.info("=== Cycle GEE terminé ===")

            engine.dispose()

        except Exception:
            logger.exception(
                "Erreur pendant le cycle GEE. Le serveur HTTP reste actif "
                "et le worker réessaiera."
            )

        logger.info(
            "Prochaine collecte automatique dans %d minute(s).",
            interval_minutes,
        )
        time.sleep(interval_minutes * 60)


@app.on_event("startup")
def start_worker() -> None:
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        thread = threading.Thread(
            target=labeling_worker,
            name="jerymotro-gee-worker",
            daemon=True,
        )
        thread.start()

    logger.info("Serveur HTTP Render démarré avant toute collecte GEE.")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
