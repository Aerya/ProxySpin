#!/usr/bin/env python3
"""
cfworker.py — Module Cloudflare Worker pour ProxySpin
======================================================
Gère la configuration, le déploiement et le routage HTTP via un Cloudflare Worker.

Architecture en mode 'cloudflare' :
    Client → _ProxyHandler (1973) → [bypass HAProxy] → CF Worker → Internet

Source Worker JS (toujours à jour) :
    https://raw.githubusercontent.com/BH3GEI/CloudflareWorkerProxy/refs/heads/main/WorkerProxy.js
"""

import datetime
import hashlib
import json
import logging
import os
import ssl
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger('proxyspin.cfworker')

# ─── Constantes ───────────────────────────────────────────────────────────────

CF_DAILY_LIMIT   = 100_000
CF_WORKER_JS_URL = (
    'https://raw.githubusercontent.com/BH3GEI/CloudflareWorkerProxy'
    '/refs/heads/main/WorkerProxy.js'
)

_CONFIG_FILE = os.environ.get('CF_CONFIG_FILE', '/data/cfworker.json')
_STATS_FILE  = os.environ.get('CF_STATS_FILE',  '/data/cfstats.json')

# Headers HTTP à ne pas transmettre (hop-by-hop)
_HOP_HEADERS = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'proxy-connection', 'te', 'trailers', 'transfer-encoding', 'upgrade', 'host',
})

# Headers de sécurité à supprimer des réponses (ils bloquent le contenu proxié)
_STRIP_RESP_HEADERS = frozenset({
    'content-security-policy', 'content-security-policy-report-only',
    'x-frame-options', 'transfer-encoding',
})


# ─── CFWorkerConfig ───────────────────────────────────────────────────────────

class CFWorkerConfig:
    """Persistance de la configuration Cloudflare Worker dans /data/cfworker.json."""

    _lock = threading.RLock()

    def load(self) -> dict:
        with self._lock:
            if not os.path.exists(_CONFIG_FILE):
                return {}
            try:
                with open(_CONFIG_FILE) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f'CFWorkerConfig: lecture échouée — {e}')
                return {}

    def save(self, data: dict) -> dict:
        with self._lock:
            existing = self.load()
            existing.update(data)
            dirpath = os.path.dirname(_CONFIG_FILE)
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
            with open(_CONFIG_FILE, 'w') as f:
                json.dump(existing, f, indent=2)
            return existing

    def get(self, key: str, default=None):
        return self.load().get(key, default)

    @property
    def worker_url(self) -> str:
        """URL de base pour proxifier : https://worker.workers.dev/------"""
        name = self.get('worker_name', '')
        sep  = self.get('separator', '------')
        return f'https://{name}.workers.dev/{sep}' if name else ''

    @property
    def worker_domain(self) -> str:
        name = self.get('worker_name', '')
        return f'https://{name}.workers.dev' if name else ''


# ─── CFRequestCounter ─────────────────────────────────────────────────────────

class CFRequestCounter:
    """
    Compteur local de requêtes avec reset automatique à minuit UTC.
    Utilise un cache en mémoire pour minimiser les accès disque.
    """

    _lock  = threading.Lock()
    _cache: dict = {}

    @staticmethod
    def _today() -> str:
        return datetime.datetime.utcnow().strftime('%Y-%m-%d')

    def _load(self) -> dict:
        today = self._today()
        if self._cache.get('date') == today:
            return dict(self._cache)
        try:
            with open(_STATS_FILE) as f:
                data = json.load(f)
            if data.get('date') == today:
                self._cache.update(data)
                return dict(data)
        except Exception:
            pass
        fresh = {'date': today, 'count': 0}
        self._cache.update(fresh)
        return fresh

    def _save(self, data: dict):
        dirpath = os.path.dirname(_STATS_FILE)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with open(_STATS_FILE, 'w') as f:
            json.dump(data, f)
        self._cache.update(data)

    def increment(self) -> int:
        with self._lock:
            data          = self._load()
            data['count'] += 1
            self._save(data)
            return data['count']

    def stats(self) -> dict:
        with self._lock:
            data     = self._load()
            count    = data.get('count', 0)
            now_utc  = datetime.datetime.utcnow()
            tomorrow = (now_utc + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            reset_in = max(0, int((tomorrow - now_utc).total_seconds()))
            return {
                'used':       count,
                'limit':      CF_DAILY_LIMIT,
                'percent':    round(count / CF_DAILY_LIMIT * 100, 1),
                'reset_in_s': reset_in,
                'reset_at':   tomorrow.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'date':       data.get('date'),
            }


# ─── CFWorkerDeployer ─────────────────────────────────────────────────────────

class CFWorkerDeployer:
    """
    Récupère WorkerProxy.js depuis GitHub (toujours à jour) et le déploie
    via l'API Cloudflare Workers.
    """

    def fetch_js(self) -> tuple:
        """Retourne (contenu_js: str, sha256_court: str)."""
        req = Request(CF_WORKER_JS_URL, headers={'User-Agent': 'ProxySpin/1.0'})
        with urlopen(req, timeout=15) as r:
            content = r.read().decode('utf-8')
        sha256 = hashlib.sha256(content.encode()).hexdigest()[:16]
        logger.info(f'CF Worker JS fetché ({len(content)} octets, sha256:{sha256})')
        return content, sha256

    def _patch_js(self, js: str, worker_name: str, separator: str) -> str:
        """Injecte le séparateur dans le script si différent du défaut."""
        if separator and separator != '------':
            js = js.replace("'------'", f"'{separator}'")
            js = js.replace('"------"', f'"{separator}"')
        return js

    def deploy(self, api_token: str, account_id: str, worker_name: str,
               separator: str = '------') -> dict:
        """
        Fetche le JS depuis GitHub, l'adapte et le déploie via l'API Cloudflare.
        Retourne {'ok': True, 'sha256': ..., 'deployed_at': ...} ou lève RuntimeError.
        """
        content, sha256 = self.fetch_js()
        patched = self._patch_js(content, worker_name, separator)

        url  = (f'https://api.cloudflare.com/client/v4/accounts/'
                f'{account_id}/workers/scripts/{worker_name}')
        data = patched.encode('utf-8')
        req  = Request(url, data=data, method='PUT')
        req.add_header('Authorization', f'Bearer {api_token}')
        req.add_header('Content-Type',  'application/javascript')

        logger.info(f'CF Worker: déploiement de "{worker_name}" (compte {account_id[:8]}…)')
        try:
            with urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
        except HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'HTTP {e.code} — {body[:300]}')

        if not resp.get('success'):
            msgs = [f"{e.get('code')}: {e.get('message')}"
                    for e in resp.get('errors', [])]
            raise RuntimeError(f"Déploiement refusé — {'; '.join(msgs)}")

        deployed_at = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        logger.info(f'CF Worker "{worker_name}" déployé avec succès (sha256:{sha256})')
        return {'ok': True, 'sha256': sha256, 'deployed_at': deployed_at}


