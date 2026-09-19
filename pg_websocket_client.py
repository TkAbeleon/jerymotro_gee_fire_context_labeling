#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local TCP -> Render WSS -> PostgreSQL bridge.

Environment:
    PG_WS_URL=wss://<service>.onrender.com/wss
    PG_GATEWAY_TOKEN=<Render secret>

Local listener:
    127.0.0.1:15432
"""

from __future__ import annotations

import asyncio
import os
import signal

from websockets.asyncio.client import connect


LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = int(os.environ.get("PG_LOCAL_PORT", "15432"))
PG_WS_URL = os.environ.get("PG_WS_URL")
PG_GATEWAY_TOKEN = os.environ.get("PG_GATEWAY_TOKEN")
BUFFER_SIZE = 64 * 1024
MAX_MESSAGE_SIZE = 8 * 1024 * 1024
stop_event = asyncio.Event()


def stop() -> None:
    stop_event.set()


async def pipe_local_to_websocket(reader, websocket) -> None:
    while True:
        data = await reader.read(BUFFER_SIZE)
        if not data:
            return
        await websocket.send(data)


async def pipe_websocket_to_local(websocket, writer) -> None:
    async for message in websocket:
        if not isinstance(message, bytes):
            raise RuntimeError("Le gateway a renvoyé un message texte.")
        writer.write(message)
        await writer.drain()


async def handle_client(reader, writer) -> None:
    peer = writer.get_extra_info("peername")
    tasks = set()
    print(f"[+] Connexion locale PostgreSQL : {peer}")

    try:
        if not PG_WS_URL:
            raise RuntimeError("PG_WS_URL n'est pas défini.")
        if not PG_GATEWAY_TOKEN:
            raise RuntimeError("PG_GATEWAY_TOKEN n'est pas défini.")

        async with connect(
            PG_WS_URL,
            additional_headers={
                "Authorization": f"Bearer {PG_GATEWAY_TOKEN}",
            },
            max_size=MAX_MESSAGE_SIZE,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            print(f"[WS] Connecté : {PG_WS_URL}")

            local_to_ws = asyncio.create_task(
                pipe_local_to_websocket(reader, websocket)
            )
            ws_to_local = asyncio.create_task(
                pipe_websocket_to_local(websocket, writer)
            )
            tasks = {local_to_ws, ws_to_local}

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
                    print(f"[RELAY] {type(error).__name__}: {error}")

    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        print(f"[-] Connexion locale terminée : {peer}")


async def main() -> None:
    if not PG_WS_URL:
        raise SystemExit(
            "PG_WS_URL obligatoire, ex. "
            "wss://mon-service.onrender.com/wss"
        )
    if not PG_GATEWAY_TOKEN:
        raise SystemExit("PG_GATEWAY_TOKEN obligatoire.")

    server = await asyncio.start_server(
        handle_client,
        LOCAL_HOST,
        LOCAL_PORT,
    )

    print("=" * 60)
    print("JeryMotro - PostgreSQL WebSocket local client")
    print("=" * 60)
    print(f"Local   : {LOCAL_HOST}:{LOCAL_PORT}")
    print(f"Gateway : {PG_WS_URL}")
    print("[OK] En attente de connexions PostgreSQL.")

    async with server:
        await stop_event.wait()

    server.close()
    await server.wait_closed()


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, stop)
        loop.add_signal_handler(signal.SIGTERM, stop)
    except (NotImplementedError, RuntimeError):
        pass

    asyncio.run(main())
