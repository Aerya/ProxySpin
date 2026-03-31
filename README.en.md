# ⬡ ProxySpin

> 🇫🇷 [Version française](README.md)

Anonymizing rotating HTTP proxy based on Tor and free proxies, with a web control panel and browser extension.

---

## Description

ProxySpin exposes a **single entry point** (port 1973) behind which each request can exit with a different IP address. It supports three modes:

- **Tor**: N independent Tor instances, each with its own 3-relay encrypted circuit
- **Free Proxy**: HTTP/SOCKS free proxies fetched automatically from configurable sources (proxifly by default), filtered to keep only `elite` or `anonymous` proxies
- **Local**: a manually maintained proxy list provided as a text file

## Architecture

```
Browser / Client
        │
        ▼
   HAProxy :1973          ← single entry point (Basic auth)
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

## Ports

| Port | Role | Auth |
|------|------|------|
| `1973` | Rotating HTTP proxy (entry point) | Basic auth (`PROXY_USER` / `PROXY_PASS`) |
| `1974` | Web control panel + JSON API | Basic auth (`STATS_USER` / `STATS_PASS`) |
| `1976` | HAProxy stats | Basic auth (`STATS_USER` / `STATS_PASS`) |

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

Or use a pre-built image (see [Docker images](#pre-built-docker-images) section).

## Configuration

All options are environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MODE` | `tor` | Mode at startup: `tor`, `proxy` or `local` |
| `AUTO_ROTATION` | `true` | Automatic rotation enabled at startup |
| `ROTATION_INTERVAL` | `60` | Rotation interval in seconds at startup |
| `tors` | `10` | Number of parallel Tor instances (mode `tor`) |
| `MAX_PROXIES` | `20` | Number of proxies active in HAProxy (modes `proxy` and `local`) |
| `COUNTRY_FILTER` | — | Country filter at startup, 2-letter ISO code (e.g. `FR`, `DE`) |
| `PROXY_USER` | — | Proxy username for port 1973 **(required)** |
| `PROXY_PASS` | — | Proxy password **(required)** |
| `STATS_USER` | — | Web UI + API username for ports 1974 and 1976 **(required)** |
| `STATS_PASS` | — | Web UI + API password **(required)** |

### Variable details

**`MODE` / `AUTO_ROTATION` / `ROTATION_INTERVAL`**
These three variables define the state **when the container starts**. Once running, all of them can be changed live from the web UI (port 1974) or the userscript — no restart needed. However, live changes are lost if the container restarts. If you want a persistent behaviour, set it in `docker-compose.yml`.

**`tors`**
In `tor` mode, ProxySpin starts N completely independent Tor processes. Each one builds its own 3-hop encrypted circuit and has its own exit IP. HAProxy distributes requests across these N instances. With `tors=10` you have 10 different exit IPs available simultaneously. Raising this value gives more IP diversity, but uses more RAM and increases startup time.

**`MAX_PROXIES`**
In `proxy` and `local` modes, ProxySpin tests potentially thousands of proxies (depending on your sources and files). Only the first `MAX_PROXIES` working proxies are activated in HAProxy — each one requires a Privoxy process. The rest are discarded. When a country filter is active, ProxySpin widens the search to `MAX_PROXIES × 5` candidates to ensure enough proxies from the target country are found.

> ⚠️ If all active proxies are blocked by the site you're visiting, you need to wait for the next rotation (automatic or triggered manually via the userscript/web UI) to get a fresh pool.

**`COUNTRY_FILTER`**
Entirely optional. Useful if you want the filter to be active from the moment the container starts, without having to select it each time in the userscript. Can be changed at any time from the web UI or userscript (no restart required).

**`PROXY_USER` / `PROXY_PASS`**
Credentials to access the proxy on port 1973. Without these variables, the proxy is open with no password — only acceptable on a fully isolated network.

**`STATS_USER` / `STATS_PASS`**
Shared credentials for the web UI + API (port 1974) and the HAProxy stats page (port 1976). Without these variables, both interfaces are open with no password.

## Security

All three exposed ports are protected by **HTTP Basic auth**:

