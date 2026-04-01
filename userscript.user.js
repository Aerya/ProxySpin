// ==UserScript==
// @name         ProxySpin — Contrôleur
// @namespace    proxyspin-controller
// @version      3.1.0
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

  // ─── i18n ─────────────────────────────────────────────────────────────────
  let LANG = GM_getValue('rp_lang', navigator.language.startsWith('fr') ? 'fr' : 'en');

  const TR = {
    fr: {
      checking:       'Vérification\u2026',
      searching:      'Recherche en cours\u2026',
      conn_tor:       'Connecté via Tor',
      conn_local:     'Connecté via proxies locaux',
      conn_proxy:     'Connecté via Free Proxy',
      circuits:       'circuit(s)',
      proxies_active: 'proxy(s) actif(s)',
      lbl_circuits:   'Circuits actifs',
      lbl_autorot:    'Rotation auto',
      lbl_country:    'Pays',
      all_countries:  '🌍 Tous',
      new_ip:         '🔄 Nouvelle IP',
      ip_changing:    "Changement d'IP\u2026",
      new_circuit:    'Nouveau circuit en cours',
      circuit_in:     'Nouveau circuit dans',
      err_api:        '\u2717 Erreur \u2014 API injoignable',
      offline:        'Docker injoignable',
      open_ui:        'Ouvrir le Web UI',
      settings:       'Paramètres',
      collapse:       'Réduire',
      expand:         'Étendre',
      lbl_url:        'URL du Web UI (ip:port ou https://\u2026)',
      lbl_user:       'Identifiant',
      lbl_pass:       'Mot de passe',
      save:           'Enregistrer',
    },
    en: {
      checking:       'Checking\u2026',
      searching:      'Searching\u2026',
      conn_tor:       'Connected via Tor',
      conn_local:     'Connected via local proxies',
      conn_proxy:     'Connected via Free Proxy',
      circuits:       'circuit(s)',
      proxies_active: 'active proxy(s)',
      lbl_circuits:   'Active circuits',
      lbl_autorot:    'Auto rotation',
      lbl_country:    'Country',
      all_countries:  '🌍 All',
      new_ip:         '🔄 New IP',
      ip_changing:    'Changing IP\u2026',
      new_circuit:    'New circuit in progress',
      circuit_in:     'New circuit in',
      err_api:        '\u2717 Error \u2014 API unreachable',
      offline:        'Docker unreachable',
      open_ui:        'Open Web UI',
      settings:       'Settings',
      collapse:       'Collapse',
      expand:         'Expand',
      lbl_url:        'Web UI URL (ip:port or https://\u2026)',
      lbl_user:       'Username',
      lbl_pass:       'Password',
      save:           'Save',
    }
  };

  function t(key) { return (TR[LANG] && TR[LANG][key]) || TR.fr[key] || key; }

  function setLang(lang) {
    LANG = lang;
    GM_setValue('rp_lang', lang);
    applyTranslations();
  }

  function applyTranslations() {
    el('rp-btn-ui').title          = t('open_ui');
    el('rp-btn-cfg').title         = t('settings');
    el('rp-btn-col').title         = panel.classList.contains('rp-collapsed') ? t('expand') : t('collapse');
    el('rp-lbl-circuits').textContent = t('lbl_circuits');
    el('rp-lbl-autorot').textContent  = t('lbl_autorot');
    el('rp-lbl-country').textContent  = t('lbl_country');
    el('rp-btn-rotate').textContent   = t('new_ip');
    el('rp-lbl-url').textContent      = t('lbl_url');
    el('rp-lbl-user').textContent     = t('lbl_user');
    el('rp-lbl-pass').textContent     = t('lbl_pass');
    el('rp-btn-save').textContent     = t('save');
    el('rp-btn-lang-fr').style.opacity = LANG === 'fr' ? '1' : '0.4';
    el('rp-btn-lang-en').style.opacity = LANG === 'en' ? '1' : '0.4';
    // Reload first option of country select
    const sel = el('rp-sel-country');
    if (sel.options.length > 0) sel.options[0].textContent = t('all_countries');
  }

  // ─── Config ───────────────────────────────────────────────────────────────
  function _migrateBase() {
    const saved = GM_getValue('rp_base', '');
    if (saved) return saved;
    const oldHost = GM_getValue('rp_host', '192.168.0.150');
    const oldPort = GM_getValue('rp_port', '1974');
    return `http://${oldHost}:${oldPort}`;
  }

  function _normalizeBase(raw) {
    const s = raw.trim().replace(/\/$/, '');
    if (/^https?:\/\//i.test(s)) return s;
    return `http://${s}`;
  }

  let CFG = {
    base: _normalizeBase(_migrateBase()),
    user: GM_getValue('rp_user', ''),
    pass: GM_getValue('rp_pass', ''),
  };
  const apiUrl = path => `${CFG.base}${path}`;

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

    #rp-ip-row {
      display: flex; align-items: center; gap: 8px;
      background: #0d0d1a; border-radius: 7px; padding: 6px 10px;
    }
    #rp-flag   { font-size: 20px; line-height: 1; }
    #rp-ip-info { flex: 1; }
    #rp-ip     { font-size: 13px; color: #e0e0e0; font-weight: bold; }
    #rp-country{ font-size: 10px; color: #666; display: block; }

    .rp-row    { display:flex; justify-content:space-between; align-items:center; font-size:11px; }
    .rp-label  { color:#555; }
    .rp-val    { color:#aaa; }

    #rp-btn-rotate {
      background:#7c83fd; color:#fff; border:none; border-radius:7px;
      padding:7px 10px; font-size:12px; cursor:pointer; width:100%;
      font-family:monospace; transition:.2s; margin-top:2px;
    }
    #rp-btn-rotate:hover:not(:disabled) { background:#5c63e0; }
    #rp-btn-rotate:disabled { opacity:.5; cursor:not-allowed; }
    #rp-cooldown { font-size:10px; color:#555; text-align:center; min-height:14px; }

    .rp-ctrl { background:none; border:none; color:#555; cursor:pointer; font-size:13px; padding:0 2px; }
    .rp-ctrl:hover { color:#aaa; }
    .rp-lang { background:none; border:none; cursor:pointer; font-size:12px; padding:0 1px; line-height:1; }
    .rp-lang:hover { opacity:1 !important; }

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
      <div style="display:flex;gap:3px;align-items:center">
        <button class="rp-lang" id="rp-btn-lang-fr" title="Français">🇫🇷</button>
        <button class="rp-lang" id="rp-btn-lang-en" title="English">🇬🇧</button>
        <button class="rp-ctrl" id="rp-btn-ui">🖥</button>
        <button class="rp-ctrl" id="rp-btn-cfg">⚙</button>
        <button class="rp-ctrl" id="rp-btn-col">−</button>
      </div>
    </div>
    <div id="rp-body">

      <div id="rp-state-bar" class="state-offline">
        <span id="rp-state-icon">○</span>
        <span id="rp-state-text">
          &nbsp;
          <span id="rp-state-sub"></span>
        </span>
      </div>

      <div id="rp-ip-row">
        <span id="rp-flag">🌐</span>
        <span id="rp-ip-info">
          <span id="rp-ip">—</span>
          <span id="rp-country"></span>
        </span>
      </div>

      <div class="rp-row">
        <span class="rp-label" id="rp-lbl-circuits"></span>
        <span class="rp-val" id="rp-instances">—</span>
      </div>
      <div class="rp-row">
        <span class="rp-label" id="rp-lbl-autorot"></span>
        <span class="rp-val" id="rp-autorot">—</span>
      </div>

      <div class="rp-row" id="rp-country-row" style="display:none">
        <span class="rp-label" id="rp-lbl-country"></span>
        <select id="rp-sel-country" style="background:#0d0d1a;border:1px solid #2d2d5e;border-radius:5px;color:#e0e0e0;font-family:monospace;font-size:11px;padding:2px 5px;max-width:120px">
          <option value=""></option>
        </select>
      </div>

      <button id="rp-btn-rotate" disabled></button>
      <div id="rp-cooldown"></div>

      <div id="rp-settings">
        <label id="rp-lbl-url"></label>
        <input id="rp-base" type="text" placeholder="http://192.168.0.150:1974">
        <label id="rp-lbl-user"></label>
        <input id="rp-user" type="text" autocomplete="username">
        <label id="rp-lbl-pass"></label>
        <input id="rp-pass" type="password" autocomplete="current-password">
        <button id="rp-btn-save"></button>
      </div>
    </div>
  `;
  document.body.appendChild(panel);

  const el = id => document.getElementById(id);

  // ─── Drag ─────────────────────────────────────────────────────────────────
  let drag = false, ox = 0, oy = 0;
  panel.addEventListener('mousedown', e => {
    if (['BUTTON', 'INPUT', 'SELECT'].includes(e.target.tagName)) return;
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
    el('rp-btn-col').title = panel.classList.contains('rp-collapsed') ? t('expand') : t('collapse');
  });

  // ─── Language switcher ────────────────────────────────────────────────────
  el('rp-btn-lang-fr').addEventListener('click', () => setLang('fr'));
  el('rp-btn-lang-en').addEventListener('click', () => setLang('en'));

  // ─── Settings ─────────────────────────────────────────────────────────────
  el('rp-base').value = CFG.base;
  el('rp-user').value = CFG.user;
  el('rp-pass').value = CFG.pass;
  el('rp-btn-ui').addEventListener('click', () => window.open(CFG.base, '_blank'));
  el('rp-btn-cfg').addEventListener('click', () => el('rp-settings').classList.toggle('open'));
  el('rp-btn-save').addEventListener('click', () => {
    CFG.base = _normalizeBase(el('rp-base').value);
    CFG.user = el('rp-user').value.trim();
    CFG.pass = el('rp-pass').value;
    GM_setValue('rp_base', CFG.base);
    GM_setValue('rp_user', CFG.user);
    GM_setValue('rp_pass', CFG.pass);
    el('rp-base').value = CFG.base;
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
          t('searching'),
          s.loading_message || ''
        );
        el('rp-flag').textContent    = '⏳';
        el('rp-ip').textContent      = '—';
        el('rp-country').textContent = '';
      } else if (s.mode === 'tor') {
        setStateBar('tor', '🧅', t('conn_tor'), `${s.instances} ${t('circuits')}`);
        if (lastMode !== 'tor') fetchGeo();
      } else if (s.mode === 'local') {
        setStateBar('proxy', '📂', t('conn_local'), `${s.instances} ${t('proxies_active')}`);
        if (lastMode !== 'local') fetchGeo();
      } else {
        setStateBar('proxy', '🌐', t('conn_proxy'), `${s.instances} ${t('proxies_active')}`);
        if (lastMode !== 'proxy') fetchGeo();
      }

      if (s.mode !== 'tor' && !s.loading) {
        el('rp-country-row').style.display = 'flex';
        loadCountries(s.country_filter);
      } else {
        el('rp-country-row').style.display = 'none';
      }

      lastMode = s.loading ? lastMode : s.mode;

    } catch {
      setStateBar('offline', '✗', t('offline'), CFG.base);
      el('rp-flag').textContent      = '❌';
      el('rp-ip').textContent        = '—';
      el('rp-country').textContent   = '';
      el('rp-instances').textContent = '—';
      el('rp-btn-rotate').disabled   = true;
    }
  }

  // ─── Géolocalisation de l'IP de sortie ────────────────────────────────────
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
    if (!_countriesLoaded) {
      try {
        const r = await gmReq('GET', apiUrl('/api/countries'));
        if (r.status !== 200) throw new Error();
        const data = JSON.parse(r.responseText);
        sel.innerHTML = `<option value="">${t('all_countries')}</option>` +
          (data.countries || []).map(function(c) {
            return '<option value="' + c.code + '">' + countryFlag(c.code) + ' ' + c.name + ' (' + c.count + ')</option>';
          }).join('');
        _countriesLoaded = true;
      } catch { /* ignore */ }
    }
    sel.value = currentFilter || '';
  }

  el('rp-sel-country').addEventListener('change', async function() {
    _countriesLoaded = false;
    await gmReq('POST', apiUrl('/api/country'), { country: this.value });
    fetchGeo();
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
      t('ip_changing'),
      t('new_circuit')
    );
    el('rp-flag').textContent    = '⏳';
    el('rp-ip').textContent      = '—';
    el('rp-country').textContent = '';

    try {
      const r = await gmReq('POST', apiUrl('/api/rotate'));
      if (r.status !== 200) throw new Error();
    } catch {
      el('rp-cooldown').textContent = t('err_api');
      el('rp-btn-rotate').disabled = false;
      checkStatus();
      return;
    }

    let remaining = COOLDOWN;
    el('rp-cooldown').textContent = `${t('circuit_in')} ${remaining}s`;
    cdTimer = setInterval(() => {
      remaining--;
      if (remaining <= 0) {
        clearInterval(cdTimer);
        el('rp-cooldown').textContent = '';
        el('rp-btn-rotate').disabled = false;
        fetchGeo();
      } else {
        el('rp-cooldown').textContent = `${t('circuit_in')} ${remaining}s`;
      }
    }, 1000);

    setTimeout(() => { checkStatus(); fetchGeo(); }, 5000);
  });

  // ─── Init ─────────────────────────────────────────────────────────────────
  applyTranslations();
  checkStatus();
  fetchGeo();
  setInterval(checkStatus, 20000);
  setInterval(fetchGeo,    90000);

})();
