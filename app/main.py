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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ISOWatcher", version="1.1.0")
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
templates = Jinja2Templates(directory="/app/templates")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
ISO_DIR = DATA_DIR / "isos"
DB_PATH = DATA_DIR / "isowatcher.db"
ISO_DIR.mkdir(parents=True, exist_ok=True)

scheduler = BackgroundScheduler()
download_progress = {}

# ─────────────────────────────────────────────────────────────────────────────
# Module rg-adguard
# ─────────────────────────────────────────────────────────────────────────────

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
                r'\[([^\]]+)\]\(https://files\.rg-adguard\.net/language/([a-f0-9\-]+)\)',
                r.text
            )
            results = []
            for label, uuid in matches:
                if re.search(search_pattern, label, re.IGNORECASE):
                    results.append({"label": label.strip(), "uuid": uuid})
            return results
    except Exception as e:
        logger.error(f"rg-adguard version list failed: {e}")
        return []

async def rg_get_lang_uuid(version_uuid: str, lang_preferences: list) -> Optional[tuple]:
    url = f"{RG_BASE}/language/{version_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url)
            matches = re.findall(
                r'\[([^\]]+)\]\(https://files\.rg-adguard\.net/files/([a-f0-9\-]+)\)',
                r.text
            )
            for pref in lang_preferences:
                for label, uuid in matches:
                    if pref.lower() in label.lower():
                        return (label, uuid)
            if matches:
                return (matches[0][0], matches[0][1])
    except Exception as e:
        logger.error(f"rg-adguard lang page failed: {e}")
    return None

async def rg_get_file_uuid(lang_uuid: str, edition_prefs: list) -> Optional[tuple]:
    url = f"{RG_BASE}/files/{lang_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url)
            matches = re.findall(
                r'\[([^\]]+\.iso)\]\(https://files\.rg-adguard\.net/file/([a-f0-9\-]+)\)',
                r.text, re.IGNORECASE
            )
            if not matches:
                return None
            for pref in edition_prefs:
                for filename, uuid in matches:
                    if pref.upper() in filename.upper():
                        return (filename, uuid)
            return (matches[0][0], matches[0][1])
    except Exception as e:
        logger.error(f"rg-adguard files page failed: {e}")
    return None

async def rg_get_file_info(file_uuid: str) -> Optional[dict]:
    url = f"{RG_BASE}/file/{file_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url)
            text = r.text

            def ex(pattern):
                m = re.search(pattern, text)
                return m.group(1).strip() if m else None

            sha256   = ex(r'\*\*SHA-256\*\*:\s*\|\s*([a-f0-9]{64})')
            md5      = ex(r'\*\*MD5\*\*:\s*\|\s*([a-f0-9]{32})')
            size_b   = ex(r'Size\*\*:\s*\|[^(]+\((\d+)\s*bytes\)')
            filename = ex(r'\*\*File\*\*:\s*\|\s*([^\|\n<]+\.iso)')
            if not filename:
                m = re.search(r'title:\s*([^\n:]+\.iso)', text, re.IGNORECASE)
                filename = m.group(1).strip() if m else None

            return {
                "filename": filename,
                "sha256": sha256,
                "md5": md5,
                "size_bytes": int(size_b) if size_b else None,
                "info_url": url,
                "file_uuid": file_uuid,
            }
    except Exception as e:
        logger.error(f"rg-adguard file info failed: {e}")
    return None

async def rg_get_direct_download_url(file_uuid: str) -> Optional[str]:
    url = f"{RG_BASE}/file/{file_uuid}"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                      headers={"User-Agent": "ISOWatcher/1.1"}) as client:
            r = await client.get(url)
            ms = re.search(r'href=["\']((https?://[^"\']+\.iso))["\']', r.text, re.IGNORECASE)
            if ms:
                return ms.group(2)
            dl = re.search(r'href=["\']((https?://files\.rg-adguard\.net/download/[^"\']+))', r.text)
            if dl:
                return dl.group(2)
    except Exception as e:
        logger.error(f"rg direct URL failed: {e}")
    return None

async def rg_resolve_latest(slug: str) -> Optional[dict]:
    cfg = RG_CATEGORIES.get(slug)
    if not cfg:
        return None
    logger.info(f"[rg-adguard] Résolution {slug}...")
    versions = await rg_get_versions(cfg["version_page_uuid"], cfg["search_pattern"])
    if not versions:
        logger.warning(f"[rg-adguard] Aucune version pour {slug}")
        return None
    latest = versions[0]
    logger.info(f"[rg-adguard] Dernière : {latest['label']}")
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
        "filename": filename,
        "sha256": meta.get("sha256") if meta else None,
        "md5": meta.get("md5") if meta else None,
        "size_bytes": meta.get("size_bytes") if meta else None,
        "info_url": f"{RG_BASE}/file/{file_uuid}",
        "file_uuid": file_uuid,
        "lang": lang_label,
    }

