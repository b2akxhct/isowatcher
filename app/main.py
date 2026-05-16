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

app = FastAPI(title="ISOWatcher", version="1.2.0")
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
        "ftp_index": "https://cdimage.debian.org/cdimage/archive/",
        "version_pattern": r'href="(\d+\.\d+(?:\.\d+)?)/?"',
        "iso_url_template": "https://cdimage.debian.org/cdimage/archive/{version}/amd64/iso-cd/debian-{version}-amd64-netinst.iso",
        "current_url":      "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/",
        "current_pattern":  r'href="(debian-[\d\.]+\-amd64-netinst\.iso)"',
    },
    "ubuntu": {
        "name": "Ubuntu",
        "ftp_index": "https://old-releases.ubuntu.com/releases/",
        "version_pattern": r'href="(\d+\.\d+(?:\.\d+)?)/?"',
        "iso_url_template": "https://old-releases.ubuntu.com/releases/{version}/ubuntu-{version}-live-server-amd64.iso",
        "current_url":      "https://releases.ubuntu.com/",
        "current_pattern":  r'href="(\d+\.\d+)/?"',
    },
    "fedora": {
        "name": "Fedora",
        "ftp_index": "https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/",
        "version_pattern": r'href="(\d+)/?"',
        "iso_url_template": "https://archives.fedoraproject.org/pub/archive/fedora/linux/releases/{version}/Server/x86_64/iso/Fedora-Server-dvd-x86_64-{version}-1.1.iso",
        "current_url":      "https://dl.fedoraproject.org/pub/fedora/linux/releases/",
        "current_pattern":  r'href="(\d+)/?"',
    },
    "arch": {
        "name": "Arch Linux",
        "ftp_index": "https://archive.archlinux.org/iso/",
        "version_pattern": r'href="(\d{4}\.\d{2}\.\d{2})/?"',
        "iso_url_template": "https://archive.archlinux.org/iso/{version}/archlinux-{version}-x86_64.iso",
        "current_url":      "https://mirror.rackspace.com/archlinux/iso/latest/",
        "current_pattern":  r'href="(archlinux-[\d\.]+-x86_64\.iso)"',
    },
}

async def fetch_archive_versions(distro_key: str, limit: int = 20) -> list:
    """Récupère les versions historiques disponibles sur les FTP officiels."""
    src = ARCHIVE_SOURCES.get(distro_key)
    if not src:
        return []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(src["ftp_index"])
            versions = re.findall(src["version_pattern"], r.text)
            # Dédoublonner et trier décroissant
            seen = set()
            unique = []
            for v in reversed(versions):
                if v not in seen:
                    seen.add(v)
                    unique.append(v)
            return unique[:limit]
    except Exception as e:
        logger.error(f"Archive fetch {distro_key}: {e}")
        return []

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
        """)
        conn.commit()

        # ── Distros pré-configurées ───────────────────────────────────────────
        # FIX Debian : stable = Debian 13 "Trixie" depuis août 2025
        # On utilise /current/ qui suit toujours la dernière stable
        # + checker via le fichier Release du miroir officiel
        distros = [
            # Linux direct
            ("Ubuntu LTS Server",   "ubuntu-lts",        "linux",   "direct",
             "https://changelogs.ubuntu.com/meta-release-lts",
             "https://releases.ubuntu.com/{version}/ubuntu-{version}-live-server-amd64.iso",
             r"Version:\s+(\d+\.\d+)"),

            ("Ubuntu Desktop LTS",  "ubuntu-desktop-lts","linux",   "direct",
             "https://changelogs.ubuntu.com/meta-release-lts",
             "https://releases.ubuntu.com/{version}/ubuntu-{version}-desktop-amd64.iso",
             r"Version:\s+(\d+\.\d+)"),

            # FIX : Debian — checker sur le fichier Release officiel
            # Version retourne maintenant "13.4" pour Trixie
            # URL /current/ suit automatiquement la stable
            ("Debian Stable",       "debian-stable",     "linux",   "direct",
             "https://deb.debian.org/debian/dists/stable/Release",
             "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-{version}-amd64-netinst.iso",
             r"Version:\s+(\d+\.\d+)"),

            ("Fedora Server",       "fedora-server",     "linux",   "direct",
             "https://dl.fedoraproject.org/pub/fedora/linux/releases/",
             "https://dl.fedoraproject.org/pub/fedora/linux/releases/{version}/Server/x86_64/iso/Fedora-Server-dvd-x86_64-{version}-1.1.iso",
             r'href="(\d{2})/?"'),

            ("Arch Linux",          "arch-linux",        "linux",   "direct",
             "https://archlinux.org/download/",
             "https://mirror.rackspace.com/archlinux/iso/{version}/archlinux-{version}-x86_64.iso",
             r"Current Release:\s*</strong>\s*(\d{4}\.\d{2}\.\d{2})"),

            # Windows rg-adguard
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

async def get_latest_debian_stable() -> Optional[str]:
    """
    FIX : Récupère la version depuis le fichier Release du miroir Debian.
    Debian 13 (Trixie) = 13.4 en mars 2026.
    Renvoie ex: '13.4'
    """
    urls = [
        "https://deb.debian.org/debian/dists/stable/Release",
        "https://ftp.debian.org/debian/dists/stable/Release",
    ]
    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(url)
                m = re.search(r"^Version:\s+(\d+\.\d+)", r.text, re.MULTILINE)
                if m:
                    return m.group(1)
        except Exception as e:
            logger.warning(f"Debian check ({url}): {e}")
    return None

async def get_latest_fedora() -> Optional[str]:
    """Scrape le FTP Fedora pour trouver la dernière version stable."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get("https://dl.fedoraproject.org/pub/fedora/linux/releases/")
            versions = re.findall(r'href="(\d{2,3})/?"', r.text)
            if versions:
                return sorted(versions, key=int)[-1]
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
    slug   = distro["slug"]
    source = distro["source"] if "source" in distro.keys() else "direct"

    if source == "rg-adguard":
        meta = await rg_resolve_latest(slug)
        return meta["version_label"] if meta else None

    # Checkers dédiés
    if slug in ("ubuntu-lts", "ubuntu-desktop-lts"):
        return await get_latest_ubuntu_lts()
    if slug == "debian-stable":
        return await get_latest_debian_stable()
    if slug == "fedora-server":
        return await get_latest_fedora()
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

