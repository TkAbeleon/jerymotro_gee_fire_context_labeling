#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Temporary PostgreSQL WebSocket gateway for Render.

PC -> WSS -> Render -> TCP 5432 -> Layerbase PostgreSQL

Required:
    PG_GATEWAY_TOKEN

Optional:
    PG_REMOTE_HOST
    PG_REMOTE_PORT
"""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
import uvicorn


PG_REMOTE_HOST = os.environ.get(
    "PG_REMOTE_HOST",
    "jerymotro-numb-ghost-pooler.sage.cloud.layerbase.dev",
)
PG_REMOTE_PORT = int(os.environ.get("PG_REMOTE_PORT", "5432"))
PG_GATEWAY_TOKEN = os.environ.get("PG_GATEWAY_TOKEN")

BUFFER_SIZE = 64 * 1024
OPEN_TIMEOUT = 20


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not PG_GATEWAY_TOKEN:
        raise RuntimeError("PG_GATEWAY_TOKEN est obligatoire.")
    yield


app = FastAPI(
    title="JeryMotro PostgreSQL WebSocket Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "jerymotro-pg-websocket-gateway",
            "status": "ok",
            "websocket": "/wss",
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _authorized(websocket: WebSocket) -> bool:
    authorization = websocket.headers.get("authorization", "")
    expected = f"Bearer {PG_GATEWAY_TOKEN}"
    return hmac.compare_digest(authorization, expected)


async def pipe_websocket_to_postgres(
    websocket: WebSocket,
    postgres_writer: asyncio.StreamWriter,
) -> None:
    while True:
        message = await websocket.receive()

        if message["type"] == "websocket.disconnect":
            return

        if message["type"] != "websocket.receive":
            continue

        data = message.get("bytes")
        if data is None:
            await websocket.close(
                code=1003,
                reason="Binary messages only",
            )
            return

        postgres_writer.write(data)
        await postgres_writer.drain()


async def pipe_postgres_to_websocket(
    websocket: WebSocket,
    postgres_reader: asyncio.StreamReader,
) -> None:
    while True:
        data = await postgres_reader.read(BUFFER_SIZE)
        if not data:
            return
        await websocket.send_bytes(data)


@app.websocket("/wss")
async def postgres_websocket(websocket: WebSocket) -> None:
    peer = str(websocket.client)

    if not _authorized(websocket):
        print(f"[AUTH] Refus WebSocket : {peer}")
        await websocket.close(code=1008, reason="Unauthorized")
        return

    await websocket.accept()
    print(f"[+] WebSocket PostgreSQL : {peer}")

    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    tasks: set[asyncio.Task[Any]] = set()

    try:
        print(
            f"[DB] Connexion vers "
            f"{PG_REMOTE_HOST}:{PG_REMOTE_PORT}..."
        )

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                PG_REMOTE_HOST,
                PG_REMOTE_PORT,
            ),
            timeout=OPEN_TIMEOUT,
        )

        print("[DB] Connexion PostgreSQL établie.")

        ws_to_db = asyncio.create_task(
            pipe_websocket_to_postgres(websocket, writer)
        )
        db_to_ws = asyncio.create_task(
            pipe_postgres_to_websocket(websocket, reader)
        )
        tasks = {ws_to_db, db_to_ws}

        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = None

            if error:
                print(
                    f"[RELAY] Erreur : "
                    f"{type(error).__name__}: {error}"
                )

    except asyncio.TimeoutError:
        print("[DB] Timeout de connexion PostgreSQL.")
        try:
            await websocket.close(
                code=1013,
                reason="PostgreSQL connection timeout",
            )
        except Exception:
            pass

    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        try:
            await websocket.close(
                code=1011,
                reason="Gateway error",
            )
        except Exception:
            pass

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        print(f"[-] WebSocket PostgreSQL terminé : {peer}")


if __name__ == "__main__":
    uvicorn.run(
        "pg_websocket_gateway:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
