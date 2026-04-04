/**
 * ProxySpin — Cloudflare Worker
 * ==============================
 * Proxy HTTP + HTTPS complet déployé sur Cloudflare Workers.
 *
 * HTTP  → fetch() direct vers la cible (URL rewriting)
 * HTTPS → WebSocket + cloudflare:sockets (tunnel TCP transparent)
 *
 * Endpoints :
 *   GET  /------<url>   → proxy HTTP
 *   WS   /wstunnel      → tunnel TCP pour HTTPS/CONNECT
 */

import { connect } from 'cloudflare:sockets';

// ─── Config ───────────────────────────────────────────────────────────────────

const SEP     = '------';    // séparateur URL proxy HTTP (configurable via cfworker.py)
const WS_PATH = '/wstunnel'; // endpoint WebSocket tunnel HTTPS

// Headers HTTP à ne pas transmettre
const HOP_HEADERS = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade',
  'content-security-policy', 'content-security-policy-report-only',
  'x-frame-options',
]);

// ─── Entry point ──────────────────────────────────────────────────────────────

export default {
  async fetch(request, env, ctx) {
    const url     = new URL(request.url);
    const upgrade = (request.headers.get('Upgrade') || '').toLowerCase();

    // Tunnel WebSocket → TCP (HTTPS/CONNECT)
    if (upgrade === 'websocket' && url.pathname === WS_PATH) {
      return handleTunnel(request);
    }

    // Proxy HTTP via URL rewriting : /------http://target.com/path
    const sepIdx = url.pathname.indexOf(SEP);
    if (sepIdx !== -1) {
      const target = url.pathname.slice(sepIdx + SEP.length) + (url.search || '');
      if (target.startsWith('http')) {
        return handleHttp(request, target);
      }
    }

    return new Response('ProxySpin Worker — OK\n', {
      status:  200,
      headers: { 'Content-Type': 'text/plain' },
    });
  },
};

// ─── Proxy HTTP ───────────────────────────────────────────────────────────────

async function handleHttp(request, target) {
  const headers = new Headers();
  for (const [k, v] of request.headers.entries()) {
    if (!HOP_HEADERS.has(k.toLowerCase())) headers.set(k, v);
  }
  if (!headers.has('user-agent')) {
    headers.set(
      'user-agent',
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    );
  }

  const init = { method: request.method, headers, redirect: 'follow' };
  if (!['GET', 'HEAD'].includes(request.method)) {
    init.body = request.body;
  }

  try {
    const resp    = await fetch(target, init);
    const rspHdrs = new Headers();
    for (const [k, v] of resp.headers.entries()) {
      if (!HOP_HEADERS.has(k.toLowerCase())) rspHdrs.set(k, v);
    }
    return new Response(resp.body, { status: resp.status, headers: rspHdrs });
  } catch (e) {
    return new Response(`CF Worker proxy error: ${e.message}`, { status: 502 });
  }
}

// ─── Tunnel WebSocket → TCP ───────────────────────────────────────────────────

function handleTunnel(request) {
  const pair   = new WebSocketPair();
  const client = pair[0];
  const server = pair[1];
  server.accept();

  runTunnel(server).catch((err) => {
    try { server.close(1011, String(err).slice(0, 120)); } catch {}
  });

  return new Response(null, { status: 101, webSocket: client });
}

async function runTunnel(ws) {
  // File d'attente thread-safe pour les messages WebSocket entrants.
  // Créée avant toute opération async pour ne perdre aucun message.
  const queue = new MsgQueue(ws);

  // Premier message : JSON { "host": "...", "port": 443 }
  const firstData = await queue.next();
  if (!firstData) return;

  let info;
  try {
    info = JSON.parse(new TextDecoder().decode(firstData));
  } catch {
    ws.close(1003, 'invalid handshake JSON');
    return;
  }

  const { host, port } = info;
  if (!host || !port) {
    ws.close(1003, 'host/port manquants');
    return;
  }

  // Ouvrir la connexion TCP vers la cible via cloudflare:sockets
  const tcp = connect({ hostname: host, port: Number(port) });

  // Confirmer au client Python que le tunnel est établi
  ws.send(JSON.stringify({ ok: true, host, port }));

  const writer = tcp.writable.getWriter();

  // ── WebSocket → TCP ────────────────────────────────────────────────────────
  const wsPipe = (async () => {
    try {
      while (true) {
        const data = await queue.next();
        if (data === null) break;          // WebSocket fermé
        await writer.write(data);
      }
    } finally {
      await writer.close().catch(() => {});
    }
  })();

  // ── TCP → WebSocket ────────────────────────────────────────────────────────
  const tcpPipe = (async () => {
    const reader = tcp.readable.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (ws.readyState === 1 /* OPEN */) ws.send(value);
      }
    } finally {
      try { ws.close(1000, 'TCP closed'); } catch {}
    }
  })();

  // Attendre que l'un des deux tuyaux se ferme
  await Promise.race([wsPipe, tcpPipe]);
}

// ─── MsgQueue : file d'attente pour les messages WebSocket ───────────────────
// Garantit qu'aucun message n'est perdu entre deux appels async.

class MsgQueue {
  constructor(ws) {
    this._buf    = [];   // messages en attente d'être consommés
    this._waited = [];   // résolveurs en attente d'un message
    this._closed = false;

    ws.addEventListener('message', (evt) => {
      const raw = evt.data;
      const bytes = raw instanceof ArrayBuffer
        ? new Uint8Array(raw)
        : typeof raw === 'string'
          ? new TextEncoder().encode(raw)
          : new Uint8Array(raw);

      if (this._waited.length > 0) {
        this._waited.shift()(bytes);
      } else {
        this._buf.push(bytes);
      }
    });

    ws.addEventListener('close', () => {
      this._closed = true;
      while (this._waited.length > 0) this._waited.shift()(null);
    });
  }

  next() {
    if (this._buf.length > 0)    return Promise.resolve(this._buf.shift());
    if (this._closed)            return Promise.resolve(null);
    return new Promise((resolve) => this._waited.push(resolve));
  }
}
