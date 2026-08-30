English version here --> https://github.com/b2akxhct/isowatcher/blob/master/EN_README.md

---

# ISOWatcher v2.0

Gestionnaire de bibliothèque ISO auto-hébergé. Surveille les nouvelles versions des distributions Linux et Windows, les télécharge automatiquement, vérifie les checksums, notifie via Discord, et synchronise la bibliothèque avec Proxmox via SSH.

Interface disponible en **français et anglais** (sélecteur en haut à droite).

---

## Fonctionnalités

### Surveillance & téléchargement automatique
- Détection automatique des nouvelles versions (Ubuntu LTS/Desktop, Debian Stable, Fedora, Arch Linux)
- Téléchargement dès qu'une nouvelle version est détectée
- Résolution dynamique du nom de fichier exact pour Debian et Fedora (scraping de l'index — évite les 404 liés à un numéro de version mal deviné)
- Vérification SHA-256 et MD5 automatique après chaque téléchargement
- Scheduler configurable depuis l'interface (quotidien / hebdomadaire / mensuel, heure précise)

### Sources Windows via rg-adguard
- Windows Server 2025, 2022, 2019
- Windows 11, Windows 10
- Checksums récupérés et vérifiés automatiquement depuis files.rg-adguard.net
- Notification Discord si l'URL directe n'est pas résolvable (téléchargement manuel guidé)

### VirtIO Drivers
- Pilotes VirtIO stable et latest via les URLs officielles Fedora (redirection permanente, toujours à jour)

### Archives FTP officielles
- Accès aux versions historiques depuis les FTP officiels : Debian, Ubuntu, Fedora, Arch Linux
- Sélection visuelle de la version dans l'interface
- Résolution automatique de l'URL de téléchargement exacte (utile pour Fedora dont le nom de fichier varie)

### Scan de la bibliothèque locale
- Parcours récursif de `/data/isos` à la recherche de fichiers `.iso` existants
- Identification automatique par pattern de nom de fichier (Debian, Ubuntu, Fedora, Arch, Rocky, AlmaLinux, Windows, Kali, openSUSE, Linux Mint…)
- Assignation manuelle interactive pour les fichiers non reconnus, **avec possibilité de créer une nouvelle catégorie à la volée** (ex: Manjaro, Windows 7…) directement depuis le scan
- Détection des fichiers modifiés (taille différente) → marquage `modified` sans écraser les données existantes
- Calcul SHA-256 à l'import
- Rapport de scan filtrable par statut

### Intégration Proxmox (Proxmox Sync)
- Conçu pour tout **NAS Linux partagé en CIFS/SMB** (Unraid, TrueNAS SCALE, un serveur Debian/Ubuntu classique, Synology/QNAP avec accès SSH activé…) — CIFS ne supporte pas les symlinks côté client, quel que soit le NAS derrière
- ISOWatcher se connecte en **SSH au NAS** et y exécute `sync_symlinks.sh`, qui crée les symlinks directement sur le filesystem natif du NAS (ext4, XFS, btrfs…)
- Proxmox monte ensuite `proxmox-view/` en CIFS depuis le NAS, indépendamment du container Docker
- Génération de clé SSH depuis l'interface, test de connexion, rapport de synchronisation détaillé (créés / mis à jour / ignorés / orphelins / conflits de noms)
- Réparation automatique des chemins en base de données si des fichiers ont été déplacés
- **Si le NAS ne permet pas l'exécution de scripts** (Synology verrouillé, QNAP restreint, appliance sans SSH) : la même logique peut tourner **depuis Proxmox lui-même**, qui accède au NAS en CIFS et crée les symlinks localement — voir la section dédiée ci-dessous

### Interface web
- Dashboard avec stats, téléchargements actifs, dernières activités
- Gestion des distributions : ajout, modification, activation/désactivation, suppression
- Bibliothèque avec checksums, taille, source et date
- Suivi de progression en temps réel (polling toutes les 1,5s)
- Logs des 100 dernières opérations
- Guide d'ajout intégré avec exemples de configuration prêts à l'emploi
- **Sélecteur de langue FR/EN**, préférence sauvegardée dans le navigateur

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
| Debian Stable | `debian-stable` | `debian`¹ | ✅ | ✅ |
| Fedora Server | `fedora-server` | `fedora`¹ | ✅ | ✅ |
| Arch Linux | `arch-linux` | Direct | ✅ | ✅ |
| VirtIO Drivers (stable) | `virtio-win-stable` | `virtio` | ✅ | ✅ |
| VirtIO Drivers (latest) | `virtio-win-latest` | `virtio` | ✅ | ✅ |
| Windows Server 2025 | `windows-server-2025` | rg-adguard | ✅ | Conditionnel² |
| Windows Server 2022 | `windows-server-2022` | rg-adguard | ✅ | Conditionnel² |
| Windows Server 2019 | `windows-server-2019` | rg-adguard | ✅ | Conditionnel² |
| Windows 11 | `windows-11` | rg-adguard | ✅ | Conditionnel² |

¹ Sources dynamiques dédiées : au lieu d'un template d'URL figé, un scraper dédié va lire l'index du répertoire officiel pour trouver le nom de fichier exact publié (évite les erreurs 404 quand le nombre de chiffres de version ou le suffixe de build changent).

² Le téléchargement automatique Windows dépend de la résolvabilité de l'URL directe côté Microsoft. Si non résolvable, une notification Discord est envoyée avec le lien rg-adguard pour téléchargement manuel.

---

## Prérequis

- Docker + Docker Compose sur le host (LXC Proxmox recommandé, ou tout Linux)
- Accès réseau sortant vers Internet (téléchargements et détection de versions)
- Un NAS Linux (Unraid, TrueNAS SCALE, serveur Debian/Ubuntu, Synology/QNAP avec SSH activé…) partagé en CIFS/SMB pour stocker les ISOs
- Pour la synchronisation Proxmox : soit un accès SSH root (ou utilisateur avec droits suffisants) sur le NAS, soit — si le NAS ne le permet pas — un accès en écriture au partage CIFS directement depuis Proxmox (voir la section dédiée)

---

## Déploiement

### 1. Décompresser

```bash
mkdir -p /opt/isowatcher
cd /opt/isowatcher
unzip isowatcher.zip
cd isowatcher
```

### 2. Monter le NAS CIFS (ISOs uniquement)

```bash
apt install cifs-utils -y
mkdir -p /mnt/nas/isos

cat > /etc/nas-creds << 'EOF'
username=VOTRE_USER
password=VOTRE_MOT_DE_PASSE
EOF
chmod 600 /etc/nas-creds

echo "//192.168.1.X/isos  /mnt/nas/isos  cifs  credentials=/etc/nas-creds,_netdev,nofail,uid=0,gid=0  0  0" >> /etc/fstab
mount -a
```

### 3. Adapter `docker-compose.yml`

Le fichier fourni monte déjà `/mnt/nas/isos` → adaptez le chemin réel si besoin. Aucun montage `proxmox-view` n'est nécessaire côté Docker : la synchronisation se fait entièrement via SSH vers le NAS (voir étape 5, ou l'alternative Proxmox-side plus bas si votre NAS ne le permet pas).

