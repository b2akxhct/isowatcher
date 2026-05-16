# ISOWatcher 🔭

Gestionnaire de bibliothèque ISO avec surveillance automatique des nouvelles versions et notifications Discord.

## Fonctionnalités

- **Surveillance automatique** des nouvelles versions (Ubuntu LTS, Debian Stable, + custom)
- **Téléchargement automatique** dès qu'une nouvelle ISO est détectée
- **Vérification SHA-256** automatique après chaque téléchargement
- **Notifications Discord** via webhook
- **Interface web** moderne avec suivi de progression en temps réel
- **Scheduler configurable** depuis l'interface (quotidien / hebdomadaire / mensuel)
- **Conservation de toutes les versions**
- **API REST** complète pour l'automatisation
- **Téléchargement manuel** avec URL personnalisée
- **Ajout de distributions custom** avec regex de détection de version

## Déploiement rapide sur Proxmox

### 1. Prérequis

Docker installé sur ton hôte Proxmox ou un LXC Docker :

```bash
# Si Docker n'est pas installé
curl -fsSL https://get.docker.com | sh
```

### 2.
### 3. Cloner et démarrer

```bash
git clone https://github.com/TON_REPO/isowatcher
cd isowatcher

# Adapter le chemin du volume dans docker-compose.yml
# Ligne : - /mnt/nas/isos:/data/isos

docker compose up -d
```

### 4. Accéder à l'interface

```
http://IP_DE_TON_PROXMOX:8080
```

## Configuration

### Distributions pré-configurées

| Distribution       | Détection auto | Téléchargement auto |
|--------------------|---------------|---------------------|
| Ubuntu LTS         | ✅            | ✅                  |
| Debian Stable      | ✅            | ✅                  |
| Windows Server     | ❌ (manuel)   | ❌ (URL manuelle)   |

### Ajouter une distribution custom

Dans l'interface → **Distributions** → **+ Ajouter** :

- **URL de vérification** : page HTML contenant le numéro de version
- **Regex pattern** : expression pour extraire la version (groupe 1)
- **Template URL** : URL de téléchargement avec `{version}` comme placeholder

**Exemple pour Fedora :**
- Check URL : `https://getfedora.org/`
- Pattern : `Fedora Linux (\d+)`
- Download URL : `https://download.fedoraproject.org/pub/fedora/linux/releases/{version}/Server/x86_64/iso/Fedora-Server-dvd-x86_64-{version}-1.1.iso`

### Discord Webhook

1. Dans ton serveur Discord → Paramètres du canal → Intégrations → Webhooks
2. Copie l'URL du webhook
3. Dans ISOWatcher → Paramètres → colle l'URL → Sauvegarder
4. Clique "Tester" pour vérifier

### Planification

Configurable depuis **Paramètres** :
- Fréquence : quotidienne, hebdomadaire, mensuelle
- Heure et jour précis
- Prise en compte immédiate sans redémarrage

## Structure des fichiers

```
/data/
├── isowatcher.db          # Base de données SQLite
└── isos/
    ├── ubuntu-lts/
    │   ├── 24.04/
    │   │   └── ubuntu-24.04-live-server-amd64.iso
    │   └── 22.04/
    │       └── ubuntu-22.04.4-live-server-amd64.iso
    ├── debian-stable/
    │   └── 12.5/
    │       └── debian-12.5.0-amd64-netinst.iso
    └── custom-distro/
        └── ...
```

## API REST

| Méthode | Endpoint                    | Description                      |
|---------|-----------------------------|----------------------------------|
| GET     | `/api/distros`              | Liste des distributions          |
| POST    | `/api/distros`              | Ajouter une distribution         |
| DELETE  | `/api/distros/{id}`         | Supprimer                        |
| PATCH   | `/api/distros/{id}/toggle`  | Activer/désactiver               |
| GET     | `/api/library`              | Bibliothèque ISO                 |
| POST    | `/api/check`                | Déclencher une vérification      |
| POST    | `/api/download`             | Téléchargement manuel            |
| GET     | `/api/progress`             | Progression des téléchargements  |
| GET     | `/api/stats`                | Statistiques globales            |
| GET     | `/api/logs`                 | Historique des opérations        |
| GET     | `/api/settings`             | Paramètres                       |
| PUT     | `/api/settings`             | Modifier les paramètres          |
| POST    | `/api/test-discord`         | Tester le webhook Discord        |

## Variables d'environnement

| Variable   | Défaut  | Description                    |
|------------|---------|--------------------------------|
| `DATA_DIR` | `/data` | Répertoire des données         |
| `TZ`       | `UTC`   | Fuseau horaire                 |


## Credits
Majoritairement développé avec l'assistance de Claude.AI
