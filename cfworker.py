#!/usr/bin/env python3
"""
cfworker.py — Module Cloudflare Worker pour ProxySpin
======================================================
Gère la configuration, le déploiement et le routage HTTP/HTTPS via un Worker.

Architecture en mode 'cloudflare' :
    HTTP  → _ProxyHandler._do_http()    → handle_http_request()  → fetch() Worker
    HTTPS → _ProxyHandler.do_CONNECT()  → handle_connect_request() → WS tunnel Worker → TCP

Worker JS local : cloudflare_worker.js (même répertoire que ce fichier)
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import select
import socket
import ssl
import struct
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger('proxyspin.cfworker')

# ─── Constantes ───────────────────────────────────────────────────────────────

CF_DAILY_LIMIT = 100_000

_WORKER_JS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cloudflare_worker.js')
_CONFIG_FILE    = os.environ.get('CF_CONFIG_FILE', '/data/cfworker.json')
_STATS_FILE     = os.environ.get('CF_STATS_FILE',  '/data/cfstats.json')

_HOP_REQUEST_HEADERS = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'proxy-connection', 'te', 'trailers', 'transfer-encoding', 'upgrade',
})
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
        """URL de base pour proxifier : https://<name>.<subdomain>.workers.dev/------"""
        cfg  = self.load()
        name = cfg.get('worker_name', '')
        sub  = cfg.get('workers_subdomain', '')
        sep  = cfg.get('separator', '------')
        if not name:
            return ''
        host = f'{name}.{sub}.workers.dev' if sub else f'{name}.workers.dev'
        return f'https://{host}/{sep}'

    @property
    def worker_host(self) -> str:
        cfg  = self.load()
        name = cfg.get('worker_name', '')
        sub  = cfg.get('workers_subdomain', '')
        if not name:
            return ''
        return f'{name}.{sub}.workers.dev' if sub else f'{name}.workers.dev'


# ─── CFRequestCounter ─────────────────────────────────────────────────────────

