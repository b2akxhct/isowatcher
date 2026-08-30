import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional
import threading

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ISOWatcher", version="2.0.0")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
templates = Jinja2Templates(directory="/app/templates")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
ISO_DIR  = DATA_DIR / "isos"
DB_PATH  = DATA_DIR / "isowatcher.db"
ISO_DIR.mkdir(parents=True, exist_ok=True)

scheduler        = BackgroundScheduler()
download_progress = {}

# ══════════════════════════════════════════════════════════════════════════════
#  Module rg-adguard (Windows)
# ══════════════════════════════════════════════════════════════════════════════

RG_BASE = "https://files.rg-adguard.net"

RG_CATEGORIES = {
    "windows-server-2025": {
        "name": "Windows Server 2025",
        "version_page_uuid": "f0bd8307-d897-ef77-dbd6-216fefbe94c5",
        "search_pattern": r"Windows Server 2025",
        "lang_preference": ["English", "French"],
        "edition_preference": ["SERVERSTANDARD", "DATACENTER", "SERVER"],
    },
    "windows-server-2022": {
        "name": "Windows Server 2022",
        "version_page_uuid": "f0bd8307-d897-ef77-dbd6-216fefbe94c5",
        "search_pattern": r"Windows Server 2022",
        "lang_preference": ["English", "French"],
        "edition_preference": ["SERVERSTANDARD", "DATACENTER", "SERVER"],
    },
    "windows-server-2019": {
        "name": "Windows Server 2019",
        "version_page_uuid": "f0bd8307-d897-ef77-dbd6-216fefbe94c5",
        "search_pattern": r"Windows Server 2019",
        "lang_preference": ["English", "French"],
        "edition_preference": ["SERVERSTANDARD", "DATACENTER", "SERVER"],
    },
    "windows-11": {
        "name": "Windows 11",
        "version_page_uuid": "f0bd8307-d897-ef77-dbd6-216fefbe94c5",
        "search_pattern": r"Windows 11,? version",
        "lang_preference": ["English", "French"],
        "edition_preference": [],
    },
    "windows-10": {
        "name": "Windows 10",
        "version_page_uuid": "f0bd8307-d897-ef77-dbd6-216fefbe94c5",
        "search_pattern": r"Windows 10,? version",
        "lang_preference": ["English", "French"],
        "edition_preference": [],
    },
}

async def rg_get_versions(category_uuid: str, search_pattern: str) -> list:
    url = f"{RG_BASE}/version/{category_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url)
            matches = re.findall(
                r'\[([^\]]+)\]\(https://files\.rg-adguard\.net/language/([a-f0-9\-]+)\)', r.text)
            return [{"label": lbl.strip(), "uuid": uid}
                    for lbl, uid in matches
                    if re.search(search_pattern, lbl, re.IGNORECASE)]
    except Exception as e:
        logger.error(f"rg version list: {e}")
        return []

async def rg_get_lang_uuid(version_uuid: str, lang_prefs: list) -> Optional[tuple]:
    url = f"{RG_BASE}/language/{version_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url)
            matches = re.findall(
                r'\[([^\]]+)\]\(https://files\.rg-adguard\.net/files/([a-f0-9\-]+)\)', r.text)
            for pref in lang_prefs:
                for lbl, uid in matches:
                    if pref.lower() in lbl.lower():
                        return (lbl, uid)
            return (matches[0][0], matches[0][1]) if matches else None
    except Exception as e:
        logger.error(f"rg lang page: {e}")
    return None

async def rg_get_file_uuid(lang_uuid: str, edition_prefs: list) -> Optional[tuple]:
    url = f"{RG_BASE}/files/{lang_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url)
            matches = re.findall(
                r'\[([^\]]+\.iso)\]\(https://files\.rg-adguard\.net/file/([a-f0-9\-]+)\)',
                r.text, re.IGNORECASE)
            if not matches:
                return None
            for pref in edition_prefs:
                for fn, uid in matches:
                    if pref.upper() in fn.upper():
                        return (fn, uid)
            return (matches[0][0], matches[0][1])
    except Exception as e:
        logger.error(f"rg files page: {e}")
    return None

async def rg_get_file_info(file_uuid: str) -> Optional[dict]:
    url = f"{RG_BASE}/file/{file_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url)
            t = r.text
            def ex(pat): m = re.search(pat, t); return m.group(1).strip() if m else None
            fn = ex(r'\*\*File\*\*:\s*\|\s*([^\|\n<]+\.iso)')
            if not fn:
                m = re.search(r'title:\s*([^\n:]+\.iso)', t, re.IGNORECASE)
                fn = m.group(1).strip() if m else None
            return {
                "filename":   fn,
                "sha256":     ex(r'\*\*SHA-256\*\*:\s*\|\s*([a-f0-9]{64})'),
                "md5":        ex(r'\*\*MD5\*\*:\s*\|\s*([a-f0-9]{32})'),
                "size_bytes": int(ex(r'Size\*\*:\s*\|[^(]+\((\d+)\s*bytes\)') or 0) or None,
                "info_url":   url,
                "file_uuid":  file_uuid,
            }
    except Exception as e:
        logger.error(f"rg file info: {e}")
    return None

async def rg_get_direct_download_url(file_uuid: str) -> Optional[str]:
    url = f"{RG_BASE}/file/{file_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                      headers={"User-Agent": "ISOWatcher/1.2"}) as client:
            r = await client.get(url)
            m = re.search(r'href=["\']((https?://[^"\']+\.iso))["\']', r.text, re.IGNORECASE)
            if m: return m.group(2)
            m = re.search(r'href=["\']((https?://files\.rg-adguard\.net/download/[^"\']+))', r.text)
            if m: return m.group(2)
    except Exception as e:
        logger.error(f"rg direct url: {e}")
    return None

async def rg_resolve_latest(slug: str) -> Optional[dict]:
    cfg = RG_CATEGORIES.get(slug)
    if not cfg:
        return None
    logger.info(f"[rg] Résolution {slug}…")
    versions = await rg_get_versions(cfg["version_page_uuid"], cfg["search_pattern"])
    if not versions:
        logger.warning(f"[rg] Aucune version pour {slug}")
        return None
    latest = versions[0]
    lang = await rg_get_lang_uuid(latest["uuid"], cfg["lang_preference"])
    if not lang:
        return None
    lang_label, lang_uuid = lang
    file_raw = await rg_get_file_uuid(lang_uuid, cfg["edition_preference"])
    if not file_raw:
        return None
    filename, file_uuid = file_raw
    meta = await rg_get_file_info(file_uuid)
    return {
        "version_label": latest["label"],
        "filename":      filename,
        "sha256":        meta.get("sha256") if meta else None,
        "md5":           meta.get("md5") if meta else None,
        "size_bytes":    meta.get("size_bytes") if meta else None,
        "info_url":      f"{RG_BASE}/file/{file_uuid}",
        "file_uuid":     file_uuid,
        "lang":          lang_label,
    }

# ══════════════════════════════════════════════════════════════════════════════
#  Archive FTP — catalogue des versions historiques
# ══════════════════════════════════════════════════════════════════════════════

# Chaque entrée décrit comment lister les versions disponibles sur le FTP officiel
ARCHIVE_SOURCES = {
    "debian": {
        "name": "Debian",
        # Archive Bookworm (12.x) + Trixie (13.x) quand disponible
        "ftp_indexes": [
            "https://cdimage.debian.org/cdimage/archive/",          # anciennes stables
            "https://cdimage.debian.org/debian-cd/",                # releases récentes
        ],
        "version_pattern": r'href="(\d+\.\d+\.\d+)/"',
        "iso_url_fn": lambda v: (
            f"https://cdimage.debian.org/cdimage/archive/{v}/amd64/iso-cd/debian-{v}-amd64-netinst.iso"
            if not v.startswith("13")
            else f"https://cdimage.debian.org/debian-cd/{v}/amd64/iso-cd/debian-{v}-amd64-netinst.iso"
        ),
    },
    "ubuntu": {
        "name": "Ubuntu",
        "ftp_indexes": [
            "https://old-releases.ubuntu.com/releases/",
            "https://releases.ubuntu.com/",
        ],
        "version_pattern": r'href="(\d+\.\d+(?:\.\d+)?)/"',
        "iso_url_fn": lambda v: (
            f"https://old-releases.ubuntu.com/releases/{v}/ubuntu-{v}-live-server-amd64.iso"
            if tuple(int(x) for x in v.split(".")[:2]) < (22, 4)
            else f"https://releases.ubuntu.com/{v}/ubuntu-{v}-live-server-amd64.iso"
        ),
    },
    "fedora": {
        "name": "Fedora",
        "ftp_indexes": [
            "https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/",
            "https://dl.fedoraproject.org/pub/fedora/linux/releases/",
        ],
        "version_pattern": r'href="(\d{2,3})/"',
        # URL dynamique : on scrape l'index ISO pour trouver le vrai nom de fichier
        "iso_url_fn": None,  # géré séparément par fetch_fedora_iso_url()
    },
    "arch": {
        "name": "Arch Linux",
        "ftp_indexes": [
            "https://archive.archlinux.org/iso/",
        ],
        "version_pattern": r'href="(\d{4}\.\d{2}\.\d{2})/"',
        "iso_url_fn": lambda v: f"https://archive.archlinux.org/iso/{v}/archlinux-{v}-x86_64.iso",
    },
}

