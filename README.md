# ISOWatcher v1.3

Gestionnaire de bibliothèque ISO auto-hébergé. Surveille les nouvelles versions des distributions Linux et Windows, les télécharge automatiquement, vérifie les checksums, et notifie via Discord.

---

## Fonctionnalités

### Surveillance & téléchargement automatique
- Détection automatique des nouvelles versions (Ubuntu LTS, Debian Stable, Fedora, Arch Linux)
- Téléchargement dès qu'une nouvelle version est détectée
- Vérification SHA-256 et MD5 automatique après chaque téléchargement
- Scheduler configurable depuis l'interface (quotidien / hebdomadaire / mensuel, heure précise)

### Sources Windows via rg-adguard
- Windows Server 2025, 2022, 2019
- Windows 11, Windows 10
- Checksums récupérés et vérifiés automatiquement depuis files.rg-adguard.net
- Notification Discord si l'URL directe n'est pas résolvable (téléchargement manuel guidé)

### Archives FTP officielles
- Accès aux versions historiques depuis les FTP officiels : Debian, Ubuntu, Fedora, Arch Linux
- Sélection visuelle de la version dans l'interface
- Téléchargement directement dans la bibliothèque

### Scan de la bibliothèque locale
- Parcours récursif de `/data/isos` à la recherche de fichiers `.iso` existants
- Identification automatique par pattern de nom de fichier (Debian, Ubuntu, Fedora, Arch, Rocky, AlmaLinux, Windows, Kali, openSUSE, Linux Mint…)
- Assignation manuelle interactive pour les fichiers non reconnus
- Détection des fichiers modifiés (taille différente) → marquage `modified` sans écraser les données existantes
- Calcul SHA-256 à l'import
- Rapport de scan filtrable par statut

### Interface web
- Dashboard avec stats, téléchargements actifs, dernières activités
- Gestion des distributions : ajout, modification, activation/désactivation, suppression
- Bibliothèque avec checksums, taille, source et date
- Suivi de progression en temps réel (polling toutes les 1,5s)
- Logs des 100 dernières opérations
- Guide d'ajout intégré avec exemples de configuration

### Notifications Discord
- Webhook configurable depuis l'interface
- Notification à chaque ISO téléchargée (avec statut checksum)
- Notification si une nouvelle version Windows est détectée sans URL directe

---

## Distributions pré-configurées

| Distribution | Slug | Source | Détection auto | DL auto |
|---|---|---|---|---|
| Ubuntu LTS Server | `ubuntu-lts` | Direct | ✅ | ✅ |
| Ubuntu Desktop LTS | `ubuntu-desktop-lts` | Direct | ✅ | ✅ |
| Debian Stable | `debian-stable` | Direct | ✅ | ✅ |
| Fedora Server | `fedora-server` | Direct | ✅ | ✅ |
| Arch Linux | `arch-linux` | Direct | ✅ | ✅ |
| Windows Server 2025 | `windows-server-2025` | rg-adguard | ✅ | Conditionnel¹ |
| Windows Server 2022 | `windows-server-2022` | rg-adguard | ✅ | Conditionnel¹ |
| Windows Server 2019 | `windows-server-2019` | rg-adguard | ✅ | Conditionnel¹ |
| Windows 11 | `windows-11` | rg-adguard | ✅ | Conditionnel¹ |

¹ Le téléchargement automatique Windows dépend de la résolvabilité de l'URL directe côté Microsoft. Si non résolvable, une notification Discord est envoyée avec le lien rg-adguard.

---

## Prérequis

- Docker + Docker Compose sur le host (Proxmox, LXC, ou tout Linux)
- Accès réseau sortant vers Internet (pour les téléchargements et la détection de versions)
- Partage NAS monté sur le host si les ISOs sont stockées sur un NAS

---

## Déploiement rapide

### 1. Décompresser

```bash
mkdir -p /opt/isowatcher
cd /opt/isowatcher
unzip isowatcher.zip
cd isowatcher
```

### 2. Configurer `docker-compose.yml`

