#!/usr/bin/env python3
"""
ProxySpin — Orchestrateur
Gère Tor, Privoxy et HAProxy pour un proxy HTTP rotatif anonymisant.
Supporte deux modes : Tor (circuits chiffrés) et Local (proxies SOCKS privés).
"""

import base64
import json
import logging
import os
import queue
import random
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

# ─── Module optionnel Cloudflare Worker ───────────────────────────────────────
try:
    import cfworker as _cfworker
    _CF_AVAILABLE = True
except Exception as _cf_import_err:
    import logging as _logging
    _logging.getLogger('proxyspin').warning(
        f'cfworker non disponible : {type(_cf_import_err).__name__}: {_cf_import_err}'
    )
    _cfworker     = None
    _CF_AVAILABLE = False

# ─── Ports ────────────────────────────────────────────────────────────────────

PROXY_PORT    = 1973   # Python ProxyAuth — point d'entrée exposé
HAPROXY_PORT  = 11973  # HAProxy interne (localhost uniquement, pas d'auth)
API_PORT      = 1974   # Web UI + API JSON
STATS_PORT    = 1976   # HAProxy stats
# Port 1975 réservé CF Worker (bridge interne, non exposé)

# ─── Auth enabled flags ────────────────────────────────────────────────────────
# Mettre 'false' pour désactiver l'auth sur un port donné.
# PROXY_AUTH_ENABLED — port 1973 (proxy rotatif)
# API_AUTH_ENABLED   — port 1974 (Web UI + API)
# STATS_AUTH_ENABLED — port 1976 (HAProxy stats)

def _proxy_auth_enabled():
    return os.environ.get('PROXY_AUTH_ENABLED', 'true').lower() != 'false'

def _api_auth_enabled():
    return os.environ.get('API_AUTH_ENABLED', 'true').lower() != 'false'

def _stats_auth_enabled():
    return os.environ.get('STATS_AUTH_ENABLED', 'true').lower() != 'false'

# ─── Logger ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG if os.environ.get('DEBUG') else logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('proxyspin')

# ─── Config globale (modifiable à chaud) ──────────────────────────────────────

config = {
    'mode':              os.environ.get('MODE',              'tor'),
    'rotation_interval': int(os.environ.get('ROTATION_INTERVAL', '60')),
    'auto_rotation':     os.environ.get('AUTO_ROTATION', 'true' if os.environ.get('MODE', 'tor') == 'tor' else 'false').lower() == 'true',
    'tor_instances':     int(os.environ.get('tors',          '10')),
    'max_proxies':       int(os.environ.get('MAX_PROXIES',   '20')),
    'country_filter':    (os.environ.get('COUNTRY_FILTER') or '').upper() or None,
}
config_lock = threading.Lock()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def ensure_dirs(*paths):
    for p in paths:
        os.makedirs(p, exist_ok=True)

