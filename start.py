#!/usr/bin/env python3
"""
ProxySpin — Orchestrateur
Gère Tor, Privoxy et HAProxy pour un proxy HTTP rotatif anonymisant.
Supporte deux modes : Tor (circuits chiffrés) et Free Proxy (proxifly).
"""

import base64
import json
import logging
import os
import queue
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen

# ─── Ports ────────────────────────────────────────────────────────────────────

PROXY_PORT = 1973   # HAProxy — point d'entrée du proxy rotatif
API_PORT   = 1974   # Web UI + API JSON
STATS_PORT = 1976   # HAProxy stats

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
    'auto_rotation':     os.environ.get('AUTO_ROTATION',     'true').lower() == 'true',
    'tor_instances':     int(os.environ.get('tors',          '10')),
    'max_free_proxies':  int(os.environ.get('MAX_PROXIES',   '20')),
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
        with open(self._config_path, 'w') as f:
            f.write(
                f'listen-address  127.0.0.1:{self.port}\n'
                f'{self._forward_directive()}\n'
                f'logdir /var/log/privoxy\n'
                f'debug 0\n'
                f'keep-alive-timeout 5\n'
                f'default-server-timeout 60\n'
                f'socket-timeout 60\n'
            )

# ─── TorProxy (Tor + Privoxy) ─────────────────────────────────────────────────

class TorProxy:
    def __init__(self, proxy_id):
        self.id      = proxy_id
        self.tor     = Tor(self._tor_port, self._control_port)
        self.privoxy = Privoxy(self.port, '127.0.0.1', self._tor_port, 'socks5t')

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
        logger.info(f'starting haproxy on port {PROXY_PORT}')
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
        for var in ('PROXY_USER', 'PROXY_PASS', 'STATS_USER', 'STATS_PASS'):
            if not os.environ.get(var, '').strip():
                raise RuntimeError(f"Variable d'environnement manquante : {var}")

    def _write_config(self):
        pu = os.environ['PROXY_USER']
        pp = os.environ['PROXY_PASS']
        su = os.environ['STATS_USER']
        sp = os.environ['STATS_PASS']
        servers = '\n'.join(
            f"  server p{b['port']} {b['addr']}:{b['port']}"
            for b in self.backends
        )
        cfg = f"""global
  maxconn 1024
  daemon
  pidfile {self.PID_FILE}

defaults
  mode http
  maxconn 1024
  option  httplog
  option  dontlognull
  retries 3
  timeout connect 5s
  timeout client 60s
  timeout server 60s


userlist proxy_users
  user {pu} insecure-password {pp}


listen stats *:{STATS_PORT}
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
  stats uri /haproxy?stats
  stats auth {su}:{sp}


frontend rotating_proxies
  bind *:{PROXY_PORT}
  option http_proxy

  # Les navigateurs envoient les credentials proxy dans Proxy-Authorization (pas Authorization)
  # On le recopie dans Authorization pour que http_auth puisse le lire
  http-request set-header Authorization %[req.hdr(Proxy-Authorization)] if {{ req.hdr(Proxy-Authorization) -m found }}
  acl auth_ok http_auth(proxy_users)
  http-request deny deny_status 407 hdr Proxy-Authenticate 'Basic realm="ProxySpin"' if !auth_ok

  # Nettoie les headers auth avant de forwarder au backend
  http-request del-header Proxy-Authorization
  http-request del-header Authorization

  default_backend tor

backend tor
  option http_proxy
  balance leastconn

{servers}
"""
        with open(self.CONFIG_PATH, 'w') as f:
            f.write(cfg)

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
    FILE_PATH = os.environ.get('SOURCES_FILE', '/data/sources.json')
    DEFAULTS = [
        {
            'url':     'https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.json',
            'label':   'proxifly http',
            'enabled': True,
        },
        {
            'url':     'https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/https/data.json',
            'label':   'proxifly https',
            'enabled': True,
        },
        {
            'url':     'https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.json',
            'label':   'proxifly socks4',
            'enabled': True,
        },
        {
            'url':     'https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.json',
            'label':   'proxifly socks5',
            'enabled': True,
        },
    ]
    _lock = threading.RLock()

    def load(self):
        with self._lock:
            if not os.path.exists(self.FILE_PATH):
                return [dict(s) for s in self.DEFAULTS]
            try:
                with open(self.FILE_PATH) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f'SourceManager: lecture échouée ({e}) — sources par défaut utilisées')
                return [dict(s) for s in self.DEFAULTS]

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