async def fetch_fedora_iso_url(version: str) -> Optional[str]:
    """Scrape le répertoire ISO Fedora pour trouver le vrai nom de fichier."""
    bases = [
        f"https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/{version}/Server/x86_64/iso/",
        f"https://dl.fedoraproject.org/pub/fedora/linux/releases/{version}/Server/x86_64/iso/",
    ]
    for base in bases:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True,
                                          headers={"User-Agent": "ISOWatcher/1.3"}) as client:
                r = await client.get(base)
                m = re.search(r'href="(Fedora-Server-(?:dvd|netinst)-x86_64-[\d\.]+-[\d\.]+\.iso)"',
                               r.text, re.IGNORECASE)
                if m:
                    return base + m.group(1)
        except Exception:
            continue
    return None

async def fetch_archive_versions(distro_key: str, limit: int = 50) -> list:
    """Récupère les versions historiques disponibles sur les FTP officiels."""
    src = ARCHIVE_SOURCES.get(distro_key)
    if not src:
        return []
    seen, unique = set(), []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                  headers={"User-Agent": "ISOWatcher/1.3"}) as client:
        for index_url in src["ftp_indexes"]:
            try:
                r = await client.get(index_url)
                versions = re.findall(src["version_pattern"], r.text)
                for v in versions:
                    if v not in seen:
                        seen.add(v)
                        unique.append(v)
            except Exception as e:
                logger.warning(f"Archive fetch {distro_key} ({index_url}): {e}")

    # Tri décroissant selon le type de version
    def sort_key(v):
        try:
            return tuple(int(x) for x in v.replace("-", ".").split("."))
        except Exception:
            return (0,)
    unique.sort(key=sort_key, reverse=True)
    return unique[:limit]

async def resolve_archive_url(distro_key: str, version: str) -> Optional[str]:
    """Résout l'URL de téléchargement pour une version d'archive."""
    src = ARCHIVE_SOURCES.get(distro_key)
    if not src:
        return None
    if distro_key == "fedora":
        return await fetch_fedora_iso_url(version)
    if src.get("iso_url_fn"):
        try:
            return src["iso_url_fn"](version)
        except Exception as e:
            logger.error(f"Archive URL build {distro_key} {version}: {e}")
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  Base de données (CIFS-safe — journal DELETE + timeout 30s)
# ══════════════════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    logger.info("Initialisation de la base de données…")
    conn = get_db()
    try:
        conn.execute("PRAGMA journal_mode=DELETE;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS distros (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL DEFAULT 'linux',
                source TEXT DEFAULT 'direct',
                check_url TEXT,
                download_url_template TEXT,
                version_pattern TEXT,
                arch TEXT DEFAULT 'amd64',
                enabled INTEGER DEFAULT 1,
                latest_version TEXT,
                last_checked TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS iso_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                distro_id INTEGER REFERENCES distros(id),
                version TEXT NOT NULL,
                arch TEXT,
                filename TEXT NOT NULL,
                filepath TEXT NOT NULL,
                size_bytes INTEGER,
                checksum_sha256 TEXT,
                checksum_md5 TEXT,
                download_url TEXT,
                source TEXT DEFAULT 'direct',
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'complete'
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS download_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                distro_id INTEGER,
                version TEXT,
                status TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO settings VALUES ('schedule_hour',    '3');
            INSERT OR IGNORE INTO settings VALUES ('schedule_minute',  '0');
            INSERT OR IGNORE INTO settings VALUES ('schedule_days',    'mon');
            INSERT OR IGNORE INTO settings VALUES ('discord_webhook',  '');
            INSERT OR IGNORE INTO settings VALUES ('check_frequency',  'weekly');
            INSERT OR IGNORE INTO settings VALUES ('ssh_host',         '');
            INSERT OR IGNORE INTO settings VALUES ('ssh_user',         'root');
            INSERT OR IGNORE INTO settings VALUES ('ssh_port',         '22');
            INSERT OR IGNORE INTO settings VALUES ('ssh_key_path',     '/data/ssh/id_rsa');
            INSERT OR IGNORE INTO settings VALUES ('ssh_script_path',  '/mnt/user/isos/isowatcher/sync_symlinks.sh');
            INSERT OR IGNORE INTO settings VALUES ('ssh_source_dir',   '/mnt/user/isos/isos');
            INSERT OR IGNORE INTO settings VALUES ('ssh_dest_dir',     '/mnt/user/isos/proxmox-view/template/iso');
        """)
        conn.commit()

        # ── Distros pré-configurées ───────────────────────────────────────────
        distros = [
            # ── Linux ──────────────────────────────────────────────────────────
            ("Ubuntu LTS Server",   "ubuntu-lts",        "linux",   "direct",
             "https://changelogs.ubuntu.com/meta-release-lts",
             "https://releases.ubuntu.com/{version}/ubuntu-{version}-live-server-amd64.iso",
             r"Version:\s+(\d+\.\d+)"),

            ("Ubuntu Desktop LTS",  "ubuntu-desktop-lts","linux",   "direct",
             "https://changelogs.ubuntu.com/meta-release-lts",
             "https://releases.ubuntu.com/{version}/ubuntu-{version}-desktop-amd64.iso",
             r"Version:\s+(\d+\.\d+)"),

            # FIX Debian : le checker retourne "13.4" mais le fichier ISO
            # s'appelle "debian-13.4.0-amd64-netinst.iso" (3 chiffres).
            # On utilise un checker dédié qui résout le nom exact du fichier
            # depuis l'index /current/ — source = "debian" pour activer ce chemin.
            ("Debian Stable",       "debian-stable",     "linux",   "debian",
             "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/",
             None,   # URL construite dynamiquement par get_latest_debian_stable()
             None),

            # FIX Fedora : source="fedora" active get_latest_fedora() qui scrappe
            # l'index du répertoire ISO pour trouver le vrai nom de fichier.
            # Le template hardcodé avec -1.1 est faux pour Fedora 42+ (peut être -1.2 etc.)
            ("Fedora Server",       "fedora-server",     "linux",   "fedora",
             "https://dl.fedoraproject.org/pub/fedora/linux/releases/",
             None,   # URL construite dynamiquement par get_latest_fedora()
             None),

            ("Arch Linux",          "arch-linux",        "linux",   "direct",
             "https://archlinux.org/download/",
             "https://mirror.rackspace.com/archlinux/iso/{version}/archlinux-{version}-x86_64.iso",
             r"Current Release:\s*</strong>\s*(\d{4}\.\d{2}\.\d{2})"),

            # ── VirtIO drivers (Fedora/Red Hat — URL stable permanente) ────────
            # La source "virtio" active un checker dédié qui résout le vrai
            # numéro de version via HEAD sur l'URL de redirection.
            ("VirtIO Drivers (stable)", "virtio-win-stable", "windows", "virtio",
             "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso",
             None, None),

            ("VirtIO Drivers (latest)", "virtio-win-latest", "windows", "virtio",
             "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest-virtio/virtio-win.iso",
             None, None),

            # ── Windows rg-adguard ─────────────────────────────────────────────
            ("Windows Server 2025", "windows-server-2025","windows","rg-adguard", None, None, None),
            ("Windows Server 2022", "windows-server-2022","windows","rg-adguard", None, None, None),
            ("Windows Server 2019", "windows-server-2019","windows","rg-adguard", None, None, None),
            ("Windows 11",          "windows-11",         "windows","rg-adguard", None, None, None),
        ]
        for d in distros:
            conn.execute("""
                INSERT OR IGNORE INTO distros
                  (name,slug,type,source,check_url,download_url_template,version_pattern)
                VALUES (?,?,?,?,?,?,?)
            """, d)
        conn.commit()

        # ── Migration sources : mettre à jour les distros existantes ──────────
        # Fedora : passer de 'direct' à 'fedora' pour activer le scraping d'index
        conn.execute("""
            UPDATE distros SET source='fedora', download_url_template=NULL, version_pattern=NULL
            WHERE slug='fedora-server' AND source='direct'
        """)
        # Debian : passer de 'direct' à 'debian' pour activer le scraping d'index
        conn.execute("""
            UPDATE distros SET source='debian', download_url_template=NULL, version_pattern=NULL
            WHERE slug='debian-stable' AND source='direct'
        """)
        conn.commit()
        logger.info("DB initialisée avec succès.")
    except Exception as e:
        logger.error(f"Erreur init DB (CIFS lock?): {e}")
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════════════════════════
#  Version checkers
# ══════════════════════════════════════════════════════════════════════════════

async def get_latest_ubuntu_lts() -> Optional[str]:
    """Retourne la dernière LTS Ubuntu (ex: 24.04)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://changelogs.ubuntu.com/meta-release-lts")
            versions = re.findall(r"Version:\s+(\d+\.\d+)", r.text)
            if versions:
                return sorted(versions, key=lambda v: tuple(map(int, v.split("."))))[-1]
    except Exception as e:
        logger.error(f"Ubuntu check: {e}")
    return None

async def get_latest_debian_stable() -> Optional[dict]:
    """
    FIX COMPLET : Scrape l'index /current/ pour trouver le vrai nom de fichier ISO
    (ex: debian-13.4.0-amd64-netinst.iso) sans le construire manuellement.
    Retourne {"version": "13.4.0", "url": "https://...", "filename": "debian-...iso"}
    """
    index_url = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/"
    fallback   = "https://cdimage.debian.org/cdimage/release/current/amd64/iso-cd/"
    for url in [index_url, fallback]:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                          headers={"User-Agent": "ISOWatcher/1.3"}) as client:
                r = await client.get(url)
                # Trouver le fichier netinst amd64 directement dans la liste
                m = re.search(r'href="(debian-(\d+\.\d+(?:\.\d+)?)-amd64-netinst\.iso)"',
                               r.text, re.IGNORECASE)
                if m:
                    filename = m.group(1)
                    version  = m.group(2)
                    return {
                        "version":  version,
                        "url":      url + filename,
                        "filename": filename,
                    }
        except Exception as e:
            logger.warning(f"Debian index check ({url}): {e}")
    return None