Ouvrir le fichier et adapter les volumes selon votre situation :

**ISOs sur le NAS (recommandé) :**
```yaml
volumes:
  - ./data:/data              # DB SQLite locale (rapide, safe)
  - /mnt/nas/isos:/data/isos  # ISOs sur le NAS monté
```

**Tout en local :**
```yaml
volumes:
  - ./data:/data
```

### 3. Monter le NAS (si applicable)

**NFS :**
```bash
mkdir -p /mnt/nas/isos

# Ajouter dans /etc/fstab
192.168.1.X:/volume1/isos  /mnt/nas/isos  nfs  defaults,_netdev,nofail  0  0

mount -a
```

**CIFS/SMB :**
```bash
apt install cifs-utils -y
mkdir -p /mnt/nas/isos

# Fichier de credentials
cat > /etc/nas-creds << EOF
username=VOTRE_USER
password=VOTRE_MOT_DE_PASSE
EOF
chmod 600 /etc/nas-creds

# Ajouter dans /etc/fstab
//192.168.1.X/isos  /mnt/nas/isos  cifs  credentials=/etc/nas-creds,_netdev,nofail  0  0

mount -a
```

### 4. Démarrer

```bash
docker compose up -d

# Vérifier
docker compose ps
docker compose logs -f
```

### 5. Accéder à l'interface

```
http://IP_DU_HOST:8080
```

---

## Compatibilité NFS / CIFS

Le code inclut deux optimisations SQLite pour les partages réseau :

```python
PRAGMA journal_mode=DELETE;   # Évite le WAL, incompatible avec les partages réseau
PRAGMA busy_timeout=30000;    # Tolère jusqu'à 30s de latence réseau
```

Ces pragmas rendent la DB compatible avec NFS et CIFS. Cependant, SQLite sur un partage réseau reste fragile en cas de coupure ou d'accès concurrent.

**Recommandation forte** : stocker la DB SQLite en local sur le host, et monter uniquement le dossier ISO sur le NAS :

```yaml
# docker-compose.yml
volumes:
  - ./data:/data              # ← local sur Proxmox : DB SQLite safe
  - /mnt/nas/isos:/data/isos  # ← NAS : uniquement les fichiers ISO
```

Cette configuration est celle du `docker-compose.yml` fourni par défaut.

---

## Structure du stockage

```
/data/
├── isowatcher.db              # Base de données SQLite (garder en local)
└── isos/
    ├── ubuntu-lts/
    │   └── 24.04/
    │       └── ubuntu-24.04-live-server-amd64.iso
    ├── ubuntu-desktop-lts/
    │   └── 24.04/
    │       └── ubuntu-24.04-desktop-amd64.iso
    ├── debian-stable/
    │   └── 13.4/
    │       └── debian-13.4-amd64-netinst.iso
    ├── fedora-server/
    │   └── 42/
    │       └── Fedora-Server-dvd-x86_64-42-1.1.iso
    ├── arch-linux/
    │   └── 2025.05.01/
    │       └── archlinux-2025.05.01-x86_64.iso
    └── windows-server-2025/
        └── Windows_Server_2025_.../
            └── *.iso
```

---

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `DATA_DIR` | `/data` | Répertoire racine des données (DB + ISOs) |
| `TZ` | `Europe/Paris` | Fuseau horaire pour le scheduler |

---

## API REST

### Distributions
| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/api/distros` | Liste toutes les distributions |
| `POST` | `/api/distros` | Ajouter une distribution |
| `PUT` | `/api/distros/{id}` | Modifier une distribution |
| `DELETE` | `/api/distros/{id}` | Supprimer une distribution |
| `PATCH` | `/api/distros/{id}/toggle` | Activer / désactiver |
| `GET` | `/api/check-version/{id}` | Vérifier la dernière version disponible |

### Bibliothèque & téléchargements
| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/api/library` | Liste les ISOs en bibliothèque |
| `POST` | `/api/check` | Déclencher une vérification de toutes les distros |
| `POST` | `/api/download` | Téléchargement manuel (`distro_id`, `version`, `url`) |
| `GET` | `/api/progress` | Progression des téléchargements en cours |

