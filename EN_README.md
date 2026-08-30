# ISOWatcher v2.0

Self-hosted ISO library manager. Monitors new releases of Linux distributions and Windows, downloads them automatically, verifies checksums, sends Discord notifications, and synchronizes the library with Proxmox via SSH.

Interface available in **French and English** (selector at top right).

---

## Features

### Automated Monitoring & Downloading
- Automatic detection of new releases (Ubuntu LTS/Desktop, Debian Stable, Fedora, Arch Linux)
- Downloads as soon as a new version is detected
- Dynamic filename resolution for Debian and Fedora (index scraping — prevents 404 errors due to misguessed version numbers)
- Automatic SHA-256 and MD5 verification after every download
- Configurable scheduler from the web UI (daily / weekly / monthly, exact time)

### Windows Sources via rg-adguard
- Windows Server 2025, 2022, 2019
- Windows 11, Windows 10
- Checksums automatically fetched and verified from files.rg-adguard.net
- Discord notification if the direct URL cannot be resolved (guided manual download)

### VirtIO Drivers
- Stable and latest VirtIO drivers via official Fedora URLs (permanent redirection, always up to date)

### Official FTP Archives
- Access to historical releases from official FTP servers: Debian, Ubuntu, Fedora, Arch Linux
- Visual version selection in the web interface
- Automatic resolution of the exact download URL (useful for Fedora whose filename varies)

### Local Library Scanning
- Recursive scan of `/data/isos` searching for existing `.iso` files
- Automatic identification using filename patterns (Debian, Ubuntu, Fedora, Arch, Rocky, AlmaLinux, Windows, Kali, openSUSE, Linux Mint…)
- Interactive manual assignment for unmapped files, **with the ability to create a new category on the fly** (e.g., Manjaro, Windows 7…) directly from the scan modal
- Detection of modified files (size mismatch) → marked as `modified` without overwriting existing data
- SHA-256 calculation upon import
- Filterable scan report by status

### Proxmox Integration (Proxmox Sync)
- Designed for any **Linux NAS shared over CIFS/SMB** (Unraid, TrueNAS SCALE, standard Debian/Ubuntu server, Synology/QNAP with SSH enabled…) — CIFS does not support client-side symlinks, regardless of the underlying NAS
- ISOWatcher connects via **SSH to the NAS** and executes `sync_symlinks.sh`, which creates symlinks directly on the native filesystem of the NAS (ext4, XFS, btrfs…)
- Proxmox then mounts `proxmox-view/` via CIFS from the NAS, independently of the Docker container
- SSH key generation directly from the interface, connection test, detailed synchronization report (created / updated / ignored / orphaned / name conflicts)
- Automatic database path repair if files were moved
- **If your NAS does not support script execution** (locked Synology, restricted QNAP, appliance without SSH): the exact same logic can run **from Proxmox itself**, accessing the NAS via CIFS and creating symlinks locally — see the dedicated section below

### Web Interface
- Dashboard with stats, active downloads, and recent activity
- Distribution management: add, edit, enable/disable, delete
- Library view with checksums, file size, source, and date
- Real-time download progress tracking (polling every 1.5s)
- Logs of the last 100 operations
- Built-in guide for adding custom distros with ready-to-use configuration examples
- **FR/EN language selector**, preference saved in the browser

### Discord Notifications
- Configurable webhook from the settings page
- Notification on every completed ISO download (including checksum status)
- Notification when a new Windows release is detected without a direct download link

---

## Pre-configured Distributions

| Distribution | Slug | Source | Auto-detect | Auto-DL |
|---|---|---|---|---|
| Ubuntu LTS Server | `ubuntu-lts` | Direct | ✅ | ✅ |
| Ubuntu Desktop LTS | `ubuntu-desktop-lts` | Direct | ✅ | ✅ |
| Debian Stable | `debian-stable` | `debian`¹ | ✅ | ✅ |
| Fedora Server | `fedora-server` | `fedora`¹ | ✅ | ✅ |
| Arch Linux | `arch-linux` | Direct | ✅ | ✅ |
| VirtIO Drivers (stable) | `virtio-win-stable` | `virtio` | ✅ | ✅ |
| VirtIO Drivers (latest) | `virtio-win-latest` | `virtio` | ✅ | ✅ |
| Windows Server 2025 | `windows-server-2025` | rg-adguard | ✅ | Conditional² |
| Windows Server 2022 | `windows-server-2022` | rg-adguard | ✅ | Conditional² |
| Windows Server 2019 | `windows-server-2019` | rg-adguard | ✅ | Conditional² |
| Windows 11 | `windows-11` | rg-adguard | ✅ | Conditional² |