def build_download_url(distro: sqlite3.Row, version: str) -> Optional[str]:
    if not distro["download_url_template"]:
        return None
    return distro["download_url_template"].replace("{version}", version)

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

        # FIX Discord : récupérer le webhook AVANT de fermer la connexion
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        webhook  = settings.get("discord_webhook", "").strip()
        if webhook:
            _discord_downloaded(webhook, distro["name"], version, filename, size, ok, source)

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
        "footer": {"text": "ISOWatcher v1.2"},
    }]})

def _discord_new_version(webhook, name, version, info_url):
    _send_discord(webhook, {"embeds": [{"title": "🔔 Nouvelle version détectée",
        "description": f"**{name}** — `{version}`\nURL directe non résolue automatiquement.\n[Voir sur rg-adguard]({info_url})",
        "color": 0x7c3aed,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "footer": {"text": "ISOWatcher v1.2"},
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
    if not distro["download_url_template"]:
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
    url = build_download_url(distro, latest)
    if not url:
        return
    tid = f"{distro['slug']}-{latest}-{datetime.now().strftime('%H%M%S')}"
    threading.Thread(target=download_iso_sync,
        args=(distro["id"], latest, url, tid),
        kwargs={"source": "direct"}, daemon=True).start()

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
async def get_archive_versions(distro_key: str, limit: int = 20):
    """Retourne les versions historiques disponibles sur le FTP officiel."""
    src = ARCHIVE_SOURCES.get(distro_key)
    if not src:
        raise HTTPException(404, f"Clé archive inconnue : {distro_key}")
    versions = await fetch_archive_versions(distro_key, limit)
    return {
        "distro_key":  distro_key,
        "name":        src["name"],
        "ftp_index":   src["ftp_index"],
        "versions":    versions,
        "url_template": src["iso_url_template"],
    }

@app.get("/api/archive")
async def list_archive_sources():
    """Liste toutes les sources d'archive disponibles."""
    return [{"key": k, "name": v["name"], "ftp_index": v["ftp_index"]}
            for k, v in ARCHIVE_SOURCES.items()]

@app.post("/api/archive/download")
async def download_archive_iso(payload: dict):
    """Télécharge une ISO d'archive. Le frontend calcule l'URL via url_template."""
    distro_key = payload.get("distro_key")
    version    = payload.get("version")
    url        = payload.get("url")
    distro_id  = payload.get("distro_id")  # peut être None → on crée une entrée ad-hoc

    if not all([distro_key, version, url]):
        raise HTTPException(400, "distro_key, version, url requis")

    src = ARCHIVE_SOURCES.get(distro_key)
    if not src:
        raise HTTPException(404, "Source archive inconnue")

    conn = get_db()
    # Chercher une distro existante avec le bon slug ou en créer une temporaire
    existing_distro = conn.execute(
        "SELECT id FROM distros WHERE slug=?", (distro_key,)).fetchone()
    if existing_distro:
        did = existing_distro["id"]
    else:
        # Créer une entrée distro ad-hoc pour l'archive
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
        p = {"embeds": [{"title": "🔔 Test ISOWatcher v1.2",
            "description": "✅ Notifications Discord opérationnelles !",
            "color": 0x00b4d8,
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "footer": {"text": "ISOWatcher v1.2"}}]}
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
#  Startup
# ══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    logger.info("Lancement ISOWatcher v1.2…")
    init_db()
    conn = get_db()
    try:
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        setup_scheduler(int(settings.get("schedule_hour", 3)),
                        int(settings.get("schedule_minute", 0)),
                        settings.get("schedule_days", "mon"))
        if not scheduler.running:
            scheduler.start()
        logger.info("ISOWatcher v1.2 prêt.")
    except Exception as e:
        logger.error(f"Erreur startup: {e}")
    finally:
        conn.close()