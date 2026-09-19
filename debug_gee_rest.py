#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic direct Service Account -> Earth Engine REST, sans Discovery."""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2 import service_account

DISCOVERY_URL = "https://earthengine.googleapis.com/$discovery/rest?version=v1&prettyPrint=false"
API_URL_TEMPLATE = "https://earthengine.googleapis.com/v1/projects/{project}/algorithms"
SCOPE = "https://www.googleapis.com/auth/earthengine"

load_dotenv()

def show(label, response):
    print(f"\n--- {label} ---")
    print("HTTP:", response.status_code)
    print("Content-Type:", response.headers.get("content-type"))
    print("Server:", response.headers.get("server"))
    print("x-guploader-uploadid:", response.headers.get("x-guploader-uploadid"))
    print("Body:")
    print(response.text[:5000])

print("=" * 90)
print("JeryMotro - diagnostic REST Earth Engine sans googleapiclient.discovery")
print("=" * 90)

key_path_raw = os.environ.get("GEE_SERVICE_ACCOUNT_JSON_PATH")
project = os.environ.get("GEE_PROJECT") or os.environ.get("GEE_PROJECT_ID")

if not key_path_raw:
    print("ERREUR: GEE_SERVICE_ACCOUNT_JSON_PATH n'est pas défini")
    sys.exit(2)

key_path = Path(key_path_raw).expanduser().resolve()
print("Credential file:", key_path)
print("Projet demandé:", project)

if not key_path.is_file():
    print("ERREUR: fichier credentials introuvable")
    sys.exit(2)

try:
    info = json.loads(key_path.read_text(encoding="utf-8"))
    print("Service Account:", info.get("client_email"))
    print("Projet du JSON:", info.get("project_id"))

    if not project:
        project = info.get("project_id")

    credentials = service_account.Credentials.from_service_account_file(
        str(key_path),
        scopes=[SCOPE],
    )
    credentials.refresh(Request())
    print("Token OAuth2: OK")
    print("Expiration:", credentials.expiry)
except Exception as exc:
    print("ERREUR CREDENTIALS:", type(exc).__name__, exc)
    traceback.print_exc()
    sys.exit(3)

session = requests.Session()
headers = {
    "Authorization": f"Bearer {credentials.token}",
    "Accept": "application/json",
    "User-Agent": "JeryMotro-GEE-REST-Diagnostic/1.0",
}

try:
    r = session.get(DISCOVERY_URL, headers=headers, timeout=30)
    show("DISCOVERY AVEC TOKEN", r)
except Exception as exc:
    print("DISCOVERY ERROR:", type(exc).__name__, exc)
    traceback.print_exc()

if not project:
    print("ERREUR: aucun projet disponible pour le test REST")
    sys.exit(4)

api_url = API_URL_TEMPLATE.format(project=project)
print("\nURL REST testée:", api_url)

try:
    r = session.get(api_url, headers=headers, timeout=30)
    show("API REST projects/{project}/algorithms AVEC TOKEN", r)
except Exception as exc:
    print("REST API ERROR:", type(exc).__name__, exc)
    traceback.print_exc()

print("\n" + "=" * 90)
print("CONCLUSION")
print("=" * 90)
if 'r' in globals() and r.status_code == 200:
    print("API REST Earth Engine accessible avec le Service Account.")
    print("Si Discovery reste en 403, le problème est isolé au Discovery / googleapiclient / endpoint Discovery.")
elif 'r' in globals():
    print("L'API REST elle-même répond en", r.status_code)
    print("Analyser le corps JSON ci-dessus : c'est l'erreur REST réelle, contrairement au 403 HTML du Discovery.")
else:
    print("Aucune réponse REST exploitable.")