# ─── Database (Modifié pour CIFS/Autofs) ────────────────────────────────────

def get_db():
    # Augmenté à 30s pour tolérer la lenteur du montage réseau
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    logger.info("Initialisation de la base de données (Optimisation CIFS)...")
    conn = get_db()
    try:
        # Désactive WAL qui est incompatible avec les partages réseau
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

            INSERT OR IGNORE INTO settings VALUES ('schedule_hour', '3');
            INSERT OR IGNORE INTO settings VALUES ('schedule_minute', '0');
            INSERT OR IGNORE INTO settings VALUES ('schedule_days', 'mon');
            INSERT OR IGNORE INTO settings VALUES ('discord_webhook', '');
            INSERT OR IGNORE INTO settings VALUES ('check_frequency', 'weekly');
        """)
        conn.commit()

        distros = [
            ("Ubuntu LTS",        "ubuntu-lts",          "linux",   "direct",
             "https://changelogs.ubuntu.com/meta-release-lts",
             "https://releases.ubuntu.com/{version}/ubuntu-{version}-live-server-amd64.iso",
             r"Version:\s+(\d+\.\d+)"),
            ("Debian Stable",     "debian-stable",        "linux",   "direct",
             "https://deb.debian.org/debian/dists/stable/Release",
             "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-{version}-amd64-netinst.iso",
             r"Version:\s+(\d+\.\d+)"),
            ("Windows Server 2025","windows-server-2025", "windows", "rg-adguard", None, None, None),
            ("Windows Server 2022","windows-server-2022", "windows", "rg-adguard", None, None, None),
            ("Windows Server 2019","windows-server-2019", "windows", "rg-adguard", None, None, None),
            ("Windows 11",         "windows-11",          "windows", "rg-adguard", None, None, None),
        ]
        for d in distros:
            conn.execute("""
                INSERT OR IGNORE INTO distros
                  (name, slug, type, source, check_url, download_url_template, version_pattern)
                VALUES (?,?,?,?,?,?,?)
            """, d)
        conn.commit()
        logger.info("Base de données initialisée avec succès.")
    except Exception as e:
        logger.error(f"Erreur d'initialisation (Probablement un verrou CIFS) : {e}")
    finally:
        conn.close()

# ─── Version checkers ────────────────────────────────────────────────────────

async def get_latest_ubuntu_lts() -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://changelogs.ubuntu.com/meta-release-lts")
            versions = re.findall(r"Version:\s+(\d+\.\d+)", r.text)
            if versions:
                return sorted(versions, key=lambda v: tuple(map(int, v.split("."))))[-1]
    except Exception as e:
        logger.error(f"Ubuntu check failed: {e}")
    return None

async def get_latest_debian_stable() -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get("https://deb.debian.org/debian/dists/stable/Release")
            match = re.search(r"Version:\s+(\d+\.\d+)", r.text)
            if match:
                return match.group(1)
    except Exception as e:
        logger.error(f"Debian check failed: {e}")
    return None

async def get_latest_version(distro: sqlite3.Row) -> Optional[str]:
    slug   = distro["slug"]
    source = distro["source"] if "source" in distro.keys() else "direct"
    if source == "rg-adguard":
        meta = await rg_resolve_latest(slug)
        return meta["version_label"] if meta else None
    if slug == "ubuntu-lts":
        return await get_latest_ubuntu_lts()
    if slug == "debian-stable":
        return await get_latest_debian_stable()
    if distro["check_url"] and distro["version_pattern"]:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(distro["check_url"])
                m = re.search(distro["version_pattern"], r.text)
                if m:
                    return m.group(1)
        except Exception as e:
            logger.error(f"Version check failed for {slug}: {e}")
    return None

def build_download_url(distro: sqlite3.Row, version: str) -> Optional[str]:
    if not distro["download_url_template"]:
        return None
    return distro["download_url_template"].replace("{version}", version)

# ─── Downloader ───────────────────────────────────────────────────────────────

def compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def compute_md5(filepath: str) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def download_iso_sync(distro_id: int, version: str, url: str, track_id: str,
                      expected_sha256: str = None, expected_md5: str = None,
                      source: str = "direct"):
    conn = get_db()
    distro = conn.execute("SELECT * FROM distros WHERE id=?", (distro_id,)).fetchone()
    if not distro:
        conn.close()
        return

    slug = distro["slug"]
    safe_version = re.sub(r'[^\w\.\-]', '_', version)[:80]
    dest_dir = ISO_DIR / slug / safe_version
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = url.split("/")[-1].split("?")[0] or f"{slug}-{safe_version}.iso"
    if not filename.lower().endswith(".iso"):
        filename += ".iso"
    filepath = dest_dir / filename

    download_progress[track_id] = {
        "distro": distro["name"], "version": version, "filename": filename,
        "percent": 0, "status": "downloading", "source": source,
        "size": "", "downloaded": "",
    }

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ISOWatcher/1.1"})
        with urllib.request.urlopen(req) as response:
            total_size = int(response.headers.get('Content-Length', 0))
            block_size = 65536
            count = 0
            with open(str(filepath), 'wb') as f:
                while True:
                    block = response.read(block_size)
                    if not block:
                        break
                    f.write(block)
                    count += 1
                    if total_size > 0:
                        pct = min(int(count * block_size * 100 / total_size), 100)
                        dl  = count * block_size
                        download_progress[track_id].update({
                            "percent": pct,
                            "size": f"{total_size/1e9:.2f} GB",
                            "downloaded": f"{min(dl, total_size)/1e9:.2f} GB",
                        })

        size   = filepath.stat().st_size
        sha256 = compute_sha256(str(filepath))
        md5    = compute_md5(str(filepath))

        checksum_ok  = True
        checksum_msg = ""
        if expected_sha256 and sha256 != expected_sha256.lower():
            checksum_ok  = False
            checksum_msg = f"SHA-256 mismatch! Attendu:{expected_sha256[:16]}... Obtenu:{sha256[:16]}..."
        elif expected_md5 and md5 != expected_md5.lower():
            checksum_ok  = False
            checksum_msg = f"MD5 mismatch! Attendu:{expected_md5} Obtenu:{md5}"

        conn.execute("""
            INSERT INTO iso_library
              (distro_id, version, arch, filename, filepath, size_bytes,
               checksum_sha256, checksum_md5, download_url, source, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (distro_id, version, distro["arch"], filename, str(filepath),
              size, sha256, md5, url, source,
              'complete' if checksum_ok else 'checksum_warning'))
        conn.execute("UPDATE distros SET latest_version=?, last_checked=? WHERE id=?",
                     (version, datetime.now().isoformat(), distro_id))
        msg = f"Downloaded {filename} ({size/1e9:.2f} GB)"
        if checksum_msg:
            msg += f" | ⚠ {checksum_msg}"
        conn.execute("INSERT INTO download_log (distro_id,version,status,message) VALUES (?,?,?,?)",
                     (distro_id, version, "success" if checksum_ok else "warning", msg))
        conn.commit()

        download_progress[track_id]["status"]  = "complete"
        download_progress[track_id]["percent"] = 100
        if checksum_msg:
            download_progress[track_id]["warning"] = checksum_msg

        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        webhook  = settings.get("discord_webhook", "")
        if webhook:
            _discord_downloaded(webhook, distro["name"], version, filename, size, checksum_ok, source)

    except Exception as e:
        logger.error(f"Download error: {e}")
        download_progress[track_id]["status"] = "error"
        download_progress[track_id]["error"]  = str(e)
        conn.execute("INSERT INTO download_log (distro_id,version,status,message) VALUES (?,?,?,?)",
                     (distro_id, version, "error", str(e)))
        conn.commit()
    finally:
        conn.close()

