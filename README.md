# ⬡ ProxySpin

> 🇬🇧 [English version](README.en.md)

Proxy HTTP rotatif anonymisant basé sur Tor et des proxies gratuits, avec interface web et extension navigateur.

---

## Description

ProxySpin expose **un point d'entrée unique** (port 1973) derrière lequel chaque requête peut sortir avec une IP différente. Il supporte trois modes :

- **Tor** : N instances Tor indépendantes, chacune avec son propre circuit chiffré à 3 nœuds relais
- **Free Proxy** : proxies HTTP/SOCKS gratuits récupérés automatiquement depuis des sources configurables (proxifly par défaut), filtrés pour ne garder que les proxies `elite` ou `anonymous`
- **Local** : liste de proxies fournie manuellement dans un fichier texte

## Architecture

```
Navigateur / Client
        │
        ▼
   HAProxy :1973          ← point d'entrée unique (auth Basic)
        │  balance leastconn
        ├── Privoxy :20000
        ├── Privoxy :20001   ← chaque instance forward vers Tor ou un proxy gratuit
        └── Privoxy :2000N
                │
        ┌───────┴──────────┐
        │ Mode Tor         │ Mode Free Proxy / Local
        │                  │
   Tor :10000         Proxy HTTP/SOCKS
   Tor :10001         (filtré par pays si actif)
   Tor :1000N
        │
   Réseau Tor → Internet (IP de sortie différente par circuit)
```

## Ports