def read_pid(pid_file):
    try:
        return int(open(pid_file).read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None

def kill_pid(pid_file):
    pid = read_pid(pid_file)
    if pid:
        try:
            os.kill(pid, signal.SIGINT)
            logger.debug(f'killed pid {pid} ({pid_file})')
        except ProcessLookupError:
            pass

def fire_and_forget(cmd):
    logger.debug(f'run: {cmd}')
    subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ─── Tor ──────────────────────────────────────────────────────────────────────

class Tor:
    def __init__(self, port, control_port):
        self.port         = port
        self.control_port = control_port
        self._pid_file    = f'/var/run/tor/{port}.pid'
        self._data_dir    = f'/var/lib/tor/{port}'

    def start(self):
        ensure_dirs('/var/lib/tor', '/var/run/tor', '/var/log/tor', self._data_dir)
        logger.info(f'starting tor on socks={self.port} control={self.control_port}')
        fire_and_forget(
            f'tor'
            f' --SocksPort {self.port}'
            f' --ControlPort {self.control_port}'
            f' --NewCircuitPeriod 15'
            f' --MaxCircuitDirtiness 15'
            f' --UseEntryGuards 0'
            f' --CircuitBuildTimeout 5'
            f' --ExitRelay 0'
            f' --ClientOnly 1'
            f' --DataDirectory {self._data_dir}'
            f' --PidFile {self._pid_file}'
            f' --Log "warn syslog"'
            f' --RunAsDaemon 1'
            f" | logger -t 'tor' 2>&1"
        )

    def stop(self):
        logger.info(f'stopping tor port {self.port}')
        kill_pid(self._pid_file)

    def rotate(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(('127.0.0.1', self.control_port))
                s.sendall(b'authenticate ""\r\nsignal newnym\r\nquit\r\n')
            logger.debug(f'rotate sent to control port {self.control_port}')
        except Exception as e:
            logger.warning(f'rotate failed (control={self.control_port}): {e}')

# ─── Privoxy (générique : Tor SOCKS5 ou HTTP proxy libre) ─────────────────────

class Privoxy:
    def __init__(self, port, upstream_host, upstream_port, protocol='socks5t'):
        self.port           = port
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._protocol      = protocol
        self._pid_file      = f'/var/run/privoxy/{port}.pid'
        self._config_path   = f'/var/lib/privoxy/{port}.conf'

    def start(self):
        ensure_dirs('/var/lib/privoxy', '/var/run/privoxy', '/var/log/privoxy')
        self._write_config()
        if os.path.exists(self._pid_file):
            os.remove(self._pid_file)
        logger.info(f'starting privoxy port {self.port} → {self._upstream_host}:{self._upstream_port} ({self._protocol})')
        fire_and_forget(
            f'privoxy --no-daemon --pidfile {self._pid_file} {self._config_path}'
            f" | logger -t 'privoxy' 2>&1"
        )

    def stop(self):
        logger.info(f'stopping privoxy port {self.port}')
        kill_pid(self._pid_file)

    def _forward_directive(self):
        h, p = self._upstream_host, self._upstream_port
        if self._protocol in ('socks5', 'socks5t'):
            return f'forward-socks5t /  {h}:{p}  .'
        if self._protocol == 'socks4':
            return f'forward-socks4 /  {h}:{p}  .'
        return f'forward /  {h}:{p}  .'

    def _write_config(self):
        lines = [
            'confdir /etc/privoxy',
            f'listen-address  127.0.0.1:{self.port}',
            self._forward_directive(),
            'logdir /var/log/privoxy',
            'debug 0',
            'hide-forwarded-for-headers 1',
            'change-x-forwarded-for block',
            'keep-alive-timeout 5',
            'default-server-timeout 60',
            'socket-timeout 60',
        ]
        with open(self._config_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')

# ─── TorProxy (Tor + Privoxy) ─────────────────────────────────────────────────

class TorProxy:
    def __init__(self, proxy_id):
        self.id               = proxy_id
        self.tor              = Tor(self._tor_port, self._control_port)
        self.privoxy          = Privoxy(self.port, '127.0.0.1', self._tor_port, 'socks5t')
        self.exit_ip          = None
        self.exit_country_code = None
        self.exit_country_name = None
        self.latency_ms       = None

    @property
    def port(self):            return 20_000 + self.id
    @property
    def _tor_port(self):       return 10_000 + self.id
    @property
    def _control_port(self):   return 30_000 + self.id

    def start(self):
        logger.info(f'starting tor proxy #{self.id}')
        self.tor.start()
        self.privoxy.start()

    def stop(self):
        logger.info(f'stopping tor proxy #{self.id}')
        self.tor.stop()
        self.privoxy.stop()

    def restart(self):
        self.stop()
        time.sleep(5)
        self.start()

    def working(self):
        test_url = os.environ.get('test_url', 'http://icanhazip.com')
        try:
            opener = build_opener(ProxyHandler({'http': f'http://127.0.0.1:{self.port}'}))
            with opener.open(test_url, timeout=10) as r:
                return r.status == 200
        except Exception:
            return False

    def probe(self):
        """Mesure la latence et récupère l'IP de sortie de ce circuit Tor."""
        try:
            opener = build_opener(ProxyHandler({'http': f'http://127.0.0.1:{self.port}'}))
            t0 = time.time()
            with opener.open('http://icanhazip.com', timeout=15) as r:
                ip = r.read().decode().strip()
            ms = int((time.time() - t0) * 1000)
            if ip != self.exit_ip:
                self.exit_country_code = None
                self.exit_country_name = None
            self.exit_ip    = ip
            self.latency_ms = ms
        except Exception:
            self.exit_ip    = None
            self.latency_ms = None

# ─── FreeProxy (proxy gratuit via Privoxy) ────────────────────────────────────

class FreeProxy:
    def __init__(self, proxy_id, upstream):
        self.id       = proxy_id
        self.upstream = upstream
        self.privoxy  = Privoxy(
            self.port,
            upstream['ip'],
            upstream['port'],
            upstream['protocol'],
        )

    @property
    def port(self): return 20_000 + self.id

    def start(self):
        logger.info(
            f"starting free proxy #{self.id}: "
            f"{self.upstream['ip']}:{self.upstream['port']} ({self.upstream['protocol']})"
        )
        self.privoxy.start()

    def stop(self):
        self.privoxy.stop()

# ─── HAProxy ──────────────────────────────────────────────────────────────────

class HAProxy:
    CONFIG_PATH = '/usr/local/etc/haproxy.cfg'
    PID_FILE    = f'/var/run/haproxy/{PROXY_PORT}.pid'

    def __init__(self):
        self.backends = []
        self._validate_credentials()

    def add_backend(self, proxy):
        self.backends.append({'addr': '127.0.0.1', 'port': proxy.port})

    def start(self):
        ensure_dirs('/var/run/haproxy')
        self._write_config()
        logger.info(f'starting haproxy on internal port {HAPROXY_PORT}')
        fire_and_forget(f'haproxy -f {self.CONFIG_PATH} | logger 2>&1')

    def soft_reload(self):
        if not os.path.exists(self.PID_FILE):
            return
        self._write_config()
        pid = read_pid(self.PID_FILE)
        if pid:
            fire_and_forget(
                f'haproxy -f {self.CONFIG_PATH} -p {self.PID_FILE} -sf {pid} | logger 2>&1'
            )

    def _validate_credentials(self):
        if _proxy_auth_enabled():
            for var in ('PROXY_USER', 'PROXY_PASS'):
                if not os.environ.get(var, '').strip():
                    raise RuntimeError(f"Variable d'environnement manquante : {var}")
        if _stats_auth_enabled():
            for var in ('STATS_USER', 'STATS_PASS'):
                if not os.environ.get(var, '').strip():
                    raise RuntimeError(f"Variable d'environnement manquante : {var}")

    def _write_config(self):
        su = os.environ.get('STATS_USER', '')
        sp = os.environ.get('STATS_PASS', '')
        stats_auth_line = f'  stats auth {su}:{sp}' if _stats_auth_enabled() else ''
        servers = '\n'.join(
            f"  server p{b['port']} {b['addr']}:{b['port']}"
            for b in self.backends
        )
        cfg = f"""global
  maxconn 1024
  daemon
  pidfile {self.PID_FILE}

defaults
  maxconn 1024
  option  dontlognull
  retries 3
  timeout connect 5s
  timeout client 60s
  timeout server 60s


listen stats
  bind *:{STATS_PORT}
  mode            http
  log             global
  maxconn 10
  timeout client  100s
  timeout server  100s
  timeout connect 100s
  timeout queue   100s
  stats enable
  stats hide-version
  stats refresh 30s
  stats show-node
  stats uri /
{stats_auth_line}


frontend rotating_proxies
  bind 127.0.0.1:{HAPROXY_PORT}
  mode tcp
  default_backend tor

backend tor
  mode tcp
  balance leastconn

{servers}
"""
        with open(self.CONFIG_PATH, 'w') as f:
            f.write(cfg)

# ─── Tunnel socket bidirectionnel ────────────────────────────────────────────

def _tunnel_sockets(a, b):
    a.setblocking(False)
    b.setblocking(False)
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                except Exception:
                    return
                if not data:
                    return
                other = b if s is a else a
                try:
                    other.sendall(data)
                except Exception:
                    return
    finally:
        try:
            b.close()
        except Exception:
            pass


# ─── Proxy HTTP avec auth (port 1973) ────────────────────────────────────────
# Mecanisme calque sur Gluetun (github.com/qdm12/gluetun) :
#   CONNECT (HTTPS) : auth -> connect HAProxy -> lit 200 -> repond 200 -> tunnel
#   HTTP            : auth -> requete propre -> connect HAProxy -> tunnel
# PROXY_AUTH_ENABLED=false pour desactiver.

_HOP_HEADERS = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate',
    'proxy-authorization', 'proxy-connection',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
})


class _ProxyHandler(BaseHTTPRequestHandler):
    server_version   = 'ProxySpin/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        logger.debug('Proxy %s -- %s' % (self.address_string(), fmt % args))

    def _auth_ok(self):
        if not _proxy_auth_enabled():
            return True
        user = os.environ.get('PROXY_USER', '')
        if not user:
            return True
        pwd  = os.environ.get('PROXY_PASS', '')
        auth = self.headers.get('Proxy-Authorization', '')
        if auth.startswith('Basic '):
            try:
                decoded = base64.b64decode(auth[6:]).decode('utf-8')
                u, _, p = decoded.partition(':')
                if u == user and p == pwd:
                    return True
            except Exception:
                pass
        self.send_response(407, 'Proxy Authentication Required')
        self.send_header('Proxy-Authenticate', 'Basic realm="ProxySpin"')
        self.send_header('Content-Length', '0')
        self.send_header('Connection', 'close')
        self.end_headers()
        logger.debug('Proxy 407 -> %s (%s %s)' % (self.address_string(), self.command, self.path))
        return False

    def do_CONNECT(self):
        if not self._auth_ok():
            return
        # Mode Cloudflare Worker : tunnel TCP via WebSocket → cloudflare:sockets
        if config.get('mode') == 'cloudflare':
            if not _CF_AVAILABLE:
                self.send_error(503, 'Module cfworker non disponible')
                return
            _cfworker.handle_connect_request(self)
            return
        def _abort():
            """Coupe la connexion client brutalement pour éviter tout fallback direct."""
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

        try:
            backend = socket.create_connection(('127.0.0.1', HAPROXY_PORT), timeout=30)
        except Exception as e:
            logger.warning('CONNECT: backend unreachable: %s' % e)
            _abort()
            return
        connect_req = ('CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (self.path, self.path)).encode()
        backend.sendall(connect_req)
        resp = b''
        try:
            while b'\r\n\r\n' not in resp:
                chunk = backend.recv(4096)
                if not chunk:
                    raise ConnectionError('HAProxy ferme avant 200')
                resp += chunk
                if len(resp) > 8192:
                    raise ConnectionError('Reponse HAProxy trop longue')
        except Exception as e:
            backend.close()
            logger.warning('CONNECT tunnel error: %s' % e)
            _abort()
            return
        first_line = resp.split(b'\r\n')[0].decode('utf-8', errors='replace')
        if '200' not in first_line:
            backend.close()
            logger.warning('CONNECT refused by upstream: %s' % first_line)
            _abort()
            return
        self.send_response(200, 'Connection Established')
        self.end_headers()
        sep   = resp.index(b'\r\n\r\n')
        extra = resp[sep + 4:]
        if extra:
            try:
                self.connection.sendall(extra)
            except Exception:
                backend.close()
                return
        logger.debug('Proxy CONNECT tunnel -> %s (HAProxy:%d)' % (self.path, HAPROXY_PORT))
        _tunnel_sockets(self.connection, backend)

    def _do_http(self):
        if not self._auth_ok():
            return
        # Mode Cloudflare Worker : bypass HAProxy, routage direct via CF Worker
        if config.get('mode') == 'cloudflare':
            if not _CF_AVAILABLE:
                self.send_error(503, 'Module cfworker non disponible')
                return
            _cfworker.handle_http_request(self)
            return
        try:
            backend = socket.create_connection(('127.0.0.1', HAPROXY_PORT), timeout=30)
        except Exception as e:
            self.send_error(502, 'Backend unreachable: %s' % e)
            return
        lines = [('%s %s %s' % (self.command, self.path, self.request_version)).encode()]
        for key, val in self.headers.items():
            if key.lower() not in _HOP_HEADERS:
                lines.append(('%s: %s' % (key, val)).encode())
        request_bytes = b'\r\n'.join(lines) + b'\r\n\r\n'
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            request_bytes += self.rfile.read(content_length)
        try:
            backend.sendall(request_bytes)
        except Exception as e:
            backend.close()
            self.send_error(502, 'Envoi backend echoue : %s' % e)
            return
        _tunnel_sockets(self.connection, backend)

    do_GET     = _do_http
    do_POST    = _do_http
    do_PUT     = _do_http
    do_DELETE  = _do_http
    do_HEAD    = _do_http
    do_OPTIONS = _do_http
    do_PATCH   = _do_http


class ProxyAuthServer:
    def start(self):
        server = ThreadingHTTPServer(('0.0.0.0', PROXY_PORT), _ProxyHandler)
        server.daemon_threads = True
        logger.info('Proxy HTTP (auth) sur port %d -> HAProxy interne :%d' % (PROXY_PORT, HAPROXY_PORT))
        threading.Thread(target=server.serve_forever, daemon=True).start()


# ─── Géolocalisation batch ────────────────────────────────────────────────────

def geolocate_missing(proxies):
    """Géolocalise via ip-api.com les proxies dont country_code est absent.
    Traitement par lots de 100 IPs (API gratuite, ~45 req/min).
    """
    targets = [p for p in proxies if not p.get('country_code')]
    if not targets:
        return
    logger.info(f'Géolocalisation de {len(targets)} IPs (ip-api.com)…')
    for i in range(0, len(targets), 100):
        batch = targets[i:i + 100]
        try:
            body = json.dumps([{'query': p['ip']} for p in batch]).encode()
            req  = Request(
                'http://ip-api.com/batch?fields=query,country,countryCode',
                data=body,
                headers={'Content-Type': 'application/json'},
            )
            with urlopen(req, timeout=10) as r:
                results = json.loads(r.read())
            by_ip = {
                row['query']: row
                for row in results
                if row.get('status') != 'fail'
            }
            for p in batch:
                info = by_ip.get(p['ip'], {})
                p['country_code'] = info.get('countryCode') or None
                p['country_name'] = info.get('country')     or None
        except Exception as e:
            logger.warning(f'Géolocalisation batch échouée — {e}')
        if i + 100 < len(targets):
            time.sleep(1.5)   # ~40 req/min, dans la limite gratuite


# ─── SourceManager ────────────────────────────────────────────────────────────

class SourceManager:
    """Gère les URLs de listes SOCKS personnalisées (proxies privés/payants)."""
    FILE_PATH = os.environ.get('SOURCES_FILE', '/data/sources.json')
    _lock = threading.RLock()

    def load(self):
        with self._lock:
            if not os.path.exists(self.FILE_PATH):
                return []
            try:
                with open(self.FILE_PATH) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f'SourceManager: lecture échouée ({e})')
                return []

    def _save(self, sources):
        ensure_dirs(os.path.dirname(self.FILE_PATH))
        with open(self.FILE_PATH, 'w') as f:
            json.dump(sources, f, indent=2)

    def add(self, url, label=''):
        if not url.startswith('http'):
            return {'ok': False, 'error': 'URL invalide (doit commencer par http)'}
        with self._lock:
            sources = self.load()
            if any(s['url'] == url for s in sources):
                return {'ok': False, 'error': 'Source déjà présente'}
            sources.append({'url': url, 'label': label or url, 'enabled': True})
            self._save(sources)
            return {'ok': True, 'sources': sources}

    def remove(self, url):
        with self._lock:
            sources = self.load()
            new = [s for s in sources if s['url'] != url]
            if len(new) == len(sources):
                return {'ok': False, 'error': 'Source introuvable'}
            self._save(new)
            return {'ok': True, 'sources': new}

    def toggle(self, url, enabled):
        with self._lock:
            sources = self.load()
            for s in sources:
                if s['url'] == url:
                    s['enabled'] = bool(enabled)
                    break
            else:
                return {'ok': False, 'error': 'Source introuvable'}
            self._save(sources)
            return {'ok': True, 'sources': sources}

# ─── Helpers de test proxy ────────────────────────────────────────────────────

_SOCKS_PROBE_HOST = '1.1.1.1'  # IP pour les handshakes SOCKS (pas de résolution DNS nécessaire)
_SOCKS_PROBE_PORT = 443

def _test_socks4(ip, port, timeout):
    """Handshake SOCKS4 + vérification que le tunnel reste ouvert (pas de faux positifs)."""
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        req = struct.pack('!BBH', 4, 1, _SOCKS_PROBE_PORT) + socket.inet_aton(_SOCKS_PROBE_HOST) + b'\x00'
        s.sendall(req)
        s.settimeout(timeout)
        resp = s.recv(8)
        if len(resp) < 2 or resp[1] != 90:
            s.close()
            return False
        # Code 90 = connecté. Vérifie que le tunnel reste ouvert :
        # 1.1.1.1:443 n'envoie rien sans TLS ClientHello → timeout = tunnel actif, close = proxy KO
        s.settimeout(2)
        try:
            data = s.recv(1)
            s.close()
            return False  # La connexion s'est fermée immédiatement — proxy inutilisable
        except socket.timeout:
            s.close()
            return True   # Tunnel vivant
    except Exception:
        return False

def _test_socks5(ip, port, timeout):
    """Handshake SOCKS5 + vérification que le tunnel reste ouvert (pas de faux positifs)."""
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.sendall(b'\x05\x01\x00')
        s.settimeout(timeout)
        resp = s.recv(2)
        if len(resp) < 2 or resp[0] != 5 or resp[1] == 0xFF:
            s.close()
            return False
        req = b'\x05\x01\x00\x01' + socket.inet_aton(_SOCKS_PROBE_HOST) + struct.pack('!H', _SOCKS_PROBE_PORT)
        s.sendall(req)
        resp = s.recv(10)
        if len(resp) < 2 or resp[1] != 0:
            s.close()
            return False
        # Même vérification tunnel
        s.settimeout(2)
        try:
            data = s.recv(1)
            s.close()
            return False
        except socket.timeout:
            s.close()
            return True
    except Exception:
        return False

def _test_proxy(proxy, timeout):
    """Teste le handshake SOCKS4 ou SOCKS5. Les proxies HTTP sont rejetés en amont."""
    protocol = proxy.get('protocol', '')
    ip, port = proxy['ip'], proxy['port']
    if protocol == 'socks4':
        return _test_socks4(ip, port, timeout)
    if protocol in ('socks5', 'socks5h'):
        return _test_socks5(ip, port, timeout)
    return False



# ─── LocalProxyLoader ────────────────────────────────────────────────────────

class LocalProxyLoader:
    DATA_DIR     = '/data'
    TEST_URL     = 'http://icanhazip.com'
    TEST_TIMEOUT = 8
    MAX_THREADS  = 15

    def load_working(self, max_count, on_found=None):
        raw = self._read_all_files()
        if not raw:
            return []
        logger.info(f'LocalProxy: {len(raw)} proxies lus — test ({self.MAX_THREADS} threads)…')

        working  = []
        lock     = threading.Lock()
        work_q   = queue.Queue()
        for p in raw:
            work_q.put(p)

        def worker():
            while True:
                with lock:
                    if len(working) >= max_count:
                        return
                try:
                    proxy = work_q.get(timeout=1)
                except queue.Empty:
                    return
                if self._test(proxy):
                    with lock:
                        if len(working) < max_count:
                            working.append(proxy)
                            logger.info(f"LocalProxy ✓ {proxy['ip']}:{proxy['port']} ({proxy['protocol']})")
                            if on_found:
                                on_found(proxy)

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.MAX_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        geolocate_missing(working)
        logger.info(f'LocalProxy: {len(working)}/{len(raw)} proxies opérationnels')
        return working

    def _read_all_files(self):
        proxies = []
        seen    = set()

        def _add(p):
            key = (p['ip'], p['port'], p['protocol'])
            if key not in seen:
                seen.add(key)
                proxies.append(p)

        # 1. Fichiers .txt locaux
        if os.path.isdir(self.DATA_DIR):
            txt_files = sorted(f for f in os.listdir(self.DATA_DIR) if f.lower().endswith('.txt'))
            for filename in txt_files:
                filepath = os.path.join(self.DATA_DIR, filename)
                file_proxies = self._read_file(filepath)
                before = len(proxies)
                for p in file_proxies:
                    _add(p)
                added = len(proxies) - before
                logger.info(f'LocalProxy: {added} proxies depuis {filename} ({len(file_proxies) - added} doublons ignorés)')

        # 2. URLs de sources configurées (optionnel — proxies SOCKS privés/payants)
        sources = [s for s in SourceManager().load() if s.get('enabled', True)]
        for source in sources:
            url   = source['url']
            label = source.get('label') or url
            try:
                with urlopen(url, timeout=15) as r:
                    content = r.read().decode('utf-8', errors='ignore')
                url_proxies = self._parse_url_content(content)
                before = len(proxies)
                for p in url_proxies:
                    _add(p)
                added = len(proxies) - before
                logger.info(f'LocalProxy: {added} proxies depuis {label} ({len(url_proxies) - added} doublons ignorés)')
            except Exception as e:
                logger.warning(f'LocalProxy: fetch échoué ({label}) — {e}')

        if not proxies:
            logger.warning('LocalProxy: aucun proxy trouvé (aucun fichier .txt ni source configurée)')
        else:
            logger.info(f'LocalProxy: {len(proxies)} proxies uniques au total')
        return proxies

    def _parse_url_content(self, content):
        """Parse le contenu d'une URL (texte brut ip:port par ligne)."""
        results = []
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            parsed = self._parse_line(line)
            if parsed:
                results.append(parsed)
        return results

    def _read_file(self, filepath):
        proxies = []
        try:
            with open(filepath) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith('#'):
                        continue
                    parsed = self._parse_line(line)
                    if parsed:
                        proxies.append(parsed)
                    else:
                        logger.warning(f'LocalProxy: ligne ignorée (format invalide) : {line!r}')
        except OSError as e:
            logger.warning(f'LocalProxy: impossible de lire {filepath} — {e}')
        return proxies

    def _parse_line(self, line):
        if '://' in line:
            protocol, rest = line.split('://', 1)
            protocol = protocol.lower().strip()
        else:
            protocol = 'http'
            rest = line
        parts = rest.rsplit(':', 1)
        if len(parts) != 2:
            return None
        ip, port_str = parts
        try:
            port = int(port_str)
        except ValueError:
            return None
        if protocol not in ('socks4', 'socks5'):
            return None  # proxies HTTP ignorés — ne supportent pas CONNECT/HTTPS
        return {'ip': ip.strip(), 'port': port, 'protocol': protocol, 'score': 0, 'country_code': None, 'country_name': None}

    def _test(self, proxy):
        return _test_proxy(proxy, self.TEST_TIMEOUT)

# ─── ProxyManager ─────────────────────────────────────────────────────────────

class ProxyManager:
    def __init__(self):
        self.haproxy        = HAProxy()
        self._proxies       = []
        self._all_working   = []   # pool complet non filtré (modes proxy/local)
        self._lock          = threading.Lock()
        self._last_rotation = time.time()
        self._loading       = False
        self._loading_msg   = None

    @property
    def mode(self):
        return config['mode']

    def start(self):
        logger.info(f"ProxyManager: démarrage en mode '{self.mode}'")
        self._launch_backends()
        self.haproxy.start()

    def rotate(self):
        with self._lock:
            if self.mode == 'tor':
                for p in self._proxies:
                    p.tor.rotate()
                self._last_rotation = time.time()
                threading.Thread(target=self._probe_tor_backends, args=(15,), daemon=True).start()
                return {'ok': True, 'mode': 'tor', 'message': f"rotate envoyé ({len(self._proxies)} circuits)"}
            elif self.mode == 'cloudflare':
                return {'ok': False, 'message': 'Rotation non applicable en mode Cloudflare Worker'}
            else:
                n = self._reload_local_proxies()
                self._last_rotation = time.time()
                return {'ok': True, 'mode': 'local', 'message': f"{n} proxies rechargés"}

    def switch_mode(self, new_mode):
        if new_mode not in ('tor', 'local', 'cloudflare'):
            return {'ok': False, 'error': 'Mode invalide (tor | local | cloudflare)'}
        with self._lock:
            if new_mode == self.mode:
                return {'ok': False, 'message': f"Déjà en mode '{new_mode}'"}
            old_mode = self.mode
            logger.info(f'Changement de mode: {old_mode} → {new_mode}')
            self._stop_backends()
            self.haproxy.backends.clear()
            with config_lock:
                config['mode'] = new_mode
                if 'AUTO_ROTATION' not in os.environ:
                    config['auto_rotation'] = (new_mode == 'tor')
            if new_mode != 'cloudflare':
                # Tor et Local utilisent HAProxy normalement
                self._launch_backends()
                self.haproxy.soft_reload()
            else:
                # CF Worker : bypass HAProxy, pas de backends à démarrer
                logger.info('Mode Cloudflare Worker activé — HAProxy bypassé')
            return {'ok': True, 'mode': self.mode, 'instances': len(self._proxies)}

    def update_config(self, params):
        with config_lock:
            if 'rotation_interval' in params:
                config['rotation_interval'] = int(params['rotation_interval'])
            if 'auto_rotation' in params:
                config['auto_rotation'] = params['auto_rotation'] in (True, 'true', 1, '1')
        return {'ok': True, **self.status}

    @property
    def status(self):
        return {
            'mode':              self.mode,
            'rotation_interval': config['rotation_interval'],
            'auto_rotation':     config['auto_rotation'],
            'instances':         len(self._proxies),
            'last_rotation':     time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self._last_rotation)),
            'loading':           self._loading,
            'loading_message':   self._loading_msg,
            'country_filter':    config.get('country_filter'),
        }

    @property
    def backends_info(self):
        if self.mode == 'tor':
            return [
                {
                    'type':             'tor',
                    'addr':             f'127.0.0.1:{p.port}',
                    'id':               p.id,
                    'exit_ip':          p.exit_ip,
                    'country_code':     p.exit_country_code,
                    'country_name':     p.exit_country_name,
                    'latency_ms':       p.latency_ms,
                }
                for p in self._proxies
            ]
        return [
            {
                'type':         p.upstream['protocol'],
                'addr':         f"{p.upstream['ip']}:{p.upstream['port']}",
                'id':           p.id,
                'country_code': p.upstream.get('country_code'),
                'country_name': p.upstream.get('country_name'),
            }
            for p in self._proxies
        ]

    def available_countries(self):
        counts = {}
        for p in self._all_working:
            cc = (p.get('country_code') or '').upper()
            if not cc:
                continue
            if cc not in counts:
                counts[cc] = {'code': cc, 'name': p.get('country_name') or cc, 'count': 0}
            counts[cc]['count'] += 1
        return sorted(counts.values(), key=lambda x: -x['count'])

    def set_country(self, code):
        code = code.upper() if code else None
        with config_lock:
            config['country_filter'] = code
        with self._lock:
            if self._all_working and self.mode == 'local':
                self._apply_country_filter()
                return {'ok': True, 'country': code, 'instances': len(self._proxies)}
        return {'ok': True, 'country': code, 'message': 'appliqué à la prochaine rotation'}

    def _filter_by_country(self, proxies, code):
        if not code:
            return proxies
        filtered = [p for p in proxies if (p.get('country_code') or '').upper() == code]
        if not filtered:
            logger.warning(f'Aucun proxy pour le pays {code} — pool complet utilisé')
            return proxies
        return filtered

    def _apply_country_filter(self):
        """Reconstruit les backends à partir du pool en mémoire, sans re-fetch."""
        self._stop_backends()
        self.haproxy.backends.clear()
        filtered = self._filter_by_country(self._all_working, config.get('country_filter'))
        for i, up in enumerate(filtered[:config['max_proxies']]):
            fp = FreeProxy(i, up)
            fp.start()
            self.haproxy.add_backend(fp)
            self._proxies.append(fp)
        self.haproxy.soft_reload()

    def health_check(self):
        if self.mode != 'tor':
            return
        for proxy in list(self._proxies):
            if not proxy.working():
                logger.warning(f'Tor proxy #{proxy.id} KO → redémarrage')
                proxy.restart()

    def run_rotation_loop(self):
        while True:
            time.sleep(5)
            if not config['auto_rotation']:
                continue
            if time.time() - self._last_rotation >= config['rotation_interval']:
                logger.info(f"Rotation auto (mode={self.mode}, interval={config['rotation_interval']}s)")
                self.rotate()

    def _launch_backends(self):
        if self.mode == 'tor':
            self._start_tor_backends()
        elif self.mode == 'cloudflare':
            pass  # Pas de backends HAProxy — bypass direct dans _ProxyHandler
        else:
            self._reload_local_proxies()

    def _start_tor_backends(self):
        for i in range(config['tor_instances']):
            proxy = TorProxy(i)
            self.haproxy.add_backend(proxy)
            proxy.start()
            self._proxies.append(proxy)
        # Sonde les circuits en background après le bootstrap (~30s)
        threading.Thread(target=self._probe_tor_backends, args=(30,), daemon=True).start()

    def _probe_tor_backends(self, delay=0):
        """Récupère l'IP de sortie et la latence de chaque circuit Tor (background)."""
        if delay:
            time.sleep(delay)
        if self.mode != 'tor' or not self._proxies:
            return
        proxies = list(self._proxies)
        threads = [threading.Thread(target=p.probe, daemon=True) for p in proxies]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        # Géolocalisation batch des nouvelles IPs
        to_geo = [p for p in proxies if p.exit_ip and not p.exit_country_code]
        if not to_geo:
            return
        try:
            body = json.dumps([{'query': p.exit_ip} for p in to_geo]).encode()
            req  = Request('http://ip-api.com/batch?fields=query,country,countryCode',
                           data=body, headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=10) as r:
                results = json.loads(r.read())
            by_ip = {row['query']: row for row in results if row.get('status') != 'fail'}
            for p in to_geo:
                info = by_ip.get(p.exit_ip, {})
                p.exit_country_code = info.get('countryCode')
                p.exit_country_name = info.get('country')
        except Exception as e:
            logger.warning(f'Géolocalisation Tor échouée — {e}')

    def _stop_backends(self):
        for p in self._proxies:
            try:
                p.stop()
            except Exception:
                pass
        self._proxies.clear()

    def _make_stream_callback(self):
        """Retourne un callback thread-safe qui démarre chaque proxy dès qu'il est validé."""
        stream_lock = threading.Lock()
        idx = [0]
        max_p = config['max_proxies']

        def on_found(proxy):
            with stream_lock:
                if idx[0] >= max_p:
                    return
                i = idx[0]
                idx[0] += 1
            fp = FreeProxy(i, proxy)
            fp.start()
            with stream_lock:
                self.haproxy.add_backend(fp)
                self._proxies.append(fp)
                self.haproxy.soft_reload()

        return on_found

    def _reload_local_proxies(self):
        country = config.get('country_filter')
        self._loading     = True
        self._loading_msg = f"Chargement et test des proxies SOCKS (dossier {LocalProxyLoader.DATA_DIR})…"
        try:
            max_fetch = config['max_proxies'] * (5 if country else 1)
            self._stop_backends()
            self.haproxy.backends.clear()
            on_found = None if country else self._make_stream_callback()
            upstream_list     = LocalProxyLoader().load_working(max_fetch, on_found=on_found)
            self._all_working = upstream_list
            if country:
                filtered = self._filter_by_country(upstream_list, country)
                for i, up in enumerate(filtered[:config['max_proxies']]):
                    fp = FreeProxy(i, up)
                    fp.start()
                    self.haproxy.add_backend(fp)
                    self._proxies.append(fp)
                self.haproxy.soft_reload()
            return len(self._proxies)
        finally:
            self._loading     = False
            self._loading_msg = None

