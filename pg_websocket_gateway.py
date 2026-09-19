#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Temporary PostgreSQL WebSocket gateway for AlwaysData.

Architecture:
    Local PC client
        |
        | WSS over HTTPS :443
        v
    AlwaysData WebSocket site
        |
        | TCP 5432
        v
    Layerbase PostgreSQL

The PostgreSQL wire protocol is transported as binary WebSocket messages.
The gateway does not parse or modify PostgreSQL messages.

Environment variables:
    PG_GATEWAY_TOKEN   required shared secret
    PG_REMOTE_HOST     default: jerymotro-numb-ghost-pooler.sage.cloud.layerbase.dev
    PG_REMOTE_PORT     default: 5432
    PG_WS_PATH         optional, informational only when behind AlwaysData pathUrl
"""

from __future__ import annotations

import asyncio
import hmac
import os
import socket
from typing import Any

from websockets.asyncio.server import ServerConnection, serve


PG_REMOTE_HOST = os.environ.get(
    "PG_REMOTE_HOST",
    "jerymotro-numb-ghost-pooler.sage.cloud.layerbase.dev",
)
PG_REMOTE_PORT = int(os.environ.get("PG_REMOTE_PORT", "5432"))
PG_GATEWAY_TOKEN = os.environ.get("PG_GATEWAY_TOKEN")

BUFFER_SIZE = 64 * 1024
OPEN_TIMEOUT = 20
MAX_MESSAGE_SIZE = 8 * 1024 * 1024

if not PG_GATEWAY_TOKEN:
    raise RuntimeError("PG_GATEWAY_TOKEN est obligatoire.")


def _authorized(connection: ServerConnection) -> bool:
    authorization = connection.request.headers.get("Authorization", "")
    expected = f"Bearer {PG_GATEWAY_TOKEN}"
    return hmac.compare_digest(authorization, expected)


async def pipe_websocket_to_postgres(
    websocket: ServerConnection,
    postgres_writer: asyncio.StreamWriter,
) -> None:
    async for message in websocket:
        if not isinstance(message, bytes):
            await websocket.close(code=1003, reason="Binary messages only")
            return

        postgres_writer.write(message)
        await postgres_writer.drain()


async def pipe_postgres_to_websocket(
    websocket: ServerConnection,
    postgres_reader: asyncio.StreamReader,
) -> None:
    while True:
        data = await postgres_reader.read(BUFFER_SIZE)
        if not data:
            return
        await websocket.send(data)


async def handle(websocket: ServerConnection) -> None:
    peer = "unknown"
    if websocket.remote_address:
        peer = str(websocket.remote_address)

    if not _authorized(websocket):
        print(f"[AUTH] Refus WebSocket : {peer}")
        await websocket.close(code=1008, reason="Unauthorized")
        return

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
            error = task.exception()
            if error and not isinstance(
                error,
                (ConnectionError, BrokenPipeError, asyncio.CancelledError),
            ):
                print(f"[RELAY] Erreur : {type(error).__name__}: {error}")

    except asyncio.TimeoutError:
        print("[DB] Timeout de connexion PostgreSQL.")
        await websocket.close(code=1013, reason="PostgreSQL connection timeout")

    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        try:
            await websocket.close(code=1011, reason="Gateway error")
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


async def main() -> None:
    host = os.environ.get("IP", "::")
    port = int(os.environ.get("PORT", "8765"))

    print("============================================================")
    print(" JeryMotro - PostgreSQL WebSocket Gateway")
    print("============================================================")
    print(f"Listen      : {host}:{port}")
    print(f"PostgreSQL  : {PG_REMOTE_HOST}:{PG_REMOTE_PORT}")
    print("Protocol    : WebSocket binary <-> PostgreSQL TCP")
    print("============================================================")

    async with serve(
        handle,
        host,
        port,
        max_size=MAX_MESSAGE_SIZE,
        ping_interval=20,
        ping_timeout=20,
    ):
        print("[OK] WebSocket gateway démarré.")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