# ─── FreeProxyFetcher ─────────────────────────────────────────────────────────

class FreeProxyFetcher:
    ALLOWED_ANONYMITY = {'elite', 'anonymous'}
    TEST_URL     = 'http://icanhazip.com'
    TEST_TIMEOUT = 8
    MAX_THREADS  = 15

    def fetch_working(self, max_count):
        logger.info('FreeProxy: récupération des listes…')
        candidates = self._fetch_all()
        logger.info(f'FreeProxy: {len(candidates)} candidats — test ({self.MAX_THREADS} threads)…')

        working  = []
        lock     = threading.Lock()
        work_q   = queue.Queue()
        for p in candidates:
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
                            logger.info(
                                f"FreeProxy ✓ {proxy['ip']}:{proxy['port']}"
                                f" ({proxy['protocol']}, score:{proxy['score']})"
                            )

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.MAX_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        geolocate_missing(working)
        logger.info(f'FreeProxy: {len(working)}/{max_count} proxies retenus')
        return working

    def _fetch_all(self):
        proxies = []
        sources = [s for s in SourceManager().load() if s.get('enabled', True)]
        if not sources:
            logger.warning('FreeProxy: aucune source activée')
            return proxies
        for source in sources:
            url   = source['url']
            label = source.get('label') or url
            try:
                with urlopen(url, timeout=15) as r:
                    content = r.read().decode('utf-8', errors='ignore')
                parsed = self._parse_source(content, url)
                logger.info(f'FreeProxy: {len(parsed)} entrées depuis {label}')
                proxies.extend(parsed)
            except Exception as e:
                logger.warning(f'FreeProxy: fetch échoué ({label}) — {e}')
        proxies.sort(key=lambda p: (-p['score'], random.random()))
        return proxies

    def _parse_source(self, content, url=''):
        """Détecte le format (JSON proxifly / texte brut ip:port) et parse les proxies."""
        try:
            data = json.loads(content)
            if isinstance(data, list):
                return self._parse_json_list(data, url)
        except (json.JSONDecodeError, ValueError):
            pass
        return self._parse_plaintext(content)

    def _parse_json_list(self, data, url=''):
        results = []
        protocol_hint = self._protocol_from_url(url)
        for item in data:
            if not isinstance(item, dict) or 'ip' not in item:
                continue
            anon = item.get('anonymity')
            if anon is not None and anon not in self.ALLOWED_ANONYMITY:
                continue
            protocol = item.get('protocol') or protocol_hint
            try:
                geo          = item.get('geolocation') or {}
                country_code = geo.get('countryCode') or geo.get('country_code') or None
                country_name = geo.get('country') or country_code
                results.append({
                    'ip':           item['ip'],
                    'port':         int(item['port']),
                    'protocol':     protocol,
                    'score':        float(item.get('score', 0)),
                    'country_code': country_code,
                    'country_name': country_name,
                })
            except (ValueError, KeyError):
                continue
        return results

    def _parse_plaintext(self, content):
        results = []
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            parsed = self._parse_line(line)
            if parsed:
                results.append(parsed)
        return results

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
        ip, port_str = parts[0].strip(), parts[1].strip()
        try:
            port = int(port_str)
        except ValueError:
            return None
        if protocol not in ('http', 'https', 'socks4', 'socks5'):
            protocol = 'http'
        return {'ip': ip, 'port': port, 'protocol': protocol, 'score': 0, 'country_code': None, 'country_name': None}

    def _protocol_from_url(self, url):
        url_lower = url.lower()
        for p in ('socks5', 'socks4', 'https'):
            if p in url_lower:
                return p
        return 'http'

    def _test(self, proxy):
        if 'socks' in proxy['protocol']:
            try:
                with socket.create_connection((proxy['ip'], proxy['port']), timeout=self.TEST_TIMEOUT):
                    return True
            except Exception:
                return False
        try:
            opener = build_opener(ProxyHandler({'http': f"http://{proxy['ip']}:{proxy['port']}"}))
            with opener.open(self.TEST_URL, timeout=self.TEST_TIMEOUT) as r:
                return r.status == 200
        except Exception:
            return False

