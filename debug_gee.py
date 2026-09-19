#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JeryMotro - Diagnostic GEE / Service Account
=============================================

Ce script ne lance PAS le traitement des feux et ne modifie PAS la base.
Il sert uniquement à identifier précisément où échoue l'accès à Google
Earth Engine.

Il teste séparément :
  1. Python et les versions des bibliothèques ;
  2. les variables GEE du fichier .env / environnement ;
  3. le fichier JSON du Service Account (sans afficher la clé privée) ;
  4. la création des credentials Google ;
  5. le rafraîchissement du token OAuth2 ;
  6. l'accès réseau HTTPS à l'API Earth Engine ;
  7. l'accès au document de découverte Google API ;
  8. ee.Initialize(..., project=...) ;
  9. un petit appel GEE après initialisation.

Usage :
    python debug_gee.py
    python debug_gee.py --verbose

Le code retourne un code de sortie non nul si un test critique échoue.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import ssl
import sys
import traceback
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:
    requests = None  # type: ignore[assignment]

try:
    import dotenv
    from dotenv import load_dotenv
except Exception:
    dotenv = None  # type: ignore[assignment]
    load_dotenv = None  # type: ignore[assignment]

try:
    import ee
except Exception:
    ee = None  # type: ignore[assignment]

try:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account
except Exception:
    google = None  # type: ignore[assignment]
    GoogleAuthRequest = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]


