#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="${1:-proxyspin:test}"
NAME="proxyspin-ci-${GITHUB_RUN_ID:-local}-$$"

PROXY_USER="ciuser"
PROXY_PASS="ci-proxy-$(date +%s)"
STATS_USER="ciadmin"
STATS_PASS="ci-api-$(date +%s)"

STATUS_FILE="$(mktemp)"
TOR_FILE="$(mktemp)"
LOG_FILE="${PROXYSPIN_LOG_FILE:-proxyspin-ci.log}"

cleanup() {
    docker logs "$NAME" >"$LOG_FILE" 2>&1 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -f "$STATUS_FILE" "$TOR_FILE"
}
trap cleanup EXIT

echo "Vérification de l'absence du binaire Pebble…"
if docker run --rm --entrypoint sh "$IMAGE" \
    -c 'test ! -e /usr/bin/pebble'; then
    echo "Pebble est absent."
else
    echo "Le binaire inutile /usr/bin/pebble est encore présent."
    exit 1
fi

echo "Démarrage de ProxySpin…"
docker run -d \
    --name "$NAME" \
    -e MODE=tor \
    -e tors=2 \
    -e ROTATION_INTERVAL=60 \
    -e PROXY_USER="$PROXY_USER" \
    -e PROXY_PASS="$PROXY_PASS" \
    -e STATS_USER="$STATS_USER" \
    -e STATS_PASS="$STATS_PASS" \
    -p 127.0.0.1::1973 \
    -p 127.0.0.1::1974 \
    "$IMAGE" >/dev/null

PROXY_PORT="$(docker port "$NAME" 1973/tcp | awk -F: 'NR == 1 {print $NF}')"
API_PORT="$(docker port "$NAME" 1974/tcp | awk -F: 'NR == 1 {print $NF}')"

[[ -n "$PROXY_PORT" && -n "$API_PORT" ]] || {
    echo "Impossible de déterminer les ports publiés."
    exit 1
}

echo "Attente de l'API et d'une sortie Tor fonctionnelle…"

PROXY_IP=""
for attempt in $(seq 1 120); do
    if curl -fsS --max-time 5 \
        -u "$STATS_USER:$STATS_PASS" \
        "http://127.0.0.1:${API_PORT}/api/status" \
        >"$STATUS_FILE" 2>/dev/null; then

        if PROXY_IP="$(curl -fsS --max-time 25 \
            --proxy "http://${PROXY_USER}:${PROXY_PASS}@127.0.0.1:${PROXY_PORT}" \
            https://api.ipify.org 2>/dev/null)"; then
            break
        fi
    fi

    if (( attempt % 10 == 0 )); then
        echo "Tentative ${attempt}/120"
        docker logs --tail 40 "$NAME" || true
    fi

    sleep 3
done

[[ -s "$STATUS_FILE" ]] || {
    echo "L'API ProxySpin n'est jamais devenue disponible."
    exit 1
}

python3 - "$STATUS_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

if not isinstance(payload, dict):
    raise SystemExit("La réponse /api/status n'est pas un objet JSON")

print("API JSON valide")
PY

[[ "$PROXY_IP" =~ ^[0-9a-fA-F:.]+$ ]] || {
    echo "Aucune adresse IP valide reçue via ProxySpin : ${PROXY_IP:-vide}"
    exit 1
}

DIRECT_IP="$(curl -fsS --max-time 15 https://api.ipify.org)"

[[ "$DIRECT_IP" != "$PROXY_IP" ]] || {
    echo "L'adresse de sortie ProxySpin est identique à celle du runner GitHub."
    exit 1
}

echo "Test HTTPS CONNECT et validation par le Tor Project…"
curl -fsS --max-time 30 \
    --proxy "http://${PROXY_USER}:${PROXY_PASS}@127.0.0.1:${PROXY_PORT}" \
    https://check.torproject.org/api/ip \
    >"$TOR_FILE"

python3 - "$TOR_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

if payload.get("IsTor") is not True:
    raise SystemExit(
        f"Le Tor Project ne reconnaît pas la sortie comme Tor : {payload}"
    )

print("Sortie Tor confirmée :", payload.get("IP"))
PY

echo "Vérification du refus des mauvais identifiants API…"
bad_api_code="$(
    curl -sS -o /dev/null -w '%{http_code}' \
        -u "wrong:wrong" \
        "http://127.0.0.1:${API_PORT}/api/status"
)"

[[ "$bad_api_code" == "401" ]] || {
    echo "Mauvais identifiants API : HTTP ${bad_api_code}, attendu 401."
    exit 1
}

echo "Vérification du refus des mauvais identifiants proxy…"
if curl -fsS --max-time 10 \
    --proxy "http://wrong:wrong@127.0.0.1:${PROXY_PORT}" \
    https://api.ipify.org >/dev/null 2>&1; then
    echo "Le proxy a accepté de mauvais identifiants."
    exit 1
fi

echo "Attente de l'état Docker healthy…"
health="starting"
for _attempt in $(seq 1 36); do
    health="$(
        docker inspect \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
            "$NAME"
    )"

    [[ "$health" == "healthy" ]] && break
    sleep 5
done

[[ "$health" == "healthy" ]] || {
    echo "État de santé final : $health"
    docker inspect "$NAME"
    exit 1
}

restarts="$(docker inspect --format '{{.RestartCount}}' "$NAME")"
[[ "$restarts" == "0" ]] || {
    echo "Le conteneur a redémarré ${restarts} fois."
    exit 1
}

if docker logs "$NAME" 2>&1 | grep -Eqi \
    'traceback|segmentation fault|fatal error|address already in use|permission denied'; then
    echo "Un motif critique a été détecté dans les logs."
    docker logs "$NAME"
    exit 1
fi

echo
echo "Test fonctionnel ProxySpin réussi."
echo "IP directe : $DIRECT_IP"
echo "IP Tor     : $PROXY_IP"