# ─── LocalProxyLoader ────────────────────────────────────────────────────────

class LocalProxyLoader:
    DATA_DIR     = '/data'
    TEST_URL     = 'http://icanhazip.com'
    TEST_TIMEOUT = 8
    MAX_THREADS  = 15

    def load_working(self, max_count):
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

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.MAX_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        geolocate_missing(working)
        logger.info(f'LocalProxy: {len(working)}/{len(raw)} proxies opérationnels')
        return working

    def _read_all_files(self):
        if not os.path.isdir(self.DATA_DIR):
            logger.warning(f'LocalProxy: dossier introuvable : {self.DATA_DIR}')
            return []
        txt_files = sorted(
            f for f in os.listdir(self.DATA_DIR)
            if f.lower().endswith('.txt')
        )
        if not txt_files:
            logger.warning(f'LocalProxy: aucun fichier .txt dans {self.DATA_DIR}')
            return []
        proxies = []
        seen = set()
        for filename in txt_files:
            filepath = os.path.join(self.DATA_DIR, filename)
            file_proxies = self._read_file(filepath)
            before = len(proxies)
            for p in file_proxies:
                key = (p['ip'], p['port'], p['protocol'])
                if key not in seen:
                    seen.add(key)
                    proxies.append(p)
            added = len(proxies) - before
            logger.info(f'LocalProxy: {added} proxies chargés depuis {filename} ({len(file_proxies) - added} doublons ignorés)')
        logger.info(f'LocalProxy: {len(proxies)} proxies uniques au total ({len(txt_files)} fichier(s))')
        return proxies

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
        if protocol not in ('http', 'https', 'socks4', 'socks5'):
            return None
        return {'ip': ip.strip(), 'port': port, 'protocol': protocol, 'score': 0, 'country_code': None, 'country_name': None}

    def _test(self, proxy):
        if 'socks' in proxy['protocol']:
            try:
                with socket.create_connection((proxy['ip'], proxy['port']), timeout=self.TEST_TIMEOUT):
                    return True
            except Exception:
                return False
        try:
            opener = build_opener(ProxyHandler({'http': f"http://{proxy['ip']}:{proxy['port']}"}))
            with opener.open(self.TEST_URL, timeout=self.TEST_TIMEOUT) as r:
                return r.status == 200
        except Exception:
            return False

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
                return {'ok': True, 'mode': 'tor', 'message': f"rotate envoyé ({len(self._proxies)} circuits)"}
            elif self.mode == 'proxy':
                n = self._rotate_free_proxies()
                self._last_rotation = time.time()
                return {'ok': True, 'mode': 'proxy', 'message': f"{n} proxies actualisés"}
            else:
                n = self._reload_local_proxies()
                self._last_rotation = time.time()
                return {'ok': True, 'mode': 'local', 'message': f"{n} proxies locaux rechargés"}

    def switch_mode(self, new_mode):
        if new_mode not in ('tor', 'proxy', 'local'):
            return {'ok': False, 'error': 'Mode invalide (tor | proxy)'}
        with self._lock:
            if new_mode == self.mode:
                return {'ok': False, 'message': f"Déjà en mode '{new_mode}'"}
            logger.info(f'Changement de mode: {self.mode} → {new_mode}')
            self._stop_backends()
            self.haproxy.backends.clear()
            with config_lock:
                config['mode'] = new_mode
            self._launch_backends()
            self.haproxy.soft_reload()
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
            return [{'type': 'tor', 'addr': f'127.0.0.1:{p.port}', 'id': p.id} for p in self._proxies]
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
            if self._all_working and self.mode in ('proxy', 'local'):
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
        for i, up in enumerate(filtered[:config['max_free_proxies']]):
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
        elif self.mode == 'proxy':
            self._rotate_free_proxies()
        else:
            self._reload_local_proxies()

    def _start_tor_backends(self):
        for i in range(config['tor_instances']):
            proxy = TorProxy(i)
            self.haproxy.add_backend(proxy)
            proxy.start()
            self._proxies.append(proxy)

    def _stop_backends(self):
        for p in self._proxies:
            try:
                p.stop()
            except Exception:
                pass
        self._proxies.clear()

    def _reload_local_proxies(self):
        country = config.get('country_filter')
        self._loading     = True
        self._loading_msg = f"Chargement et test des proxies locaux (dossier {LocalProxyLoader.DATA_DIR})…"
        try:
            # Charge plus de candidats si un filtre pays est actif
            max_fetch = config['max_free_proxies'] * (5 if country else 1)
            upstream_list     = LocalProxyLoader().load_working(max_fetch)
            self._all_working = upstream_list
            self._stop_backends()
            self.haproxy.backends.clear()
            filtered = self._filter_by_country(upstream_list, country)
            for i, up in enumerate(filtered[:config['max_free_proxies']]):
                fp = FreeProxy(i, up)
                fp.start()
                self.haproxy.add_backend(fp)
                self._proxies.append(fp)
            self.haproxy.soft_reload()
            return len(self._proxies)
        finally:
            self._loading     = False
            self._loading_msg = None

    def _rotate_free_proxies(self):
        country = config.get('country_filter')
        self._loading     = True
        self._loading_msg = f"Récupération et test des proxies (max {config['max_free_proxies']})…"
        try:
            # Charge plus de candidats si un filtre pays est actif
            max_fetch = config['max_free_proxies'] * (5 if country else 1)
            upstream_list     = FreeProxyFetcher().fetch_working(max_fetch)
            self._all_working = upstream_list
            self._stop_backends()
            self.haproxy.backends.clear()
            filtered = self._filter_by_country(upstream_list, country)
            for i, up in enumerate(filtered[:config['max_free_proxies']]):
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
<html lang="fr">
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
    .badge-ok{background:#1a3020;color:#4caf50}
    .badge-off{background:#3e1a1a;color:#f44336}
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
    .src-url{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#555;font-size:11px}
    .btn-del{background:#3e1a1a;color:#f44336;border:1px solid #f44336;border-radius:5px;padding:2px 8px;font-size:11px;cursor:pointer;font-family:monospace}
    .btn-del:hover{background:#5e2a2a}
  </style>
</head>
<body>
  <h1>&#x2B21; PROXYSPIN</h1>
  <p class="subtitle">Panel de contr&#xF4;le</p>

  <div class="grid">
    <div class="card">
      <h2>STATUT</h2>
      <div class="row"><span class="label">Mode</span><span id="mode-badge" class="badge">&#x2026;</span></div>
      <div class="row"><span class="label">Instances</span><span class="value" id="instances">&#x2026;</span></div>
      <div class="row"><span class="label">Rotation auto</span><span class="badge" id="auto-badge">&#x2026;</span></div>
      <div class="row"><span class="label">Intervalle</span><span class="value" id="interval-val">&#x2026;</span></div>
      <div class="row" id="country-row" style="display:none">
        <span class="label">Filtre pays</span>
        <select id="sel-country" style="width:auto;padding:3px 8px;font-size:12px">
          <option value="">&#x1F30D; Tous</option>
        </select>
      </div>
      <p id="last-rot"></p>
      <button class="btn-primary" id="btn-rotate">&#x1F504; Nouvelle IP</button>
      <div id="msg"></div>
    </div>

    <div class="card">
      <h2>CONFIGURATION</h2>
      <div class="field">
        <label>Mode</label>
        <select id="sel-mode">
          <option value="tor">&#x1F9C5; Tor</option>
          <option value="proxy">&#x1F310; Proxies gratuits (proxifly)</option>
          <option value="local">&#x1F4C2; Proxies locaux (fichier)</option>
        </select>
      </div>
      <div class="field">
        <label>Rotation automatique</label>
        <label class="toggle"><input type="checkbox" id="chk-auto"> Activ&#xE9;e</label>
      </div>
      <div class="field">
        <label>Intervalle de rotation (secondes)</label>
        <input type="number" id="inp-interval" min="10" max="3600" value="60">
      </div>
      <button class="btn-save" id="btn-save">Enregistrer</button>
      <div id="msg-cfg"></div>
    </div>
  </div>

  <div class="card">
    <h2>BACKENDS ACTIFS</h2>
    <table>
      <thead><tr><th>#</th><th>Type</th><th>Adresse</th><th>Pays</th></tr></thead>
      <tbody id="backends-body"><tr><td colspan="4" style="color:#555">Chargement&#x2026;</td></tr></tbody>
    </table>
  </div>

  <div class="card" style="margin-top:16px">
    <h2>SOURCES DE PROXIES <span style="color:#555;font-size:11px;font-weight:normal">(mode proxy)</span></h2>
    <table>
      <thead><tr><th style="width:32px">On</th><th>Label</th><th>URL</th><th style="width:32px"></th></tr></thead>
      <tbody id="sources-body"><tr><td colspan="4" style="color:#555">Chargement&#x2026;</td></tr></tbody>
    </table>
    <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:flex-end">
      <div style="flex:3;min-width:200px">
        <div class="field" style="margin-bottom:0"><label>URL de la source</label>
        <input type="text" id="inp-src-url" placeholder="https://&#x2026;"></div>
      </div>
      <div style="flex:1;min-width:120px">
        <div class="field" style="margin-bottom:0"><label>Label (optionnel)</label>
        <input type="text" id="inp-src-label" placeholder="Mon repo"></div>
      </div>
      <button class="btn-save" id="btn-add-src" style="white-space:nowrap;height:34px">+ Ajouter</button>
    </div>
    <div id="msg-sources" style="font-size:12px;color:#4caf50;min-height:16px;margin-top:6px"></div>
  </div>

  <script>
    const $ = id => document.getElementById(id);

    function countryFlag(code) {
      if (!code || code.length !== 2) return '🌍';
      var c = code.toUpperCase();
      return String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65) +
             String.fromCodePoint(0x1F1E6 + c.charCodeAt(1) - 65);
    }

    var _countriesCache = [];
    async function loadCountriesWeb(currentFilter) {
      var r = await api('GET', '/api/countries').catch(() => ({ countries: [] }));
      _countriesCache = r.countries || [];
      var sel = $('sel-country');
      var prev = sel.value;
      sel.innerHTML = '<option value="">🌍 Tous</option>' +
        _countriesCache.map(function(c) {
          return '<option value="' + c.code + '">' + countryFlag(c.code) + ' ' + c.name + ' (' + c.count + ')</option>';
        }).join('');
      sel.value = currentFilter || prev || '';
    }

    $('sel-country').addEventListener('change', async function() {
      await api('POST', '/api/country', { country: this.value });
      refresh();
    });

    async function api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const r = await fetch(path, opts);
      return r.json();
    }

    async function refresh() {
      const s = await api('GET', '/api/status').catch(() => null);
      if (!s) return;

      const modeBadge = $('mode-badge');
      modeBadge.textContent = s.mode === 'tor' ? '🧅 Tor' : '🌐 Free Proxy';
      modeBadge.className = 'badge ' + (s.mode === 'tor' ? 'badge-tor' : 'badge-proxy');

      $('instances').textContent = s.instances;
      $('auto-badge').textContent = s.auto_rotation ? 'ON' : 'OFF';
      $('auto-badge').className = 'badge ' + (s.auto_rotation ? 'badge-ok' : 'badge-off');
      $('interval-val').textContent = s.rotation_interval + 's';
      $('last-rot').textContent = 'Dernière rotation : ' + new Date(s.last_rotation).toLocaleTimeString();

      $('sel-mode').value     = s.mode;
      $('chk-auto').checked   = s.auto_rotation;
      $('inp-interval').value = s.rotation_interval;

      const bk = await api('GET', '/api/backends').catch(() => ({ backends: [] }));
      const tbody = $('backends-body');
      if (!bk.backends || bk.backends.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:#555">Aucun backend actif</td></tr>';
      } else {
        tbody.innerHTML = bk.backends.map(b => {
          var flag = b.country_code ? countryFlag(b.country_code) : '—';
          var cc   = b.country_code || '';
          return '<tr><td>' + b.id + '</td><td class="type-' + b.type + '">' + b.type + '</td><td>' + b.addr + '</td><td title="' + (b.country_name || '') + '">' + flag + ' ' + cc + '</td></tr>';
        }).join('');
      }

      // Filtre pays (proxy/local uniquement)
      if (s.mode !== 'tor') {
        $('country-row').style.display = 'flex';
        loadCountriesWeb(s.country_filter);
      } else {
        $('country-row').style.display = 'none';
      }
    }

    $('btn-rotate').addEventListener('click', async () => {
      $('btn-rotate').disabled = true;
      $('msg').textContent = '⏳ En cours…';
      const r = await api('POST', '/api/rotate').catch(() => ({ ok: false }));
      $('msg').textContent = r.ok ? '✓ ' + r.message : '✗ Erreur';
      setTimeout(() => { $('msg').textContent = ''; $('btn-rotate').disabled = false; }, 5000);
      refresh();
    });

    $('btn-save').addEventListener('click', async () => {
      const mode     = $('sel-mode').value;
      const auto     = $('chk-auto').checked;
      const interval = parseInt($('inp-interval').value, 10);
      $('msg-cfg').textContent = '⏳ Application…';
      const s = await api('GET', '/api/status');
      if (s.mode !== mode) {
        $('msg-cfg').textContent = '⏳ Changement de mode (peut prendre 1-2 min)…';
        await api('POST', '/api/mode', { mode });
      }
      await api('POST', '/api/config', { auto_rotation: auto, rotation_interval: interval });
      $('msg-cfg').textContent = '✓ Configuration sauvegardée';
      setTimeout(() => { $('msg-cfg').textContent = ''; }, 4000);
      refresh();
    });

    refresh();
    setInterval(refresh, 8000);

    // ── Sources ──────────────────────────────────────────────────────────────
    async function loadSources() {
      const r = await api('GET', '/api/sources').catch(() => ({ sources: [] }));
      renderSources(r.sources || []);
    }

    function renderSources(sources) {
      var tbody = $('sources-body');
      if (!sources.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:#555">Aucune source</td></tr>';
        return;
      }
      tbody.innerHTML = sources.map(function(s) {
        var u = s.url.replace(/"/g, '&quot;').replace(/</g, '&lt;');
        var lbl = (s.label || s.url).replace(/</g, '&lt;');
        var short = s.url.length > 60 ? s.url.slice(0, 57) + '…' : s.url;
        return '<tr>' +
          '<td><input type="checkbox" class="src-toggle" data-url="' + u + '"' + (s.enabled ? ' checked' : '') + '></td>' +
          '<td>' + lbl + '</td>' +
          '<td class="src-url" title="' + u + '">' + short + '</td>' +
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
      else { $('msg-sources').textContent = '✗ ' + (r.error || 'Erreur'); }
    }

    $('btn-add-src').addEventListener('click', async function() {
      var url   = $('inp-src-url').value.trim();
      var label = $('inp-src-label').value.trim();
      if (!url) return;
      $('msg-sources').textContent = '⏳ Ajout…';
      var r = await api('POST', '/api/sources', { url: url, label: label }).catch(() => ({ ok: false }));
      if (r.ok) {
        $('inp-src-url').value = '';
        $('inp-src-label').value = '';
        $('msg-sources').textContent = '✓ Source ajoutée';
        loadSources();
      } else {
        $('msg-sources').textContent = '✗ ' + (r.error || 'Erreur');
      }
      setTimeout(function() { $('msg-sources').textContent = ''; }, 3000);
    });

    loadSources();
  </script>
</body>
</html>"""

# ─── API + Web UI (port 1974) ─────────────────────────────────────────────────

class APIHandler(BaseHTTPRequestHandler):
    manager = None  # Défini depuis main()

    def log_message(self, fmt, *args):
        logger.debug(f'API {self.address_string()} — {fmt % args}')

    def _check_auth(self):
        """Vérifie le Basic auth avec les identifiants STATS_USER / STATS_PASS."""
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
            self._json({'sources': SourceManager().load()})
        elif path == '/api/countries':
            self._json({'countries': self.manager.available_countries()})
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
            self._json(SourceManager().toggle(params.get('url', ''), params.get('enabled', True)))
        elif path == '/api/country':
            self._json(self.manager.set_country(params.get('country', '')))
        else:
            self._json({'error': 'not found'}, 404)

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
    start_api_server(manager)

    threading.Thread(target=manager.run_rotation_loop, daemon=True).start()
    threading.Thread(target=health_check_loop, args=(manager,), daemon=True).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info('Arrêt...')
