#!/usr/bin/env python3
"""Healthcheck interne de ProxySpin.

Ce contrôle reste volontairement local et rapide :
- port proxy 1973 disponible ;
- API 1974 disponible ;
- /api/status retourne du JSON avec HTTP 200.

Les tests réseau externes via Tor sont exécutés dans la CI GitHub.
"""

import base64
import json
import os
import socket
import sys
import urllib.error
import urllib.request


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


if not tcp_open("127.0.0.1", 1973):
    fail("Le port proxy 1973 n'écoute pas")

if not tcp_open("127.0.0.1", 1974):
    fail("Le port API 1974 n'écoute pas")

request = urllib.request.Request("http://127.0.0.1:1974/api/status")

if os.getenv("API_AUTH_ENABLED", "true").lower() != "false":
    user = os.getenv("STATS_USER", "")
    password = os.getenv("STATS_PASS", "")

    if not user or not password:
        fail("STATS_USER ou STATS_PASS absent alors que l'auth API est active")

    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")

try:
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            fail(f"L'API retourne HTTP {response.status}")
        payload = json.load(response)
except (OSError, urllib.error.URLError, ValueError) as exc:
    fail(f"Échec du contrôle API : {exc}")

if not isinstance(payload, dict):
    fail("La réponse de /api/status n'est pas un objet JSON")

print("healthy")
