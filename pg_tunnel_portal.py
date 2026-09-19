#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JeryMotro - Portail PostgreSQL temporaire via SSH / AlwaysData
===============================================================

But
---
Permettre à une application locale de joindre un PostgreSQL distant
accessible depuis le serveur AlwaysData, alors que le port PostgreSQL
distant est bloqué directement par le fournisseur d'accès Internet local.

Architecture :

    PC
      |
      | 127.0.0.1:15432
      v
    Ce portail TCP
      |
      | ssh tk
      v
    AlwaysData
      |
      | TCP 5432
      v
    jerymotro-numb-ghost-pooler.sage.cloud.layerbase.dev:5432

Le portail est VOLONTAIREMENT TEMPORAIRE :
- aucun service systemd ;
- aucun daemon permanent ;
- aucune modification du serveur AlwaysData ;
- Ctrl+C arrête immédiatement le portail.

Le programme utilise l'alias SSH "tk" défini dans ~/.ssh/config.
Il ne demande donc pas de connaître ici l'hôte, le port SSH ou l'utilisateur.

Usage
-----
    python3 pg_tunnel_portal.py

Puis, dans un autre terminal local :

    psql "postgresql://postgres:MOT_DE_PASSE@127.0.0.1:15432/jerymotro?sslmode=require"

Important
---------
Le portail ne connaît ni ne stocke le mot de passe PostgreSQL.
Il transporte simplement les octets TCP entre PostgreSQL local et distant.
"""

from __future__ import annotations

import base64
import signal
import shlex
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SSH_ALIAS = "tk"

LOCAL_HOST = "127.0.0.1"

# Port local FIXE du portail.
# 5432 est volontairement évité : il est souvent occupé par PostgreSQL local.
LOCAL_PORT = 15432

REMOTE_HOST = "jerymotro-numb-ghost-pooler.sage.cloud.layerbase.dev"
REMOTE_PORT = 5432

BUFFER_SIZE = 64 * 1024
SOCKET_TIMEOUT = 30

# Messages SSH éventuels : ne jamais les mélanger au flux PostgreSQL stdout.
SSH_STDERR = subprocess.DEVNULL


@dataclass
class ConnectionStats:
    sent_bytes: int = 0
    received_bytes: int = 0


# ---------------------------------------------------------------------------
# Arrêt propre
# ---------------------------------------------------------------------------

stop_event = threading.Event()


def handle_signal(signum: int, frame) -> None:  # noqa: ARG001
    print(f"\n[!] Signal {signum} reçu. Arrêt du portail...")
    stop_event.set()


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def close_socket(sock: socket.socket | None) -> None:
    if sock is None:
        return

    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass

    try:
        sock.close()
    except OSError:
        pass


def close_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None:
        return

    if proc.poll() is not None:
        return

    try:
        proc.terminate()
    except OSError:
        pass

    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test SSH
# ---------------------------------------------------------------------------


def test_ssh_alias() -> bool:
    """
    Vérifie que l'alias SSH "tk" fonctionne avant de commencer à écouter.
    Aucun shell interactif n'est lancé.
    """
    print(f"[*] Vérification de l'alias SSH : {SSH_ALIAS}")

    try:
        result = subprocess.run(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                SSH_ALIAS,
                "printf 'JERYMOTRO_SSH_OK'",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
    except FileNotFoundError:
        print("[ERREUR] La commande 'ssh' est introuvable.")
        return False
    except subprocess.TimeoutExpired:
        print("[ERREUR] Timeout pendant le test SSH.")
        return False

    stdout = result.stdout.decode("utf-8", errors="replace").strip()

    if result.returncode != 0 or "JERYMOTRO_SSH_OK" not in stdout:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        print("[ERREUR] L'alias SSH 'tk' n'est pas utilisable en BatchMode.")
        if stderr:
            print(f"        SSH : {stderr}")
        return False

    print("[OK] Alias SSH 'tk' fonctionnel.")
    return True


# ---------------------------------------------------------------------------
# Processus SSH
# ---------------------------------------------------------------------------


def build_remote_command() -> str:
    """
    Construit le programme Python exécuté temporairement sur AlwaysData.

    Le code est encodé en Base64 avant d'être envoyé à SSH afin d'éviter
    les problèmes de quoting avec les fonctions Python et les caractères
    spéciaux du programme distant.

    Le processus distant :
      - ouvre TCP vers le PostgreSQL Layerbase ;
      - lit stdin (flux PostgreSQL venant du PC) ;
      - écrit stdout (réponses PostgreSQL vers le PC).
    """
    remote_program = f"""
import socket
import sys
import threading

HOST = {REMOTE_HOST!r}
PORT = {REMOTE_PORT!r}

sock = socket.create_connection((HOST, PORT), timeout={SOCKET_TIMEOUT!r})
sock.settimeout(None)

def client_to_database():
    try:
        while True:
            data = sys.stdin.buffer.read({BUFFER_SIZE})
            if not data:
                break
            sock.sendall(data)
    except Exception:
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except Exception:
            pass

def database_to_client():
    try:
        while True:
            data = sock.recv({BUFFER_SIZE})
            if not data:
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except Exception:
        pass

thread_up = threading.Thread(target=client_to_database, daemon=True)
thread_down = threading.Thread(target=database_to_client, daemon=True)

thread_up.start()
thread_down.start()

thread_up.join()
thread_down.join()

try:
    sock.close()
