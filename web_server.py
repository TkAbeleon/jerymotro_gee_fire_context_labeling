#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Serveur HTTP Render + console web de supervision + worker GEE."""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine

from gee_fire_context_labeling import (
    load_config,
    ensure_columns_exist,
    initialize_gee,
    run_labeling_job,
)

# ---------------------------------------------------------------------------
# Logging centralisé pour la console web
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _redact_secrets(message: str) -> str:
    """Évite d'afficher des secrets évidents dans la console web."""
    message = re.sub(
        r"(postgresql(?:\+[^:]+)?://[^:/\s]+:)[^@\s]+(@)",
        r"\1••••••\2",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(
        r"([\"']?(?:password|token|secret|private_key)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+",
        r"\1••••••",
        message,
        flags=re.IGNORECASE,
    )
    return message


class LogBufferHandler(logging.Handler):
    """Conserve les derniers logs en mémoire pour /api/logs."""

    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.buffer: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.lock = threading.RLock()
        self.next_id = 1

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = _redact_secrets(self.format(record))
            with self.lock:
                item = {
                    "id": self.next_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
                self.next_id += 1
                self.buffer.append(item)

            _update_status_from_log(message, record.levelname)
        except Exception:
            self.handleError(record)

    def snapshot(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.lock:
            items = list(self.buffer)
        return items[-limit:]


log_buffer = LogBufferHandler(maxlen=500)
log_buffer.setFormatter(
    logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)

# Capturer les logs du module GEE et des bibliothèques sans empêcher Render
# de continuer à écrire les logs dans stdout/stderr.
root_logger = logging.getLogger()
root_logger.addHandler(log_buffer)

logger = logging.getLogger("jerymotro.render")

# ---------------------------------------------------------------------------
# État du service
# ---------------------------------------------------------------------------

STATE_LOCK = threading.RLock()
SERVICE_STATE: dict[str, Any] = {
    "server": "online",
    "worker": "starting",
    "cycle": "waiting",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_activity": None,
    "last_cycle_started": None,
    "last_cycle_finished": None,
    "last_error": None,
    "total_processed": 0,
    "total_labeled": 0,
    "current_batch": 0,
    "current_batch_labeled": 0,
    "next_run_in_seconds": None,
}


def _update_status_from_log(message: str, level: str) -> None:
    """Met à jour les indicateurs visibles à partir des logs métier."""
    now = datetime.now(timezone.utc).isoformat()

    progression = re.search(
        r"Progression cumulée\s*:\s*(\d+) détection\(s\) traitées,\s*(\d+) labellisées",
        message,
    )
    batch = re.search(
        r"Lot traité\s*:\s*(\d+)\/(\d+) détections labellisées",
        message,
    )

    with STATE_LOCK:
        SERVICE_STATE["last_activity"] = now

        if "=== Worker GEE démarré" in message:
            SERVICE_STATE["worker"] = "running"
            SERVICE_STATE["cycle"] = "running"
            SERVICE_STATE["last_cycle_started"] = now
            SERVICE_STATE["last_error"] = None
        elif "=== Cycle GEE terminé ===" in message:
            SERVICE_STATE["worker"] = "idle"
            SERVICE_STATE["cycle"] = "completed"
            SERVICE_STATE["last_cycle_finished"] = now
        elif "Prochaine collecte automatique" in message:
            SERVICE_STATE["worker"] = "scheduled"
            SERVICE_STATE["cycle"] = "waiting"
            match = re.search(r"dans (\d+) minute", message)
            if match:
                SERVICE_STATE["next_run_in_seconds"] = int(match.group(1)) * 60
        elif "Erreur pendant le cycle GEE" in message or level == "ERROR":
            SERVICE_STATE["worker"] = "error"
            SERVICE_STATE["cycle"] = "error"
            SERVICE_STATE["last_error"] = message

        if progression:
            SERVICE_STATE["total_processed"] = int(progression.group(1))
            SERVICE_STATE["total_labeled"] = int(progression.group(2))

        if batch:
            SERVICE_STATE["current_batch_labeled"] = int(batch.group(1))
            SERVICE_STATE["current_batch"] = int(batch.group(2))


app = FastAPI(
    title="JeryMotro GEE Fire Context Labeling",
    docs_url="/docs",
    redoc_url=None,
)

_worker_started = False
_worker_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Console web
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JeryMotro · GEE Monitor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b0d10;
      --panel: #11151a;
      --panel-2: #0d1116;
      --border: #20262e;
      --text: #eef2f6;
      --muted: #8b96a3;
      --green: #45d483;
      --amber: #f4bd4f;
      --red: #ff6b76;
      --blue: #64a8ff;
      --shadow: 0 18px 50px rgba(0,0,0,.22);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top right, rgba(100,168,255,.08), transparent 28%),
        radial-gradient(circle at top left, rgba(69,212,131,.06), transparent 24%),
        var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 100vh;
    }
    .wrap { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 36px; }
    header { display:flex; align-items:flex-start; justify-content:space-between; gap:20px; margin-bottom:22px; }
    .eyebrow { color: var(--blue); font-size: 12px; font-weight: 700; letter-spacing:.12em; text-transform:uppercase; }
    h1 { margin:5px 0 5px; font-size: clamp(24px, 4vw, 36px); letter-spacing:-.04em; }
    .sub { margin:0; color:var(--muted); font-size:14px; }
    .status-pill {
      display:inline-flex; align-items:center; gap:8px; padding:9px 12px;
      border:1px solid var(--border); border-radius:999px; background:rgba(17,21,26,.85);
      font-size:13px; white-space:nowrap;
    }
    .dot { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 0 4px rgba(69,212,131,.1); }
    .dot.amber { background:var(--amber); box-shadow:0 0 0 4px rgba(244,189,79,.1); }
    .dot.red { background:var(--red); box-shadow:0 0 0 4px rgba(255,107,118,.1); }
    .grid { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:12px; }
    .card {
      background:linear-gradient(180deg, rgba(17,21,26,.94), rgba(13,17,22,.94));
      border:1px solid var(--border); border-radius:16px; padding:16px; box-shadow:var(--shadow);
    }
    .label { color:var(--muted); font-size:12px; margin-bottom:9px; }
    .value { font-size:23px; font-weight:750; letter-spacing:-.02em; }
    .value.small { font-size:16px; }
    .layout { display:grid; grid-template-columns:minmax(0, 1fr) 330px; gap:12px; margin-top:12px; }
    .panel-title { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:12px; }
    .panel-title strong { font-size:15px; }
    .muted { color:var(--muted); font-size:12px; }
    .logs {
      height:560px; overflow:auto; background:#080a0d; border:1px solid var(--border);
      border-radius:12px; padding:13px; font:12px/1.65 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .log { padding:2px 0; word-break:break-word; }
    .log.INFO { color:#d5dbe2; }
    .log.WARNING { color:var(--amber); }
    .log.ERROR, .log.CRITICAL { color:var(--red); }
    .log.DEBUG { color:#84909e; }
    .side { display:flex; flex-direction:column; gap:12px; }
    .row { display:flex; justify-content:space-between; gap:15px; padding:9px 0; border-bottom:1px solid var(--border); }
    .row:last-child { border-bottom:0; }
    .row span:first-child { color:var(--muted); }
    .row span:last-child { text-align:right; font-weight:650; }
    .progress { height:8px; border-radius:999px; background:#1a2027; overflow:hidden; margin-top:12px; }
    .progress > div { height:100%; width:100%; background:linear-gradient(90deg, var(--blue), var(--green)); transform-origin:left; transition:transform .35s ease; }
    footer { margin-top:12px; text-align:right; color:var(--muted); font-size:11px; }
    button {
      border:1px solid var(--border); background:#151a20; color:var(--text); border-radius:9px;
      padding:7px 10px; cursor:pointer;
    }
    button:hover { background:#1a2027; }
    @media (max-width: 900px) { .grid { grid-template-columns:repeat(2,1fr); } .layout { grid-template-columns:1fr; } .logs { height:430px; } }
    @media (max-width: 580px) { .wrap { width:min(100% - 20px, 1180px); padding-top:18px; } header { align-items:flex-start; } .grid { grid-template-columns:1fr 1fr; } }
  </style>
</head>
<body>
  <main class="wrap">
    <header>
      <div>
        <div class="eyebrow">JeryMotro · Monitoring</div>
        <h1>GEE Fire Context</h1>
        <p class="sub">Supervision du serveur Render et du worker de labellisation.</p>
      </div>
      <div class="status-pill"><span class="dot" id="statusDot"></span><span id="statusText">Connexion…</span></div>
    </header>

    <section class="grid">
      <div class="card"><div class="label">Worker</div><div class="value small" id="workerState">—</div></div>
      <div class="card"><div class="label">Cycle</div><div class="value small" id="cycleState">—</div></div>
      <div class="card"><div class="label">Détections traitées</div><div class="value" id="processed">0</div></div>
      <div class="card"><div class="label">Labellisées</div><div class="value" id="labeled">0</div></div>
    </section>

    <section class="layout">
      <div class="card">
        <div class="panel-title">
          <strong>Logs en direct</strong>
          <button onclick="clearLogs()">Effacer l’affichage</button>
        </div>
        <div class="logs" id="logs"><div class="muted">En attente des logs…</div></div>
      </div>

      <aside class="side">
        <div class="card">
          <div class="panel-title"><strong>État du service</strong><span class="muted" id="refresh">—</span></div>
          <div class="row"><span>Démarré</span><span id="started">—</span></div>
          <div class="row"><span>Dernière activité</span><span id="activity">—</span></div>
          <div class="row"><span>Dernier cycle</span><span id="finished">—</span></div>
          <div class="row"><span>Prochaine collecte</span><span id="next">—</span></div>
        </div>

        <div class="card">
          <div class="panel-title"><strong>Dernier lot</strong><span class="muted">GEE</span></div>
          <div class="row"><span>Labellisé</span><span id="batchLabel">0</span></div>
          <div class="row"><span>Taille</span><span id="batchSize">0</span></div>
          <div class="progress"><div id="batchProgress"></div></div>
        </div>

        <div class="card">
          <div class="panel-title"><strong>Erreur récente</strong></div>
          <div class="muted" id="error" style="white-space:pre-wrap;word-break:break-word">Aucune erreur.</div>
        </div>
      </aside>
    </section>

    <footer>Actualisation automatique · <span id="clock">—</span></footer>
  </main>

<script>
let followLogs = true;
let renderedLogIds = new Set();

function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  return isNaN(d) ? value : d.toLocaleString("fr-FR", {dateStyle:"short", timeStyle:"medium"});
}
function esc(value) {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}
function stateLabel(value) {
  return ({
    starting:"Démarrage",
    running:"En cours",
    idle:"En attente",
    scheduled:"Planifié",
    completed:"Terminé",
    waiting:"En attente",
    error:"Erreur"
  })[value] || value || "—";
}
function updateClock() {
  document.getElementById("clock").textContent = new Date().toLocaleTimeString("fr-FR");
}
function clearLogs() {
  document.getElementById("logs").innerHTML = "";
  renderedLogIds.clear();
}
async function refresh() {
  try {
    const [s, l] = await Promise.all([
      fetch("/api/status", {cache:"no-store"}).then(r => r.json()),
      fetch("/api/logs?limit=300", {cache:"no-store"}).then(r => r.json())
    ]);

    document.getElementById("statusText").textContent = "Serveur opérationnel";
    document.getElementById("statusDot").className = "dot" + (s.worker === "error" ? " red" : (s.worker === "starting" ? " amber" : ""));
    document.getElementById("workerState").textContent = stateLabel(s.worker);
    document.getElementById("cycleState").textContent = stateLabel(s.cycle);
    document.getElementById("processed").textContent = Number(s.total_processed || 0).toLocaleString("fr-FR");
    document.getElementById("labeled").textContent = Number(s.total_labeled || 0).toLocaleString("fr-FR");
    document.getElementById("started").textContent = fmtDate(s.started_at);
    document.getElementById("activity").textContent = fmtDate(s.last_activity);
    document.getElementById("finished").textContent = fmtDate(s.last_cycle_finished);
    document.getElementById("next").textContent = s.next_run_in_seconds ? Math.round(s.next_run_in_seconds / 60) + " min" : "—";
    document.getElementById("batchLabel").textContent = s.current_batch_labeled || 0;
    document.getElementById("batchSize").textContent = s.current_batch || 0;
    const p = s.current_batch ? Math.min(1, s.current_batch_labeled / s.current_batch) : 0;
    document.getElementById("batchProgress").style.transform = "scaleX(" + p + ")";
    document.getElementById("error").textContent = s.last_error || "Aucune erreur.";
    document.getElementById("refresh").textContent = "à l'instant";

    const box = document.getElementById("logs");
    for (const item of (l.logs || [])) {
      if (renderedLogIds.has(item.id)) continue;
      if (box.children.length === 1 && box.firstElementChild?.classList.contains("muted")) box.innerHTML = "";
      const line = document.createElement("div");
      line.className = "log " + item.level;
      line.innerHTML = esc(item.message);
      box.appendChild(line);
      renderedLogIds.add(item.id);
    }

    if (followLogs) box.scrollTop = box.scrollHeight;
  } catch (err) {
    document.getElementById("statusText").textContent = "Serveur inaccessible";
    document.getElementById("statusDot").className = "dot red";
  }
}
document.getElementById("logs").addEventListener("scroll", function() {
  followLogs = this.scrollTop + this.clientHeight >= this.scrollHeight - 40;
});
updateClock();
refresh();
setInterval(refresh, 2000);
setInterval(updateClock, 1000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/api/status")
def status() -> dict[str, Any]:
    with STATE_LOCK:
        snapshot = dict(SERVICE_STATE)
    snapshot["server"] = "online"
    return snapshot


@app.get("/api/logs")
def logs(limit: int = Query(default=300, ge=1, le=500)) -> dict[str, Any]:
    return {"logs": log_buffer.snapshot(limit=limit)}


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": "jerymotro-gee-fire-context-labeling"},
    )


# ---------------------------------------------------------------------------
# Worker GEE
# ---------------------------------------------------------------------------


def labeling_worker() -> None:
    startup_delay = int(os.environ.get("LABELING_START_DELAY_SECONDS", "20"))
    interval_minutes = int(os.environ.get("INTERVAL_MINUTES", "360"))

    with STATE_LOCK:
        SERVICE_STATE["worker"] = "starting"
        SERVICE_STATE["cycle"] = "waiting"

    logger.info(
        "Serveur HTTP prêt. Le worker GEE commencera dans %d seconde(s).",
        startup_delay,
    )
    time.sleep(startup_delay)

    while True:
        engine = None
        try:
            # Le service Render est dédié à la collecte automatique.
            # On force donc l'activation du worker dans le processus HTTP,
            # même si une ancienne variable Render contient false.
            raw_auto = os.environ.get("ENABLE_AUTO_LABELING", "").strip().lower()
            if raw_auto not in {"1", "true", "yes", "on"}:
                logger.warning(
                    "ENABLE_AUTO_LABELING=%r détecté dans Render : activation "
                    "automatique forcée pour le Web Service.",
                    raw_auto or "<absent>",
                )
                os.environ["ENABLE_AUTO_LABELING"] = "true"

            config = load_config()

            if not config.enable_auto_labeling:
                with STATE_LOCK:
                    SERVICE_STATE["worker"] = "idle"
                    SERVICE_STATE["cycle"] = "disabled"
                logger.error("Le worker automatique n'a pas pu être activé.")
                return

            logger.info("Initialisation de la connexion PostgreSQL du worker...")
            engine = create_engine(config.database_url, pool_pre_ping=True, future=True)

            logger.info("Vérification du schéma PostgreSQL...")
            ensure_columns_exist(engine)

            logger.info("Initialisation de Google Earth Engine...")
            initialize_gee(config)

            with STATE_LOCK:
                SERVICE_STATE["worker"] = "running"
                SERVICE_STATE["cycle"] = "running"
                SERVICE_STATE["last_cycle_started"] = datetime.now(timezone.utc).isoformat()
                SERVICE_STATE["last_error"] = None
                SERVICE_STATE["next_run_in_seconds"] = None

            logger.info("=== Worker GEE démarré : collecte/labellisation ===")
            run_labeling_job(engine, config)
            logger.info("=== Cycle GEE terminé ===")

        except Exception:
            with STATE_LOCK:
                SERVICE_STATE["worker"] = "error"
                SERVICE_STATE["cycle"] = "error"
                SERVICE_STATE["last_error"] = "Une erreur est survenue. Consultez les logs ci-dessus."
            logger.exception(
                "Erreur pendant le cycle GEE. Le serveur HTTP reste actif "
                "et le worker réessaiera."
            )

        finally:
            if engine is not None:
                engine.dispose()

        with STATE_LOCK:
            SERVICE_STATE["worker"] = "scheduled"
            SERVICE_STATE["cycle"] = "waiting"
            SERVICE_STATE["next_run_in_seconds"] = interval_minutes * 60
            SERVICE_STATE["last_cycle_finished"] = datetime.now(timezone.utc).isoformat()

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