def _discord_downloaded(webhook, name, version, filename, size, ok=True, source="direct"):
    source_label = "rg-adguard (OEM Microsoft)" if source == "rg-adguard" else \
                   "Manuel" if source == "manual" else "Direct"
    payload = {"embeds": [{"title": f"{'✅' if ok else '⚠️'} ISO téléchargée",
        "color": 0x00b4d8 if ok else 0xffd600,
        "fields": [
            {"name": "Distribution", "value": name,        "inline": True},
            {"name": "Version",      "value": version,     "inline": True},
            {"name": "Source",       "value": source_label,"inline": True},
            {"name": "Fichier",      "value": filename,    "inline": False},
            {"name": "Taille",       "value": f"{size/1e9:.2f} GB", "inline": True},
            {"name": "Checksum",     "value": "✅ Vérifié" if ok else "⚠️ Divergence", "inline": True},
        ],
        "timestamp": datetime.now().isoformat(),
        "footer": {"text": "ISOWatcher v1.1"},
    }]}
    try:
        req = urllib.request.Request(webhook, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error(f"Discord error: {e}")

def _discord_new_version(webhook, name, version, info_url):
    payload = {"embeds": [{"title": "🔔 Nouvelle version détectée",
        "description": f"**{name}** — version `{version}`\n"
                       f"URL directe non résolue automatiquement.\n"
                       f"[Voir sur rg-adguard]({info_url})",
        "color": 0x7c3aed,
        "timestamp": datetime.now().isoformat(),
        "footer": {"text": "ISOWatcher v1.1"},
    }]}
    try:
        req = urllib.request.Request(webhook, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error(f"Discord error: {e}")

# ─── Check & auto-download ────────────────────────────────────────────────────

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
            logger.error(f"Check failed for {distro['name']}: {e}")

async def _check_rg(distro):
    meta = await rg_resolve_latest(distro["slug"])
    if not meta:
        return
    version = meta["version_label"]
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM iso_library WHERE distro_id=? AND version=? AND status IN ('complete','checksum_warning')",
        (distro["id"], version)
    ).fetchone()
    conn.execute("UPDATE distros SET last_checked=?, latest_version=? WHERE id=?",
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
                      f"Nouvelle version détectée : {version}. URL directe non résolue. Voir : {meta['info_url']}"))
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        conn.commit()
        conn.close()
        webhook = settings.get("discord_webhook", "")
        if webhook:
            _discord_new_version(webhook, distro["name"], version, meta["info_url"])
        return
    track_id = f"{distro['slug']}-{datetime.now().strftime('%H%M%S')}"
    t = threading.Thread(
        target=download_iso_sync,
        args=(distro["id"], version, dl_url, track_id),
        kwargs={"expected_sha256": meta.get("sha256"), "expected_md5": meta.get("md5"),
                "source": "rg-adguard"},
        daemon=True
    )
    t.start()