### 4. Démarrer

```bash
docker compose up -d
docker compose logs -f
```

### 5. Configurer la synchronisation Proxmox (SSH)

Cette méthode fonctionne pour tout NAS Linux avec accès SSH (Unraid, TrueNAS SCALE, Debian/Ubuntu, Synology/QNAP avec SSH activé…). Si votre NAS ne permet pas l'exécution de scripts, passez directement à la section **Alternative : script exécuté depuis Proxmox**.

**Copier le script sur le NAS :**
```bash
scp sync_symlinks.sh root@NAS_IP:/mnt/user/isos/isowatcher/
ssh root@NAS_IP "chmod +x /mnt/user/isos/isowatcher/sync_symlinks.sh"
```

**Depuis l'interface ISOWatcher → Proxmox Sync :**
1. Cliquer **🔑 Générer clé SSH**
2. Copier la clé publique affichée dans `~/.ssh/authorized_keys` sur le NAS
3. Renseigner : IP du NAS, chemins source/destination, chemin du script
4. **🔌 Tester SSH** pour valider la connexion
5. **↻ Synchroniser** pour lancer la première synchronisation

**Déclarer le storage dans Proxmox** (`/etc/pve/storage.cfg`), en montant `proxmox-view` directement depuis le NAS en CIFS — indépendamment de ce container Docker :

```ini
dir: isowatcher
  path /mnt/proxmox-view
  content iso
```

Vérifier : `pvesm list isowatcher`

### 6. Accéder à l'interface

```
http://IP_DU_HOST:8080
```

---

## Pourquoi SSH et pas un montage local ?

CIFS/SMB ne supporte pas les symlinks côté client — toute tentative de `ln -s`, `os.link()` ou équivalent depuis le container Docker échoue avec `Errno 95: Operation not supported`. Ceci est vrai **quel que soit le NAS derrière** (Unraid, TrueNAS, Synology, QNAP, un simple serveur Debian) : c'est une limitation du protocole CIFS lui-même, pas du NAS.

La solution : exécuter la création des liens **directement sur une machine qui voit le filesystem en natif** (pas à travers CIFS). Deux options selon ce que votre NAS permet :