### Archives FTP
| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/api/archive` | Liste les sources d'archive disponibles |
| `GET` | `/api/archive/{key}` | Versions historiques d'une distro (`?limit=30`) |
| `POST` | `/api/archive/download` | Télécharger une ISO d'archive |

### Scan local
| Méthode | Endpoint | Description |
|---|---|---|
| `POST` | `/api/scan/start` | Lancer le scan de la bibliothèque locale |
| `GET` | `/api/scan/status` | État du scan (progression, résultats, non-identifiés) |
| `POST` | `/api/scan/assign` | Assigner manuellement distro + version à un fichier |
| `POST` | `/api/scan/ignore` | Ignorer un fichier non identifié |

### Paramètres & outils
| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | Statistiques globales |
| `GET` | `/api/logs` | 100 dernières entrées de log |
| `GET` | `/api/settings` | Paramètres actuels |
| `PUT` | `/api/settings` | Modifier les paramètres |
| `POST` | `/api/test-discord` | Tester le webhook Discord |
| `GET` | `/api/rg-sources` | Slugs rg-adguard disponibles |

---

## Ajouter une distribution personnalisée

### Mode Direct (Linux, BSD…)

Trois informations nécessaires :

```
Nom          : Rocky Linux 9
Slug         : rocky-linux-9
URL vérif.   : https://dl.rockylinux.org/pub/rocky/
Regex        : href="(9\.\d+)/?"
Template URL : https://dl.rockylinux.org/pub/rocky/{version}/isos/x86_64/Rocky-{version}-x86_64-minimal.iso
```

Le placeholder `{version}` est remplacé par la version détectée automatiquement.

### Mode rg-adguard (Windows)

Sélectionner la source `rg-adguard` dans le formulaire et choisir le slug Windows dans la liste. Aucune URL à renseigner.

---

## Commandes utiles

```bash
# Logs en temps réel
docker compose -f /opt/isowatcher/isowatcher/docker-compose.yml logs -f

# Redémarrer après mise à jour
docker compose -f /opt/isowatcher/isowatcher/docker-compose.yml up -d --build

# Arrêter
docker compose -f /opt/isowatcher/isowatcher/docker-compose.yml down

# Espace utilisé par les ISOs
du -sh /mnt/nas/isos/

# Vérifier le montage NAS
mountpoint -q /mnt/nas/isos && echo "Monté" || echo "Non monté"
```

---

## Dépannage

| Problème | Cause probable | Solution |
|---|---|---|
| Port 8080 déjà utilisé | Conflit avec un autre service | Changer `"8080:8080"` en `"8090:8080"` dans `docker-compose.yml` |
| `database is locked` au démarrage | DB sur partage réseau lent | Déplacer `./data` en local (voir section NFS/CIFS) |
| NAS non accessible au boot | Montage réseau pas encore prêt | Vérifier `_netdev,nofail` dans `/etc/fstab` |
| Permission refusée sur `/mnt/nas/isos` | Droits NFS insuffisants | Ajouter `no_root_squash` côté NAS ou ajuster `uid/gid` |
| Discord : HTTP 400 | URL webhook invalide ou expirée | Recréer le webhook dans les paramètres Discord |
| Debian version non détectée | `deb.debian.org` injoignable | Le fallback sur `ftp.debian.org` prend le relais automatiquement |
| Windows DL non automatique | URL Microsoft non résolvable | Notification Discord envoyée avec lien rg-adguard |

---

## Stack technique

- **Backend** : Python 3.12, FastAPI, APScheduler, httpx, SQLite
- **Frontend** : HTML/CSS/JS vanilla (pas de framework)
- **Runtime** : Docker, image `python:3.12-slim`
- **DB** : SQLite avec `journal_mode=DELETE` (compatible NFS/CIFS)


## Credits
Majoritairement développé avec l'assistance de Claude.AI