- **Port 1973** (proxy) — via HAProxy, credentials `PROXY_USER` / `PROXY_PASS`
- **Port 1974** (web UI + API) — via Python, credentials `STATS_USER` / `STATS_PASS`
- **Port 1976** (HAProxy stats) — via HAProxy, credentials `STATS_USER` / `STATS_PASS`

Port 1974 protects the entire control panel and JSON API. The browser automatically shows a login dialog on first open. The Tampermonkey userscript stores credentials locally (⚙ field in the floating panel).

## Proxy sources (proxy mode)

In `proxy` mode, ProxySpin fetches from a configurable source list managed from the web panel (port 1974, **PROXY SOURCES** card).

### Default sources

The four [proxifly](https://github.com/proxifly/free-proxy-list) lists are pre-loaded (http, https, socks4, socks5). ProxySpin fetches them directly from `raw.githubusercontent.com` on every rotation — since proxifly updates its lists daily, IPs are always fresh.

### Managing sources

1. **The 4 proxifly sources are active by default** — no configuration needed to get started.
2. **Multiple sources** — add as many as you like. They are merged and tested together before feeding the pool.
3. **Direct URLs** — any public API returning a proxy list is supported (e.g. proxyscrape).
4. **Other GitHub repos** — use the **Raw** link of the file, not the GitHub page URL:
   - ❌ `https://github.com/user/repo/blob/main/list.json`
   - ✅ `https://raw.githubusercontent.com/user/repo/main/list.json`

   The **Raw** button at the top of any GitHub file gives you the correct link.
5. **Checkboxes** — each source can be disabled without being deleted, and re-enabled later.

The configuration is persisted in `data/sources.json` (Docker volume).

### Compatible URL examples

```
# JSON format (proxifly-style) — automatically filters elite/anonymous
https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.json

# Plain text format, one proxy per line (ip:port)
https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all

# Any other repo or API in either of these two formats
```

ProxySpin auto-detects the format of each source:
- **JSON** (list of objects with `ip`, `port`, `anonymity`…) — filtered to `elite` / `anonymous` only
- **Plain text** (one entry per line) — accepted formats: `ip:port`, `http://ip:port`, `socks5://ip:port`…

## Country filter (proxy and local modes)

In `proxy` and `local` modes, the proxy pool can be restricted to a specific country.

### How it works

1. **Proxifly JSON**: the country is directly included in the data (`geolocation.countryCode`) — no extra request needed.
2. **Plain-text sources**: IPs have no country metadata. ProxySpin automatically geolocates them after the connectivity test via [ip-api.com](http://ip-api.com) (free, 100 IPs per request).
3. The full tested and geolocated pool is **kept in memory**. Changing the country filter instantly re-slices this pool — **no network re-fetch**.
4. If the requested country has no proxies in the current pool, a warning is logged and all proxies are used.
5. When a country filter is active, ProxySpin fetches up to `MAX_PROXIES × 5` candidates during the fetch phase to ensure enough proxies from the desired country.

### Usage

**From the userscript** (floating panel): a dropdown appears in proxy/local modes, listing available countries with their flag and proxy count. Selection is instant.

**From the web panel** (port 1974): dropdown in the Status card.

**At startup** via environment variable:
```yaml
- COUNTRY_FILTER=FR
```

> ℹ️ The country filter is not available in **Tor** mode: Tor circuits select their exit node automatically.

## Local proxy mode

Use your own proxy list instead of Tor or proxifly.

**1. Drop one or more `.txt` files into the `data/` folder**:

ProxySpin automatically scans **all `.txt` files** in the `/data` volume and merges them. You can have `data/list1.txt`, `data/list2.txt`, etc. Duplicates (same IP+port+protocol) are ignored.

Format of a `.txt` file:

```
# One proxy per line — blank lines and # are ignored
#
# Accepted formats:
#   ip:port                  → HTTP by default
#   http://ip:port
#   https://ip:port
#   socks4://ip:port
#   socks5://ip:port

1.2.3.4:8080
http://5.6.7.8:3128
socks5://9.10.11.12:1080
socks4://13.14.15.16:1080
```

> ℹ️ If you add new `.txt` files to the `data/` folder, **restart the container** for them to be picked up. Automatic rotation only re-reads files that were present at startup.

**2. Set the mode to `local`** in `docker-compose.yml`:

```yaml
- MODE=local
```

or switch on the fly from the web panel (port 1974).

At startup, each proxy is tested in parallel (15 threads) then geolocated. Only responsive proxies are added to the pool. Automatic rotation re-reads the files on every cycle — file contents can be updated without restarting the container.

## Usage

**Configure your browser** to use `http://YOUR_IP:1973` as an HTTP proxy (credentials `PROXY_USER` / `PROXY_PASS`).

**Web panel**: `http://YOUR_IP:1974` (credentials `STATS_USER` / `STATS_PASS`)
**HAProxy stats**: `http://YOUR_IP:1976/haproxy?stats` (same credentials)

## Web Panel (port 1974)

Allows you to:
- Switch between **Tor**, **Free Proxy** and **Local** modes on the fly
- Enable / disable automatic rotation and change the interval
- Force an immediate IP change
- **Filter proxies by country** (dropdown, proxy/local modes)
- View active backends with their country and flag
- Manage proxy sources (add, enable/disable, delete)

## Browser Extension (Tampermonkey)

The `userscript.user.js` file adds a floating panel on every page:

- Displays the current exit IP with the **country flag**
- Shows the active mode (**🧅 Tor**, **🌐 Free Proxy** or **📂 Local**)
- **Country selection dropdown** (proxy/local modes) — lists available countries with flag and proxy count
- Detects whether the Docker container is running
- **New IP** button with cooldown
- Configurable settings via ⚙: Docker host, API port, username and password

**Install:** open `userscript.user.js` in your browser with Tampermonkey installed, then set the host, port and credentials in ⚙.

## IP Rotation

**Tor mode**: sends `signal newnym` to each instance's Control Port → new 3-relay circuit → new exit IP.

**Free Proxy / Local mode**: re-fetches all enabled sources, re-tests in parallel (15 threads), geolocates IPs without country data, then applies the country filter if active.

> ⚠️ Tor exit nodes are publicly listed. Some websites systematically block all Tor traffic. Free Proxy mode is less detectable in this regard but offers fewer anonymity guarantees.

## JSON API (port 1974)

All requests require **Basic auth** (`STATS_USER` / `STATS_PASS`).

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

Images are published automatically to [GitHub Container Registry](https://github.com/Aerya/ProxySpin/pkgs/container/proxyspin):

```bash
# AMD64 (PC, server)
docker pull ghcr.io/aerya/proxyspin:latest-amd64

# ARM64 (Raspberry Pi 4+, Apple Silicon via Rosetta, ARM servers)
docker pull ghcr.io/aerya/proxyspin:latest-arm64

# Multi-arch (Docker picks the right image automatically)
docker pull ghcr.io/aerya/proxyspin:latest
```

## Automatic updates

### Weekly rebuild (Tor, Privoxy, HAProxy)

Every Monday at 3am UTC, the Docker images are rebuilt automatically. Since the Dockerfile installs packages via `apt-get` without pinned version numbers, each rebuild automatically picks up the latest available version from the official repositories — including the [official Tor Project repository](https://deb.torproject.org).

### Smoke test (regression protection)

Before every image is published, the CI runs an automatic smoke test:

```
OK    tor     : Tor version 0.4.x.x
OK    privoxy : Privoxy version 3.x.x
OK    haproxy : HAProxy version 2.x.x
OK    python3 : 3.10.x
```

If a package is missing or broken after an update, the workflow stops and **the previous image remains intact** in the registry.

### Dependabot (base image + GitHub Actions)

[Dependabot](https://github.com/Aerya/ProxySpin/network/updates) automatically opens Pull Requests every Monday to update the `ubuntu:22.04` base image and the versions of GitHub Actions used in CI workflows.

### Summary

| Component | Mechanism | Frequency |
|-----------|-----------|-----------|
| Tor, Privoxy, HAProxy, Python | Weekly rebuild | Monday 3am UTC |
| Ubuntu base image | Dependabot PR | Monday |
| GitHub Actions (CI) | Dependabot PR | Monday |
| Regression after update | Smoke test CI | Every build |

---

## License

MIT
