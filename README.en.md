# ⬡ ProxySpin

> 🇫🇷 [Version française](README.md)

Anonymizing rotating HTTP proxy based on Tor and free proxies, with a web control panel and browser extension.

---

## Description

ProxySpin exposes a **single entry point** (port 1973) behind which each request can exit with a different IP address. It supports three modes:

- **Tor**: N independent Tor instances, each with its own 3-relay encrypted circuit
- **Free Proxy**: **SOCKS4/SOCKS5** free proxies fetched automatically from configurable sources (proxifly by default), filtered to keep only `elite` or `anonymous` proxies
- **Local**: a manually maintained proxy list provided as a text file

## Architecture

```
Browser / Client
        │
        ▼
  Python Proxy :1973      ← exposed entry point (Basic auth, HTTP + HTTPS CONNECT)
        │
        ▼
   HAProxy :11973          ← internal only (localhost), TCP load balancer
        │  balance leastconn
        ├── Privoxy :20000
        ├── Privoxy :20001   ← each instance forwards to Tor or a free proxy
        └── Privoxy :2000N
                │
        ┌───────┴──────────┐
        │ Tor mode         │ Free Proxy / Local mode
        │                  │
   Tor :10000         HTTP/SOCKS Proxy
   Tor :10001         (country-filtered if active)
   Tor :1000N
        │
   Tor Network → Internet (different exit IP per circuit)
```