| Port | Rôle | Auth |
|------|------|------|
| `1973` | Proxy HTTP rotatif (point d'entrée) | Basic auth (`PROXY_USER` / `PROXY_PASS`) |
| `1974` | Interface web de contrôle + API JSON | Basic auth (`STATS_USER` / `STATS_PASS`) |
| `1976` | HAProxy stats | Basic auth (`STATS_USER` / `STATS_PASS`) |

## Démarrage rapide

Éditer `docker-compose.yml` et renseigner les mots de passe :

```yaml
- PROXY_USER=monuser
- PROXY_PASS=motdepasse_fort
- STATS_USER=admin
- STATS_PASS=motdepasse_fort
```

Lancer :

```bash
docker compose up -d
```

Ou utiliser l'image pré-buildée (voir section [Images Docker](#images-docker-pré-buildées)).

## Configuration

Toutes les options sont des variables d'environnement dans `docker-compose.yml` :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `MODE` | `tor` | Mode au démarrage : `tor`, `proxy` ou `local` |
| `AUTO_ROTATION` | `true` | Rotation automatique activée au démarrage |
| `ROTATION_INTERVAL` | `60` | Intervalle de rotation en secondes au démarrage |
| `tors` | `10` | Nombre d'instances Tor parallèles (mode `tor`) |
| `MAX_PROXIES` | `20` | Nombre de proxies actifs dans HAProxy (modes `proxy` et `local`) |
| `COUNTRY_FILTER` | — | Filtre pays au démarrage, code ISO 2 lettres (ex. `FR`, `DE`) |
| `PROXY_USER` | — | Identifiant proxy port 1973 **(obligatoire)** |
| `PROXY_PASS` | — | Mot de passe proxy **(obligatoire)** |
| `STATS_USER` | — | Identifiant web UI + API ports 1974 et 1976 **(obligatoire)** |
| `STATS_PASS` | — | Mot de passe web UI + API **(obligatoire)** |

### Détail des variables

**`MODE` / `AUTO_ROTATION` / `ROTATION_INTERVAL`**
Ces trois variables définissent l'état **au démarrage du conteneur**. Une fois lancé, tout peut être modifié à chaud depuis le web UI (port 1974) ou le userscript — sans redémarrer. En revanche, ces changements en live sont perdus si le conteneur redémarre. Si vous voulez un comportement persistant, fixez-le dans `docker-compose.yml`.

**`tors`**
En mode `tor`, ProxySpin démarre N processus Tor complètement indépendants. Chacun construit son propre circuit chiffré à 3 nœuds et possède sa propre IP de sortie. HAProxy répartit les requêtes entre ces N instances. Avec `tors=10`, vous avez 10 IPs de sortie différentes disponibles simultanément. Augmenter cette valeur donne plus de diversité, mais consomme plus de RAM et rallonge le démarrage.

**`MAX_PROXIES`**
En modes `proxy` et `local`, ProxySpin teste potentiellement des milliers de proxies (selon vos sources et fichiers). Seuls les `MAX_PROXIES` premiers proxies opérationnels sont activés dans HAProxy — chacun nécessitant un processus Privoxy. Les autres candidats testés sont ignorés. Si un filtre pays est actif, ProxySpin élargit la recherche à `MAX_PROXIES × 5` candidats pour avoir suffisamment de proxies du pays voulu dans le lot.

> ⚠️ Si tous les proxies actifs sont bloqués par le site visité, il faut attendre la prochaine rotation (automatique ou manuelle via le userscript/web UI) pour obtenir un nouveau pool.

**`COUNTRY_FILTER`**
Entièrement optionnel. Utile si vous souhaitez que le filtre soit actif dès le démarrage, sans avoir à le sélectionner à chaque fois dans le userscript. Peut être modifié à tout moment depuis le web UI ou le userscript (sans redémarrer).

**`PROXY_USER` / `PROXY_PASS`**
Identifiants pour accéder au proxy sur le port 1973. Sans ces variables, le proxy est accessible sans mot de passe — à n'utiliser que sur un réseau totalement isolé.

**`STATS_USER` / `STATS_PASS`**
Identifiants communs pour le web UI + API (port 1974) et la page de stats HAProxy (port 1976). Sans ces variables, ces interfaces sont accessibles sans mot de passe.

## Sécurité

Les trois ports exposés sont protégés par **HTTP Basic auth** :

- **Port 1973** (proxy) — via HAProxy, identifiants `PROXY_USER` / `PROXY_PASS`
- **Port 1974** (web UI + API) — via Python, identifiants `STATS_USER` / `STATS_PASS`
- **Port 1976** (stats HAProxy) — via HAProxy, identifiants `STATS_USER` / `STATS_PASS`

Le port 1974 protège l'ensemble du panneau de contrôle et de l'API JSON. Le navigateur affiche automatiquement une boîte de connexion à la première ouverture. Le userscript Tampermonkey stocke les identifiants localement (champ ⚙ du panneau flottant).

## Sources de proxies (mode proxy)

En mode `proxy`, ProxySpin interroge une liste de sources configurable depuis l'interface web (port 1974, carte **SOURCES DE PROXIES**).

### Sources par défaut

Les quatre listes [proxifly](https://github.com/proxifly/free-proxy-list) sont pré-chargées (http, https, socks4, socks5). ProxySpin les récupère directement sur `raw.githubusercontent.com` à chaque rotation — proxifly mettant ses listes à jour quotidiennement, les IPs sont toujours fraîches.

### Gestion des sources

1. **Les 4 sources proxifly sont actives par défaut** — pas de configuration nécessaire pour démarrer.
2. **Plusieurs sources** — ajoutez-en autant que vous voulez. Elles sont fusionnées et testées ensemble avant d'alimenter le pool.
3. **URLs directes** — toute API publique renvoyant une liste de proxies est acceptée (ex. proxyscrape).
4. **Autres dépôts GitHub** — utilisez le lien **Raw** du fichier, pas l'URL de la page :
   - ❌ `https://github.com/user/repo/blob/main/list.json`
   - ✅ `https://raw.githubusercontent.com/user/repo/main/list.json`

   Le bouton **Raw** en haut de chaque fichier sur GitHub donne le bon lien.
5. **Cases à cocher** — chaque source peut être désactivée sans être supprimée, et réactivée plus tard.

La configuration est persistée dans `data/sources.json` (volume Docker).

### Exemples d'URLs compatibles

```
# Format JSON (proxifly-style) — filtre automatiquement elite/anonymous
https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.json

# Format texte brut, une ligne par proxy (ip:port)
https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all

# N'importe quel autre repo ou API dans l'un de ces deux formats
```

ProxySpin détecte automatiquement le format de chaque source :
- **JSON** (liste d'objets avec `ip`, `port`, `anonymity`…) — filtre `elite` / `anonymous`
- **Texte brut** (une entrée par ligne) — formats acceptés : `ip:port`, `http://ip:port`, `socks5://ip:port`…

## Filtre par pays (modes proxy et local)

En modes `proxy` et `local`, il est possible de restreindre le pool à un pays spécifique.

### Comment ça marche

1. **Proxifly JSON** : le pays est directement inclus dans les données (`geolocation.countryCode`) — aucune requête supplémentaire.
2. **Sources texte brut** : les IPs n'ont pas de métadonnée de pays. ProxySpin les géolocalise automatiquement après le test de fonctionnement via [ip-api.com](http://ip-api.com) (gratuit, 100 IPs par requête).
3. Le pool complet testé et géolocalisé est **conservé en mémoire**. Changer de pays filtre ce pool instantanément, **sans re-fetch réseau**.
4. Si le pays demandé n'a aucun proxy dans le pool actuel, un avertissement est émis et tous les proxies sont utilisés.
5. Quand un filtre pays est actif, ProxySpin récupère jusqu'à `MAX_PROXIES × 5` candidats lors du fetch pour avoir suffisamment de proxies du pays voulu.

### Utilisation

**Depuis le userscript** (panneau flottant) : un menu déroulant apparaît en modes proxy/local, listant les pays disponibles dans le pool actuel avec leur drapeau et le nombre de proxies. La sélection est instantanée.

**Depuis l'interface web** (port 1974) : menu déroulant dans la carte Statut.

**Au démarrage** via variable d'environnement :
```yaml
- COUNTRY_FILTER=FR
```

> ℹ️ Le filtre pays n'est pas disponible en mode **Tor** : les circuits Tor choisissent leur nœud de sortie automatiquement.

## Mode proxies locaux

Permet d'utiliser votre propre liste de proxies au lieu de Tor ou de proxifly.

**1. Déposer un ou plusieurs fichiers `.txt` dans le dossier `data/`** :

ProxySpin scanne automatiquement **tous les fichiers `.txt`** du volume `/data` et les fusionne. Vous pouvez donc avoir `data/liste1.txt`, `data/liste2.txt`, etc. Les doublons (même IP+port+protocole) sont ignorés.

Format d'un fichier `.txt` :

```
# Un proxy par ligne — lignes vides et # ignorés
#
# Formats acceptés :
#   ip:port                  → HTTP par défaut
#   http://ip:port
#   https://ip:port
#   socks4://ip:port
#   socks5://ip:port

1.2.3.4:8080
http://5.6.7.8:3128
socks5://9.10.11.12:1080
socks4://13.14.15.16:1080
```

> ℹ️ Si vous ajoutez de nouveaux fichiers `.txt` dans le dossier `data/`, **redémarrez le conteneur** pour qu'ils soient pris en compte. La rotation automatique relit uniquement les fichiers déjà présents au démarrage.

**2. Sélectionner le mode `local`** dans `docker-compose.yml` :

```yaml
- MODE=local
```

ou basculer à chaud depuis l'interface web (port 1974).

Au démarrage, chaque proxy est testé en parallèle (15 threads), puis géolocalisé. Seuls les proxies qui répondent sont ajoutés au pool. La rotation automatique relit les fichiers à chaque cycle — le contenu des fichiers peut être modifié sans redémarrer le conteneur.

## Utilisation

**Configurer le navigateur** pour utiliser `http://VOTRE_IP:1973` comme proxy HTTP (identifiants `PROXY_USER` / `PROXY_PASS`).

**Interface web** : `http://VOTRE_IP:1974` (identifiants `STATS_USER` / `STATS_PASS`)
**HAProxy stats** : `http://VOTRE_IP:1976/haproxy?stats` (mêmes identifiants)

## Interface web (port 1974)

Permet de :
- Basculer entre les modes **Tor**, **Free Proxy** et **Local** à chaud
- Activer / désactiver la rotation automatique et modifier l'intervalle
- Forcer un changement d'IP immédiat
- **Filtrer les proxies par pays** (menu déroulant, modes proxy/local)
- Visualiser les backends actifs avec leur pays et drapeau
- Gérer les sources de proxies (ajouter, activer/désactiver, supprimer)

## Extension navigateur (Tampermonkey)

Le fichier `userscript.user.js` ajoute un panneau flottant sur toutes les pages :

- Affiche l'IP de sortie actuelle avec le **drapeau du pays**
- Indique le mode actif (**🧅 Tor**, **🌐 Free Proxy** ou **📂 Local**)
- **Menu déroulant de sélection du pays** (modes proxy/local)
- Détecte si le Docker tourne
- Bouton **Nouvelle IP** avec cooldown
- Paramètres configurables via ⚙ : hôte Docker, port API, identifiant et mot de passe

**Installation :** ouvrir `userscript.user.js` dans le navigateur avec Tampermonkey installé, puis renseigner l'hôte, le port et les identifiants dans ⚙.

## Rotation des IP

**Mode Tor** : envoie `signal newnym` au Control Port de chaque instance → nouveau circuit à 3 nœuds → nouvelle IP de sortie.

**Mode Free Proxy / Local** : re-fetche toutes les sources activées, re-teste en parallèle (15 threads), géolocalise les IPs sans pays, puis applique le filtre pays si actif.

> ⚠️ Les exit nodes Tor sont publiquement listés. Certains sites bloquent systématiquement tout le trafic Tor. Le mode Free Proxy est plus discret sur ce point mais offre moins de garanties d'anonymat.

## API JSON (port 1974)

Toutes les requêtes nécessitent un **Basic auth** (`STATS_USER` / `STATS_PASS`).

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `GET` | `/api/status` | État général (mode, instances, filtre pays…) |
| `GET` | `/api/backends` | Liste des backends actifs avec pays |
| `GET` | `/api/countries` | Pays disponibles dans le pool actuel |
| `GET` | `/api/sources` | Sources de proxies configurées |
| `POST` | `/api/rotate` | Forcer une rotation |
| `POST` | `/api/mode` | Changer de mode (`{"mode":"proxy"}`) |
| `POST` | `/api/config` | Modifier la config (`auto_rotation`, `rotation_interval`) |
| `POST` | `/api/country` | Définir le filtre pays (`{"country":"FR"}` ou `""`) |
| `POST` | `/api/sources` | Ajouter une source (`{"url":"…","label":"…"}`) |
| `POST` | `/api/sources/remove` | Supprimer une source |
| `POST` | `/api/sources/toggle` | Activer/désactiver une source |

## Images Docker pré-buildées

Les images sont publiées automatiquement sur [GitHub Container Registry](https://github.com/Aerya/ProxySpin/pkgs/container/proxyspin) :

```bash
# AMD64 (PC, serveur)
docker pull ghcr.io/aerya/proxyspin:latest-amd64

# ARM64 (Raspberry Pi 4+, Apple Silicon via Rosetta, serveurs ARM)
docker pull ghcr.io/aerya/proxyspin:latest-arm64

# Multi-arch (Docker choisit automatiquement la bonne image)
docker pull ghcr.io/aerya/proxyspin:latest
```

## Mises à jour automatiques

### Rebuild hebdomadaire (Tor, Privoxy, HAProxy)

Chaque lundi à 3h UTC, les images Docker sont reconstruites automatiquement. Comme le Dockerfile installe les paquets via `apt-get` sans numéro de version fixé, chaque rebuild récupère automatiquement la dernière version disponible dans les dépôts officiels — y compris le [dépôt officiel Tor Project](https://deb.torproject.org).

### Smoke test (protection contre les régressions)

Avant chaque publication d'image, le CI exécute un smoke test automatique :

```
OK    tor     : Tor version 0.4.x.x
OK    privoxy : Privoxy version 3.x.x
OK    haproxy : HAProxy version 2.x.x
OK    python3 : 3.10.x
```

Si un paquet est manquant ou cassé après une mise à jour, le workflow s'arrête et **l'image précédente reste intacte** dans le registry.

### Dependabot (image de base + GitHub Actions)

[Dependabot](https://github.com/Aerya/ProxySpin/network/updates) ouvre automatiquement des Pull Requests chaque lundi pour mettre à jour l'image de base `ubuntu:22.04` et les versions des GitHub Actions.

### Résumé

| Composant | Mécanisme | Fréquence |
|-----------|-----------|-----------|
| Tor, Privoxy, HAProxy, Python | Rebuild hebdomadaire | Lundi 3h UTC |
| Image de base Ubuntu | Dependabot PR | Lundi |
| GitHub Actions (CI) | Dependabot PR | Lundi |
| Régression après màj | Smoke test CI | À chaque build |

---

## Licence

MIT