async def get_latest_fedora() -> Optional[dict]:
    """
    FIX COMPLET : Détecte le dernier Fedora ET scrape l'index du répertoire ISO
    pour trouver le vrai nom de fichier (ex: Fedora-Server-dvd-x86_64-44-1.1.iso).
    Retourne {"version": "44", "url": "https://...", "filename": "Fedora-...iso"}
    """
    base = "https://dl.fedoraproject.org/pub/fedora/linux/releases/"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                      headers={"User-Agent": "ISOWatcher/1.3"}) as client:
            # 1. Trouver la dernière version
            r = await client.get(base)
            versions = re.findall(r'href="(\d{2,3})/"', r.text)
            if not versions:
                return None
            latest = sorted(versions, key=int)[-1]

            # 2. Scraper le répertoire ISO pour trouver le vrai nom de fichier
            iso_index = f"{base}{latest}/Server/x86_64/iso/"
            r2 = await client.get(iso_index)
            # Chercher le DVD server (pas le netinst)
            m = re.search(
                r'href="(Fedora-Server-dvd-x86_64-[\d\.]+-[\d\.]+\.iso)"',
                r2.text, re.IGNORECASE
            )
            if m:
                filename = m.group(1)
                return {
                    "version":  latest,
                    "url":      iso_index + filename,
                    "filename": filename,
                }
            # Fallback : netinst si pas de DVD
            m2 = re.search(
                r'href="(Fedora-Server-netinst-x86_64-[\d\.]+-[\d\.]+\.iso)"',
                r2.text, re.IGNORECASE
            )
            if m2:
                filename = m2.group(1)
                return {
                    "version":  latest,
                    "url":      iso_index + filename,
                    "filename": filename,
                }
    except Exception as e:
        logger.error(f"Fedora check: {e}")
    return None

async def get_latest_arch() -> Optional[str]:
    """Récupère la date de release d'Arch Linux (format YYYY.MM.DD)."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get("https://archlinux.org/download/")
            m = re.search(r"Current Release:\s*</strong>\s*(\d{4}\.\d{2}\.\d{2})", r.text)
            if m:
                return m.group(1)
            # Fallback : lister le miroir
            r2 = await client.get("https://mirror.rackspace.com/archlinux/iso/latest/")
            m2 = re.search(r'href="archlinux-(\d{4}\.\d{2}\.\d{2})-x86_64\.iso"', r2.text)
            if m2:
                return m2.group(1)
    except Exception as e:
        logger.error(f"Arch check: {e}")
    return None

async def get_latest_version(distro: sqlite3.Row) -> Optional[str]:
    """Retourne uniquement la version string."""
    slug   = distro["slug"]
    source = distro["source"] if "source" in distro.keys() else "direct"

    if source == "rg-adguard":
        meta = await rg_resolve_latest(slug)
        return meta["version_label"] if meta else None
    if source == "virtio":
        meta = await get_latest_virtio(distro["check_url"])
        return meta["version"] if meta else None
    if source == "debian":
        meta = await get_latest_debian_stable()
        return meta["version"] if meta else None
    if source == "fedora":
        meta = await get_latest_fedora()
        return meta["version"] if meta else None

    # Checkers par slug pour les distros "direct"
    if slug in ("ubuntu-lts", "ubuntu-desktop-lts"):
        return await get_latest_ubuntu_lts()
    if slug == "arch-linux":
        return await get_latest_arch()

    # Checker générique via check_url + version_pattern
    if distro["check_url"] and distro["version_pattern"]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(distro["check_url"])
                m = re.search(distro["version_pattern"], r.text)
                if m:
                    return m.group(1)
        except Exception as e:
            logger.error(f"Generic check {slug}: {e}")
    return None

async def resolve_download_url(distro: sqlite3.Row, version: str) -> Optional[str]:
    """
    Résout l'URL de téléchargement réelle selon la source.
    Debian et Fedora re-scrappent l'index pour avoir le nom de fichier exact.
    """
    slug   = distro["slug"]
    source = distro["source"] if "source" in distro.keys() else "direct"

    if source == "virtio":
        meta = await get_latest_virtio(distro["check_url"])
        return meta["url"] if meta else None
    if source == "debian":
        meta = await get_latest_debian_stable()
        return meta["url"] if meta else None
    if source == "fedora":
        meta = await get_latest_fedora()
        return meta["url"] if meta else None

    return build_download_url(distro, version)

def build_download_url(distro: sqlite3.Row, version: str) -> Optional[str]:
    if not distro["download_url_template"]:
        return None
    return distro["download_url_template"].replace("{version}", version)

async def get_latest_virtio(stable_url: str) -> Optional[dict]:
    """
    Résout la version réelle de virtio-win en suivant la redirection HTTP.
    L'URL stable/latest redirige vers le vrai fichier versionné.
    """
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                      headers={"User-Agent": "ISOWatcher/1.3"}) as client:
            r = await client.head(stable_url)
            final_url = str(r.url)
            m = re.search(r'virtio-win-([\d\.]+)\.iso', final_url, re.IGNORECASE)
            version = m.group(1) if m else "latest"
            return {"version": version, "url": final_url}
    except Exception as e:
        logger.error(f"VirtIO check: {e}")
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  Downloader
# ══════════════════════════════════════════════════════════════════════════════

def compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
    return h.hexdigest()

def compute_md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
    return h.hexdigest()

def download_iso_sync(distro_id: int, version: str, url: str, track_id: str,
                      expected_sha256: str = None, expected_md5: str = None,
                      source: str = "direct"):
    conn = get_db()
    distro = conn.execute("SELECT * FROM distros WHERE id=?", (distro_id,)).fetchone()
    if not distro:
        conn.close()
        return

    slug         = distro["slug"]
    safe_version = re.sub(r'[^\w\.\-]', '_', version)[:80]
    dest_dir     = ISO_DIR / slug / safe_version
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename     = url.split("/")[-1].split("?")[0] or f"{slug}-{safe_version}.iso"
    if not filename.lower().endswith(".iso"):
        filename += ".iso"
    filepath = dest_dir / filename

    download_progress[track_id] = {
        "distro": distro["name"], "version": version, "filename": filename,
        "percent": 0, "status": "downloading", "source": source,
        "size": "", "downloaded": "",
    }

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ISOWatcher/1.2"})
        with urllib.request.urlopen(req) as response:
            total = int(response.headers.get("Content-Length", 0))
            count = 0
            with open(str(filepath), "wb") as f:
                while True:
                    block = response.read(65536)
                    if not block:
                        break
                    f.write(block)
                    count += 1
                    if total > 0:
                        dl = count * 65536
                        download_progress[track_id].update({
                            "percent":    min(int(dl * 100 / total), 100),
                            "size":       f"{total/1e9:.2f} GB",
                            "downloaded": f"{min(dl,total)/1e9:.2f} GB",
                        })

        size   = filepath.stat().st_size
        sha256 = compute_sha256(str(filepath))
        md5    = compute_md5(str(filepath))

        ok, msg = True, ""
        if expected_sha256 and sha256 != expected_sha256.lower():
            ok, msg = False, f"SHA-256 mismatch! {expected_sha256[:16]}… vs {sha256[:16]}…"
        elif expected_md5 and md5 != expected_md5.lower():
            ok, msg = False, f"MD5 mismatch! {expected_md5} vs {md5}"

        conn.execute("""
            INSERT INTO iso_library
              (distro_id,version,arch,filename,filepath,size_bytes,
               checksum_sha256,checksum_md5,download_url,source,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (distro_id, version, distro["arch"], filename, str(filepath),
              size, sha256, md5, url, source, "complete" if ok else "checksum_warning"))
        conn.execute("UPDATE distros SET latest_version=?,last_checked=? WHERE id=?",
                     (version, datetime.now().isoformat(), distro_id))
        log_msg = f"Downloaded {filename} ({size/1e9:.2f} GB)"
        if msg: log_msg += f" | ⚠ {msg}"
        conn.execute("INSERT INTO download_log (distro_id,version,status,message) VALUES (?,?,?,?)",
                     (distro_id, version, "success" if ok else "warning", log_msg))
        conn.commit()

        download_progress[track_id]["status"]  = "complete"
        download_progress[track_id]["percent"] = 100
        if msg: download_progress[track_id]["warning"] = msg

        # FIX Discord
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        webhook  = settings.get("discord_webhook", "").strip()
        if webhook:
            _discord_downloaded(webhook, distro["name"], version, filename, size, ok, source)

        # Sync proxmox-view via SSH après chaque téléchargement
        # (non bloquant — s'exécute en arrière-plan)
        threading.Thread(target=sync_via_ssh, daemon=True).start()

    except Exception as e:
        logger.error(f"Download error: {e}")
        download_progress[track_id]["status"] = "error"
        download_progress[track_id]["error"]  = str(e)
        conn.execute("INSERT INTO download_log (distro_id,version,status,message) VALUES (?,?,?,?)",
                     (distro_id, version, "error", str(e)))
        conn.commit()
    finally:
        conn.close()