# ─── Web UI HTML ──────────────────────────────────────────────────────────────

WEB_UI_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ProxySpin — Panel</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:'Courier New',monospace;background:#0d0d1a;color:#e0e0e0;min-height:100vh;padding:24px}
    h1{color:#7c83fd;font-size:22px;margin-bottom:6px;letter-spacing:1px}
    .subtitle{color:#555;font-size:12px;margin-bottom:28px}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
    @media(max-width:700px){.grid{grid-template-columns:1fr}}
    .card{background:#13132a;border:1px solid #2d2d5e;border-radius:12px;padding:20px}
    .card h2{color:#7c83fd;font-size:14px;margin-bottom:16px;letter-spacing:.5px}
    .row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;font-size:13px}
    .label{color:#666}
    .value{color:#e0e0e0;font-weight:bold}
    .badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:bold}
    .badge-tor{background:#1a1a3e;color:#7c83fd;border:1px solid #7c83fd}
    .badge-proxy{background:#1a3020;color:#4caf50;border:1px solid #4caf50}
    .badge-cloudflare{background:#1a2010;color:#ff9800;border:1px solid #ff9800}
    .badge-ok{background:#1a3020;color:#4caf50}
    .badge-off{background:#3e1a1a;color:#f44336}
    .quota-bar-bg{background:#0d0d1a;border-radius:6px;overflow:hidden;height:8px;margin:8px 0}
    .quota-bar{height:100%;border-radius:6px;transition:width .5s,background .3s}
    .quota-bar.ok{background:#4caf50}
    .quota-bar.warn{background:#ff9800}
    .quota-bar.danger{background:#f44336}
    .cf-note{margin-top:12px;padding:8px 12px;background:#2a1a00;border:1px solid #ff980033;border-radius:6px;font-size:11px;color:#ff9800;line-height:1.5}
    select,input[type=number]{background:#0d0d1a;border:1px solid #2d2d5e;border-radius:6px;color:#e0e0e0;font-family:monospace;font-size:13px;padding:6px 10px;width:100%}
    .field{margin-bottom:12px}
    .field label{display:block;color:#666;font-size:11px;margin-bottom:5px}
    .toggle{display:flex;align-items:center;gap:10px;cursor:pointer}
    .toggle input{width:auto}
    button{border:none;border-radius:8px;cursor:pointer;font-family:monospace;font-size:13px;padding:9px 16px;transition:.2s}
    .btn-primary{background:#7c83fd;color:#fff;width:100%;margin-top:8px}
    .btn-primary:hover{background:#5c63e0}
    .btn-primary:disabled{opacity:.5;cursor:not-allowed}
    .btn-save{background:#2d5e3e;color:#4caf50;border:1px solid #4caf50;width:100%;margin-top:8px}
    .btn-save:hover{background:#3d7e4e}
    table{width:100%;border-collapse:collapse;font-size:12px;margin-top:4px}
    th{color:#555;text-align:left;padding:4px 8px;border-bottom:1px solid #2d2d5e}
    td{padding:6px 8px;border-bottom:1px solid #1a1a2e;color:#ccc}
    tr:last-child td{border-bottom:none}
    .type-tor{color:#7c83fd}
    .type-http{color:#4caf50}
    .type-socks5{color:#ff9800}
    #msg{font-size:12px;color:#4caf50;min-height:16px;margin-top:6px;text-align:center}
    #last-rot{font-size:11px;color:#555}
    .src-url{color:#aaa;font-size:11px;word-break:break-all}
    .btn-del{background:#3e1a1a;color:#f44336;border:1px solid #f44336;border-radius:5px;padding:2px 8px;font-size:11px;cursor:pointer;font-family:monospace}
    .btn-del:hover{background:#5e2a2a}
    #lang-bar{position:fixed;top:16px;right:20px;display:flex;gap:6px;z-index:9999}
    .btn-lang{background:none;border:none;cursor:pointer;font-size:18px;padding:2px;opacity:.4;transition:.15s}
    .btn-lang:hover,.btn-lang.active{opacity:1}
  </style>
</head>
<body>
  <div id="lang-bar">
    <button class="btn-lang" id="btn-lang-fr" title="Fran&#xE7;ais">&#x1F1EB;&#x1F1F7;</button>
    <button class="btn-lang" id="btn-lang-en" title="English">&#x1F1EC;&#x1F1E7;</button>
  </div>

  <h1>&#x2B21; PROXYSPIN</h1>
  <p class="subtitle" id="lbl-subtitle"></p>

  <div class="grid">
    <div class="card">
      <h2 id="lbl-status-card"></h2>
      <div class="row"><span class="label" id="lbl-mode"></span><span id="mode-badge" class="badge">&#x2026;</span></div>
      <div class="row"><span class="label" id="lbl-instances"></span><span class="value" id="instances">&#x2026;</span></div>
      <div class="row"><span class="label" id="lbl-autorot"></span><span class="badge" id="auto-badge">&#x2026;</span></div>
      <div class="row"><span class="label" id="lbl-interval"></span><span class="value" id="interval-val">&#x2026;</span></div>
      <div class="row" id="country-row" style="display:none">
        <span class="label" id="lbl-country-filter"></span>
        <select id="sel-country" style="width:auto;padding:3px 8px;font-size:12px">
          <option value=""></option>
        </select>
      </div>
      <p id="last-rot"></p>
      <button class="btn-primary" id="btn-rotate"></button>
      <div id="msg"></div>
    </div>

    <div class="card">
      <h2 id="lbl-config-card"></h2>
      <div class="field">
        <label id="lbl-mode-field"></label>
        <select id="sel-mode">
          <option value="tor">&#x1F9C5; Tor</option>
          <option value="local" id="opt-local"></option>
          <option value="cloudflare" id="opt-cloudflare"></option>
        </select>
      </div>
      <div class="field">
        <label id="lbl-autorot-field"></label>
        <label class="toggle"><input type="checkbox" id="chk-auto"> <span id="lbl-autorot-check"></span></label>
      </div>
      <div class="field">
        <label id="lbl-interval-field"></label>
        <input type="number" id="inp-interval" min="10" max="3600" value="60">
      </div>
      <button class="btn-save" id="btn-save"></button>
      <div id="msg-cfg"></div>
    </div>
  </div>

  <div class="card">
    <h2 id="lbl-backends-card"></h2>
    <table id="backends-table">
      <thead id="backends-head"></thead>
      <tbody id="backends-body"></tbody>
    </table>
  </div>

  <div class="card" style="margin-top:16px">
    <h2><span id="lbl-sources-card"></span> <span style="color:#555;font-size:11px;font-weight:normal" id="lbl-sources-mode"></span></h2>
    <table>
      <thead><tr><th style="width:32px" id="th-on"></th><th style="width:140px" id="th-label"></th><th>URL</th><th style="width:32px"></th></tr></thead>
      <tbody id="sources-body"><tr><td colspan="4" style="color:#555" id="td-src-loading"></td></tr></tbody>
    </table>
    <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:flex-end">
      <div style="flex:1;min-width:120px">
        <div class="field" style="margin-bottom:0"><label id="lbl-src-label"></label>
        <input type="text" id="inp-src-label"></div>
      </div>
      <div style="flex:3;min-width:200px">
        <div class="field" style="margin-bottom:0"><label id="lbl-src-url"></label>
        <input type="text" id="inp-src-url" placeholder="https://&#x2026;"></div>
      </div>
      <button class="btn-save" id="btn-add-src" style="white-space:nowrap;height:34px"></button>
    </div>
    <div id="msg-sources" style="font-size:12px;color:#4caf50;min-height:16px;margin-top:6px"></div>
  </div>

  <!-- ─── Carte Cloudflare Worker (visible uniquement en mode cloudflare) ─── -->
  <div class="card" id="cf-card" style="margin-top:16px;display:none">
    <h2 id="lbl-cf-card"></h2>

    <div class="field">
      <label id="lbl-cf-token"></label>
      <input type="password" id="inp-cf-token" autocomplete="off" placeholder="&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;&#x2022;">
    </div>
    <div class="field">
      <label id="lbl-cf-account"></label>
      <input type="text" id="inp-cf-account" placeholder="abc123def456...">
    </div>
    <div class="field">
      <label id="lbl-cf-name"></label>
      <input type="text" id="inp-cf-name" placeholder="mon-proxy">
      <span id="cf-worker-url" style="color:#555;font-size:11px;display:block;margin-top:4px"></span>
    </div>
    <div class="field">
      <label id="lbl-cf-sep"></label>
      <input type="text" id="inp-cf-sep" placeholder="------">
    </div>
    <div style="display:flex;gap:8px;margin-top:4px">
      <button class="btn-save" id="btn-cf-save" style="flex:1;margin-top:0"></button>
      <button class="btn-primary" id="btn-cf-deploy" style="flex:1;margin-top:0"></button>
    </div>
    <div id="msg-cf" style="font-size:12px;color:#4caf50;min-height:16px;margin-top:6px;text-align:center"></div>

    <div style="margin-top:14px;padding-top:14px;border-top:1px solid #2d2d5e">
      <div class="row"><span class="label" id="lbl-cf-deployed"></span><span class="value" id="cf-deployed-at" style="color:#666;font-size:12px">—</span></div>
      <div class="row" style="margin-top:6px"><span class="label" id="lbl-cf-sha"></span><span id="cf-sha" style="color:#555;font-family:monospace;font-size:11px">—</span></div>
    </div>

    <div style="margin-top:14px;padding-top:14px;border-top:1px solid #2d2d5e">
      <div class="row">
        <span class="label" id="lbl-cf-quota"></span>
        <span class="value" id="cf-quota-count">—</span>
      </div>
      <div class="quota-bar-bg">
        <div class="quota-bar ok" id="cf-quota-bar" style="width:0%"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#555">
        <span id="cf-quota-pct">0%</span>
        <span id="cf-quota-reset"></span>
      </div>
    </div>

    <div class="cf-note" id="cf-https-warn"></div>
  </div>

  <script>
    // ── i18n ──────────────────────────────────────────────────────────────────
    var LANG = localStorage.getItem('ps_lang') || (navigator.language.startsWith('fr') ? 'fr' : 'en');
    var TR = {
      fr: {
        subtitle:       'Panel de contr\u00f4le',
        status_card:    'STATUT',
        lbl_mode:       'Mode',
        lbl_instances:  'Instances',
        lbl_autorot:    'Rotation auto',
        lbl_interval:   'Intervalle',
        lbl_country:    'Filtre pays',
        all_countries:  '🌍 Tous',
        config_card:    'CONFIGURATION',
        lbl_mode_field: 'Mode',
        opt_local:      '📂 Proxies SOCKS locaux',
        lbl_autorot_f:  'Rotation automatique',
        lbl_autorot_c:  'Activ\u00e9e',
        lbl_interval_f: 'Intervalle de rotation (secondes)',
        save:           'Enregistrer',
        backends_card:  'BACKENDS ACTIFS',
        th_type:        'Type',
        th_addr:        'Adresse',
        th_country:     'Pays',
        th_exit_ip:     'IP de sortie',
        th_latency:     'Latence',
        probing:        'Sondage en cours\u2026',
        loading:        'Chargement\u2026',
        no_backend:     'Aucun backend actif',
        sources_card:   'SOURCES SOCKS',
        sources_mode:   '(mode local)',
        th_on:          'On',
        th_label:       'Label',
        src_url_lbl:    'URL de la source',
        src_lbl_lbl:    'Label (optionnel)',
        src_lbl_ph:     'Mon repo',
        add_btn:        '+ Ajouter',
        no_source:      'Aucune source',
        last_rot:       'Derni\u00e8re rotation\u00a0: ',
        in_progress:    '\u29d7 En cours\u2026',
        cfg_saved:      '\u2713 Configuration sauvegard\u00e9e',
        applying:       '\u29d7 Application\u2026',
        mode_switch:    '\u29d7 Changement de mode (peut prendre 1-2 min)\u2026',
        adding:         '\u29d7 Ajout\u2026',
        src_added:      '\u2713 Source ajout\u00e9e',
        error:          'Erreur',
        new_ip:         '🔄 Nouvelle IP',
        rotate_ok:      '\u2713 IP chang\u00e9e',
        opt_cloudflare: '&#x2601; Cloudflare Worker',
        cf_card:        'CLOUDFLARE WORKER',
        cf_token:       'API Token Cloudflare',
        cf_account:     'Account ID',
        cf_name:        'Nom du Worker',
        cf_sep:         'S\u00e9parateur',
        cf_save:        'Sauvegarder',
        cf_deploy:      '&#x1F680; D\u00e9ployer / Mettre \u00e0 jour',
        cf_deploying:   '\u29d7 D\u00e9ploiement en cours\u2026',
        cf_deployed:    'D\u00e9ploy\u00e9 avec succ\u00e8s',
        cf_not_dep:     'Jamais d\u00e9ploy\u00e9',
        cf_last_dep:    'Dernier d\u00e9ploiement',
        cf_sha:         'Version JS',
        cf_quota:       'Quota journalier',
        cf_reset_in:    'Reset dans',
        cf_warn:        '\u2705 CF Worker supporte HTTP et HTTPS (tunnel TCP via WebSocket).',
      },
      en: {
        subtitle:       'Control panel',
        status_card:    'STATUS',
        lbl_mode:       'Mode',
        lbl_instances:  'Instances',
        lbl_autorot:    'Auto rotation',
        lbl_interval:   'Interval',
        lbl_country:    'Country filter',
        all_countries:  '🌍 All',
        config_card:    'CONFIGURATION',
        lbl_mode_field: 'Mode',
        opt_local:      '📂 Local SOCKS proxies',
        lbl_autorot_f:  'Automatic rotation',
        lbl_autorot_c:  'Enabled',
        lbl_interval_f: 'Rotation interval (seconds)',
        save:           'Save',
        backends_card:  'ACTIVE BACKENDS',
        th_type:        'Type',
        th_addr:        'Address',
        th_country:     'Country',
        th_exit_ip:     'Exit IP',
        th_latency:     'Latency',
        probing:        'Probing\u2026',
        loading:        'Loading\u2026',
        no_backend:     'No active backend',
        sources_card:   'SOCKS SOURCES',
        sources_mode:   '(local mode)',
        th_on:          'On',
        th_label:       'Label',
        src_url_lbl:    'Source URL',
        src_lbl_lbl:    'Label (optional)',
        src_lbl_ph:     'My repo',
        add_btn:        '+ Add',
        no_source:      'No source',
        last_rot:       'Last rotation: ',
        in_progress:    '\u29d7 In progress\u2026',
        cfg_saved:      '\u2713 Configuration saved',
        applying:       '\u29d7 Applying\u2026',
        mode_switch:    '\u29d7 Switching mode (may take 1-2 min)\u2026',
        adding:         '\u29d7 Adding\u2026',
        src_added:      '\u2713 Source added',
        error:          'Error',
        new_ip:         '🔄 New IP',
        rotate_ok:      '\u2713 IP changed',
        opt_cloudflare: '&#x2601; Cloudflare Worker',
        cf_card:        'CLOUDFLARE WORKER',
        cf_token:       'Cloudflare API Token',
        cf_account:     'Account ID',
        cf_name:        'Worker Name',
        cf_sep:         'Separator',
        cf_save:        'Save',
        cf_deploy:      '&#x1F680; Deploy / Update',
        cf_deploying:   '\u29d7 Deploying\u2026',
        cf_deployed:    'Successfully deployed',
        cf_not_dep:     'Never deployed',
        cf_last_dep:    'Last deployment',
        cf_sha:         'JS Version',
        cf_quota:       'Daily quota',
        cf_reset_in:    'Reset in',
        cf_warn:        '\u2705 CF Worker supports HTTP and HTTPS (TCP tunnel via WebSocket).',
      }
    };
    function t(k) { return (TR[LANG] && TR[LANG][k]) || TR.fr[k] || k; }

    function applyTranslations() {
      document.documentElement.lang = LANG;
      document.getElementById('lbl-subtitle').textContent       = t('subtitle');
      document.getElementById('lbl-status-card').textContent    = t('status_card');
      document.getElementById('lbl-mode').textContent           = t('lbl_mode');
      document.getElementById('lbl-instances').textContent      = t('lbl_instances');
      document.getElementById('lbl-autorot').textContent        = t('lbl_autorot');
      document.getElementById('lbl-interval').textContent       = t('lbl_interval');
      document.getElementById('lbl-country-filter').textContent = t('lbl_country');
      document.getElementById('lbl-config-card').textContent    = t('config_card');
      document.getElementById('lbl-mode-field').textContent     = t('lbl_mode_field');
      document.getElementById('opt-local').textContent          = t('opt_local');
      document.getElementById('lbl-autorot-field').textContent  = t('lbl_autorot_f');
      document.getElementById('lbl-autorot-check').textContent  = t('lbl_autorot_c');
      document.getElementById('lbl-interval-field').textContent = t('lbl_interval_f');
      document.getElementById('btn-save').textContent           = t('save');
      document.getElementById('lbl-backends-card').textContent  = t('backends_card');
      document.getElementById('lbl-sources-card').textContent   = t('sources_card');
      document.getElementById('lbl-sources-mode').textContent   = t('sources_mode');
      document.getElementById('th-on').textContent              = t('th_on');
      document.getElementById('th-label').textContent           = t('th_label');
      document.getElementById('lbl-src-url').textContent        = t('src_url_lbl');
      document.getElementById('lbl-src-label').textContent      = t('src_lbl_lbl');
      document.getElementById('inp-src-label').placeholder      = t('src_lbl_ph');
      document.getElementById('btn-add-src').textContent        = t('add_btn');
      document.getElementById('btn-rotate').textContent         = t('new_ip');
      document.getElementById('opt-cloudflare').innerHTML       = t('opt_cloudflare');
      document.getElementById('lbl-cf-card').textContent        = t('cf_card');
      document.getElementById('lbl-cf-token').textContent       = t('cf_token');
      document.getElementById('lbl-cf-account').textContent     = t('cf_account');
      document.getElementById('lbl-cf-name').textContent        = t('cf_name');
      document.getElementById('lbl-cf-sep').textContent         = t('cf_sep');
      document.getElementById('btn-cf-save').textContent        = t('cf_save');
      document.getElementById('btn-cf-deploy').innerHTML        = t('cf_deploy');
      document.getElementById('lbl-cf-deployed').textContent    = t('cf_last_dep');
      document.getElementById('lbl-cf-sha').textContent         = t('cf_sha');
      document.getElementById('lbl-cf-quota').textContent       = t('cf_quota');
      document.getElementById('cf-https-warn').textContent      = t('cf_warn');
      // country select first option
      var sel = document.getElementById('sel-country');
      if (sel.options.length > 0) sel.options[0].textContent = t('all_countries');
      // lang buttons
      document.getElementById('btn-lang-fr').classList.toggle('active', LANG === 'fr');
      document.getElementById('btn-lang-en').classList.toggle('active', LANG === 'en');
    }

    function setLang(lang) {
      LANG = lang;
      localStorage.setItem('ps_lang', lang);
      applyTranslations();
    }

    document.getElementById('btn-lang-fr').addEventListener('click', function() { setLang('fr'); });
    document.getElementById('btn-lang-en').addEventListener('click', function() { setLang('en'); });

    // ── Helpers ───────────────────────────────────────────────────────────────
    const $ = id => document.getElementById(id);

    function latColor(ms) {
      if (ms < 1000) return '#4caf50';
      if (ms < 3000) return '#ff9800';
      return '#f44336';
    }

    function countryFlag(code) {
      if (!code || code.length !== 2) return '🌍';
      var c = code.toUpperCase();
      return String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65) +
             String.fromCodePoint(0x1F1E6 + c.charCodeAt(1) - 65);
    }

    async function api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      return r.json();
    }

    // ── Pays ──────────────────────────────────────────────────────────────────
    var _countriesCache = [];
    async function loadCountriesWeb(currentFilter) {
      var r = await api('GET', '/api/countries').catch(() => ({ countries: [] }));
      _countriesCache = r.countries || [];
      var sel = $('sel-country');
      var prev = sel.value;
      sel.innerHTML = '<option value="">' + t('all_countries') + '</option>' +
        _countriesCache.map(function(c) {
          return '<option value="' + c.code + '">' + countryFlag(c.code) + ' ' + c.name + ' (' + c.count + ')</option>';
        }).join('');
      sel.value = currentFilter || prev || '';
    }

    $('sel-country').addEventListener('change', async function() {
      await api('POST', '/api/country', { country: this.value });
      refresh();
    });

    // ── Refresh ───────────────────────────────────────────────────────────────
    async function refresh() {
      const s = await api('GET', '/api/status').catch(() => null);
      if (!s) return;

      const modeBadge = $('mode-badge');
      if (s.mode === 'tor') {
        modeBadge.textContent = '🧅 Tor';
        modeBadge.className = 'badge badge-tor';
      } else if (s.mode === 'cloudflare') {
        modeBadge.innerHTML = '&#x2601; Cloudflare';
        modeBadge.className = 'badge badge-cloudflare';
      } else {
        modeBadge.textContent = '📂 Local';
        modeBadge.className = 'badge badge-proxy';
      }

      $('instances').textContent  = s.mode === 'cloudflare' ? '1 Worker' : s.instances;
      $('auto-badge').textContent = s.auto_rotation ? 'ON' : 'OFF';
      $('auto-badge').className   = 'badge ' + (s.auto_rotation ? 'badge-ok' : 'badge-off');
      $('interval-val').textContent = s.rotation_interval + 's';
      $('last-rot').textContent   = t('last_rot') + new Date(s.last_rotation).toLocaleTimeString();

      // Carte CF Worker : visible uniquement en mode cloudflare
      var cfCard = $('cf-card');
      if (s.mode === 'cloudflare') {
        cfCard.style.display = 'block';
        loadCFStatus();
        startCFQuotaPolling();
      } else {
        cfCard.style.display = 'none';
        stopCFQuotaPolling();
      }

      $('sel-mode').value     = s.mode;
      $('chk-auto').checked   = s.auto_rotation;
      $('inp-interval').value = s.rotation_interval;

      const bk = await api('GET', '/api/backends').catch(() => ({ backends: [] }));
      const tbody = $('backends-body');
      const thead = $('backends-head');
      if (!bk.backends || bk.backends.length === 0) {
        thead.innerHTML = '';
        tbody.innerHTML = '<tr><td style="color:#555">' + t('no_backend') + '</td></tr>';
      } else if (s.mode === 'tor') {
        thead.innerHTML = '<tr><th>#</th><th>' + t('th_exit_ip') + '</th><th>' + t('th_country') + '</th><th>' + t('th_latency') + '</th></tr>';
        tbody.innerHTML = bk.backends.map(function(b) {
          var flag    = b.country_code ? countryFlag(b.country_code) : '';
          var country = flag + ' ' + (b.country_name || b.country_code || '');
          var ip      = b.exit_ip   || '<span style="color:#555">' + t('probing') + '</span>';
          var lat     = b.latency_ms != null ? '<span style="color:' + latColor(b.latency_ms) + '">' + b.latency_ms + ' ms</span>' : '<span style="color:#555">\u2014</span>';
          return '<tr><td>' + b.id + '</td><td style="font-family:monospace">' + ip + '</td><td title="' + (b.country_name || '') + '">' + country + '</td><td>' + lat + '</td></tr>';
        }).join('');
      } else {
        thead.innerHTML = '<tr><th>#</th><th>' + t('th_type') + '</th><th>' + t('th_addr') + '</th><th>' + t('th_country') + '</th></tr>';
        tbody.innerHTML = bk.backends.map(function(b) {
          var flag = b.country_code ? countryFlag(b.country_code) : '\u2014';
          var cc   = b.country_code || '';
          return '<tr><td>' + b.id + '</td><td class="type-' + b.type + '">' + b.type + '</td><td>' + b.addr + '</td><td title="' + (b.country_name || '') + '">' + flag + ' ' + cc + '</td></tr>';
        }).join('');
      }

      if (s.mode === 'local') {
        $('country-row').style.display = 'flex';
        loadCountriesWeb(s.country_filter);
      } else {
        $('country-row').style.display = 'none';
      }
    }

    $('btn-rotate').addEventListener('click', async () => {
      $('btn-rotate').disabled = true;
      $('msg').textContent = t('in_progress');
      const r = await api('POST', '/api/rotate').catch(() => ({ ok: false }));
      $('msg').textContent = r.ok ? t('rotate_ok') : '\u2717 ' + t('error');
      setTimeout(() => { $('msg').textContent = ''; $('btn-rotate').disabled = false; }, 5000);
      refresh();
    });

    $('btn-save').addEventListener('click', async () => {
      const mode     = $('sel-mode').value;
      const auto     = $('chk-auto').checked;
      const interval = parseInt($('inp-interval').value, 10);
      $('msg-cfg').textContent = t('applying');
      const s = await api('GET', '/api/status');
      if (s.mode !== mode) {
        $('msg-cfg').textContent = t('mode_switch');
        await api('POST', '/api/mode', { mode });
      }
      await api('POST', '/api/config', { auto_rotation: auto, rotation_interval: interval });
      $('msg-cfg').textContent = t('cfg_saved');
      setTimeout(() => { $('msg-cfg').textContent = ''; }, 4000);
      refresh();
    });

    applyTranslations();
    refresh();
    setInterval(refresh, 8000);

    // ── Sources ───────────────────────────────────────────────────────────────
    async function loadSources() {
      const r = await api('GET', '/api/sources').catch(() => ({ sources: [] }));
      renderSources(r.sources || []);
    }

    function renderSources(sources) {
      var tbody = $('sources-body');
      if (!sources.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:#555">' + t('no_source') + '</td></tr>';
        return;
      }
      tbody.innerHTML = sources.map(function(s) {
        var u = s.url.replace(/"/g, '&quot;').replace(/</g, '&lt;');
        var lbl = (s.label || s.url).replace(/</g, '&lt;');
        return '<tr>' +
          '<td><input type="checkbox" class="src-toggle" data-url="' + u + '"' + (s.enabled ? ' checked' : '') + '></td>' +
          '<td>' + lbl + '</td>' +
          '<td class="src-url">' + u + '</td>' +
          '<td><button class="btn-del" data-url="' + u + '">&#x2715;</button></td>' +
          '</tr>';
      }).join('');
      tbody.querySelectorAll('.src-toggle').forEach(function(cb) {
        cb.addEventListener('change', function() { toggleSource(cb.dataset.url, cb.checked); });
      });
      tbody.querySelectorAll('.btn-del').forEach(function(btn) {
        btn.addEventListener('click', function() { removeSource(btn.dataset.url); });
      });
    }

    async function toggleSource(url, enabled) {
      await api('POST', '/api/sources/toggle', { url: url, enabled: enabled }).catch(() => {});
      loadSources();
    }

    async function removeSource(url) {
      var r = await api('POST', '/api/sources/remove', { url: url }).catch(() => ({ ok: false }));
      if (r.ok) { loadSources(); }
      else { $('msg-sources').textContent = '\u2717 ' + (r.error || t('error')); }
    }

    $('btn-add-src').addEventListener('click', async function() {
      var url   = $('inp-src-url').value.trim();
      var label = $('inp-src-label').value.trim();
      if (!url) return;
      $('msg-sources').textContent = t('adding');
      var r = await api('POST', '/api/sources', { url: url, label: label }).catch(() => ({ ok: false }));
      if (r.ok) {
        $('inp-src-url').value = '';
        $('inp-src-label').value = '';
        $('msg-sources').textContent = t('src_added');
        loadSources();
      } else {
        $('msg-sources').textContent = '\u2717 ' + (r.error || t('error'));
      }
      setTimeout(function() { $('msg-sources').textContent = ''; }, 3000);
    });

    loadSources();

    // ── Cloudflare Worker ────────────────────────────────────────────────────
    var _cfStatusLoaded = false;
    var _cfQuotaInterval = null;

    async function loadCFStatus() {
      if (_cfStatusLoaded) return;
      const r = await api('GET', '/api/cf/status').catch(() => null);
      if (!r) return;
      _cfStatusLoaded = true;
      $('inp-cf-account').value = r.account_id  || '';
      $('inp-cf-name').value    = r.worker_name || '';
      $('inp-cf-sep').value     = r.separator   || '------';
      // Ne pas pré-remplir le token (sécurité)
      if (r.worker_url) {
        $('cf-worker-url').textContent = '\u2192 ' + r.worker_url;
      }
      $('cf-deployed-at').textContent = r.last_deployed || t('cf_not_dep');
      $('cf-sha').textContent = r.last_sha256 ? 'sha256:' + r.last_sha256 : '\u2014';
    }

    async function refreshCFQuota() {
      const r = await api('GET', '/api/cf/stats').catch(() => null);
      if (!r || r.used === undefined) return;
      var pct = r.percent || 0;
      var barEl = $('cf-quota-bar');
      $('cf-quota-count').textContent = r.used.toLocaleString() + ' / ' + r.limit.toLocaleString();
      barEl.style.width = pct + '%';
      barEl.className = 'quota-bar ' + (pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : 'ok');
      $('cf-quota-pct').textContent = pct + '%';
      if (r.reset_in_s > 0) {
        var h = Math.floor(r.reset_in_s / 3600);
        var m = Math.floor((r.reset_in_s % 3600) / 60);
        $('cf-quota-reset').textContent = t('cf_reset_in') + ' ' + h + 'h ' + m + 'min';
      }
    }

    function startCFQuotaPolling() {
      if (_cfQuotaInterval) return;
      refreshCFQuota();
      _cfQuotaInterval = setInterval(refreshCFQuota, 30000);
    }

    function stopCFQuotaPolling() {
      if (_cfQuotaInterval) { clearInterval(_cfQuotaInterval); _cfQuotaInterval = null; }
    }

    // Prévisualisation URL en direct
    $('inp-cf-name').addEventListener('input', function() {
      var name = this.value.trim();
      $('cf-worker-url').textContent = name ? '\u2192 https://' + name + '.workers.dev' : '';
    });

    // Sauvegarde config
    $('btn-cf-save').addEventListener('click', async function() {
      var body = {
        account_id:  $('inp-cf-account').value.trim(),
        worker_name: $('inp-cf-name').value.trim(),
        separator:   $('inp-cf-sep').value.trim() || '------',
      };
      var token = $('inp-cf-token').value.trim();
      if (token) body.api_token = token;
      $('msg-cf').textContent = t('applying');
      var r = await api('POST', '/api/cf/config', body).catch(() => ({ ok: false }));
      $('msg-cf').textContent = r.ok ? t('cfg_saved') : '\u2717 ' + (r.error || t('error'));
      if (r.ok) { $('inp-cf-token').value = ''; _cfStatusLoaded = false; }
      setTimeout(function() { $('msg-cf').textContent = ''; }, 4000);
    });

    // Déploiement
    $('btn-cf-deploy').addEventListener('click', async function() {
      $('btn-cf-deploy').disabled = true;
      $('msg-cf').textContent = t('cf_deploying');
      var r = await api('POST', '/api/cf/deploy').catch(() => ({ ok: false }));
      if (r.ok) {
        $('cf-deployed-at').textContent = r.deployed_at || '';
        $('cf-sha').textContent = r.sha256 ? 'sha256:' + r.sha256 : '';
        $('msg-cf').textContent = '\u2713 ' + t('cf_deployed');
      } else {
        $('msg-cf').textContent = '\u2717 ' + (r.error || t('error'));
      }
      $('btn-cf-deploy').disabled = false;
      setTimeout(function() { $('msg-cf').textContent = ''; }, 6000);
    });
  </script>

  <footer style="margin-top:24px;padding:16px 0 8px;border-top:1px solid #2d2d5e;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;font-size:11px;color:#555">
    <span>&#x2B21; ProxySpin</span>
    <div style="display:flex;align-items:center;gap:10px">
      <a href="https://github.com/Aerya" target="_blank" style="color:#7c83fd;text-decoration:none;font-weight:500">GitHub</a>
      <a href="https://upandclear.org/" target="_blank" style="color:#7c83fd;text-decoration:none;font-weight:500">Blog</a>
      <span style="color:#444">Par</span>
      <strong style="color:#7c83fd">Aerya</strong>
      <img src="https://upandclear.org/wp-content/uploads/2024/06/Logo.detoure1.png.webp" alt="Aerya" style="width:20px;height:20px;border-radius:50%;object-fit:cover">
    </div>
  </footer>
</body>
</html>"""

# ─── API + Web UI (port 1974) ─────────────────────────────────────────────────

class APIHandler(BaseHTTPRequestHandler):
    manager = None  # Défini depuis main()

    def log_message(self, fmt, *args):
        logger.debug(f'API {self.address_string()} — {fmt % args}')

    def _check_auth(self):
        """Vérifie le Basic auth. API_AUTH_ENABLED=false pour désactiver."""
        if not _api_auth_enabled():
            return True
        expected_user = os.environ.get('STATS_USER', '')
        expected_pass = os.environ.get('STATS_PASS', '')
        header = self.headers.get('Authorization', '')
        if header.startswith('Basic '):
            try:
                decoded = base64.b64decode(header[6:]).decode('utf-8')
                user, _, pwd = decoded.partition(':')
                if user == expected_user and pwd == expected_pass:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="ProxySpin"')
        self.send_header('Content-Length', '0')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        path = self.path.split('?')[0]
        if path in ('/', '/index.html'):
            self._html(WEB_UI_HTML)
        elif path == '/api/status':
            self._json(self.manager.status)
        elif path == '/api/backends':
            self._json({'backends': self.manager.backends_info})
        elif path == '/api/sources':
            self._json({'sources': SourceManager().load()})  # listes SOCKS personnalisées
        elif path == '/api/countries':
            self._json({'countries': self.manager.available_countries()})
        elif path == '/api/cf/status':
            self._json(self._cf_status())
        elif path == '/api/cf/stats':
            self._json(self._cf_stats())
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        if not self._check_auth():
            return
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b'{}'
        try:
            params = json.loads(body)
        except Exception:
            params = {}
        path = self.path.split('?')[0]
        if path == '/api/rotate':
            self._json(self.manager.rotate())
        elif path == '/api/mode':
            self._json(self.manager.switch_mode(params.get('mode', '')))
        elif path == '/api/config':
            self._json(self.manager.update_config(params))
        elif path == '/api/sources':
            self._json(SourceManager().add(params.get('url', ''), params.get('label', '')))
        elif path == '/api/sources/remove':
            self._json(SourceManager().remove(params.get('url', '')))
        elif path == '/api/sources/toggle':
            self._json(SourceManager().toggle(params.get('url', ''), params.get('enabled', True)))  # noqa
        elif path == '/api/country':
            self._json(self.manager.set_country(params.get('country', '')))
        elif path == '/api/cf/config':
            self._json(self._cf_save_config(params))
        elif path == '/api/cf/deploy':
            self._json(self._cf_deploy())
        else:
            self._json({'error': 'not found'}, 404)

    # ── Helpers CF Worker ─────────────────────────────────────────────────────

    def _cf_status(self) -> dict:
        if not _CF_AVAILABLE:
            return {'available': False, 'error': 'Module cfworker non disponible'}
        cfg  = _cfworker.CFWorkerConfig().load()
        name = cfg.get('worker_name', '')
        return {
            'available':     True,
            'worker_name':   name,
            'account_id':    cfg.get('account_id', ''),
            'separator':     cfg.get('separator', '------'),
            'last_sha256':   cfg.get('last_sha256', ''),
            'last_deployed': cfg.get('last_deployed', ''),
            'worker_url':    f'https://{name}.workers.dev' if name else '',
            'configured':    bool(cfg.get('api_token') and cfg.get('account_id') and name),
        }

    def _cf_stats(self) -> dict:
        if not _CF_AVAILABLE:
            return {'available': False}
        return _cfworker.CFRequestCounter().stats()

    def _cf_save_config(self, params: dict) -> dict:
        if not _CF_AVAILABLE:
            return {'ok': False, 'error': 'Module cfworker non disponible'}
        allowed = ('api_token', 'account_id', 'worker_name', 'separator')
        data    = {k: v for k, v in params.items() if k in allowed and v}
        if not data:
            return {'ok': False, 'error': 'Aucun paramètre valide fourni'}
        saved = _cfworker.CFWorkerConfig().save(data)
        # Ne jamais renvoyer l'API Token dans la réponse
        safe  = {k: v for k, v in saved.items() if k != 'api_token'}
        return {'ok': True, **safe}

    def _cf_deploy(self) -> dict:
        if not _CF_AVAILABLE:
            return {'ok': False, 'error': 'Module cfworker non disponible'}
        cfg   = _cfworker.CFWorkerConfig().load()
        token = cfg.get('api_token', '')
        acct  = cfg.get('account_id', '')
        name  = cfg.get('worker_name', '')
        sep   = cfg.get('separator', '------')
        if not all([token, acct, name]):
            return {
                'ok':    False,
                'error': 'Configuration incomplète (API Token, Account ID, Worker Name requis)',
            }
        try:
            result = _cfworker.CFWorkerDeployer().deploy(token, acct, name, sep)
            if result.get('ok'):
                _cfworker.CFWorkerConfig().save({
                    'last_sha256':   result['sha256'],
                    'last_deployed': result['deployed_at'],
                })
            return result
        except Exception as e:
            logger.warning(f'CF Worker deploy échoué — {e}')
            return {'ok': False, 'error': str(e)}

    def _html(self, content):
        encoded = content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, data, code=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_api_server(manager):
    APIHandler.manager = manager
    server = ThreadingHTTPServer(('0.0.0.0', API_PORT), APIHandler)
    logger.info(f'Web UI & API sur http://0.0.0.0:{API_PORT}')
    threading.Thread(target=server.serve_forever, daemon=True).start()

# ─── Main ─────────────────────────────────────────────────────────────────────

def health_check_loop(manager):
    while True:
        time.sleep(30)
        manager.health_check()


def run_check():
    """Mode --check : vérifie que les binaires requis sont installés dans l'image.
    Utilisé par le smoke test du CI avant de publier l'image Docker.
    Exit 0 si tout est OK, 1 sinon.
    """
    import shutil
    ok = True
    checks = [
        ('tor',     ['tor',     '--version']),
        ('privoxy', ['privoxy', '--version']),
        ('haproxy', ['haproxy', '-v']),
    ]
    for name, cmd in checks:
        if not shutil.which(name):
            print(f'FAIL  {name} : introuvable dans PATH', file=sys.stderr)
            ok = False
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            version = (r.stdout or r.stderr).splitlines()[0]
            print(f'OK    {name} : {version}')
        except Exception as e:
            print(f'WARN  {name} : présent mais version illisible ({e})')
    print(f'OK    python3 : {sys.version.split()[0]}')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    if '--check' in sys.argv:
        run_check()

    manager = ProxyManager()
    manager.start()
    ProxyAuthServer().start()
    start_api_server(manager)

    threading.Thread(target=manager.run_rotation_loop, daemon=True).start()
    threading.Thread(target=health_check_loop, args=(manager,), daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info('Arrêt...')
