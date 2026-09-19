#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import inspect
import json
import os
import sys
import traceback
from pathlib import Path

import ee
import httplib2
from google_auth_httplib2 import AuthorizedHttp
from google.auth.transport.requests import Request
from google.oauth2 import service_account

DISCOVERY = "https://earthengine.googleapis.com/$discovery/rest?version=v1&prettyPrint=false"
KEY_PATH = Path(os.environ.get("GEE_SERVICE_ACCOUNT_JSON_PATH", "service-account.json")).expanduser().resolve()
PROJECT = os.environ.get("GEE_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")

def section(name):
    print("\n" + "=" * 90)
    print(name)
    print("=" * 90)

section("1. VERSIONS EXACTES")
print("Python:", sys.version)
print("earthengine-api:", getattr(ee, "__version__", "unknown"))
print("ee module:", ee.__file__)
try:
    import googleapiclient
    import googleapiclient.discovery as discovery
    print("google-api-python-client:", getattr(googleapiclient, "__version__", "unknown"))
    print("discovery.build:", inspect.signature(discovery.build))
except Exception as exc:
    print("googleapiclient ERROR:", repr(exc))

section("2. CODE INTERNE EARTH ENGINE")
try:
    source = inspect.getsource(ee._cloud_api_utils.build_cloud_resource)
    print("with_quota_project(None) present:", "with_quota_project(None)" in source)
    print("static_discovery=False present:", "static_discovery=False" in source)
    for line in source.splitlines():
        if "quota_project" in line or "static_discovery" in line or "discoveryServiceUrl" in line:
            print(line.strip())
except Exception as exc:
    print("SOURCE INSPECTION ERROR:", type(exc).__name__, exc)
    traceback.print_exc()

section("3. JSON SERVICE ACCOUNT")
print("KEY:", KEY_PATH)
if not KEY_PATH.is_file():
    print("ERREUR: fichier introuvable")
    sys.exit(2)
data = json.loads(KEY_PATH.read_text(encoding="utf-8"))
print("type:", data.get("type"))
print("project_id:", data.get("project_id"))
print("client_email:", data.get("client_email"))
print("private_key_id:", "present" if data.get("private_key_id") else "missing")
print("private_key:", "present" if data.get("private_key") else "missing")
if PROJECT is None:
    PROJECT = data.get("project_id")
print("PROJECT EFFECTIF:", PROJECT)

section("4. TOKEN OAUTH2")
creds = service_account.Credentials.from_service_account_file(
    str(KEY_PATH),
    scopes=["https://www.googleapis.com/auth/earthengine"],
)
print("service_account_email:", creds.service_account_email)
print("quota_project_id AVANT:", repr(creds.quota_project_id))
try:
    creds.refresh(Request())
    print("TOKEN: OK")
    print("expiry:", creds.expiry)
except Exception as exc:
    print("TOKEN ERROR:", type(exc).__name__, exc)
    traceback.print_exc()
    sys.exit(3)

def discovery_test(label, credentials):
    print("\n---", label, "---")
    try:
        base = httplib2.Http(timeout=20)
        http = AuthorizedHttp(credentials, http=base) if credentials else base
        response, body = http.request(
            DISCOVERY,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "JeryMotro-GEE-Debug/1.0"},
        )
        body_text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
        print("HTTP:", response.status)
        print("Content-Type:", response.get("content-type"))
        print("Body:")
        print(body_text[:4000])
        return int(response.status), body_text
    except Exception as exc:
        print("HTTP ERROR:", type(exc).__name__, exc)
        traceback.print_exc()
        return None, str(exc)

section("5. DISCOVERY A/B")
status_plain, body_plain = discovery_test("SANS credentials", None)
status_auth, body_auth = discovery_test("SERVICE ACCOUNT", creds)
status_stripped = None
if hasattr(creds, "with_quota_project"):
    stripped = creds.with_quota_project(None)
    print("quota_project_id APRES suppression:", repr(stripped.quota_project_id))
    status_stripped, body_stripped = discovery_test("SERVICE ACCOUNT + quota_project SUPPRIME", stripped)
else:
    print("with_quota_project() indisponible")

section("6. ee.Initialize() ISOLÉ")
try:
    ee.Initialize(credentials=creds, project=PROJECT)
    print("ee.Initialize: OK")
except Exception as exc:
    print("ee.Initialize ERROR:")
    print(type(exc).__name__ + ":", exc)
    print("\nTRACEBACK COMPLET:")
    traceback.print_exc()

section("7. CONCLUSION AUTOMATIQUE")
print("Discovery sans credentials :", status_plain)
print("Discovery avec credentials  :", status_auth)
print("Discovery sans quota project:", status_stripped)
if status_auth == 403 and status_stripped == 200:
    print("CAUSE PROBABLE = quota_project injecté / chemin client ancien")
elif status_auth == 403 and status_stripped == 403:
    print("CAUSE = 403 Discovery persistant même quota_project supprimé")
    print("Comparer surtout les versions et la réponse HTTP complète.")
elif status_auth == 200:
    print("Discovery fonctionne avec le Service Account.")
else:
    print("Résultat inconclusif : conserver toute la sortie pour analyse.")