¹ Dedicated dynamic sources: instead of a static URL template, a dedicated scraper reads the official directory index to extract the exact published filename (prevents 404 errors when version digits or build suffixes change).

² Automatic Windows downloading depends on direct URL resolution on Microsoft's side. If unresolvable, a Discord notification is sent with the rg-adguard link for manual download.

---

## Prerequisites

- Docker + Docker Compose installed on the host (Proxmox LXC recommended, or any Linux machine)
- Outbound network access to the internet (for downloads and version checks)
- A Linux NAS (Unraid, TrueNAS SCALE, Debian/Ubuntu server, Synology/QNAP with SSH enabled…) sharing a CIFS/SMB share to store the ISO files
- For Proxmox sync: either SSH root access (or a user with sufficient permissions) on the NAS, OR — if the NAS does not support SSH script execution — write access to the CIFS share directly from Proxmox (see dedicated section)

---

## Deployment

### 1. Extract files

```bash
mkdir -p /opt/isowatcher
cd /opt/isowatcher
unzip isowatcher.zip
cd isowatcher
```

### 2. Mount the CIFS NAS (ISOs only)

```bash
apt install cifs-utils -y
mkdir -p /mnt/nas/isos

cat > /etc/nas-creds << 'EOF'
username=YOUR_USERNAME
password=YOUR_PASSWORD
EOF
chmod 600 /etc/nas-creds

echo "//192.168.1.X/isos  /mnt/nas/isos  cifs  credentials=/etc/nas-creds,_netdev,nofail,uid=0,gid=0  0  0" >> /etc/fstab
mount -a
```

### 3. Adjust `docker-compose.yml`