# ══════════════════════════════════════════════════════════════════════════════
#  Discord helpers
#  FIX : utilisation de httpx au lieu de urllib pour le webhook
#  (urllib échoue parfois sur les redirections Discord)
# ══════════════════════════════════════════════════════════════════════════════

def _send_discord(webhook: str, payload: dict):
    """Envoi Discord robuste via urllib avec Content-Type correct."""
    if not webhook or not webhook.startswith("http"):
        logger.warning("Discord webhook invalide ou vide, notification ignorée.")
        return
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req  = urllib.request.Request(
            webhook, data=data,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "ISOWatcher/1.2"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info(f"Discord: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        logger.error(f"Discord HTTPError {e.code}: {body}")
    except Exception as e:
        logger.error(f"Discord send error: {e}")

def _discord_downloaded(webhook, name, version, filename, size, ok=True, source="direct"):
    src_label = {"rg-adguard": "rg-adguard (OEM Microsoft)",
                 "manual": "Manuel"}.get(source, "Direct")
    _send_discord(webhook, {"embeds": [{"title": f"{'✅' if ok else '⚠️'} ISO téléchargée",
        "color": 0x00b4d8 if ok else 0xffd600,
        "fields": [
            {"name": "Distribution", "value": name,       "inline": True},
            {"name": "Version",      "value": str(version),"inline": True},
            {"name": "Source",       "value": src_label,  "inline": True},
            {"name": "Fichier",      "value": filename,   "inline": False},
            {"name": "Taille",       "value": f"{size/1e9:.2f} GB", "inline": True},
            {"name": "Checksum",     "value": "✅ OK" if ok else "⚠️ Divergence", "inline": True},
        ],
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "footer": {"text": "ISOWatcher v2.0"},
    }]})

def _discord_new_version(webhook, name, version, info_url):
    _send_discord(webhook, {"embeds": [{"title": "🔔 Nouvelle version détectée",
        "description": f"**{name}** — `{version}`\nURL directe non résolue automatiquement.\n[Voir sur rg-adguard]({info_url})",
        "color": 0x7c3aed,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "footer": {"text": "ISOWatcher v2.0"},
    }]})

# ══════════════════════════════════════════════════════════════════════════════
#  Scheduler & check logic
# ══════════════════════════════════════════════════════════════════════════════

async def check_and_download_all():
    conn = get_db()
    distros = conn.execute("SELECT * FROM distros WHERE enabled=1").fetchall()
    conn.close()
    for distro in distros:
        source = distro["source"] if "source" in distro.keys() else "direct"
        try:
            if source == "rg-adguard":
                await _check_rg(distro)
            else:
                await _check_direct(distro)
        except Exception as e:
            logger.error(f"Check {distro['name']}: {e}")

async def _check_rg(distro):
    meta = await rg_resolve_latest(distro["slug"])
    if not meta:
        return
    version = meta["version_label"]
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM iso_library WHERE distro_id=? AND version=? AND status IN ('complete','checksum_warning')",
        (distro["id"], version)).fetchone()
    conn.execute("UPDATE distros SET last_checked=?,latest_version=? WHERE id=?",
                 (datetime.now().isoformat(), version, distro["id"]))
    conn.commit()
    conn.close()
    if existing:
        return
    dl_url = await rg_get_direct_download_url(meta["file_uuid"])
    if not dl_url:
        conn = get_db()
        conn.execute("INSERT INTO download_log (distro_id,version,status,message) VALUES (?,?,?,?)",
                     (distro["id"], version, "info",
                      f"Nouvelle version : {version}. URL directe non résolue. Voir : {meta['info_url']}"))
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        conn.commit(); conn.close()
        webhook = settings.get("discord_webhook", "").strip()
        if webhook:
            _discord_new_version(webhook, distro["name"], version, meta["info_url"])
        return
    tid = f"{distro['slug']}-{datetime.now().strftime('%H%M%S')}"
    threading.Thread(target=download_iso_sync,
        args=(distro["id"], version, dl_url, tid),
        kwargs={"expected_sha256": meta.get("sha256"), "expected_md5": meta.get("md5"),
                "source": "rg-adguard"}, daemon=True).start()

async def _check_direct(distro):
    source = distro["source"] if "source" in distro.keys() else "direct"
    # Sources qui n'ont pas de template mais un checker dédié
    dynamic_sources = ("virtio", "debian", "fedora")
    if source not in dynamic_sources and not distro["download_url_template"]:
        return
    latest = await get_latest_version(distro)
    if not latest:
        return
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM iso_library WHERE distro_id=? AND version=? AND status IN ('complete','checksum_warning')",
        (distro["id"], latest)).fetchone()
    conn.execute("UPDATE distros SET last_checked=?,latest_version=? WHERE id=?",
                 (datetime.now().isoformat(), latest, distro["id"]))
    conn.commit(); conn.close()
    if existing:
        return
    url = await resolve_download_url(distro, latest)
    if not url:
        return
    src_label = source if source in ("virtio", "archive", "fedora", "debian") else "direct"
    tid = f"{distro['slug']}-{latest}-{datetime.now().strftime('%H%M%S')}"
    threading.Thread(target=download_iso_sync,
        args=(distro["id"], latest, url, tid),
        kwargs={"source": src_label}, daemon=True).start()

def run_check():
    asyncio.run(check_and_download_all())

def setup_scheduler(hour: int, minute: int, days: str):
    scheduler.remove_all_jobs()
    scheduler.add_job(run_check, CronTrigger(day_of_week=days, hour=hour, minute=minute),
                      id="main_check")
    logger.info(f"Scheduler: {days} à {hour:02d}:{minute:02d}")

# ══════════════════════════════════════════════════════════════════════════════
#  Pydantic models
# ══════════════════════════════════════════════════════════════════════════════

class DistroCreate(BaseModel):
    name: str
    slug: str
    type: str = "linux"
    source: str = "direct"
    check_url: Optional[str] = None
    download_url_template: Optional[str] = None
    version_pattern: Optional[str] = None
    arch: str = "amd64"
    enabled: bool = True

class DistroUpdate(BaseModel):
    name: Optional[str] = None
    check_url: Optional[str] = None
    download_url_template: Optional[str] = None
    version_pattern: Optional[str] = None
    arch: Optional[str] = None
    enabled: Optional[bool] = None

# ══════════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ── Distros ──────────────────────────────────────────────────────────────────

@app.get("/api/distros")
async def list_distros():
    conn = get_db()
    rows = conn.execute("""
        SELECT d.*, COUNT(i.id) as iso_count, SUM(i.size_bytes) as total_size
        FROM distros d
        LEFT JOIN iso_library i ON d.id=i.distro_id AND i.status IN ('complete','checksum_warning')
        GROUP BY d.id ORDER BY d.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/distros")
async def create_distro(d: DistroCreate):
    conn = get_db()
    try:
        conn.execute("""
            INSERT INTO distros (name,slug,type,source,check_url,download_url_template,version_pattern,arch,enabled)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (d.name, d.slug, d.type, d.source, d.check_url,
              d.download_url_template, d.version_pattern, d.arch, int(d.enabled)))
        conn.commit()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()