Port **1973 is managed by a Python server** (modelled after Gluetun's mechanism) that verifies authentication before forwarding to HAProxy on internal port 11973. HAProxy runs in **TCP mode** (compatible with HAProxy 2.5+) and relays raw bytes to the Privoxy instances that do the actual proxying.

## Ports

| Port | Role | Auth |
|------|------|------|
| `1973` | Rotating HTTP proxy (entry point) | Basic auth (`PROXY_USER` / `PROXY_PASS`) |
| `1974` | Web control panel + JSON API | Basic auth (`STATS_USER` / `STATS_PASS`) |
| `1976` | HAProxy stats *(disabled by default)* | Basic auth (`STATS_USER` / `STATS_PASS`) |

## Quick start

Edit `docker-compose.yml` and set passwords:

```yaml
- PROXY_USER=myuser
- PROXY_PASS=strong_password
- STATS_USER=admin
- STATS_PASS=strong_password
```

Start:

```bash
docker compose up -d
```

## Configuration

All options are environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MODE` | `tor` | Mode at startup: `tor`, `proxy` or `local` |
| `AUTO_ROTATION` | `true` (`tor` mode) / `false` (`proxy` and `local` modes) | Automatic rotation enabled at startup |
| `ROTATION_INTERVAL` | `60` | Rotation interval in seconds at startup |
| `tors` | `10` | Number of parallel Tor instances (mode `tor`) |
| `MAX_PROXIES` | `20` | Number of proxies active in HAProxy (modes `proxy` and `local`) |
| `COUNTRY_FILTER` | — | Country filter at startup, 2-letter ISO code (e.g. `FR`, `DE`) |
| `PROXY_USER` | — | Proxy username for port 1973 |
| `PROXY_PASS` | — | Proxy password |
| `STATS_USER` | — | Web UI + API username for ports 1974 and 1976 |
| `STATS_PASS` | — | Web UI + API password |
| `PROXY_AUTH_ENABLED` | `true` | Auth on port 1973 (`false` to disable) |
| `API_AUTH_ENABLED` | `true` | Auth on port 1974 (`false` to disable) |
| `STATS_AUTH_ENABLED` | `true` | Auth on port 1976 (`false` to disable) |

### Disabling authentication

By default, all three ports are protected. To disable auth on a port (useful on a trusted local network), uncomment the relevant line in `docker-compose.yml`:

```yaml
# - PROXY_AUTH_ENABLED=false   # port 1973 — rotating proxy
# - API_AUTH_ENABLED=false     # port 1974 — Web UI + API
# - STATS_AUTH_ENABLED=false   # port 1976 — HAProxy stats
```

When auth is disabled, the corresponding `PROXY_USER`/`PROXY_PASS` or `STATS_USER`/`STATS_PASS` variables become optional.

### Variable details

**`MODE` / `AUTO_ROTATION` / `ROTATION_INTERVAL`**
These variables define the state **when the container starts**. Once running, all of them can be changed live from the web UI (port 1974) or the userscript — no restart needed. Live changes are lost if the container restarts; set them in `docker-compose.yml` for persistent behaviour.

Auto-rotation is **enabled by default in `tor` mode** (circuit renewal every 60 seconds) and **disabled by default in `proxy` and `local` modes** — free proxies are less stable and frequent rotation can interrupt active connections. It can be enabled manually from the Web UI. Switching modes from the Web UI automatically adjusts this behaviour (unless `AUTO_ROTATION` is explicitly set in `docker-compose.yml`).

**`tors`**
In `tor` mode, ProxySpin starts N completely independent Tor processes. Each one builds its own 3-hop encrypted circuit and has its own exit IP. HAProxy distributes requests across these N instances. With `tors=10` you have 10 different exit IPs available simultaneously.

**`MAX_PROXIES`**
In `proxy` and `local` modes, only the first `MAX_PROXIES` working proxies are activated in HAProxy. When a country filter is active, ProxySpin widens the search to `MAX_PROXIES × 5` candidates.

**`COUNTRY_FILTER`**
Entirely optional. Can be changed at any time from the web UI or userscript, no restart required.

## Security

All three exposed ports can be protected by **HTTP Basic auth**:

- **Port 1973** (proxy) — Python server, credentials `PROXY_USER` / `PROXY_PASS`
- **Port 1974** (web UI + API) — Python server, credentials `STATS_USER` / `STATS_PASS`
- **Port 1976** (HAProxy stats) — HAProxy, credentials `STATS_USER` / `STATS_PASS`

> ⚠️ **Local network**: when the browser is configured to use proxy 1973, all traffic goes through Privoxy→Tor, including requests to your local network (192.168.x.x). Add your local addresses to the browser's proxy exceptions to reach ports 1974 and 1976 directly.
>
> In LibreWolf / Firefox: `Settings → General → Network Settings → No proxy for:`
> ```
> localhost, 127.0.0.1, 192.168.0.0/24
> ```

> ⚠️ **WebRTC leak**: WebRTC can reveal your real IP address even behind a proxy, because it establishes P2P connections that bypass the browser's proxy settings. **Disable WebRTC** in your browser before using ProxySpin.
>
> Disable guide (Chrome, Firefox, Safari, Opera, Edge): [K3V1991/How-to-disable-WebRTC](https://github.com/K3V1991/How-to-disable-WebRTC-in-Chrome-Firefox-Safari-Opera-and-Edge)
>
> In LibreWolf / Firefox: `about:config` → `media.peerconnection.enabled` → `false`

## Usage

**Configure your browser** to use `http://YOUR_IP:1973` as an HTTP proxy (credentials `PROXY_USER` / `PROXY_PASS` if auth is enabled).

**Web panel**: `http://YOUR_IP:1974` (credentials `STATS_USER` / `STATS_PASS` if auth is enabled)

**HAProxy stats**: `http://YOUR_IP:1976/` (same credentials) — disabled by default, uncomment in `docker-compose.yml` to enable

## Proxy sources (proxy mode)

In `proxy` mode, ProxySpin fetches from a configurable source list managed from the web panel (port 1974, **PROXY SOURCES** card).

### Default sources

The [proxifly](https://github.com/proxifly/free-proxy-list) **socks4** and **socks5** lists are pre-loaded and active by default — no configuration needed.

> ⚠️ **Why SOCKS only?** Free HTTP proxies do not support `CONNECT`, which is required for HTTPS traffic (virtually the entire modern web). Without `CONNECT`, the browser falls back to a direct connection and leaks the real IP. Only SOCKS proxies natively tunnel both HTTP and HTTPS.

### Managing sources

- Multiple sources can be added; they are merged and tested together.
- **Other GitHub repos**: use the **Raw** link of the file, not the GitHub page URL:
  - ❌ `https://github.com/user/repo/blob/main/list.json`
  - ✅ `https://raw.githubusercontent.com/user/repo/main/list.json`
- Each source can be disabled without being deleted via the checkboxes.

The configuration is persisted in `data/sources.json` (Docker volume).

### Supported formats

ProxySpin auto-detects the format:
- **JSON** (list of objects with `ip`, `port`, `anonymity`…) — filtered to `elite` / `anonymous`
- **Plain text** (one entry per line) — `socks4://ip:port`, `socks5://ip:port`

> HTTP/HTTPS entries are silently ignored.

## Country filter (proxy and local modes)

The proxy pool can be restricted to a specific country. The full pool is **kept in memory** — changing the country is instant, no network re-fetch.

- **From the userscript**: dropdown with flags
- **From the web panel** (port 1974): dropdown in the Status card
- **At startup**: `- COUNTRY_FILTER=FR`

> ℹ️ Not available in **Tor** mode: Tor circuits select their exit node automatically.

## Local proxy mode

Drop `.txt` files into the `data/` folder (one proxy per line) and set `MODE=local`.

Accepted formats: `socks4://ip:port`, `socks5://ip:port`. HTTP/HTTPS entries are ignored.

> ℹ️ If you add new `.txt` files after startup, restart the container for them to be picked up.

## Web Panel (port 1974)

- Switch between **Tor**, **Free Proxy** and **Local** modes on the fly
- Enable / disable automatic rotation and change the interval
- Force an immediate IP change
- Filter proxies by country (proxy/local modes)
- View active backends with country and flag
- Manage proxy sources

## Browser Extension (Tampermonkey)

The `userscript.user.js` file adds a floating panel on every page:

- Active mode (**🧅 Tor**, **🌐 Free Proxy** or **📂 Local**)
- Country selection dropdown (proxy/local modes)
- **New IP** button with cooldown
- Configurable settings via ⚙: Docker host, API port, username and password

**Install:** open `userscript.user.js` in your browser with Tampermonkey installed, then set the host, port and credentials in ⚙.

> ℹ️ If the proxy is active in the browser, add your server IP to the proxy exceptions so the userscript can reach port 1974 directly (without going through Tor).

## JSON API (port 1974)

Requests require **Basic auth** if `API_AUTH_ENABLED=true` (default).

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/status` | General state (mode, instances, country filter…) |
| `GET` | `/api/backends` | Active backends list with country |
| `GET` | `/api/countries` | Available countries in the current pool |
| `GET` | `/api/sources` | Configured proxy sources |
| `POST` | `/api/rotate` | Force a rotation |
| `POST` | `/api/mode` | Switch mode (`{"mode":"proxy"}`) |
| `POST` | `/api/config` | Update config (`auto_rotation`, `rotation_interval`) |
| `POST` | `/api/country` | Set country filter (`{"country":"FR"}` or `""`) |
| `POST` | `/api/sources` | Add a source (`{"url":"…","label":"…"}`) |
| `POST` | `/api/sources/remove` | Delete a source |
| `POST` | `/api/sources/toggle` | Enable/disable a source |

## Pre-built Docker images

```bash
# Multi-arch (Docker picks the right image automatically)
docker pull ghcr.io/aerya/proxyspin:latest

# AMD64 (PC, server)
docker pull ghcr.io/aerya/proxyspin:latest-amd64

# ARM64 (Raspberry Pi 4+, Apple Silicon, ARM servers)
docker pull ghcr.io/aerya/proxyspin:latest-arm64
```

## Automatic updates

| Component | Mechanism | Frequency |
|-----------|-----------|-----------|
| Tor, Privoxy, HAProxy, Python | Weekly rebuild | Monday 3am UTC |
| Ubuntu base image | Dependabot PR | Monday |
| GitHub Actions (CI) | Dependabot PR | Monday |
| Regression after update | Smoke test CI | Every build |

---

## License

MIT