The provided file already maps `/mnt/nas/isos` → update the real path if needed. No `proxmox-view` mount is needed inside Docker: synchronization is handled entirely over SSH to the NAS (see step 5, or the Proxmox-side alternative below if your NAS doesn't allow SSH).

### 4. Start the application

```bash
docker compose up -d
docker compose logs -f
```

### 5. Configure Proxmox Sync (SSH)

This method works for any Linux NAS with SSH access (Unraid, TrueNAS SCALE, Debian/Ubuntu, Synology/QNAP with SSH enabled…). If your NAS cannot execute scripts, skip directly to the **Alternative: Script executed from Proxmox** section.

**Copy the script to the NAS:**
```bash
scp sync_symlinks.sh root@NAS_IP:/mnt/user/isos/isowatcher/
ssh root@NAS_IP "chmod +x /mnt/user/isos/isowatcher/sync_symlinks.sh"
```

**From the ISOWatcher interface → Proxmox Sync:**
1. Click **🔑 Generate SSH Key**
2. Copy the displayed public key into `~/.ssh/authorized_keys` on the NAS
3. Fill in: NAS IP, source/destination paths, script path
4. Click **🔌 Test SSH Connection** to validate access
5. Click **↻ Synchronize** to launch the initial sync

**Declare the storage in Proxmox** (`/etc/pve/storage.cfg`), by mounting `proxmox-view` directly from the NAS via CIFS — independently of this Docker container:

```ini
dir: isowatcher
  path /mnt/proxmox-view
  content iso
```

Verify with: `pvesm list isowatcher`

### 6. Access the interface

```
http://HOST_IP:8080
```

---

## Why SSH and not a local mount?

CIFS/SMB does not support client-side symlinks — any attempt to run `ln -s`, `os.link()`, or similar commands from inside the Docker container will fail with `Errno 95: Operation not supported`. This holds true **regardless of the underlying NAS** (Unraid, TrueNAS, Synology, QNAP, standard Debian server): it is a limitation of the CIFS protocol itself, not the NAS hardware or OS.

The solution: execute symlink creation **directly on a host that sees the native filesystem** (not through CIFS). Two options depending on what your NAS allows:

### Option A — Script executed on the NAS via SSH (recommended, documented above)

If the NAS allows script execution (Unraid, TrueNAS SCALE, any Linux-based NAS with SSH enabled — including Synology/QNAP if SSH is enabled in settings), ISOWatcher connects via SSH to the NAS and triggers `sync_symlinks.sh`. The script runs natively on the NAS filesystem (ext4, XFS, btrfs…) where symlinks work normally.

```
NAS (Unraid, TrueNAS, Synology with SSH, etc.)
├── isos/<distro>/<version>/*.iso        ← real files (CIFS mounted into Docker)
└── proxmox-view/template/iso/*.iso      ← symlinks (created BY THE NAS via SSH)

Proxmox: mounts proxmox-view/ via CIFS directly from the NAS
Docker (ISOWatcher): never touches proxmox-view locally, only triggers operations via SSH
```

### Option B — Script executed from Proxmox itself (Locked NAS, without SSH)

If the NAS allows neither SSH access nor script execution (locked appliance, Synology with SSH disabled by policy, etc.), the same symlink logic can be executed **from Proxmox**, provided Proxmox accesses the NAS share in a way that supports symlinks — typically by **mounting the share using NFS** instead of CIFS (NFS natively supports symlinks), or using a CIFS mount with Unix extensions enabled on the server side if supported.

Steps:
1. Mount the ISOs share via **NFS** on Proxmox (instead of CIFS): `mount -t nfs NAS_IP:/volume1/isos /mnt/isos`
2. Copy `sync_symlinks.sh` directly onto Proxmox and run it locally (or via a cron job) instead of via SSH to the NAS:
   ```bash
   ISOWATCHER_SOURCE_DIR=/mnt/isos/isos    ISOWATCHER_DEST_DIR=/mnt/isos/proxmox-view/template/iso    bash sync_symlinks.sh
   ```
3. Declare the Proxmox storage directly pointing to this local NFS mount point — no second separate CIFS mount needed for `proxmox-view`.

In this scenario, ISOWatcher's SSH configuration (Proxmox Sync page) is not needed: Proxmox handles synchronization locally, for example via a scheduled task (`cron`) triggered manually or after each ISOWatcher check cycle.

Key takeaway: **regardless of the NAS brand**, what matters is knowing (a) if you can execute a script on the NAS itself via SSH, and if not, (b) if you can mount the share via NFS on Proxmox to bypass the CIFS limitation. ISOWatcher documents and automates Option A; Option B is configured manually on the Proxmox side using the provided script.

---

## NFS / CIFS Database Compatibility

The code incorporates two SQLite optimizations specifically designed for network shares:

```python
PRAGMA journal_mode=DELETE;   # Avoids WAL mode, which is incompatible with network shares
PRAGMA busy_timeout=30000;    # Tolerates up to 30s of network latency
```

**Strong recommendation**: Keep the SQLite database stored locally (`./data`), and only network-mount the ISO directory — this is the default setup in the provided `docker-compose.yml`.

---

## Storage Structure

```
/data/
├── isowatcher.db              # SQLite Database (Local)
├── ssh/                       # Generated SSH key pair (id_rsa / id_rsa.pub)
└── isos/                      # Mounted from CIFS NAS
    ├── ubuntu-lts/24.04/ubuntu-24.04-live-server-amd64.iso
    ├── ubuntu-desktop-lts/24.04/ubuntu-24.04-desktop-amd64.iso
    ├── debian-stable/13.5/debian-13.5.0-amd64-netinst.iso
    ├── fedora-server/42/Fedora-Server-dvd-x86_64-42-1.2.iso
    ├── arch-linux/2025.08.01/archlinux-2025.08.01-x86_64.iso
    ├── virtio-win-stable/.../virtio-win-x.x.x.iso
    └── windows-server-2025/.../*.iso
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `/data` | Root directory for data (DB + ISOs) |
| `TZ` | `Europe/Paris` | Timezone used by the scheduler |

---

## REST API

### Distributions
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/distros` | List all distributions |
| `POST` | `/api/distros` | Add a distribution |
| `PUT` | `/api/distros/{id}` | Update a distribution |
| `DELETE` | `/api/distros/{id}` | Delete a distribution |
| `PATCH` | `/api/distros/{id}/toggle` | Toggle enable / disable |
| `GET` | `/api/check-version/{id}` | Check latest available version |
| `GET` | `/api/rg-sources` | List available rg-adguard slugs |

### Library & Downloads
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/library` | List ISOs stored in library |
| `POST` | `/api/check` | Trigger manual check for all distros |
| `POST` | `/api/download` | Manual download (`distro_id`, `version`, `url`) |
| `GET` | `/api/progress` | Get active download progress |

### FTP Archives
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/archive` | List available archive sources |
| `GET` | `/api/archive/{key}` | Get historical versions for a distro (`?limit=50`) |
| `GET` | `/api/archive/{key}/{version}/url` | Resolve exact download URL |
| `POST` | `/api/archive/download` | Download an archived ISO |

### Local Scan
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/scan/start` | Trigger local library scan |
| `GET` | `/api/scan/status` | Get scan status (progress, results, unmapped files) |
| `POST` | `/api/scan/assign` | Manually assign distro + version to a file |
| `POST` | `/api/scan/ignore` | Ignore an unmapped file |
| `POST` | `/api/scan/create-category` | Create a new category on the fly |

### Proxmox Sync (SSH)
| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/symlinks/sync` | Trigger `sync_symlinks.sh` on the NAS via SSH |
| `GET` | `/api/symlinks/status` | Get SSH config status + actual remote directory listing |
| `POST` | `/api/symlinks/test-ssh` | Test SSH connectivity |
| `POST` | `/api/symlinks/generate-key` | Generate SSH key pair |
| `PUT` | `/api/symlinks/config` | Update SSH host/user/port/paths configuration |
| `DELETE` | `/api/symlinks/clean` | Run sync to clean up orphaned symlinks |
| `POST` | `/api/symlinks/repair` | Repair DB file paths then trigger re-sync |

### Settings & Tools
| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | Global system statistics |
| `GET` | `/api/logs` | Fetch last 100 log entries |
| `GET` | `/api/settings` | Current settings |
| `PUT` | `/api/settings` | Update settings |
| `POST` | `/api/test-discord` | Test Discord webhook |

---

## Adding a Custom Distribution

### Direct Mode (Linux, BSD…)

Three required fields (see also the built-in guide in the web UI, featuring examples for Rocky Linux, AlmaLinux, openSUSE, Kali, Linux Mint):

```
Name          : Rocky Linux 9
Slug          : rocky-linux-9
Check URL     : https://dl.rockylinux.org/pub/rocky/
Regex         : href="(9\.\d+)/?"
Template URL  : https://dl.rockylinux.org/pub/rocky/{version}/isos/x86_64/Rocky-{version}-x86_64-minimal.iso
```

The `{version}` placeholder is automatically populated upon detection.

### rg-adguard Mode (Windows)

Select `rg-adguard` source in the form and pick the desired Windows slug from the dropdown. No target URL required.

### From Local Scan

During a scan, any unrecognized `.iso` file can be assigned to an existing distribution **or** mapped to an entirely new category created directly from the assignment modal (Name, slug, type, architecture) — useful for quickly importing one-off ISOs (Manjaro, Windows 7, etc.) without going through the full creation wizard.

---

## Useful Commands

```bash
# View real-time logs
docker compose -f /opt/isowatcher/isowatcher/docker-compose.yml logs -f

# Rebuild and restart after updates
docker compose -f /opt/isowatcher/isowatcher/docker-compose.yml up -d --build

# Stop container stack
docker compose -f /opt/isowatcher/isowatcher/docker-compose.yml down

# Disk space used by ISOs
du -sh /mnt/nas/isos/

# Verify NAS mount
mountpoint -q /mnt/nas/isos && echo "Mounted" || echo "Not mounted"

# Test SSH connection to NAS manually
ssh -i ./data/ssh/id_rsa root@NAS_IP echo OK
```

---

## Troubleshooting

| Issue | Probable Cause | Solution |
|---|---|---|
| Port 8080 already in use | Conflict with another service | Change `"8080:8080"` to `"8090:8080"` in `docker-compose.yml` |
| `database is locked` on startup | DB located on slow network share | Keep `./data` local (see NFS/CIFS section) |
| Symlinks not created, `Errno 95` | Attempted creation via Docker/CIFS | Ensure sync runs over SSH (Proxmox Sync page → Test SSH) |
| NAS has no SSH available (locked Synology, closed appliance…) | NAS cannot execute scripts | Use Option B (script executed on Proxmox side via NFS mount) — see "Why SSH and not a local mount" |
| `proxmox-view` storage always empty | SSH not configured or invalid remote path | Verify IP, paths, and key settings in Proxmox Sync; test SSH connection |
| NAS unavailable at system boot | Network mount not ready yet | Verify `_netdev,nofail` flags in `/etc/fstab` |
| Discord: HTTP 400 | Invalid or expired webhook URL | Recreate the webhook in Discord channel settings |
| Debian/Fedora version not detected | FTP index temporarily unreachable | Scraper will retry during the next scheduled cycle |
| Windows DL not automatic | Microsoft direct URL unresolvable | Discord notification sent containing rg-adguard link |
| "Repair DB" finds nothing | File missing from storage | Run a Local Scan to re-index files |

---

## Tech Stack

- **Backend**: Python 3.12, FastAPI, APScheduler, httpx, SQLite
- **Frontend**: Vanilla HTML/CSS/JS (no framework), built-in FR/EN i18n
- **Runtime**: Docker, `python:3.12-slim` image
- **Database**: SQLite configured with `journal_mode=DELETE` (NFS/CIFS compatible)
- **Proxmox Integration**: SSH + Bash script executed on the NAS (or from Proxmox if NAS restricts SSH) — `sync_symlinks.sh`