@app.put("/api/distros/{distro_id}")
async def update_distro(distro_id: int, d: DistroUpdate):
    """Modification d'une distribution existante."""
    conn = get_db()
    try:
        current = conn.execute("SELECT * FROM distros WHERE id=?", (distro_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Distribution introuvable")
        fields = {}
        if d.name is not None:                  fields["name"]                  = d.name
        if d.check_url is not None:             fields["check_url"]             = d.check_url
        if d.download_url_template is not None: fields["download_url_template"] = d.download_url_template
        if d.version_pattern is not None:       fields["version_pattern"]       = d.version_pattern
        if d.arch is not None:                  fields["arch"]                  = d.arch
        if d.enabled is not None:               fields["enabled"]               = int(d.enabled)
        if not fields:
            return {"status": "no_change"}
        set_clause = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE distros SET {set_clause} WHERE id=?",
                     list(fields.values()) + [distro_id])
        conn.commit()
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()

@app.delete("/api/distros/{distro_id}")
async def delete_distro(distro_id: int):
    conn = get_db()
    conn.execute("DELETE FROM distros WHERE id=?", (distro_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.patch("/api/distros/{distro_id}/toggle")
async def toggle_distro(distro_id: int):
    conn = get_db()
    conn.execute("UPDATE distros SET enabled=1-enabled WHERE id=?", (distro_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ── Library ──────────────────────────────────────────────────────────────────

@app.get("/api/library")
async def get_library():
    conn = get_db()
    rows = conn.execute("""
        SELECT i.*, d.name as distro_name, d.slug, d.type
        FROM iso_library i JOIN distros d ON i.distro_id=d.id
        WHERE i.status IN ('complete','checksum_warning')
        ORDER BY i.downloaded_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ── Archive (FTP historique) ─────────────────────────────────────────────────

@app.get("/api/archive/{distro_key}")
async def get_archive_versions(distro_key: str, limit: int = 50):
    """Retourne les versions historiques disponibles sur le FTP officiel."""
    src = ARCHIVE_SOURCES.get(distro_key)
    if not src:
        raise HTTPException(404, f"Clé archive inconnue : {distro_key}")
    versions = await fetch_archive_versions(distro_key, limit)
    return {
        "distro_key": distro_key,
        "name":       src["name"],
        "ftp_index":  src["ftp_indexes"][0],
        "versions":   versions,
    }

@app.get("/api/archive")
async def list_archive_sources():
    """Liste toutes les sources d'archive disponibles."""
    return [{"key": k, "name": v["name"], "ftp_index": v["ftp_indexes"][0]}
            for k, v in ARCHIVE_SOURCES.items()]

@app.get("/api/archive/{distro_key}/{version}/url")
async def get_archive_url(distro_key: str, version: str):
    """Résout l'URL de téléchargement pour une version d'archive spécifique."""
    url = await resolve_archive_url(distro_key, version)
    if not url:
        raise HTTPException(404, f"URL introuvable pour {distro_key} {version}")
    return {"url": url, "distro_key": distro_key, "version": version}

@app.post("/api/archive/download")
async def download_archive_iso(payload: dict):
    """Télécharge une ISO d'archive."""
    distro_key = payload.get("distro_key")
    version    = payload.get("version")
    url        = payload.get("url")

    if not all([distro_key, version, url]):
        raise HTTPException(400, "distro_key, version, url requis")

    src = ARCHIVE_SOURCES.get(distro_key)
    if not src:
        raise HTTPException(404, "Source archive inconnue")

    conn = get_db()
    existing_distro = conn.execute(
        "SELECT id FROM distros WHERE slug=?", (distro_key,)).fetchone()
    if existing_distro:
        did = existing_distro["id"]
    else:
        conn.execute("""
            INSERT OR IGNORE INTO distros (name,slug,type,source,arch)
            VALUES (?,?,'linux','archive','amd64')
        """, (src["name"] + " (Archive)", distro_key))
        conn.commit()
        did = conn.execute("SELECT id FROM distros WHERE slug=?", (distro_key,)).fetchone()["id"]
    conn.close()

    tid = f"archive-{distro_key}-{version}-{datetime.now().strftime('%H%M%S')}"
    threading.Thread(target=download_iso_sync,
        args=(did, version, url, tid),
        kwargs={"source": "archive"}, daemon=True).start()
    return {"status": "started", "track_id": tid}

# ── Vérification & téléchargement ────────────────────────────────────────────

@app.post("/api/check")
async def trigger_check(background_tasks: BackgroundTasks):
    background_tasks.add_task(check_and_download_all)
    return {"status": "check_started"}

@app.post("/api/download")
async def manual_download(payload: dict):
    distro_id = payload.get("distro_id")
    version   = payload.get("version")
    url       = payload.get("url")
    if not all([distro_id, version, url]):
        raise HTTPException(400, "distro_id, version, url requis")
    tid = f"manual-{distro_id}-{datetime.now().strftime('%H%M%S')}"
    threading.Thread(target=download_iso_sync,
        args=(distro_id, version, url, tid),
        kwargs={"source": "manual"}, daemon=True).start()
    return {"status": "started", "track_id": tid}

@app.get("/api/progress")
async def get_progress():
    return download_progress

@app.get("/api/check-version/{distro_id}")
async def check_version(distro_id: int):
    conn = get_db()
    distro = conn.execute("SELECT * FROM distros WHERE id=?", (distro_id,)).fetchone()
    conn.close()
    if not distro:
        raise HTTPException(404, "Not found")
    source = distro["source"] if "source" in distro.keys() else "direct"
    if source == "rg-adguard":
        meta = await rg_resolve_latest(distro["slug"])
        if meta:
            return {"version": meta["version_label"], "distro": distro["name"],
                    "source": "rg-adguard", "filename": meta.get("filename"),
                    "size_bytes": meta.get("size_bytes"), "sha256": meta.get("sha256"),
                    "info_url": meta.get("info_url")}
        return {"version": None, "distro": distro["name"], "source": "rg-adguard"}
    version = await get_latest_version(distro)
    return {"version": version, "distro": distro["name"], "source": source}

# ── Logs & stats ─────────────────────────────────────────────────────────────

@app.get("/api/logs")
async def get_logs():
    conn = get_db()
    rows = conn.execute("""
        SELECT l.*, d.name as distro_name FROM download_log l
        LEFT JOIN distros d ON l.distro_id=d.id
        ORDER BY l.created_at DESC LIMIT 100
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/stats")
async def get_stats():
    conn = get_db()
    total_isos    = conn.execute("SELECT COUNT(*) FROM iso_library WHERE status IN ('complete','checksum_warning')").fetchone()[0]
    total_size    = conn.execute("SELECT SUM(size_bytes) FROM iso_library WHERE status IN ('complete','checksum_warning')").fetchone()[0] or 0
    distros_count = conn.execute("SELECT COUNT(*) FROM distros WHERE enabled=1").fetchone()[0]
    rg_count      = conn.execute("SELECT COUNT(*) FROM distros WHERE enabled=1 AND source='rg-adguard'").fetchone()[0]
    last_dl       = conn.execute("SELECT MAX(downloaded_at) FROM iso_library").fetchone()[0]
    conn.close()
    return {
        "total_isos":        total_isos,
        "total_size_gb":     round(total_size / 1e9, 2),
        "distros_monitored": distros_count,
        "rg_adguard_distros":rg_count,
        "last_download":     last_dl,
        "active_downloads":  len([p for p in download_progress.values() if p.get("status") == "downloading"]),
    }

# ── Paramètres ───────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

@app.put("/api/settings")
async def update_settings(payload: dict):
    conn = get_db()
    for k, v in payload.items():
        conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, str(v)))
    conn.commit()
    settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
    conn.close()
    setup_scheduler(int(settings.get("schedule_hour", 3)),
                    int(settings.get("schedule_minute", 0)),
                    settings.get("schedule_days", "mon"))
    return {"status": "ok"}

@app.post("/api/test-discord")
async def test_discord(payload: dict):
    webhook = payload.get("webhook_url", "").strip()
    if not webhook:
        raise HTTPException(400, "webhook_url requis")
    # FIX : test synchrone avec retour d'erreur détaillé
    try:
        p = {"embeds": [{"title": "🔔 Test ISOWatcher v2.0",
            "description": "✅ Notifications Discord opérationnelles !",
            "color": 0x00b4d8,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "footer": {"text": "ISOWatcher v2.0"}}]}
        data = json.dumps(p, ensure_ascii=False).encode("utf-8")
        req  = urllib.request.Request(
            webhook, data=data,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "ISOWatcher/1.2"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"status": "ok", "http_code": resp.status}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise HTTPException(400, f"Discord HTTP {e.code}: {body}")
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get("/api/rg-sources")
async def list_rg_sources():
    return [{"slug": s, "name": c["name"]} for s, c in RG_CATEGORIES.items()]

# ══════════════════════════════════════════════════════════════════════════════
#  Module SCAN — bibliothèque locale
# ══════════════════════════════════════════════════════════════════════════════

# Patterns de reconnaissance automatique par nom de fichier
# Chaque entrée : (regex sur le nom de fichier) -> {slug, version_group}
ISO_FILENAME_PATTERNS = [
    # Debian : debian-13.4-amd64-netinst.iso
    (re.compile(r'debian-(\d+[\.\d]*)-(?:amd64|arm64|i386)', re.IGNORECASE),
     "debian-stable"),
    # Ubuntu server : ubuntu-24.04-live-server-amd64.iso
    (re.compile(r'ubuntu-(\d+\.\d+(?:\.\d+)?)-(?:live-)?server', re.IGNORECASE),
     "ubuntu-lts"),
    # Ubuntu desktop : ubuntu-24.04-desktop-amd64.iso
    (re.compile(r'ubuntu-(\d+\.\d+(?:\.\d+)?)-desktop', re.IGNORECASE),
     "ubuntu-desktop-lts"),
    # Fedora : Fedora-Server-dvd-x86_64-41-1.1.iso
    (re.compile(r'fedora-server[^\d]*(\d+)', re.IGNORECASE),
     "fedora-server"),
    # Arch : archlinux-2025.05.01-x86_64.iso
    (re.compile(r'archlinux-(\d{4}\.\d{2}\.\d{2})', re.IGNORECASE),
     "arch-linux"),
    # Rocky Linux : Rocky-9.3-x86_64-minimal.iso
    (re.compile(r'rocky-(\d+\.\d+)', re.IGNORECASE),
     None),  # slug dynamique → "rocky-linux"
    # AlmaLinux : AlmaLinux-9.3-x86_64-minimal.iso
    (re.compile(r'almalinux-(\d+\.\d+)', re.IGNORECASE),
     None),
    # Windows Server : 17763.xxx.XXXXXX_SERVER_EVAL_x64.iso
    (re.compile(r'windows.server.*(2019|2022|2025)', re.IGNORECASE),
     None),  # version = année
    # Windows 11
    (re.compile(r'win(?:dows)?[\._\-]?11', re.IGNORECASE),
     "windows-11"),
    # Windows 10
    (re.compile(r'win(?:dows)?[\._\-]?10', re.IGNORECASE),
     "windows-10"),
    # Kali
    (re.compile(r'kali-linux-(\d{4}\.\d+)', re.IGNORECASE),
     None),
    # openSUSE
    (re.compile(r'opensuse-leap-(\d+\.\d+)', re.IGNORECASE),
     None),
    # Linux Mint
    (re.compile(r'linuxmint-(\d+\.\d+)', re.IGNORECASE),
     None),
]

# État du scan en cours (partagé entre le thread de scan et l'API)
scan_state = {
    "running":     False,
    "progress":    0,       # 0-100
    "total_files": 0,
    "scanned":     0,
    "found_new":   0,
    "found_modified": 0,
    "found_known": 0,
    "unidentified": [],     # liste des fichiers non reconnus
    "results":     [],      # rapport complet
    "started_at":  None,
    "finished_at": None,
    "error":       None,
}

def _identify_iso(filename: str, filepath: Path) -> dict:
    """
    Tente d'identifier une ISO depuis son nom de fichier.
    Retourne {"identified": True/False, "slug": ..., "version": ..., "distro_name": ...}
    """
    name = filename.lower()
    for pattern, slug in ISO_FILENAME_PATTERNS:
        m = pattern.search(filename)
        if m:
            version = m.group(1) if m.lastindex and m.lastindex >= 1 else "unknown"
            # Cas spéciaux où le slug dépend du contenu
            if slug is None:
                if "rocky" in name:
                    slug = "rocky-linux"
                elif "alma" in name:
                    slug = "almalinux"
                elif "windows" in name and "server" in name:
                    year = m.group(1) if m.lastindex else "unknown"
                    slug = f"windows-server-{year}"
                elif "kali" in name:
                    slug = "kali-linux"
                elif "opensuse" in name:
                    slug = "opensuse-leap"
                elif "linuxmint" in name or "mint" in name:
                    slug = "linux-mint"
                else:
                    slug = "unknown"
            return {
                "identified":   True,
                "slug":         slug,
                "version":      version,
                "distro_name":  slug.replace("-", " ").title(),
                "confidence":   "auto",
            }
    return {
        "identified":   False,
        "slug":         None,
        "version":      None,
        "distro_name":  None,
        "confidence":   None,
    }

def _scan_worker():
    """Thread de scan : parcourt ISO_DIR et analyse chaque fichier .iso trouvé."""
    global scan_state
    scan_state.update({
        "running": True, "progress": 0, "scanned": 0,
        "found_new": 0, "found_modified": 0, "found_known": 0,
        "unidentified": [], "results": [],
        "started_at": datetime.now().isoformat(),
        "finished_at": None, "error": None,
    })

    try:
        # 1. Lister tous les .iso récursivement
        all_isos = list(ISO_DIR.rglob("*.iso"))
        scan_state["total_files"] = len(all_isos)
        logger.info(f"[scan] {len(all_isos)} fichier(s) ISO trouvé(s)")

        conn = get_db()

        for idx, iso_path in enumerate(all_isos):
            scan_state["scanned"]  = idx + 1
            scan_state["progress"] = int((idx + 1) / max(len(all_isos), 1) * 100)

            filename  = iso_path.name
            filepath  = str(iso_path)
            size      = iso_path.stat().st_size

            # 2. Vérifier si déjà en base (par chemin)
            existing = conn.execute(
                "SELECT * FROM iso_library WHERE filepath=?", (filepath,)
            ).fetchone()

            if existing:
                existing = dict(existing)
                # Comparer la taille pour détecter une modification
                if existing.get("size_bytes") and existing["size_bytes"] != size:
                    # Fichier modifié → on marque sans écraser le checksum
                    conn.execute(
                        "UPDATE iso_library SET status='modified', size_bytes=? WHERE filepath=?",
                        (size, filepath)
                    )
                    conn.commit()
                    scan_state["found_modified"] += 1
                    scan_state["results"].append({
                        "filepath": filepath, "filename": filename,
                        "size": size, "status": "modified",
                        "distro_name": existing.get("distro_name", "?"),
                        "version": existing.get("version", "?"),
                        "note": f"Taille changée : {existing['size_bytes']/1e9:.2f} GB → {size/1e9:.2f} GB",
                    })
                else:
                    scan_state["found_known"] += 1
                    scan_state["results"].append({
                        "filepath": filepath, "filename": filename,
                        "size": size, "status": "known",
                        "version": existing.get("version", "?"),
                        "note": "Déjà en bibliothèque",
                    })
                continue

            # 3. Nouveau fichier — tentative d'identification
            ident = _identify_iso(filename, iso_path)

            if ident["identified"]:
                # Calcul SHA-256
                sha256 = compute_sha256(filepath)

                # Chercher ou créer la distro en base
                slug = ident["slug"]
                distro_row = conn.execute(
                    "SELECT id, name FROM distros WHERE slug=?", (slug,)
                ).fetchone()

                if distro_row:
                    distro_id   = distro_row["id"]
                    distro_name = distro_row["name"]
                else:
                    # Créer une distro ad-hoc pour les distros non pré-configurées
                    display_name = ident["distro_name"]
                    os_type = "windows" if "windows" in slug else "linux"
                    conn.execute("""
                        INSERT OR IGNORE INTO distros (name,slug,type,source,arch,enabled)
                        VALUES (?,?,'linux','scan','amd64',1)
                    """, (display_name, slug))
                    conn.commit()
                    distro_row  = conn.execute("SELECT id, name FROM distros WHERE slug=?", (slug,)).fetchone()
                    distro_id   = distro_row["id"]
                    distro_name = distro_row["name"]

                conn.execute("""
                    INSERT INTO iso_library
                      (distro_id, version, arch, filename, filepath, size_bytes,
                       checksum_sha256, source, status)
                    VALUES (?,?,?,?,?,?,?,'scan','complete')
                """, (distro_id, ident["version"], "amd64",
                      filename, filepath, size, sha256))
                conn.commit()

                scan_state["found_new"] += 1
                scan_state["results"].append({
                    "filepath": filepath, "filename": filename,
                    "size": size, "status": "imported",
                    "distro_name": distro_name,
                    "version": ident["version"],
                    "slug": slug,
                    "sha256": sha256,
                    "note": f"Importé automatiquement (confiance: {ident['confidence']})",
                })
            else:
                # Non identifié → ajout à la liste d'attente manuelle
                scan_state["found_new"] += 1  # compte comme nouveau à traiter
                entry = {
                    "filepath": filepath,
                    "filename": filename,
                    "size":     size,
                    "size_gb":  round(size / 1e9, 2),
                    "status":   "unidentified",
                    "note":     "Identification manuelle requise",
                }
                scan_state["unidentified"].append(entry)
                scan_state["results"].append({**entry})

        conn.close()
        scan_state["progress"]    = 100
        scan_state["running"]     = False
        scan_state["finished_at"] = datetime.now().isoformat()
        logger.info(f"[scan] Terminé — {scan_state['found_new']} nouveaux, "
                    f"{scan_state['found_modified']} modifiés, "
                    f"{scan_state['found_known']} déjà connus, "
                    f"{len(scan_state['unidentified'])} non identifiés")

    except Exception as e:
        logger.error(f"[scan] Erreur : {e}")
        scan_state["running"]  = False
        scan_state["error"]    = str(e)
        scan_state["finished_at"] = datetime.now().isoformat()

# ── Routes scan ───────────────────────────────────────────────────────────────

@app.post("/api/scan/start")
async def start_scan():
    """Lance le scan de la bibliothèque locale en arrière-plan."""
    if scan_state["running"]:
        raise HTTPException(409, "Un scan est déjà en cours")
    threading.Thread(target=_scan_worker, daemon=True).start()
    return {"status": "started"}

@app.get("/api/scan/status")
async def get_scan_status():
    """Retourne l'état courant du scan (progression + résultats partiels)."""
    return scan_state

@app.post("/api/scan/assign")
async def assign_unidentified(payload: dict):
    """
    Assigne manuellement une distro et une version à un fichier non identifié.
    payload: { filepath, distro_id, version }
    """
    filepath   = payload.get("filepath")
    distro_id  = payload.get("distro_id")
    version    = payload.get("version", "unknown")

    if not filepath or not distro_id:
        raise HTTPException(400, "filepath et distro_id requis")

    iso_path = Path(filepath)
    if not iso_path.exists():
        raise HTTPException(404, f"Fichier introuvable : {filepath}")

    conn = get_db()
    distro = conn.execute("SELECT * FROM distros WHERE id=?", (distro_id,)).fetchone()
    if not distro:
        conn.close()
        raise HTTPException(404, "Distribution introuvable")

    # Vérifier qu'il n'est pas déjà en base
    existing = conn.execute(
        "SELECT id FROM iso_library WHERE filepath=?", (filepath,)
    ).fetchone()

    # Calcul checksum (peut être long pour les grosses ISOs)
    sha256 = compute_sha256(filepath)
    size   = iso_path.stat().st_size

    if existing:
        conn.execute("""
            UPDATE iso_library
            SET distro_id=?, version=?, checksum_sha256=?, size_bytes=?,
                source='scan', status='complete'
            WHERE filepath=?
        """, (distro_id, version, sha256, size, filepath))
    else:
        conn.execute("""
            INSERT INTO iso_library
              (distro_id, version, arch, filename, filepath, size_bytes,
               checksum_sha256, source, status)
            VALUES (?,?,?,?,?,?,?,'scan','complete')
        """, (distro_id, version, distro["arch"], iso_path.name,
              filepath, size, sha256))

    conn.commit()
    conn.close()

    # Retirer de la liste des non-identifiés
    scan_state["unidentified"] = [
        x for x in scan_state["unidentified"] if x["filepath"] != filepath
    ]
    # Mettre à jour dans results
    for r in scan_state["results"]:
        if r["filepath"] == filepath:
            r["status"]      = "imported"
            r["distro_name"] = distro["name"]
            r["version"]     = version
            r["sha256"]      = sha256
            r["note"]        = "Assigné manuellement"

    return {"status": "ok", "sha256": sha256, "size_gb": round(size/1e9, 2)}

@app.post("/api/scan/ignore")
async def ignore_unidentified(payload: dict):
    """Ignore définitivement un fichier non identifié."""
    filepath = payload.get("filepath")
    if not filepath:
        raise HTTPException(400, "filepath requis")
    scan_state["unidentified"] = [
        x for x in scan_state["unidentified"] if x["filepath"] != filepath
    ]
    for r in scan_state["results"]:
        if r["filepath"] == filepath:
            r["status"] = "ignored"
            r["note"]   = "Ignoré manuellement"
    return {"status": "ok"}

@app.post("/api/scan/create-category")
async def create_category(payload: dict):
    """
    Crée une nouvelle catégorie (distribution) à la volée depuis le scan.
    payload: { name, slug, type, arch }
    Retourne { status, id, name, slug }
    """
    name = payload.get("name", "").strip()
    slug = payload.get("slug", "").strip()
    typ  = payload.get("type", "linux")
    arch = payload.get("arch", "amd64")

    if not name or not slug:
        raise HTTPException(400, "name et slug requis")

    slug = re.sub(r'[^a-z0-9\-]', '-', slug.lower()).strip('-')

    conn = get_db()
    try:
        existing = conn.execute(
            "SELECT id, name FROM distros WHERE slug=?", (slug,)
        ).fetchone()
        if existing:
            conn.close()
            return {"status": "existing", "id": existing["id"],
                    "name": existing["name"],
                    "message": f"Catégorie '{existing['name']}' déjà existante"}

        conn.execute("""
            INSERT INTO distros (name, slug, type, source, arch, enabled)
            VALUES (?, ?, ?, 'scan', ?, 1)
        """, (name, slug, typ, arch))
        conn.commit()
        new_id = conn.execute(
            "SELECT id FROM distros WHERE slug=?", (slug,)
        ).fetchone()["id"]
        conn.close()
        logger.info(f"[scan] Catégorie créée : {name} ({slug}) id={new_id}")
        return {"status": "created", "id": new_id, "name": name, "slug": slug}
    except Exception as e:
        conn.close()
        raise HTTPException(400, str(e))

# ══════════════════════════════════════════════════════════════════════════════
#  Module PROXMOX VIEW — intégration Proxmox via SSH + script bash sur Unraid
#
#  POURQUOI SSH :
#    Tout est sur CIFS/SMB (Unraid). CIFS ne supporte pas les symlinks côté
#    client (Errno 95, os.link, ln -s… tout échoue depuis Docker/LXC).
#    La solution : exécuter le script de création de symlinks DIRECTEMENT
#    sur Unraid via SSH. Unraid utilise XFS natif → symlinks parfaitement
#    supportés. Python n'est qu'un déclencheur SSH.
#
#  FLUX :
#    ISOWatcher (Docker/LXC) → SSH → Unraid → sync_symlinks.sh → XFS → symlinks
#    Proxmox → montage CIFS proxmox-view → voit les symlinks comme des .iso
#
#  PRÉ-REQUIS :
#    1. Clé SSH générée et déposée sur Unraid (voir page Proxmox Sync)
#    2. sync_symlinks.sh copié sur Unraid (fourni dans le zip)
#    3. SSH configuré dans les paramètres ISOWatcher
# ══════════════════════════════════════════════════════════════════════════════

import subprocess

def _get_ssh_settings() -> dict:
    conn = get_db()
    rows = conn.execute(
        "SELECT key, value FROM settings WHERE key LIKE 'ssh_%'"
    ).fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def _test_ssh(cfg: dict) -> tuple[bool, str]:
    """Teste la connexion SSH vers Unraid. Retourne (ok, message)."""
    host = cfg.get("ssh_host", "").strip()
    if not host:
        return False, "Adresse IP Unraid non configurée"
    user = cfg.get("ssh_user", "root").strip() or "root"
    port = cfg.get("ssh_port", "22").strip() or "22"
    key  = cfg.get("ssh_key_path", "").strip()

    cmd = ["ssh",
           "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=10",
           "-o", "BatchMode=yes",
           "-p", port]
    if key and Path(key).exists():
        cmd += ["-i", key]
    cmd += [f"{user}@{host}", "echo OK"]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and "OK" in r.stdout:
            return True, "Connexion SSH réussie"
        return False, r.stderr.strip() or f"Code retour SSH : {r.returncode}"
    except subprocess.TimeoutExpired:
        return False, "Timeout SSH (10s)"
    except Exception as e:
        return False, str(e)

def sync_via_ssh() -> dict:
    """
    Déclenche sync_symlinks.sh sur Unraid via SSH.
    Retourne un rapport JSON.
    """
    cfg     = _get_ssh_settings()
    host    = cfg.get("ssh_host", "").strip()
    user    = cfg.get("ssh_user", "root").strip() or "root"
    port    = cfg.get("ssh_port", "22").strip() or "22"
    key     = cfg.get("ssh_key_path", "").strip()
    script  = cfg.get("ssh_script_path", "").strip()
    src_dir = cfg.get("ssh_source_dir", "").strip()
    dst_dir = cfg.get("ssh_dest_dir", "").strip()

    if not host:
        return {"error": "SSH non configuré — renseignez l'IP Unraid dans les paramètres",
                "created": 0, "updated": 0, "skipped": 0, "removed": 0, "conflicts": 0}
    if not script:
        return {"error": "Chemin du script non configuré",
                "created": 0, "updated": 0, "skipped": 0, "removed": 0, "conflicts": 0}

    # Construire la commande SSH
    ssh_cmd = ["ssh",
               "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=15",
               "-o", "BatchMode=yes",
               "-p", port]
    if key and Path(key).exists():
        ssh_cmd += ["-i", key]
    ssh_cmd.append(f"{user}@{host}")

    # Passer les dossiers en variables d'env pour le script
    env_prefix = ""
    if src_dir:
        env_prefix += f"ISOWATCHER_SOURCE_DIR='{src_dir}' "
    if dst_dir:
        env_prefix += f"ISOWATCHER_DEST_DIR='{dst_dir}' "

    ssh_cmd.append(f"bash {script} {env_prefix}".strip() if not env_prefix else
                   f"{env_prefix} bash {script}")

    try:
        logger.info(f"[proxmox] SSH sync → {user}@{host}:{port} : {script}")
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=300)

        stdout = r.stdout
        stderr = r.stderr.strip()

        if r.returncode != 0:
            return {"error": f"Script SSH échoué (code {r.returncode}) : {stderr}",
                    "stdout": stdout, "created": 0, "updated": 0,
                    "skipped": 0, "removed": 0, "conflicts": 0}

        # Parser le résultat JSON émis par le script
        result = {"created": 0, "updated": 0, "skipped": 0,
                  "removed": 0, "conflicts": 0, "stdout": stdout}
        for line in stdout.splitlines():
            if line.startswith("JSON_RESULT:"):
                try:
                    data = json.loads(line[len("JSON_RESULT:"):])
                    result.update(data)
                except Exception:
                    pass
                break

        logger.info(f"[proxmox] SSH sync OK : {result}")
        return result

    except subprocess.TimeoutExpired:
        return {"error": "Timeout SSH (5min) — vérifiez la connexion réseau",
                "created": 0, "updated": 0, "skipped": 0, "removed": 0, "conflicts": 0}
    except Exception as e:
        return {"error": str(e),
                "created": 0, "updated": 0, "skipped": 0, "removed": 0, "conflicts": 0}

def _generate_ssh_key(key_path: str) -> tuple[bool, str]:
    """Génère une paire de clés SSH si elle n'existe pas."""
    key_file = Path(key_path)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        # Lire la clé publique existante
        pub = key_file.with_suffix(".pub")
        if pub.exists():
            return True, pub.read_text().strip()
        return True, "Clé privée existante (clé publique introuvable)"
    try:
        r = subprocess.run(
            ["ssh-keygen", "-t", "rsa", "-b", "4096",
             "-f", str(key_file), "-N", "", "-C", "isowatcher@proxmox"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            pub = key_file.with_suffix(".pub")
            return True, pub.read_text().strip() if pub.exists() else "Clé générée"
        return False, r.stderr.strip()
    except Exception as e:
        return False, str(e)

def repair_db_paths() -> dict:
    """Répare les entrées DB dont le filepath est introuvable sur disque."""
    conn  = get_db()
    isos  = conn.execute("SELECT * FROM iso_library").fetchall()
    fixed, missing_marked, not_found = 0, 0, []
    for iso in isos:
        src = Path(iso["filepath"])
        if src.exists():
            continue
        found = list(ISO_DIR.rglob(iso["filename"]))
        if found:
            conn.execute("UPDATE iso_library SET filepath=? WHERE id=?",
                         (str(found[0]), iso["id"]))
            fixed += 1
            logger.info(f"[repair] {iso['filepath']} → {found[0]}")
        else:
            conn.execute("UPDATE iso_library SET status='missing' WHERE id=?", (iso["id"],))
            missing_marked += 1
            not_found.append({"filename": iso["filename"], "old_path": iso["filepath"]})
            logger.warning(f"[repair] Introuvable : {iso['filepath']}")
    conn.commit()
    conn.close()
    return {"fixed": fixed, "missing_marked": missing_marked, "not_found": not_found}

# ── Routes proxmox / SSH ──────────────────────────────────────────────────────

@app.post("/api/symlinks/sync")
async def trigger_sync():
    """Déclenche sync_symlinks.sh sur Unraid via SSH."""
    report = sync_via_ssh()
    return report

def _remote_ls_proxmox_view(cfg: dict) -> list:
    """
    Liste le contenu réel du dossier proxmox-view SUR UNRAID via SSH.
    Remplace l'ancienne lecture d'un dossier local qui n'existe plus
    dans l'architecture SSH (rien n'écrit en local dans le container).
    """
    host = cfg.get("ssh_host", "").strip()
    dest = cfg.get("ssh_dest_dir", "").strip()
    if not host or not dest:
        return []
    user = cfg.get("ssh_user", "root").strip() or "root"
    port = cfg.get("ssh_port", "22").strip() or "22"
    key  = cfg.get("ssh_key_path", "").strip()

    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
               "-o", "BatchMode=yes", "-p", port]
    if key and Path(key).exists():
        ssh_cmd += ["-i", key]
    # -L : suit les symlinks pour donner la vraie taille du fichier cible
    ssh_cmd += [f"{user}@{host}",
                f"find '{dest}' -maxdepth 1 -iname '*.iso' -printf '%f\\t%s\\t%l\\n' 2>/dev/null"]
    try:
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return []
        entries = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            name = parts[0]
            try:
                size = int(parts[1])
            except ValueError:
                size = 0
            is_link = len(parts) > 2 and bool(parts[2])
            entries.append({
                "name":       name,
                "size_gb":    round(size / 1e9, 2),
                "is_symlink": is_link,
                "broken":     is_link and size == 0,
            })
        return sorted(entries, key=lambda e: e["name"])
    except Exception as e:
        logger.warning(f"[proxmox] Listing distant échoué : {e}")
        return []

@app.get("/api/symlinks/status")
async def get_proxmox_status():
    """Retourne l'état SSH + liste réelle des fichiers dans proxmox-view (via SSH)."""
    cfg  = _get_ssh_settings()
    conn = get_db()
    total_isos    = conn.execute(
        "SELECT COUNT(*) FROM iso_library WHERE status IN ('complete','checksum_warning')"
    ).fetchone()[0]
    missing_count = conn.execute(
        "SELECT COUNT(*) FROM iso_library WHERE status='missing'"
    ).fetchone()[0]
    conn.close()

    ssh_configured = bool(cfg.get("ssh_host", "").strip())
    entries = _remote_ls_proxmox_view(cfg) if ssh_configured else []

    return {
        "symlink_dir":      cfg.get("ssh_dest_dir", ""),
        "dir_exists":       len(entries) > 0 or ssh_configured,
        "ssh_configured":   ssh_configured,
        "ssh_host":         cfg.get("ssh_host", ""),
        "total_symlinks":   len(entries),
        "total_isos":       total_isos,
        "missing_in_db":    missing_count,
        "symlinks":         entries,
    }

@app.post("/api/symlinks/test-ssh")
async def test_ssh_connection():
    """Teste la connexion SSH vers Unraid."""
    cfg = _get_ssh_settings()
    ok, msg = _test_ssh(cfg)
    return {"ok": ok, "message": msg}

@app.post("/api/symlinks/generate-key")
async def generate_ssh_key():
    """Génère une paire de clés SSH dans le container."""
    cfg      = _get_ssh_settings()
    key_path = cfg.get("ssh_key_path", "/data/ssh/id_rsa").strip() or "/data/ssh/id_rsa"
    ok, pub  = _generate_ssh_key(key_path)
    return {"ok": ok, "public_key": pub, "key_path": key_path}

@app.put("/api/symlinks/config")
async def update_proxmox_config(payload: dict):
    conn = get_db()
    for k, v in payload.items():
        if k.startswith("ssh_") or k == "symlink_dir":
            conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, str(v)))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/symlinks/clean")
async def clean_proxmox_view():
    """
    Nettoie les orphelins sur Unraid.
    Le script sync_symlinks.sh nettoie déjà les orphelins à chaque sync
    (section "Nettoyage des symlinks orphelins" du script) — cet endpoint
    relance simplement une sync complète via SSH pour déclencher ce nettoyage.
    """
    report = sync_via_ssh()
    return {"removed": report.get("removed", 0), "sync": report}

@app.post("/api/symlinks/repair")
async def repair_and_sync():
    repair = repair_db_paths()
    sync   = sync_via_ssh()
    return {**repair, "sync": sync}

# ══════════════════════════════════════════════════════════════════════════════
#  Startup
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    logger.info("Lancement ISOWatcher v2.0…")
    init_db()
    conn = get_db()
    try:
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        setup_scheduler(int(settings.get("schedule_hour", 3)),
                        int(settings.get("schedule_minute", 0)),
                        settings.get("schedule_days", "mon"))
        if not scheduler.running:
            scheduler.start()
        threading.Thread(target=repair_db_paths, daemon=True).start()
        logger.info("ISOWatcher v2.0 prêt.")
    except Exception as e:
        logger.error(f"Erreur startup: {e}")
    finally:
        conn.close()