DISCOVERY_URL = (
    "https://earthengine.googleapis.com/$discovery/rest?version=v1&prettyPrint=false"
)
EE_API_ROOT = "https://earthengine.googleapis.com"
REQUIRED_SCOPES = ("https://www.googleapis.com/auth/earthengine",)
ENV_KEYS = (
    "GEE_AUTH_MODE",
    "GEE_SERVICE_ACCOUNT_JSON_PATH",
    "GEE_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


class Diagnostic:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.failed = False
        self.warnings = 0
        self.project_id: str | None = None
        self.service_account_email: str | None = None
        self.credentials: Any = None
        self.key_path: Path | None = None

    @staticmethod
    def line() -> None:
        print("=" * 88)

    def title(self, name: str) -> None:
        self.line()
        print(f"[TEST] {name}")
        self.line()

    def ok(self, message: str) -> None:
        print(f"OK   {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"WARN {message}")

    def fail(self, message: str) -> None:
        self.failed = True
        print(f"FAIL {message}")

    def info(self, message: str) -> None:
        print(f"INFO {message}")

    def exception(self, label: str, exc: BaseException) -> None:
        self.fail(f"{label}: {type(exc).__name__}: {exc}")
        print("      Traceback complet :")
        traceback.print_exc()

    def run(self) -> int:
        self._test_runtime()
        self._load_env()
        self._test_environment()
        self._load_service_account()
        self._test_google_credentials()
        self._test_dns_tls()
        self._test_discovery_http()
        self._test_ee_initialize()
        self._test_ee_request()

        self.line()
        if self.failed:
            print("RESULTAT : ECHEC - au moins un test critique a échoué.")
            print("Les blocs FAIL ci-dessus indiquent l'étape exacte à corriger.")
            return 1

        if self.warnings:
            print(f"RESULTAT : OK avec {self.warnings} avertissement(s).")
        else:
            print("RESULTAT : TOUS LES TESTS SONT PASSES.")
        return 0

    def _test_runtime(self) -> None:
        self.title("1/9 - Environnement Python et bibliothèques")

        self.info(f"Python      : {sys.version.replace(chr(10), ' ')}")
        self.info(f"Executable  : {sys.executable}")
        self.info(f"Plateforme  : {platform.platform()}")
        self.info(f"OpenSSL     : {ssl.OPENSSL_VERSION}")

        if ee is None:
            self.fail("Le module 'earthengine-api' (import ee) est indisponible.")
        else:
            self.ok(f"earthengine-api : {getattr(ee, '__version__', 'version inconnue')}")

        if requests is None:
            self.fail("Le module 'requests' est indisponible.")
        else:
            self.ok(f"requests         : {getattr(requests, '__version__', 'version inconnue')}")

        if dotenv is None:
            self.fail("Le module 'python-dotenv' est indisponible.")
        else:
            self.ok(f"python-dotenv    : {getattr(dotenv, '__version__', 'version inconnue')}")

        try:
            import googleapiclient

            self.ok(
                "google-api-python-client : "
                + getattr(googleapiclient, "__version__", "version inconnue")
            )
        except Exception as exc:
            self.exception("Import google-api-python-client", exc)

        try:
            import google.auth

            self.ok(
                "google-auth : "
                + getattr(google.auth, "__version__", "version inconnue")
            )
        except Exception as exc:
            self.exception("Import google-auth", exc)

    def _load_env(self) -> None:
        self.title("2/9 - Chargement du .env et des variables GEE")

        if load_dotenv is None:
            return

        env_path = Path.cwd() / ".env"
        if env_path.is_file():
            load_dotenv(dotenv_path=env_path, override=False)
            self.ok(f".env trouvé : {env_path}")
        else:
            load_dotenv(override=False)
            self.warn(f".env introuvable dans {Path.cwd()} ; environnement système utilisé.")

        for key in ENV_KEYS:
            value = os.environ.get(key)
            if value is None or value == "":
                self.info(f"{key}=<non défini>")
            else:
                if "PATH" in key:
                    self.info(f"{key}={value}")
                elif "EMAIL" in key:
                    self.info(f"{key}={value}")
                else:
                    self.info(f"{key}={value}")

        mode = os.environ.get("GEE_AUTH_MODE", "service_account").strip().lower()
        self.ok(f"Mode d'authentification effectivement sélectionné : {mode}")

        if mode == "browser":
            self.warn(
                "GEE_AUTH_MODE=browser est actif. Ce script est conçu pour diagnostiquer "
                "le Service Account ; il ne lance volontairement pas ee.Authenticate()."
            )
        elif mode != "service_account":
            self.fail(f"GEE_AUTH_MODE invalide : {mode!r}")

    def _test_environment(self) -> None:
        self.title("3/9 - Vérification de la configuration effective")

        mode = os.environ.get("GEE_AUTH_MODE", "service_account").strip().lower()
        if mode != "service_account":
            self.warn("Les tests Service Account sont partiellement désactivés car le mode n'est pas service_account.")

        raw_path = os.environ.get("GEE_SERVICE_ACCOUNT_JSON_PATH")
        if not raw_path:
            self.fail("GEE_SERVICE_ACCOUNT_JSON_PATH n'est pas défini.")
            return

        self.key_path = Path(raw_path).expanduser().resolve()
        self.info(f"Chemin credentials : {self.key_path}")

        if not self.key_path.is_file():
            self.fail(f"Fichier Service Account introuvable : {self.key_path}")
            return

        self.ok("Le fichier Service Account existe et est un fichier normal.")

        project_env = os.environ.get("GEE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project_env:
            self.project_id = project_env.strip()
            self.ok(f"Projet GEE fourni par l'environnement : {self.project_id}")
        else:
            self.warn(
                "Aucun GEE_PROJECT_ID / GOOGLE_CLOUD_PROJECT dans l'environnement. "
                "Le script tentera d'utiliser project_id du JSON."
            )

    def _load_service_account(self) -> None:
        self.title("4/9 - Lecture et validation du JSON Service Account")

        if self.key_path is None or not self.key_path.is_file():
            return

        try:
            raw = self.key_path.read_text(encoding="utf-8")
            key_data = json.loads(raw)
        except Exception as exc:
            self.exception("Lecture JSON Service Account", exc)
            return

        required_fields = ("type", "project_id", "private_key_id", "private_key", "client_email", "token_uri")
        missing = [field for field in required_fields if not key_data.get(field)]
        if missing:
            self.fail(f"Champs JSON manquants : {', '.join(missing)}")
            return

        self.service_account_email = str(key_data["client_email"])
        json_project = str(key_data["project_id"])

        self.ok(f"type           = {key_data['type']}")
        self.ok(f"project_id     = {json_project}")
        self.ok(f"client_email   = {self.service_account_email}")
        self.ok("private_key_id = présent")
        self.ok("private_key    = présente (contenu masqué)")
        self.ok(f"token_uri      = {key_data['token_uri']}")

        if self.project_id is None:
            self.project_id = json_project
            self.ok(f"Projet sélectionné depuis le JSON : {self.project_id}")
        elif self.project_id != json_project:
            self.warn(
                "GEE_PROJECT_ID ne correspond pas au project_id du JSON : "
                f"env={self.project_id!r}, json={json_project!r}"
            )

        if key_data.get("type") != "service_account":
            self.warn(
                f"Le champ type vaut {key_data.get('type')!r}, pas 'service_account'."
            )

        if service_account is None:
            self.fail("google-auth / service_account est indisponible.")
            return

        try:
            self.credentials = service_account.Credentials.from_service_account_file(
                str(self.key_path),
                scopes=list(REQUIRED_SCOPES),
            )
            self.ok("Credentials Google créées avec succès.")
            self.info(f"Credentials principal : {self.credentials.service_account_email}")
            self.info(f"Scopes configurés     : {', '.join(self.credentials.scopes or [])}")
            self.info(f"Quota project         : {self.credentials.quota_project_id!r}")
        except Exception as exc:
            self.exception("Création des credentials Google", exc)

    def _test_google_credentials(self) -> None:
        self.title("5/9 - Test réel du token OAuth2 du Service Account")

        if self.credentials is None or GoogleAuthRequest is None:
            return

        try:
            request = GoogleAuthRequest()
            self.credentials.refresh(request)

            token = self.credentials.token
            if not token:
                self.fail("Le refresh a réussi mais aucun access token n'a été retourné.")
                return

            self.ok("Le Service Account a obtenu un access token OAuth2.")
            self.info(f"Token obtenu : oui (longueur={len(token)})")
            self.info(
                f"Expiration     : {self.credentials.expiry.isoformat() if self.credentials.expiry else 'inconnue'}"
            )
        except Exception as exc:
            self.exception(
                "Refresh OAuth2 du Service Account",
                exc,
            )
            print(
                "\n      INTERPRETATION : si ce test échoue, le problème est avant Earth Engine "
                "(clé, signature, service account, token endpoint, horloge système ou accès réseau)."
            )

    def _test_dns_tls(self) -> None:
        self.title("6/9 - Test DNS / TCP / TLS vers earthengine.googleapis.com")

        try:
            host = "earthengine.googleapis.com"
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            ips = sorted({item[4][0] for item in addresses})
            self.ok(f"DNS résolu : {', '.join(ips)}")

            with socket.create_connection((host, 443), timeout=10) as raw_sock:
                context = ssl.create_default_context()
                with context.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                    self.ok(
                        "Connexion TCP/TLS réussie "
                        f"(TLS={tls_sock.version()}, cipher={tls_sock.cipher()[0]})."
                    )
        except Exception as exc:
            self.exception("DNS/TCP/TLS Earth Engine", exc)

    def _test_discovery_http(self) -> None:
        self.title("7/9 - Test HTTP direct du document de découverte Earth Engine")

        if requests is None:
            return

        def do_request(auth: bool) -> None:
            headers = {
                "User-Agent": "JeryMotro-GEE-Diagnostic/1.0",
                "Accept": "application/json",
            }
            if auth and self.credentials is not None:
                try:
                    self.credentials.before_request(
                        requests.Session(), "GET", DISCOVERY_URL, headers
                    )
                except Exception:
                    # On ne dépend pas de ce chemin pour le diagnostic ; le token
                    # est ajouté explicitement ci-dessous.
                    pass
                token = self.credentials.token
                if token:
                    headers["Authorization"] = f"Bearer {token}"

            response = requests.get(
                DISCOVERY_URL,
                headers=headers,
                timeout=20,
                allow_redirects=True,
            )
            content_type = response.headers.get("content-type", "")
            body_preview = response.text[:800].replace("\n", " ")

            label = "AVEC Authorization" if auth else "SANS Authorization"
            print(
                f"INFO {label}: HTTP {response.status_code}, "
                f"Content-Type={content_type!r}, "
                f"Final-URL={response.url!r}"
            )
            print(f"INFO {label}: corps (800 premiers caractères) = {body_preview!r}")

            if response.status_code == 200:
                self.ok(f"Document de découverte accessible {label.lower()}.")
                if "application/json" not in content_type.lower():
                    self.warn(
                        "Réponse HTTP 200 mais Content-Type non JSON ; vérifiez un éventuel proxy."
                    )
            elif response.status_code == 403:
                self.warn(
                    f"Earth Engine répond HTTP 403 {label.lower()}. "
                    "C'est cohérent avec l'erreur actuelle de googleapiclient."
                )
            else:
                self.warn(
                    f"Earth Engine répond HTTP {response.status_code} {label.lower()}."
                )

        try:
            do_request(auth=False)
        except Exception as exc:
            self.exception("HTTP direct sans authentification", exc)

        if self.credentials is not None:
            try:
                do_request(auth=True)
            except Exception as exc:
                self.exception("HTTP direct avec token du Service Account", exc)

    def _test_ee_initialize(self) -> None:
        self.title("8/9 - Test de ee.Initialize()")

        if ee is None or self.credentials is None:
            return

        project = self.project_id
        if not project:
            self.fail(
                "Aucun projet GCP/Earth Engine déterminé. "
                "Définissez GEE_PROJECT_ID ou fournissez project_id dans le JSON."
            )
            return

        self.info(f"Projet transmis à ee.Initialize : {project}")

        try:
            ee.Initialize(
                credentials=self.credentials,
                project=project,
            )
            self.ok("ee.Initialize(credentials=..., project=...) a réussi.")
        except Exception as exc:
            self.exception("ee.Initialize()", exc)
            print(
                "\n      DIAGNOSTIC IMPORTANT : l'erreur ci-dessus est volontairement "
                "isolée de l'application JeryMotro."
            )
            if "403" in str(exc) and "$discovery/rest" in str(exc):
                print(
                    "      Le 403 survient pendant la récupération du document de découverte "
                    "Google API. Comparez surtout les résultats des tests 5, 6 et 7."
                )

    def _test_ee_request(self) -> None:
        self.title("9/9 - Test d'une requête Earth Engine après initialisation")

        if ee is None:
            return

        try:
            # Cette opération force une vraie requête au backend GEE.
            result = ee.Number(1).add(1).getInfo()
            if result == 2:
                self.ok("Requête GEE minimale réussie : ee.Number(1)+1 = 2.")
            else:
                self.warn(f"Réponse inattendue de GEE : {result!r}")
        except Exception as exc:
            self.exception("Requête GEE minimale", exc)
            print(
                "\n      INTERPRETATION : si ee.Initialize() réussit mais ce test échoue, "
                "l'initialisation est bonne et le problème se situe au niveau de l'API "
                "Earth Engine / projet / permissions / réseau."
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnostic détaillé Google Earth Engine / Service Account."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Réservé aux futures extensions de diagnostic ; conservé pour compatibilité.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return Diagnostic(verbose=args.verbose).run()


if __name__ == "__main__":
    raise SystemExit(main())