### Option A — Script exécuté sur le NAS via SSH (recommandé, celle documentée ci-dessus)

Si le NAS permet l'exécution de scripts (Unraid, TrueNAS SCALE, tout NAS basé sur Linux avec accès SSH activé — y compris Synology/QNAP si SSH est activé dans leurs paramètres), ISOWatcher se connecte en SSH au NAS et y lance `sync_symlinks.sh`. Le script tourne alors sur le filesystem natif du NAS (ext4, XFS, btrfs…) où les symlinks fonctionnent normalement.

```
NAS (Unraid, TrueNAS, Synology avec SSH, etc.)
├── isos/<distro>/<version>/*.iso        ← fichiers réels (CIFS monté dans Docker)
└── proxmox-view/template/iso/*.iso      ← symlinks (créés PAR LE NAS via SSH)

Proxmox : monte proxmox-view/ en CIFS directement depuis le NAS
Docker (ISOWatcher) : ne touche jamais proxmox-view en local, uniquement via SSH
```

### Option B — Script exécuté depuis Proxmox lui-même (NAS fermé, sans SSH)

Si le NAS ne permet ni SSH ni exécution de script (appliance verrouillée, Synology avec SSH désactivé par politique, etc.), la même logique de création de symlinks peut être exécutée **depuis Proxmox**, à condition que Proxmox accède au partage NAS d'une façon qui supporte les symlinks — typiquement en **montant le partage en NFS** plutôt qu'en CIFS (NFS supporte les symlinks nativement), ou en utilisant un montage CIFS avec les extensions Unix activées côté serveur si le NAS les propose.

Concrètement :
1. Monter le partage ISOs en **NFS** sur Proxmox (au lieu de CIFS) : `mount -t nfs NAS_IP:/volume1/isos /mnt/isos`
2. Copier `sync_symlinks.sh` directement sur Proxmox et l'exécuter localement (ou via une tâche cron) plutôt que via SSH vers le NAS :
   ```bash
   ISOWATCHER_SOURCE_DIR=/mnt/isos/isos \
   ISOWATCHER_DEST_DIR=/mnt/isos/proxmox-view/template/iso \
   bash sync_symlinks.sh
   ```
3. Déclarer le storage Proxmox directement sur ce même point de montage NFS local — plus besoin d'un second montage CIFS séparé pour `proxmox-view`

Dans ce scénario, la configuration SSH d'ISOWatcher (page Proxmox Sync) reste inutile : c'est Proxmox qui gère la synchronisation localement, par exemple via une tâche planifiée (`cron`) qui appelle le script après chaque cycle de vérification d'ISOWatcher, ou manuellement.

L'essentiel à retenir : **peu importe la marque du NAS**, ce qui compte est de savoir (a) si on peut exécuter un script sur le NAS lui-même via SSH, et si non, (b) si on peut monter le partage en NFS depuis Proxmox pour contourner la limitation CIFS. ISOWatcher documente et automatise l'option A ; l'option B se met en place manuellement côté Proxmox avec le même script fourni.

---

## Compatibilité NFS / CIFS pour la base de données

Le code inclut deux optimisations SQLite pour les partages réseau :

```python
PRAGMA journal_mode=DELETE;   # Évite le WAL, incompatible avec les partages réseau
PRAGMA busy_timeout=30000;    # Tolère jusqu'à 30s de latence réseau
```

**Recommandation forte** : garder la base de données SQLite en local (`./data`), et ne monter en réseau que le dossier des ISOs — c'est la configuration du `docker-compose.yml` fourni par défaut.

---

## Structure du stockage