# ─── Routage HTTP via CF Worker ───────────────────────────────────────────────

# Instance partagée du compteur (en mémoire, thread-safe)
_counter = CFRequestCounter()


def handle_http_request(handler) -> None:
    """
    Gère une requête HTTP sortante via le Cloudflare Worker.

    Appelée depuis _ProxyHandler._do_http() quand le mode est 'cloudflare'.
    Remplace le comportement HAProxy/Privoxy : aucun backend local nécessaire.

    handler : instance de BaseHTTPRequestHandler (_ProxyHandler)
    """
    cfg            = CFWorkerConfig()
    worker_url_base = cfg.worker_url  # ex: https://mon-proxy.workers.dev/------

    if not worker_url_base:
        _send_error(handler, 503,
                    'CF Worker non configuré — renseignez les paramètres dans la WebUI')
        return

    target = handler.path  # Forme absolue : http://example.com/path
    if not target.startswith('http'):
        _send_error(handler, 400, f'URL cible invalide pour CF Worker : {target}')
        return

    # URL complète vers le Worker : https://worker.dev/------http://example.com/path
    full_url = worker_url_base + target

    # ── Headers à transmettre (sans hop-by-hop) ───────────────────────────────
    forward_headers = {
        k: v for k, v in handler.headers.items()
        if k.lower() not in _HOP_HEADERS
    }
    # Forcer un User-Agent réaliste pour ne pas être bloqué par le Worker
    if 'user-agent' not in {k.lower() for k in forward_headers}:
        forward_headers['User-Agent'] = (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        )

    # ── Body (POST/PUT/PATCH) ─────────────────────────────────────────────────
    body = None
    content_length = int(handler.headers.get('Content-Length', 0))
    if content_length > 0:
        body = handler.rfile.read(content_length)

    # ── Requête vers le Worker ────────────────────────────────────────────────
    try:
        req = Request(full_url, data=body, headers=forward_headers,
                      method=handler.command)
        ctx = ssl.create_default_context()
        with urlopen(req, timeout=30, context=ctx) as resp:
            resp_body = resp.read()
            status    = resp.status
            resp_hdrs = resp.headers

        _counter.increment()

        # ── Réponse vers le client ────────────────────────────────────────────
        handler.send_response(status)
        for k, v in resp_hdrs.items():
            if k.lower() not in _STRIP_RESP_HEADERS:
                handler.send_header(k, v)
        handler.send_header('Content-Length', str(len(resp_body)))
        handler.end_headers()
        handler.wfile.write(resp_body)

    except Exception as e:
        logger.warning(f'CFWorker: requête échouée ({target[:80]}) — {e}')
        _send_error(handler, 502, f'CF Worker erreur : {e}')


def _send_error(handler, code: int, message: str) -> None:
    """Envoie une réponse d'erreur HTTP propre sans lever d'exception."""
    try:
        body = message.encode('utf-8')
        handler.send_response(code)
        handler.send_header('Content-Type',   'text/plain; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Connection',     'close')
        handler.end_headers()
        handler.wfile.write(body)
    except Exception:
        pass