class CFRequestCounter:
    """Compteur local de requêtes avec reset automatique à minuit UTC."""

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
    Lit cloudflare_worker.js depuis le repo local et le déploie
    sur Cloudflare Workers via l'API (format ES Module).
    """

    def fetch_js(self) -> tuple:
        """Lit cloudflare_worker.js et retourne (contenu: str, sha256: str)."""
        if not os.path.exists(_WORKER_JS_PATH):
            raise FileNotFoundError(
                f'cloudflare_worker.js introuvable : {_WORKER_JS_PATH}\n'
                f'Assurez-vous que le fichier est présent dans le répertoire de ProxySpin.'
            )
        with open(_WORKER_JS_PATH, encoding='utf-8') as f:
            content = f.read()
        sha256 = hashlib.sha256(content.encode()).hexdigest()[:16]
        logger.info(f'CF Worker JS lu ({len(content)} octets, sha256:{sha256})')
        return content, sha256

    def _patch_js(self, js: str, separator: str) -> str:
        """Injecte le séparateur dans le script si différent du défaut."""
        if separator and separator != '------':
            js = js.replace("const SEP     = '------'", f"const SEP     = '{separator}'")
        return js

    def _fetch_subdomain(self, api_token: str, account_id: str) -> str:
        """Récupère le sous-domaine workers.dev du compte (ex: 'aerya' → aerya.workers.dev)."""
        url = f'https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/subdomain'
        req = Request(url)
        req.add_header('Authorization', f'Bearer {api_token}')
        try:
            with urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            subdomain = data.get('result', {}).get('subdomain', '')
            if subdomain:
                logger.info(f'CF subdomain détecté : {subdomain}.workers.dev')
            else:
                logger.warning('CF subdomain introuvable dans la réponse API')
            return subdomain
        except Exception as e:
            logger.warning(f'Impossible de récupérer le subdomain CF : {e}')
            return ''

    def deploy(self, api_token: str, account_id: str, worker_name: str,
               separator: str = '------') -> dict:
        """
        Déploie le Worker comme ES Module (requis pour cloudflare:sockets).
        Utilise multipart/form-data avec metadata {"main_module": "worker.js"}.
        """
        content, sha256 = self.fetch_js()
        patched = self._patch_js(content, separator)

        # ── Multipart/form-data pour ES Module ────────────────────────────────
        boundary = b'proxyspin-deploy-boundary'
        meta     = json.dumps({'main_module': 'worker.js'}).encode()
        code     = patched.encode('utf-8')

        body = (
            b'--' + boundary + b'\r\n'
            b'Content-Disposition: form-data; name="metadata"\r\n'
            b'Content-Type: application/json\r\n\r\n'
            + meta + b'\r\n'
            b'--' + boundary + b'\r\n'
            b'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
            b'Content-Type: application/javascript+module\r\n\r\n'
            + code + b'\r\n'
            b'--' + boundary + b'--\r\n'
        )

        url = (f'https://api.cloudflare.com/client/v4/accounts/'
               f'{account_id}/workers/scripts/{worker_name}')
        req = Request(url, data=body, method='PUT')
        req.add_header('Authorization', f'Bearer {api_token}')
        req.add_header('Content-Type',  f'multipart/form-data; boundary={boundary.decode()}')

        logger.info(f'CF Worker: déploiement ES Module "{worker_name}" (compte {account_id[:8]}…)')
        try:
            with urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
        except HTTPError as e:
            body_err = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'HTTP {e.code} — {body_err[:400]}')

        if not resp.get('success'):
            msgs = [f"{e.get('code')}: {e.get('message')}"
                    for e in resp.get('errors', [])]
            raise RuntimeError(f"Déploiement refusé — {'; '.join(msgs)}")

        deployed_at = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

        # ── Récupération du sous-domaine workers.dev ───────────────────────────
        subdomain = self._fetch_subdomain(api_token, account_id)
        CFWorkerConfig().save({
            'last_sha256':       sha256,
            'last_deployed_at':  deployed_at,
            'workers_subdomain': subdomain,
        })

        worker_fqdn = f'{worker_name}.{subdomain}.workers.dev' if subdomain else f'{worker_name}.workers.dev'
        logger.info(f'CF Worker "{worker_name}" déployé → {worker_fqdn} (sha256:{sha256})')
        return {'ok': True, 'sha256': sha256, 'deployed_at': deployed_at, 'worker_url': f'https://{worker_fqdn}'}


# ─── Client WebSocket minimal (RFC 6455, sans dépendance externe) ─────────────

class _WSClient:
    """
    Client WebSocket minimaliste sur TLS.
    Implémente uniquement ce dont on a besoin pour le tunnel HTTPS :
      - Handshake HTTP Upgrade
      - Envoi de frames text (opcode 1) et binary (opcode 2) masquées
      - Réception de frames (text, binary, close)
    """

    def __init__(self, host: str, port: int = 443, path: str = '/'):
        self._host = host
        self._port = port
        self._path = path
        self._sock = None

    def connect(self, timeout: float = 15.0):
        ctx = ssl.create_default_context()
        raw = socket.create_connection((self._host, self._port), timeout=timeout)
        self._sock = ctx.wrap_socket(raw, server_hostname=self._host)
        self._sock.settimeout(timeout)
        self._handshake()
        self._sock.settimeout(None)   # blocking sans timeout pour le relay

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f'GET {self._path} HTTP/1.1\r\n'
            f'Host: {self._host}\r\n'
            f'Upgrade: websocket\r\n'
            f'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n'
            f'\r\n'
        )
        self._sock.sendall(req.encode())
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError('WebSocket: connexion fermée pendant le handshake')
            resp += chunk
        status = resp.split(b'\r\n')[0].decode('utf-8', errors='replace')
        if '101' not in status:
            raise ConnectionError(f'WebSocket upgrade refusé : {status}')

    # ── Envoi ──────────────────────────────────────────────────────────────────

    def send_text(self, text: str):
        self._send_frame(1, text.encode('utf-8'))

    def send_binary(self, data: bytes):
        self._send_frame(2, data)

    def _send_frame(self, opcode: int, payload: bytes):
        """Envoie un frame WebSocket masqué (obligatoire côté client, RFC 6455 §5.3)."""
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack('!H', length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack('!Q', length)
        masked  = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    # ── Réception ──────────────────────────────────────────────────────────────

    def recv(self) -> tuple:
        """
        Reçoit un frame WebSocket.
        Retourne (opcode: int, data: bytes).
        opcode 1 = text, 2 = binary, 8 = close, 9 = ping, 10 = pong.
        """
        header = self._read_exact(2)
        opcode = header[0] & 0x0F
        masked = (header[1] & 0x80) != 0
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack('!H', self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._read_exact(8))[0]
        mask_key = self._read_exact(4) if masked else None
        data     = self._read_exact(length)
        if mask_key:
            data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        return opcode, data

    def _read_exact(self, n: int) -> bytes:
        buf = b''
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError('WebSocket: connexion fermée')
            buf += chunk
        return buf

    def fileno(self) -> int:
        return self._sock.fileno()

    def close(self):
        try:
            # Close frame (opcode 8), masqué, payload vide
            self._sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


# ─── Compteur partagé ─────────────────────────────────────────────────────────

_counter = CFRequestCounter()


# ─── Proxy HTTP ───────────────────────────────────────────────────────────────

def handle_http_request(handler) -> None:
    """
    Gère une requête HTTP via fetch() Cloudflare Worker (URL rewriting).
    Appelée depuis _ProxyHandler._do_http() en mode 'cloudflare'.
    """
    cfg             = CFWorkerConfig()
    worker_url_base = cfg.worker_url

    if not worker_url_base:
        _send_error(handler, 503, 'CF Worker non configuré — renseignez les paramètres dans la WebUI')
        return

    target = handler.path
    if not target.startswith('http'):
        _send_error(handler, 400, f'URL cible invalide pour CF Worker : {target}')
        return

    full_url = worker_url_base + target

    forward_headers = {
        k: v for k, v in handler.headers.items()
        if k.lower() not in _HOP_REQUEST_HEADERS
    }
    if 'user-agent' not in {k.lower() for k in forward_headers}:
        forward_headers['User-Agent'] = (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )

    body = None
    content_length = int(handler.headers.get('Content-Length', 0))
    if content_length > 0:
        body = handler.rfile.read(content_length)

    try:
        req = Request(full_url, data=body, headers=forward_headers, method=handler.command)
        ctx = ssl.create_default_context()
        with urlopen(req, timeout=30, context=ctx) as resp:
            resp_body = resp.read()
            status    = resp.status
            resp_hdrs = resp.headers

        _counter.increment()

        handler.send_response(status)
        for k, v in resp_hdrs.items():
            if k.lower() not in _STRIP_RESP_HEADERS:
                handler.send_header(k, v)
        handler.send_header('Content-Length', str(len(resp_body)))
        handler.end_headers()
        handler.wfile.write(resp_body)

    except Exception as e:
        logger.warning(f'CFWorker HTTP: requête échouée ({target[:80]}) — {e}')
        _send_error(handler, 502, f'CF Worker erreur : {e}')


# ─── Tunnel HTTPS (CONNECT) ───────────────────────────────────────────────────

def handle_connect_request(handler) -> None:
    """
    Gère une requête HTTPS (CONNECT) via WebSocket + cloudflare:sockets.
    Appelée depuis _ProxyHandler.do_CONNECT() en mode 'cloudflare'.

    Flux :
        Browser → CONNECT host:port
        Python  → WS wss://worker.workers.dev/wstunnel
        Worker  → TCP host:port  (via cloudflare:sockets)
        Relay bidirectionnel : browser_socket ↔ WebSocket ↔ TCP ↔ cible
    """
    cfg         = CFWorkerConfig()
    worker_host = cfg.worker_host

    if not worker_host:
        _send_connect_error(handler, 'CF Worker non configuré')
        return

    # Parse "host:port" depuis CONNECT
    target = handler.path
    if ':' in target:
        host, port_str = target.rsplit(':', 1)
        try:
            port = int(port_str)
        except ValueError:
            _send_connect_error(handler, f'Port invalide : {port_str}')
            return
    else:
        host = target
        port = 443

    try:
        ws = _WSClient(worker_host, 443, '/wstunnel')
        ws.connect(timeout=15.0)

        # Handshake tunnel : envoyer host:port au Worker
        ws.send_text(json.dumps({'host': host, 'port': port}))

        # Attendre la confirmation {"ok": true}
        opcode, data = ws.recv()
        if opcode == 8:   # Close
            _send_connect_error(handler, 'Worker a fermé la connexion lors du handshake')
            return
        ack = json.loads(data.decode('utf-8', errors='replace'))
        if not ack.get('ok'):
            _send_connect_error(handler, f'Worker a refusé : {ack}')
            return

        # Confirmer au browser que le tunnel est établi
        handler.send_response(200, 'Connection Established')
        handler.end_headers()

        logger.debug(f'CF Worker tunnel établi : {host}:{port}')
        _counter.increment()

        # Relay bidirectionnel
        _relay_tunnel(handler.connection, ws)

    except Exception as e:
        logger.warning(f'CF Worker CONNECT échoué ({target}) — {e}')
        _send_connect_error(handler, str(e))


def _relay_tunnel(browser_sock: socket.socket, ws: _WSClient) -> None:
    """
    Relay bidirectionnel entre le socket browser et le WebSocket Worker.

    Thread ws→browser : lit les frames WS binaires, envoie au browser.
    Thread principal  : lit les octets du browser, les envoie en frames WS.
    """
    stop_event = threading.Event()

    def ws_to_browser():
        try:
            while not stop_event.is_set():
                opcode, data = ws.recv()
                if opcode == 8:     # Close
                    break
                if opcode in (1, 2) and data:
                    browser_sock.sendall(data)
        except Exception:
            pass
        finally:
            stop_event.set()
            try:
                browser_sock.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    t = threading.Thread(target=ws_to_browser, daemon=True)
    t.start()

    try:
        while not stop_event.is_set():
            ready = select.select([browser_sock], [], [], 1.0)[0]
            if not ready:
                continue
            data = browser_sock.recv(65536)
            if not data:
                break
            ws.send_binary(data)
    except Exception:
        pass
    finally:
        stop_event.set()
        ws.close()

    t.join(timeout=5)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _send_error(handler, code: int, message: str) -> None:
    """Envoie une réponse d'erreur HTTP pour une requête ordinaire."""
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


def _send_connect_error(handler, message: str) -> None:
    """Envoie une réponse d'erreur pour une requête CONNECT."""
    try:
        body = message.encode('utf-8')
        handler.send_response(502, 'CF Worker Error')
        handler.send_header('Content-Type',   'text/plain; charset=utf-8')
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Connection',     'close')
        handler.end_headers()
        handler.wfile.write(body)
    except Exception:
        pass