```
/data/
├── isowatcher.db              # Base de données SQLite (locale)
├── ssh/                       # Clé SSH générée (id_rsa / id_rsa.pub)
└── isos/                      # Monté depuis le NAS CIFS
    ├── ubuntu-lts/24.04/ubuntu-24.04-live-server-amd64.iso
    ├── ubuntu-desktop-lts/24.04/ubuntu-24.04-desktop-amd64.iso
    ├── debian-stable/13.5/debian-13.5.0-amd64-netinst.iso
    ├── fedora-server/42/Fedora-Server-dvd-x86_64-42-1.2.iso
    ├── arch-linux/2025.08.01/archlinux-2025.08.01-x86_64.iso
    ├── virtio-win-stable/.../virtio-win-x.x.x.iso
    └── windows-server-2025/.../*.iso
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
| `GET` | `/api/rg-sources` | Slugs rg-adguard disponibles |

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
| `GET` | `/api/archive/{key}` | Versions historiques d'une distro (`?limit=50`) |
| `GET` | `/api/archive/{key}/{version}/url` | Résout l'URL de téléchargement exacte |
| `POST` | `/api/archive/download` | Télécharger une ISO d'archive |

### Scan local
| Méthode | Endpoint | Description |
|---|---|---|
| `POST` | `/api/scan/start` | Lancer le scan de la bibliothèque locale |
| `GET` | `/api/scan/status` | État du scan (progression, résultats, non-identifiés) |
| `POST` | `/api/scan/assign` | Assigner manuellement distro + version à un fichier |
| `POST` | `/api/scan/ignore` | Ignorer un fichier non identifié |
| `POST` | `/api/scan/create-category` | Créer une nouvelle catégorie à la volée |

### Proxmox Sync (SSH)
| Méthode | Endpoint | Description |
|---|---|---|
| `POST` | `/api/symlinks/sync` | Déclenche `sync_symlinks.sh` sur le NAS via SSH |
| `GET` | `/api/symlinks/status` | État de la config SSH + listing distant réel |
| `POST` | `/api/symlinks/test-ssh` | Teste la connexion SSH |
| `POST` | `/api/symlinks/generate-key` | Génère une paire de clés SSH |
| `PUT` | `/api/symlinks/config` | Configure host/user/port/chemins SSH |
| `DELETE` | `/api/symlinks/clean` | Relance une sync pour nettoyer les orphelins |
| `POST` | `/api/symlinks/repair` | Répare les chemins DB puis re-synchronise |

### Paramètres & outils
| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | Statistiques globales |
| `GET` | `/api/logs` | 100 dernières entrées de log |
| `GET` | `/api/settings` | Paramètres actuels |
| `PUT` | `/api/settings` | Modifier les paramètres |
| `POST` | `/api/test-discord` | Tester le webhook Discord |

---

## Ajouter une distribution personnalisée

### Mode Direct (Linux, BSD…)

Trois informations nécessaires (voir aussi le Guide d'ajout intégré à l'interface, avec exemples Rocky Linux, AlmaLinux, openSUSE, Kali, Linux Mint) :

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

### Depuis le Scan local

Lors d'un scan, tout fichier `.iso` non reconnu peut être assigné à une catégorie existante **ou** à une toute nouvelle catégorie créée directement depuis le modal d'assignation (nom, slug, type, architecture) — pratique pour intégrer une ISO ponctuelle (Manjaro, Windows 7, etc.) sans passer par le formulaire complet.

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

# Tester la connexion SSH vers le NAS manuellement
ssh -i ./data/ssh/id_rsa root@NAS_IP echo OK
```

---

## Dépannage

| Problème | Cause probable | Solution |
|---|---|---|
| Port 8080 déjà utilisé | Conflit avec un autre service | Changer `"8080:8080"` en `"8090:8080"` dans `docker-compose.yml` |
| `database is locked` au démarrage | DB sur partage réseau lent | Garder `./data` en local (voir section NFS/CIFS) |
| Symlinks non créés, `Errno 95` | Tentative de création côté Docker/CIFS | Vérifier que la sync passe bien par SSH (page Proxmox Sync → Tester SSH) |
| NAS sans SSH disponible (Synology verrouillé, appliance fermée…) | Le NAS ne permet pas d'exécuter de script | Utiliser l'option B (script exécuté depuis Proxmox via un montage NFS) — voir section "Pourquoi SSH et pas un montage local" |
| `Contenu proxmox-view` toujours vide | SSH non configuré ou chemin distant incorrect | Vérifier IP, chemins et clé dans Proxmox Sync ; tester la connexion |
| NAS non accessible au boot | Montage réseau pas encore prêt | Vérifier `_netdev,nofail` dans `/etc/fstab` |
| Discord : HTTP 400 | URL webhook invalide ou expirée | Recréer le webhook dans les paramètres Discord |
| Debian/Fedora version non détectée | Index FTP temporairement injoignable | Le scraper réessaie au prochain cycle planifié |
| Windows DL non automatique | URL Microsoft non résolvable | Notification Discord envoyée avec lien rg-adguard |
| "Réparer DB" ne trouve rien | Fichier réellement absent du stockage | Relancer un Scan local pour ré-indexer |

---

## Stack technique

- **Backend** : Python 3.12, FastAPI, APScheduler, httpx, SQLite
- **Frontend** : HTML/CSS/JS vanilla (pas de framework), i18n FR/EN intégré
- **Runtime** : Docker, image `python:3.12-slim`
- **DB** : SQLite avec `journal_mode=DELETE` (compatible NFS/CIFS)
- **Intégration Proxmox** : SSH + script bash exécuté sur le NAS (ou depuis Proxmox si le NAS ne le permet pas) — `sync_symlinks.sh`