async def _check_direct(distro):
    if not distro["download_url_template"]:
        return
    latest = await get_latest_version(distro)
    if not latest:
        return
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM iso_library WHERE distro_id=? AND version=? AND status IN ('complete','checksum_warning')",
        (distro["id"], latest)
    ).fetchone()
    conn.execute("UPDATE distros SET last_checked=?, latest_version=? WHERE id=?",
                 (datetime.now().isoformat(), latest, distro["id"]))
    conn.commit()
    conn.close()
    if existing:
        return
    url = build_download_url(distro, latest)
    if not url:
        return
    track_id = f"{distro['slug']}-{latest}-{datetime.now().strftime('%H%M%S')}"
    t = threading.Thread(
        target=download_iso_sync,
        args=(distro["id"], latest, url, track_id),
        kwargs={"source": "direct"},
        daemon=True
    )
    t.start()

def run_check():
    asyncio.run(check_and_download_all())

def setup_scheduler(hour: int, minute: int, days: str):
    scheduler.remove_all_jobs()
    scheduler.add_job(run_check, CronTrigger(day_of_week=days, hour=hour, minute=minute),
                      id="main_check")
    logger.info(f"Scheduler: {days} à {hour:02d}:{minute:02d}")

# ─── Models ──────────────────────────────────────────────────────────────────

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

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

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
        raise HTTPException(400, "distro_id, version, url required")
    track_id = f"manual-{distro_id}-{datetime.now().strftime('%H%M%S')}"
    t = threading.Thread(
        target=download_iso_sync,
        args=(distro_id, version, url, track_id),
        kwargs={"source": "manual"},
        daemon=True
    )
    t.start()
    return {"status": "started", "track_id": track_id}

@app.get("/api/progress")
async def get_progress():
    return download_progress

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
        "total_isos": total_isos,
        "total_size_gb": round(total_size / 1e9, 2),
        "distros_monitored": distros_count,
        "rg_adguard_distros": rg_count,
        "last_download": last_dl,
        "active_downloads": len([p for p in download_progress.values() if p.get("status") == "downloading"])
    }

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
    webhook = payload.get("webhook_url", "")
    if not webhook:
        raise HTTPException(400, "webhook_url required")
    try:
        p = {"embeds": [{"title": "🔔 Test ISOWatcher",
            "description": "Notifications Discord opérationnelles ✅",
            "color": 0x00b4d8, "timestamp": datetime.now().isoformat(),
            "footer": {"text": "ISOWatcher v1.1"}}]}
        req = urllib.request.Request(webhook, data=json.dumps(p).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(400, str(e))

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
    return {"version": version, "distro": distro["name"], "source": "direct"}

@app.get("/api/rg-sources")
async def list_rg_sources():
    return [{"slug": s, "name": c["name"]} for s, c in RG_CATEGORIES.items()]

# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("Lancement du startup...")
    init_db()
    conn = get_db()
    try:
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        setup_scheduler(int(settings.get("schedule_hour", 3)),
                        int(settings.get("schedule_minute", 0)),
                        settings.get("schedule_days", "mon"))
        if not scheduler.running:
            scheduler.start()
        logger.info("ISOWatcher v1.1 démarré — rg-adguard intégré")
    except Exception as e:
        logger.error(f"Erreur fatale lors du démarrage : {e}")
    finally:
        conn.close()