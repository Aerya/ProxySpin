// ==UserScript==
// @name         ProxySpin — Contrôleur
// @namespace    proxyspin-controller
// @version      3.0.0
// @description  Affiche l'état, le mode (Tor/Proxy), l'IP de sortie et le drapeau du pays en permanence.
// @match        *://*/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_addStyle
// @connect      *
// ==/UserScript==

(function () {
  'use strict';

  // ─── Config ───────────────────────────────────────────────────────────────
  let CFG = {
    host: GM_getValue('rp_host', '192.168.0.150'),
    port: GM_getValue('rp_port', '1974'),
    user: GM_getValue('rp_user', ''),
    pass: GM_getValue('rp_pass', ''),
  };
  const apiUrl = path => `http://${CFG.host}:${CFG.port}${path}`;

  // ─── Convertit un code pays ISO en emoji drapeau ──────────────────────────
  function countryFlag(code) {
    if (!code || code.length !== 2) return '🌐';
    return [...code.toUpperCase()].map(c =>
      String.fromCodePoint(0x1F1E6 + c.charCodeAt(0) - 65)
    ).join('');
  }

  // ─── Styles ───────────────────────────────────────────────────────────────
  GM_addStyle(`
    #rp-panel {
      position: fixed; bottom: 20px; right: 20px; z-index: 2147483647;
      background: #1a1a2e; color: #e0e0e0; border-radius: 12px;
      padding: 12px 16px; font-family: monospace; font-size: 13px;
      box-shadow: 0 4px 24px rgba(0,0,0,.7); min-width: 240px;
      border: 1px solid #2d2d5e; cursor: move; user-select: none;
    }
    #rp-panel.rp-collapsed { min-width: unset; padding: 8px 12px; }
    #rp-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; gap:8px; }
    #rp-panel.rp-collapsed #rp-header { margin-bottom:0; }
    #rp-title { font-weight:bold; color:#7c83fd; font-size:12px; letter-spacing:.5px; }
    #rp-body  { display:flex; flex-direction:column; gap:8px; }
    #rp-panel.rp-collapsed #rp-body { display:none; }

    /* Bande d'état en haut du panel */
    #rp-state-bar {
      display: flex; align-items: center; gap: 8px;
      padding: 7px 10px; border-radius: 8px; font-size: 12px;
      font-weight: bold; margin-bottom: 2px;
      transition: background .4s, color .4s;
    }
    #rp-state-bar.state-loading  { background:#2a2a10; color:#ffd54f; border:1px solid #ffd54f44; }
    #rp-state-bar.state-tor      { background:#14143a; color:#7c83fd; border:1px solid #7c83fd44; }
    #rp-state-bar.state-proxy    { background:#0e2e18; color:#4caf50; border:1px solid #4caf5044; }
    #rp-state-bar.state-offline  { background:#2e0e0e; color:#f44336; border:1px solid #f4433644; }
    #rp-state-icon { font-size: 16px; }
    #rp-state-text { flex: 1; line-height: 1.3; }
    #rp-state-sub  { font-weight:normal; font-size:10px; opacity:.7; display:block; }

    /* IP + drapeau */
    #rp-ip-row {
      display: flex; align-items: center; gap: 8px;
      background: #0d0d1a; border-radius: 7px; padding: 6px 10px;
    }
    #rp-flag   { font-size: 20px; line-height: 1; }
    #rp-ip-info { flex: 1; }
    #rp-ip     { font-size: 13px; color: #e0e0e0; font-weight: bold; }
    #rp-country{ font-size: 10px; color: #666; display: block; }

    /* Infos */
    .rp-row    { display:flex; justify-content:space-between; align-items:center; font-size:11px; }
    .rp-label  { color:#555; }
    .rp-val    { color:#aaa; }

    /* Bouton */
    #rp-btn-rotate {
      background:#7c83fd; color:#fff; border:none; border-radius:7px;
      padding:7px 10px; font-size:12px; cursor:pointer; width:100%;
      font-family:monospace; transition:.2s; margin-top:2px;
    }
    #rp-btn-rotate:hover:not(:disabled) { background:#5c63e0; }
    #rp-btn-rotate:disabled { opacity:.5; cursor:not-allowed; }
    #rp-cooldown { font-size:10px; color:#555; text-align:center; min-height:14px; }

    /* Contrôles header */
    .rp-ctrl { background:none; border:none; color:#555; cursor:pointer; font-size:13px; padding:0 2px; }
    .rp-ctrl:hover { color:#aaa; }

    /* Settings */
    #rp-settings { display:none; flex-direction:column; gap:6px; margin-top:6px; border-top:1px solid #2d2d5e; padding-top:8px; }
    #rp-settings.open { display:flex; }
    #rp-settings label { font-size:11px; color:#666; }
    #rp-settings input { background:#0d0d1a; border:1px solid #2d2d5e; border-radius:5px; color:#e0e0e0; font-family:monospace; font-size:12px; padding:3px 7px; width:100%; box-sizing:border-box; }
    #rp-btn-save { background:#2d5e3e; color:#4caf50; border:1px solid #4caf50; border-radius:5px; padding:4px 8px; font-size:11px; cursor:pointer; font-family:monospace; }
    #rp-btn-save:hover { background:#3d7e4e; }

    @keyframes rp-spin { to { transform: rotate(360deg); } }
    .rp-spin { display:inline-block; animation: rp-spin 1s linear infinite; }
  `);

  // ─── DOM ──────────────────────────────────────────────────────────────────
  const panel = document.createElement('div');
  panel.id = 'rp-panel';
  panel.innerHTML = `
    <div id="rp-header">
      <span id="rp-title">⬡ PROXYSPIN</span>
      <div style="display:flex;gap:3px">
        <button class="rp-ctrl" id="rp-btn-cfg" title="Paramètres">⚙</button>
        <button class="rp-ctrl" id="rp-btn-col" title="Réduire">−</button>
      </div>
    </div>
    <div id="rp-body">

      <!-- Bande d'état principale -->
      <div id="rp-state-bar" class="state-offline">
        <span id="rp-state-icon">○</span>
        <span id="rp-state-text">
          Vérification…
          <span id="rp-state-sub"></span>
        </span>
      </div>

      <!-- IP + Drapeau -->
      <div id="rp-ip-row">
        <span id="rp-flag">🌐</span>
        <span id="rp-ip-info">
          <span id="rp-ip">—</span>
          <span id="rp-country"></span>
        </span>
      </div>

      <!-- Infos -->
      <div class="rp-row">
        <span class="rp-label">Circuits actifs</span>
        <span class="rp-val" id="rp-instances">—</span>
      </div>
      <div class="rp-row">
        <span class="rp-label">Rotation auto</span>
        <span class="rp-val" id="rp-autorot">—</span>
      </div>

      <!-- Filtre pays (proxy/local uniquement) -->
      <div class="rp-row" id="rp-country-row" style="display:none">
        <span class="rp-label">Pays</span>
        <select id="rp-sel-country" style="background:#0d0d1a;border:1px solid #2d2d5e;border-radius:5px;color:#e0e0e0;font-family:monospace;font-size:11px;padding:2px 5px;max-width:120px">
          <option value="">🌍 Tous</option>
        </select>
      </div>

      <button id="rp-btn-rotate" disabled>🔄 Nouvelle IP</button>
      <div id="rp-cooldown"></div>

      <!-- Paramètres -->
      <div id="rp-settings">
        <label>Hôte Docker</label>
        <input id="rp-host" type="text">
        <label>Port API</label>
        <input id="rp-port" type="text">
        <label>Identifiant</label>
        <input id="rp-user" type="text" autocomplete="username">
        <label>Mot de passe</label>
        <input id="rp-pass" type="password" autocomplete="current-password">
        <button id="rp-btn-save">Enregistrer</button>
      </div>
    </div>
  `;
  document.body.appendChild(panel);

  const el = id => document.getElementById(id);

  // ─── Drag ─────────────────────────────────────────────────────────────────
  let drag = false, ox = 0, oy = 0;
  panel.addEventListener('mousedown', e => {
    if (['BUTTON', 'INPUT'].includes(e.target.tagName)) return;
    drag = true;
    const r = panel.getBoundingClientRect();
    ox = e.clientX - r.left; oy = e.clientY - r.top;
  });
  document.addEventListener('mousemove', e => {
    if (!drag) return;
    panel.style.right  = 'unset';
    panel.style.bottom = 'unset';
    panel.style.left   = `${e.clientX - ox}px`;
    panel.style.top    = `${e.clientY - oy}px`;
  });
  document.addEventListener('mouseup', () => { drag = false; });

  // ─── Collapse ─────────────────────────────────────────────────────────────
  el('rp-btn-col').addEventListener('click', () => {
    panel.classList.toggle('rp-collapsed');
    el('rp-btn-col').textContent = panel.classList.contains('rp-collapsed') ? '+' : '−';
  });

  // ─── Settings ─────────────────────────────────────────────────────────────
  el('rp-host').value = CFG.host;
  el('rp-port').value = CFG.port;
  el('rp-user').value = CFG.user;
  el('rp-pass').value = CFG.pass;
  el('rp-btn-cfg').addEventListener('click', () => el('rp-settings').classList.toggle('open'));
  el('rp-btn-save').addEventListener('click', () => {
    CFG.host = el('rp-host').value.trim();
    CFG.port = el('rp-port').value.trim();
    CFG.user = el('rp-user').value.trim();
    CFG.pass = el('rp-pass').value;
    GM_setValue('rp_host', CFG.host);
    GM_setValue('rp_port', CFG.port);
    GM_setValue('rp_user', CFG.user);
    GM_setValue('rp_pass', CFG.pass);
    el('rp-settings').classList.remove('open');
    checkStatus();
  });

  // ─── HTTP helper ──────────────────────────────────────────────────────────
  const gmReq = (method, url, body) => new Promise((res, rej) => {
    const headers = {};
    if (body) headers['Content-Type'] = 'application/json';
    if (CFG.user) headers['Authorization'] = 'Basic ' + btoa(CFG.user + ':' + CFG.pass);
    GM_xmlhttpRequest({
      method, url, timeout: 6000,
      data:      body ? JSON.stringify(body) : undefined,
      headers,
      onload:    r  => res(r),
      onerror:   () => rej(new Error('network')),
      ontimeout: () => rej(new Error('timeout')),
    });
  });

  // ─── Mise à jour de la bande d'état ───────────────────────────────────────
  function setStateBar(state, icon, text, sub = '') {
    const bar = el('rp-state-bar');
    bar.className = `state-${state}`;
    el('rp-state-icon').innerHTML = icon;
    el('rp-state-text').childNodes[0].nodeValue = text + ' ';
    el('rp-state-sub').textContent = sub;
  }

  // ─── Statut du proxy ──────────────────────────────────────────────────────
  let lastMode = null;

  async function checkStatus() {
    try {
      const r = await gmReq('GET', apiUrl('/api/status'));
      if (r.status !== 200) throw new Error();
      const s = JSON.parse(r.responseText);

      el('rp-instances').textContent = s.instances || '0';
      el('rp-autorot').textContent   = s.auto_rotation
        ? `ON · ${s.rotation_interval}s`
        : 'OFF';
      el('rp-btn-rotate').disabled = s.loading;

      if (s.loading) {
        setStateBar(
          'loading',
          '<span class="rp-spin">⟳</span>',
          'Recherche en cours…',
          s.loading_message || ''
        );
        el('rp-flag').textContent    = '⏳';
        el('rp-ip').textContent      = '—';
        el('rp-country').textContent = '';
      } else if (s.mode === 'tor') {
        setStateBar('tor', '🧅', 'Connecté via Tor', `${s.instances} circuit(s)`);
        if (lastMode !== 'tor') fetchGeo();
      } else if (s.mode === 'local') {
        setStateBar('proxy', '📂', 'Connecté via proxies locaux', `${s.instances} proxy(s) actif(s)`);
        if (lastMode !== 'local') fetchGeo();
      } else {
        setStateBar('proxy', '🌐', 'Connecté via Free Proxy', `${s.instances} proxy(s) actif(s)`);
        if (lastMode !== 'proxy') fetchGeo();
      }

      // Sélecteur de pays (masqué en mode Tor)
      if (s.mode !== 'tor' && !s.loading) {
        el('rp-country-row').style.display = 'flex';
        loadCountries(s.country_filter);
      } else {
        el('rp-country-row').style.display = 'none';
      }

      lastMode = s.loading ? lastMode : s.mode;

    } catch {
      setStateBar('offline', '✗', 'Docker injoignable', `${CFG.host}:${CFG.port}`);
      el('rp-flag').textContent    = '❌';
      el('rp-ip').textContent      = '—';
      el('rp-country').textContent = '';
      el('rp-instances').textContent = '—';
      el('rp-btn-rotate').disabled = true;
    }
  }

  // ─── Géolocalisation de l'IP de sortie ────────────────────────────────────
  // ipapi.co/json/ retourne l'IP + pays de la requête → passe par le proxy du navigateur
  async function fetchGeo() {
    el('rp-ip').textContent      = '…';
    el('rp-flag').textContent    = '🌐';
    el('rp-country').textContent = '';
    try {
      const r = await gmReq('GET', 'https://ipapi.co/json/');
      if (r.status !== 200) throw new Error();
      const d = JSON.parse(r.responseText);
      el('rp-ip').textContent      = d.ip        || '?';
      el('rp-flag').textContent    = countryFlag(d.country_code);
      el('rp-country').textContent = d.country_name || d.country_code || '';
    } catch {
      // Fallback : juste l'IP brute
      try {
        const r2 = await gmReq('GET', 'https://icanhazip.com/');
        el('rp-ip').textContent = r2.responseText.trim() || '?';
      } catch {
        el('rp-ip').textContent = '(erreur)';
      }
    }
  }

  // ─── Filtre pays ──────────────────────────────────────────────────────────
  let _countriesLoaded = false;

  async function loadCountries(currentFilter) {
    const sel = el('rp-sel-country');
    // Ne recharge la liste que si le pool a changé (sinon juste sync la valeur)
    if (!_countriesLoaded) {
      try {
        const r = await gmReq('GET', apiUrl('/api/countries'));
        if (r.status !== 200) throw new Error();
        const data = JSON.parse(r.responseText);
        sel.innerHTML = '<option value="">🌍 Tous</option>' +
          (data.countries || []).map(function(c) {
            return '<option value="' + c.code + '">' + countryFlag(c.code) + ' ' + c.name + ' (' + c.count + ')</option>';
          }).join('');
        _countriesLoaded = true;
      } catch { /* ignore */ }
    }
    sel.value = currentFilter || '';
  }

  el('rp-sel-country').addEventListener('change', async function() {
    _countriesLoaded = false;  // force reload au prochain checkStatus (pool peut changer)
    await gmReq('POST', apiUrl('/api/country'), { country: this.value });
    fetchGeo();  // l'IP va changer
  });

  // ─── Nouvelle IP ──────────────────────────────────────────────────────────
  const COOLDOWN = 15;
  let cdTimer = null;

  el('rp-btn-rotate').addEventListener('click', async () => {
    el('rp-btn-rotate').disabled = true;
    el('rp-cooldown').textContent = '';
    clearInterval(cdTimer);

    setStateBar(
      'loading',
      '<span class="rp-spin">⟳</span>',
      'Changement d\'IP…',
      'Nouveau circuit en cours'
    );
    el('rp-flag').textContent    = '⏳';
    el('rp-ip').textContent      = '—';
    el('rp-country').textContent = '';

    try {
      const r = await gmReq('POST', apiUrl('/api/rotate'));
      if (r.status !== 200) throw new Error();
    } catch {
      el('rp-cooldown').textContent = '✗ Erreur — API injoignable';
      el('rp-btn-rotate').disabled = false;
      checkStatus();
      return;
    }

    let remaining = COOLDOWN;
    el('rp-cooldown').textContent = `Nouveau circuit dans ${remaining}s`;
    cdTimer = setInterval(() => {
      remaining--;
      if (remaining <= 0) {
        clearInterval(cdTimer);
        el('rp-cooldown').textContent = '';
        el('rp-btn-rotate').disabled = false;
        fetchGeo();
      } else {
        el('rp-cooldown').textContent = `Nouveau circuit dans ${remaining}s`;
      }
    }, 1000);

    // L'IP met quelques secondes à changer
    setTimeout(() => { checkStatus(); fetchGeo(); }, 5000);
  });

  // ─── Init ─────────────────────────────────────────────────────────────────
  checkStatus();
  fetchGeo();
  setInterval(checkStatus, 20000); // statut toutes les 20s
  setInterval(fetchGeo,    90000); // IP toutes les 90s

})();