except Exception:
    pass
""".strip()

    encoded = base64.b64encode(
        remote_program.encode("utf-8")
    ).decode("ascii")

    python_code = (
        "import base64; "
        "exec(base64.b64decode(" + repr(encoded) + "))"
    )
    return "python3 -u -c " + shlex.quote(python_code)


def start_ssh_process() -> subprocess.Popen[bytes]:
    """
    Ouvre une connexion SSH non interactive vers AlwaysData.

    stdout = flux brut vers PostgreSQL.
    stderr est volontairement ignoré pour ne jamais contaminer stdout.
    """
    return subprocess.Popen(
        [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=20",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "TCPKeepAlive=yes",
            "-o",
            "LogLevel=ERROR",
            SSH_ALIAS,
            build_remote_command(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=SSH_STDERR,
        bufsize=0,
    )


# ---------------------------------------------------------------------------
# Relais bidirectionnel
# ---------------------------------------------------------------------------


def pipe_socket_to_ssh(
    client: socket.socket,
    proc: subprocess.Popen[bytes],
    stats: ConnectionStats,
) -> None:
    if proc.stdin is None:
        return

    try:
        while not stop_event.is_set():
            data = client.recv(BUFFER_SIZE)

            if not data:
                break

            proc.stdin.write(data)
            proc.stdin.flush()
            stats.sent_bytes += len(data)

    except (BrokenPipeError, ConnectionResetError, OSError):
        pass

    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def pipe_ssh_to_socket(
    client: socket.socket,
    proc: subprocess.Popen[bytes],
    stats: ConnectionStats,
) -> None:
    if proc.stdout is None:
        return

    try:
        while not stop_event.is_set():
            data = proc.stdout.read(BUFFER_SIZE)

            if not data:
                break

            client.sendall(data)
            stats.received_bytes += len(data)

    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


# ---------------------------------------------------------------------------
# Connexion client
# ---------------------------------------------------------------------------


def handle_client(
    client: socket.socket,
    address: tuple[str, int],
) -> None:
    stats = ConnectionStats()
    proc: subprocess.Popen[bytes] | None = None

    print(f"[+] Connexion PostgreSQL : {address[0]}:{address[1]}")

    try:
        client.settimeout(None)

        proc = start_ssh_process()

        if proc.stdin is None or proc.stdout is None:
            raise RuntimeError("Impossible d'ouvrir stdin/stdout du processus SSH.")

        to_ssh = threading.Thread(
            target=pipe_socket_to_ssh,
            args=(client, proc, stats),
            daemon=True,
            name="pg-client-to-ssh",
        )

        to_client = threading.Thread(
            target=pipe_ssh_to_socket,
            args=(client, proc, stats),
            daemon=True,
            name="pg-ssh-to-client",
        )

        to_ssh.start()
        to_client.start()

        to_ssh.join()
        to_client.join()

    except Exception as exc:  # noqa: BLE001
        print(f"[ERREUR] Connexion {address}: {type(exc).__name__}: {exc}")

    finally:
        close_process(proc)
        close_socket(client)

    print(
        f"[-] Connexion terminée : {address[0]}:{address[1]} | "
        f"PC→PG={stats.sent_bytes} octets | "
        f"PG→PC={stats.received_bytes} octets"
    )


# ---------------------------------------------------------------------------
# Serveur local
# ---------------------------------------------------------------------------


def run_server() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((LOCAL_HOST, LOCAL_PORT))
        local_port = LOCAL_PORT

        server.listen(20)
        server.settimeout(1.0)
    except OSError as exc:
        close_socket(server)
        print(
            f"[ERREUR] Impossible d'écouter sur {LOCAL_HOST}:{LOCAL_PORT} : "
            f"{type(exc).__name__}: {exc}"
        )
        print(
            f"[INFO] Le port local est fixe : {LOCAL_PORT}. "
            "Choisissez un autre port dans le code s'il est déjà occupé."
        )
        raise SystemExit(1) from exc

    print()
    print("=" * 72)
    print(" JERYMOTRO — PORTAIL POSTGRESQL TEMPORAIRE")
    print("=" * 72)
    print(f" Local      : {LOCAL_HOST}:{local_port}")
    print(f" SSH        : {SSH_ALIAS}")
    print(f" PostgreSQL : {REMOTE_HOST}:{REMOTE_PORT}")
    print("=" * 72)
    print()
    print("[OK] Portail démarré.")
    print(
        f"[INFO] Les connexions locales vers {LOCAL_HOST}:{local_port} "
        "seront relayées"
    )
    print(
        f"[INFO] Exemple psql : "
        f"postgresql://postgres:MOT_DE_PASSE@{LOCAL_HOST}:{local_port}/jerymotro"
        "?sslmode=require"
    )
    print("[INFO] via SSH 'tk' vers le PostgreSQL distant.")
    print("[INFO] Ctrl+C pour arrêter.")
    print()

    try:
        while not stop_event.is_set():
            try:
                client, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop_event.is_set():
                    break
                raise

            thread = threading.Thread(
                target=handle_client,
                args=(client, address),
                daemon=True,
                name=f"pg-client-{address[0]}-{address[1]}",
            )
            thread.start()

    except KeyboardInterrupt:
        stop_event.set()

    finally:
        close_socket(server)

    print("[OK] Portail PostgreSQL arrêté.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if not test_ssh_alias():
        return 1

    run_server()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
