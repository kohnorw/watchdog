import os
import re
import time
import threading
import logging
import json
import queue
import subprocess
import requests
from datetime import datetime
from flask import Flask, jsonify, request, Response, stream_with_context
from plexapi.server import PlexServer
from jinja2 import Template

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE = "/data/config.json"
CONFIG_DEFAULTS = {
    "PLEX_URL":               "",
    "PLEX_TOKEN":             "",
    "PLEX_TV_LIBRARY":        "TV Shows",
    "SONARR_URL":             "",
    "SONARR_API_KEY":         "",
    "SONARR_ROOT_FOLDER":     "/tv",
    "SONARR_QUALITY_PROFILE": "HD-1080p",
    "TMDB_API_KEY":           "",
    "DEBRID_PATH":            "/docker/zurg/mnt/zurg/shows",
    "POLL_INTERVAL":          30,
    "AUTO_SYMLINK":           False,
    "SETUP_COMPLETE":         False,
    "INITIAL_SCAN_DONE":      False,
    "IGNORED_SERIES":         [],
}

def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg

def save_config():
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save config: {e}")

CONFIG = load_config()

# ── State ─────────────────────────────────────────────────────────────────────
log_queue        = queue.Queue()
log_history      = []
LOG_HISTORY_MAX  = 2000
watchdog_running = False
watchdog_thread  = None
scan_running     = False
pending_readd    = []

# ── Logging ───────────────────────────────────────────────────────────────────
class QueueHandler(logging.Handler):
    def emit(self, record):
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": record.levelname, "msg": self.format(record)}
        log_queue.put(entry)
        log_history.append(entry)
        if len(log_history) > LOG_HISTORY_MAX:
            log_history.pop(0)

logger = logging.getLogger("watchdog")
logger.setLevel(logging.DEBUG)
qh = QueueHandler()
qh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(qh)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(sh)

ONGOING_STATUSES = {"returning series", "in production", "planned", "pilot", "continuing"}
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}

# ── Sonarr ────────────────────────────────────────────────────────────────────
def sonarr_get(path):
    r = requests.get(f"{CONFIG['SONARR_URL']}/api/v3/{path}", headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]}, timeout=10)
    r.raise_for_status()
    return r.json()

def sonarr_post(path, payload):
    r = requests.post(f"{CONFIG['SONARR_URL']}/api/v3/{path}", headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]}, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()

def get_quality_profile_id():
    profiles = sonarr_get("qualityprofile")
    for p in profiles:
        if p["name"].lower() == CONFIG["SONARR_QUALITY_PROFILE"].lower():
            return p["id"]
    return profiles[0]["id"] if profiles else 1

def get_sonarr_series_map():
    return {s["tvdbId"]: s for s in sonarr_get("series") if s.get("tvdbId")}

def refresh_and_rescan_series(sonarr_id, title):
    """
    1. RefreshSeries  — pulls latest episode/metadata from TVDB
    2. RescanSeries   — scans disk and imports any symlinked files
    """
    try:
        sonarr_post("command", {"name": "RefreshSeries", "seriesId": sonarr_id})
        logger.info(f"  🔄 Refresh triggered for '{title}'")
    except Exception as e:
        logger.warning(f"  ⚠ Refresh failed for '{title}': {e}")
    time.sleep(2)  # give Sonarr time to finish refresh before disk scan
    try:
        sonarr_post("command", {"name": "RescanSeries", "seriesId": sonarr_id})
        logger.info(f"  🔍 Rescan triggered for '{title}'")
        return True
    except Exception as e:
        logger.warning(f"  ⚠ Rescan failed for '{title}': {e}")
        return False

def rescan_series(sonarr_id, title):
    try:
        sonarr_post("command", {"name": "RescanSeries", "seriesId": sonarr_id})
        logger.info(f"  🔍 Rescan triggered for '{title}'")
        return True
    except Exception as e:
        logger.warning(f"  ⚠ Rescan failed for '{title}': {e}")
        return False

# ── TMDB ──────────────────────────────────────────────────────────────────────
def get_show_status(tvdb_id, title):
    key = CONFIG.get("TMDB_API_KEY", "")
    if key:
        try:
            r = requests.get(f"https://api.themoviedb.org/3/find/{tvdb_id}",
                             params={"api_key": key, "external_source": "tvdb_id"}, timeout=8)
            results = r.json().get("tv_results", [])
            if results:
                sid = results[0]["id"]
                r2 = requests.get(f"https://api.themoviedb.org/3/tv/{sid}", params={"api_key": key}, timeout=8)
                status = r2.json().get("status", "").lower()
                return status in ONGOING_STATUSES, status
        except Exception as e:
            logger.debug(f"TMDB check failed for '{title}': {e}")
    try:
        results = sonarr_get(f"series/lookup?term=tvdb:{tvdb_id}")
        if results:
            status = results[0].get("status", "").lower()
            return status in {"continuing", "upcoming"}, status
    except Exception:
        pass
    return True, "unknown"

# ── Add series ────────────────────────────────────────────────────────────────
def add_series_to_sonarr(tvdb_id, title, quality_profile_id):
    try:
        results = sonarr_get(f"series/lookup?term=tvdb:{tvdb_id}")
        if not results:
            logger.warning(f"  ⚠ No lookup result for '{title}' (TVDB:{tvdb_id})")
            return False
        lookup = results[0]
        # Monitor all seasons, Sonarr will only track missing episodes
        seasons = [dict(s, monitored=True) for s in lookup.get("seasons", [])]
        payload = {
            "title":            lookup["title"],
            "tvdbId":           tvdb_id,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath":   CONFIG["SONARR_ROOT_FOLDER"],
            "seasonFolder":     True,
            "monitored":        True,
            "addOptions": {
                "monitor":                      "missingOnly",
                "searchForMissingEpisodes":     True,
                "searchForCutoffUnmetEpisodes": False,
            },
            "seasons":  seasons,
            "images":   lookup.get("images", []),
            "path":     f"{CONFIG['SONARR_ROOT_FOLDER']}/{lookup['title']}",
        }
        added = sonarr_post("series", payload)
        sonarr_id = added.get("id")
        logger.info(f"  ✅ Added: {lookup['title']} (TVDB:{tvdb_id}) — all episodes marked missing")
        # Immediately symlink any available debrid files for this new series
        time.sleep(1)
        logger.info(f"  🔗 Creating symlinks for newly added series: {lookup['title']}")
        symlink_series(added)
        return True
    except requests.HTTPError as e:
        if e.response.status_code == 400:
            logger.debug(f"  Already in Sonarr: {title}")
        else:
            logger.error(f"  ❌ Failed to add '{title}': {e}")
        return False

# ── Debrid symlinks ───────────────────────────────────────────────────────────
def normalize(s):
    """Lowercase, strip punctuation and extra spaces for fuzzy matching."""
    s = s.lower()
    s = re.sub(r'[:\-"!&.,]', " ", s)   # common punctuation to space
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_readable(path):
    """Check if a file is actually readable (not just that the path exists)."""
    try:
        with open(path, 'rb') as f:
            f.read(1024)  # read first 1KB to confirm file is accessible
        return True
    except (IOError, OSError):
        return False

def build_debrid_index(debrid_path):
    """Build a flat index of all video files in debrid: {filename: full_path}"""
    index = {}
    try:
        for folder in os.listdir(debrid_path):
            fp = os.path.join(debrid_path, folder)
            if not os.path.isdir(fp):
                continue
            for root, dirs, files in os.walk(fp):
                for fname in files:
                    if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
                        index[fname] = os.path.join(root, fname)
    except Exception as e:
        logger.warning(f"  ⚠ Could not build debrid index: {e}")
    return index

def get_plex_files_for_series(title, tvdb_id):
    """
    Ask Plex for all episode file paths for this series.
    Returns list of (season_num, ep_num, file_path) tuples.
    """
    try:
        plex   = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
        tv_lib = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
        results = []
        for show in tv_lib.all():
            match = False
            if tvdb_id:
                for guid in show.guids:
                    if "tvdb://" in guid.id:
                        try:
                            if int(guid.id.replace("tvdb://", "")) == tvdb_id:
                                match = True
                        except ValueError:
                            pass
            if not match and show.title.lower() == title.lower():
                match = True
            if not match:
                continue
            for season in show.seasons():
                snum = season.seasonNumber
                if snum == 0:
                    continue
                for ep in season.episodes():
                    try:
                        fpath = ep.media[0].parts[0].file
                        results.append((snum, ep.index, fpath))
                    except (IndexError, AttributeError):
                        pass
            break
        return results
    except Exception as e:
        logger.warning(f"  ⚠ Plex lookup failed for '{title}': {e}")
        return []

def find_debrid_files_for_series(title, tvdb_id=None):
    """
    1. Get all episode file paths from Plex for this series
    2. For each Plex file, check if the filename exists in debrid
    3. Only return files that exist and are accessible in debrid
    """
    debrid_path = CONFIG.get("DEBRID_PATH", "").strip()
    if not debrid_path or not os.path.isdir(debrid_path):
        logger.warning(f"  ⚠ Debrid path not found: {debrid_path}")
        return []

    logger.info(f"  🔍 Getting episode list from Plex for '{title}'...")
    plex_files = get_plex_files_for_series(title, tvdb_id)

    if not plex_files:
        logger.info(f"  ⚠ No Plex episodes found for '{title}' — falling back to debrid scan")
        return find_debrid_files_fallback(title, debrid_path)

    logger.info(f"  📺 Plex has {len(plex_files)} episodes — cross-referencing with debrid...")
    debrid_index = build_debrid_index(debrid_path)
    logger.info(f"  📦 Debrid index: {len(debrid_index)} video files")

    found = []
    missing = []
    for snum, enum, plex_path in plex_files:
        fname = os.path.basename(plex_path)
        # Check if exact filename exists in debrid
        if fname in debrid_index:
            debrid_file = debrid_index[fname]
            if is_readable(debrid_file):
                found.append(debrid_file)
                logger.debug(f"    ✅ S{snum:02}E{enum:02} readable in debrid: {fname}")
            else:
                missing.append(f"S{snum:02}E{enum:02}")
                logger.info(f"    ⚠ S{snum:02}E{enum:02} exists but not readable (debrid not cached?): {fname}")
        else:
            # Try matching by SxxExx pattern in case filename differs
            ep_pattern = re.compile(rf"[Ss]{snum:02}[Ee]{enum:02}", re.IGNORECASE)
            alt = next((v for k, v in debrid_index.items() if ep_pattern.search(k) and
                        normalize(title.split("(")[0]) in normalize(k)), None)
            if alt and is_readable(alt):
                found.append(alt)
                logger.debug(f"    ✅ S{snum:02}E{enum:02} matched by pattern: {os.path.basename(alt)}")
            else:
                missing.append(f"S{snum:02}E{enum:02}")
                logger.debug(f"    ❌ S{snum:02}E{enum:02} not found/readable in debrid: {fname}")

    logger.info(f"  ✅ {len(found)} accessible in debrid, {len(missing)} not found/accessible")
    if missing:
        logger.debug(f"  Missing: {', '.join(missing[:10])}{'...' if len(missing)>10 else ''}")
    return found

def find_debrid_files_fallback(title, debrid_path):
    """Fallback: search debrid folder by show name when Plex has no data."""
    clean_title = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()
    norm_title  = normalize(clean_title)
    logger.info(f"  🔍 Fallback debrid search for '{clean_title}'")
    try:
        all_folders = os.listdir(debrid_path)
    except Exception as e:
        logger.warning(f"  ⚠ Could not list debrid: {e}")
        return []

    # Score each folder
    norm_words = norm_title.split()
    scored = []
    for folder in all_folders:
        norm_f = normalize(folder)
        score  = sum(1 for w in norm_words if w in norm_f)
        if score > 0:
            scored.append((score, folder))
    scored.sort(reverse=True)
    if not scored:
        logger.info(f"  ❌ No debrid folder found for '{clean_title}'")
        return []

    best = scored[0][0]
    matched = [f for s, f in scored if s >= max(1, best - 1)]
    logger.info(f"  📁 Fallback matched: {', '.join(matched)}")

    files = []
    for folder in matched:
        folder_path = os.path.join(debrid_path, folder)
        if not os.path.isdir(folder_path):
            continue
        for root, dirs, fnames in os.walk(folder_path):
            for fname in fnames:
                if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
                    full = os.path.join(root, fname)
                    if is_readable(full):
                        files.append(full)
    logger.info(f"  📦 Fallback found {len(files)} accessible files")
    return files

def remove_missing_episode_files(sonarr_series):
    """
    Check all episode files registered in Sonarr for this series.
    If the symlink no longer exists or is not readable, delete the episode file
    record from Sonarr so it knows to re-download it.
    """
    title     = sonarr_series["title"]
    sonarr_id = sonarr_series["id"]
    try:
        ep_files = sonarr_get(f"episodefile?seriesId={sonarr_id}")
    except Exception as e:
        logger.warning(f"  ⚠ Could not fetch episode files for '{title}': {e}")
        return 0

    removed = 0
    for ef in ep_files:
        path = ef.get("path", "")
        if not path:
            continue
        # Check if the file (symlink) still exists and is readable
        if os.path.exists(path) and is_readable(path):
            continue
        # File is gone or unreadable — remove from Sonarr so it re-downloads
        try:
            requests.delete(
                f"{CONFIG['SONARR_URL']}/api/v3/episodefile/{ef['id']}",
                headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]},
                timeout=10
            ).raise_for_status()
            removed += 1
            reason = "unreadable" if os.path.exists(path) else "missing"
            logger.info(f"  🗑 Removed {reason} episode file from Sonarr: {os.path.basename(path)}")
        except Exception as e:
            logger.warning(f"  ⚠ Could not remove episode file {ef['id']}: {e}")

    if removed:
        logger.info(f"  ✅ '{title}' — {removed} episode files removed from Sonarr (will re-download)")
    return removed

def symlink_series(sonarr_series):
    title       = sonarr_series["title"]
    series_path = sonarr_series.get("path", "")
    if not series_path:
        logger.warning(f"  ⚠ No Sonarr path for '{title}'")
        return 0

    debrid_files = find_debrid_files_for_series(title, tvdb_id=sonarr_series.get("tvdbId"))
    if not debrid_files:
        return 0

    if not os.path.exists(series_path):
        logger.info(f"  📁 Creating series directory: {series_path}")
    os.makedirs(series_path, exist_ok=True)
    os.chmod(series_path, 0o777)

    created = repaired = skipped = 0

    for src in debrid_files:
        filename = os.path.basename(src)
        season_num = None
        for part in src.replace("\\", "/").split("/"):
            if part.lower().startswith("season "):
                try:
                    season_num = int(part.lower().replace("season ", ""))
                except ValueError:
                    pass
        if season_num is None:
            m = re.search(r"[Ss](\d{1,2})[Ee]\d{1,2}", filename)
            if m:
                season_num = int(m.group(1))

        season_dir = os.path.join(series_path, f"Season {season_num:02}") if season_num else series_path
        if not os.path.exists(season_dir):
            logger.info(f"    📁 Creating season directory: {season_dir}")
        os.makedirs(season_dir, exist_ok=True)
        os.chmod(season_dir, 0o777)

        dst = os.path.join(season_dir, filename)

        # Verify source is readable before symlinking
        if not is_readable(src):
            logger.info(f"    ⏭ Skipping (not readable in debrid): {filename}")
            continue

        # Valid readable symlink to same src — skip
        if os.path.islink(dst) and is_readable(dst):
            if os.readlink(dst) == src:
                skipped += 1
                continue
            logger.info(f"    🔄 Updating symlink (target changed): {filename}")
            os.remove(dst)
            repaired += 1
        elif os.path.islink(dst):
            reason = "unreadable" if os.path.exists(dst) else "broken"
            logger.info(f"    🔧 Removing {reason} symlink: {filename}")
            os.remove(dst)
            repaired += 1
        elif os.path.exists(dst):
            skipped += 1
            continue

        try:
            os.symlink(src, dst)
            created += 1
            logger.info(f"    🔗 Symlinked: {filename}")
        except Exception as e:
            logger.warning(f"    ⚠ Symlink failed '{filename}': {e}")

    total = created + repaired
    if total:
        logger.info(f"  ✅ '{title}' — {created} new, {repaired} repaired, {skipped} unchanged")
    else:
        logger.debug(f"  ✓ '{title}' — {skipped} up to date")
    return created

def symlink_all(sonarr_map):
    logger.info("🔗 Starting symlink pass for all series...")
    total = 0
    for tvdb_id, series in sonarr_map.items():
        total += symlink_series(series)
        time.sleep(0.1)
    logger.info(f"✅ Symlink pass done — {total} new symlinks")

def check_and_repair_symlinks(sonarr_map):
    debrid_path = CONFIG.get("DEBRID_PATH", "").strip()
    if not debrid_path or not os.path.isdir(debrid_path):
        return 0
    logger.info(f"🔧 Checking symlinks...")
    # Build debrid file index
    file_map = {}
    try:
        for folder in os.listdir(debrid_path):
            fp = os.path.join(debrid_path, folder)
            if os.path.isdir(fp):
                for root, dirs, files in os.walk(fp):
                    for fname in files:
                        if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
                            file_map[fname] = os.path.join(root, fname)
        logger.info(f"  📦 Debrid index: {len(file_map)} files")
    except Exception as e:
        logger.warning(f"  ⚠ Could not build debrid index: {e}")
        return 0

    removed = repaired = 0
    for tvdb_id, series in sonarr_map.items():
        path = series.get("path", "")
        if not path or not os.path.isdir(path):
            continue
        for root, dirs, files in os.walk(path):
            for fname in files:
                fpath = os.path.join(root, fname)
                if not os.path.islink(fpath):
                    continue
                if os.path.exists(fpath):
                    continue
                old_target = os.readlink(fpath)
                try:
                    os.remove(fpath)
                    removed += 1
                except Exception:
                    continue
                new_target = file_map.get(fname)
                if new_target and is_readable(new_target):
                    try:
                        os.symlink(new_target, fpath)
                        repaired += 1
                        logger.info(f"  🔧 Repaired: {fname}")
                    except Exception as e:
                        logger.warning(f"  ⚠ Repair failed for {fname}: {e}")
                else:
                    logger.info(f"  🗑 Removed broken symlink (no match): {fname}")
    if removed:
        logger.info(f"🔧 Symlink repair — {repaired} repaired, {removed - repaired} removed")
    else:
        logger.debug("🔧 All symlinks OK")
    return removed

# ── Scans ─────────────────────────────────────────────────────────────────────
def do_initial_scan():
    global watchdog_running, watchdog_thread, scan_running
    if scan_running:
        return
    scan_running = True
    logger.info("🔍 Starting initial Plex scan...")
    try:
        plex   = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
        tv_lib = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
    except Exception as e:
        logger.error(f"❌ Plex connection failed: {e}")
        scan_running = False
        return
    try:
        sonarr_map         = get_sonarr_series_map()
        quality_profile_id = get_quality_profile_id()
        logger.info(f"📋 Sonarr has {len(sonarr_map)} series (TVDB IDs: {list(sonarr_map.keys())[:5]}...)")
    except Exception as e:
        logger.error(f"❌ Sonarr connection failed: {e}")
        scan_running = False
        return

    shows = tv_lib.all()
    logger.info(f"📺 {len(shows)} shows in Plex")
    logger.info(f"📋 {len(sonarr_map)} series currently in Sonarr")

    added = skipped_ended = skipped_exists = skipped_no_tvdb = 0
    for show in shows:
        tvdb_id = None
        for guid in show.guids:
            if "tvdb://" in guid.id:
                try: tvdb_id = int(guid.id.replace("tvdb://", ""))
                except ValueError: pass
        if not tvdb_id:
            skipped_no_tvdb += 1
            logger.debug(f"  ⚠ No TVDB ID for '{show.title}' — skipping")
            continue
        if tvdb_id in sonarr_map:
            skipped_exists += 1
            logger.debug(f"  ✓ Already in Sonarr: '{show.title}' (TVDB:{tvdb_id})")
            continue
        is_ongoing, status = get_show_status(tvdb_id, show.title)
        if not is_ongoing:
            skipped_ended += 1
            logger.debug(f"  ⏭ Ended/cancelled: '{show.title}' [{status}]")
            continue
        if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
            logger.debug(f"  🚫 Ignored: '{show.title}'")
            continue
        logger.info(f"  📡 Adding: {show.title} (TVDB:{tvdb_id}) [{status}]")
        if add_series_to_sonarr(tvdb_id, show.title, quality_profile_id):
            added += 1
            sonarr_map = get_sonarr_series_map()  # refresh so next iteration sees new entry
        time.sleep(0.3)

    logger.info(f"✅ Scan done — {added} added | {skipped_exists} already in Sonarr | {skipped_ended} ended | {skipped_no_tvdb} no TVDB ID")
    sonarr_map = get_sonarr_series_map()
    symlink_all(sonarr_map)
    CONFIG["INITIAL_SCAN_DONE"] = True
    scan_running = False
    save_config()
    logger.info("🐕 Starting watchdog loop...")
    watchdog_running = True
    watchdog_thread  = threading.Thread(target=do_watchdog_loop, daemon=True)
    watchdog_thread.start()

def do_watchdog_loop():
    logger.info(f"🐕 Watchdog running every {CONFIG['POLL_INTERVAL']}s")
    while watchdog_running:
        try:
            plex               = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
            tv_lib             = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
            sonarr_map         = get_sonarr_series_map()
            quality_profile_id = get_quality_profile_id()
            for show in tv_lib.all():
                tvdb_id = None
                for guid in show.guids:
                    if "tvdb://" in guid.id:
                        try: tvdb_id = int(guid.id.replace("tvdb://", ""))
                        except ValueError: pass
                if not tvdb_id or tvdb_id in sonarr_map:
                    continue
                is_ongoing, status = get_show_status(tvdb_id, show.title)
                if not is_ongoing:
                    continue
                if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
                    already = any(p["tvdbId"] == tvdb_id for p in pending_readd)
                    if not already:
                        logger.info(f"⚠ '{show.title}' was deleted — awaiting confirmation")
                        pending_readd.append({"tvdbId": tvdb_id, "title": show.title, "status": status})
                    continue
                logger.info(f"🆕 New show: {show.title} [{status}]")
                add_series_to_sonarr(tvdb_id, show.title, quality_profile_id)
            sonarr_map = get_sonarr_series_map()

            # Remove Sonarr episode file records where symlinks are gone/unreadable
            total_removed = 0
            for tvdb_id, series in sonarr_map.items():
                total_removed += remove_missing_episode_files(series)
            if total_removed:
                logger.info(f"🗑 Removed {total_removed} missing/unreadable episode files from Sonarr")

            # Repair broken symlinks
            broken = check_and_repair_symlinks(sonarr_map)
            if broken:
                logger.info(f"🔧 Repaired {broken} symlinks")

            # Auto-symlink all series if enabled
            if CONFIG.get("AUTO_SYMLINK", False):
                logger.info("🔗 Auto-symlink enabled — syncing all series...")
                symlink_all(sonarr_map)
            else:
                logger.debug("💤 Auto-symlink off — sync manually from Series page")
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        time.sleep(CONFIG["POLL_INTERVAL"])

def auto_start():
    global watchdog_running, watchdog_thread
    logger.info("🔁 Auto-starting watchdog...")
    try:
        plex               = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
        tv_lib             = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
        sonarr_map         = get_sonarr_series_map()
        quality_profile_id = get_quality_profile_id()
        added = 0
        for show in tv_lib.all():
            tvdb_id = None
            for guid in show.guids:
                if "tvdb://" in guid.id:
                    try: tvdb_id = int(guid.id.replace("tvdb://", ""))
                    except ValueError: pass
            if not tvdb_id or tvdb_id in sonarr_map:
                continue
            is_ongoing, status = get_show_status(tvdb_id, show.title)
            if not is_ongoing or tvdb_id in CONFIG.get("IGNORED_SERIES", []):
                continue
            logger.info(f"  🆕 New show: {show.title} [{status}]")
            add_series_to_sonarr(tvdb_id, show.title, quality_profile_id)
            added += 1
            time.sleep(0.3)
        sonarr_map = get_sonarr_series_map()
        symlink_all(sonarr_map)
        logger.info(f"✅ Auto-start done — {added} new shows added")
    except Exception as e:
        logger.error(f"Auto-start error: {e}")
    logger.info("🐕 Starting watchdog loop...")
    watchdog_running = True
    watchdog_thread  = threading.Thread(target=do_watchdog_loop, daemon=True)
    watchdog_thread.start()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html")) as f:
        tmpl = Template(f.read())
    return tmpl.render(config=CONFIG)

@app.route("/api/status")
def api_status():
    return jsonify({"setupComplete": CONFIG.get("SETUP_COMPLETE", False),
                    "initialScanDone": CONFIG.get("INITIAL_SCAN_DONE", False),
                    "watchdogRunning": watchdog_running,
                    "scanRunning": scan_running})

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        for k, v in (request.json or {}).items():
            if k in CONFIG:
                CONFIG[k] = v
        save_config()
        return jsonify({"ok": True})
    return jsonify(CONFIG)

@app.route("/api/setup", methods=["POST"])
def api_setup():
    for k, v in (request.json or {}).items():
        if k in CONFIG:
            CONFIG[k] = v
    CONFIG["SETUP_COMPLETE"] = True
    save_config()
    logger.info("✅ Setup complete")
    return jsonify({"ok": True})

@app.route("/api/test-connections", methods=["POST"])
def api_test_connections():
    data = request.json or {}
    out  = {"plex": False, "sonarr": False, "tmdb": False, "errors": {}}
    try:
        plex = PlexServer(data.get("PLEX_URL"), data.get("PLEX_TOKEN"))
        plex.library.section(data.get("PLEX_TV_LIBRARY", "TV Shows"))
        out["plex"] = True
    except Exception as e:
        out["errors"]["plex"] = str(e)
    try:
        r = requests.get(f"{data.get('SONARR_URL')}/api/v3/system/status",
                         headers={"X-Api-Key": data.get("SONARR_API_KEY")}, timeout=8)
        r.raise_for_status()
        out["sonarr"] = True
    except Exception as e:
        out["errors"]["sonarr"] = str(e)
    if data.get("TMDB_API_KEY"):
        try:
            r = requests.get("https://api.themoviedb.org/3/configuration",
                             params={"api_key": data.get("TMDB_API_KEY")}, timeout=8)
            r.raise_for_status()
            out["tmdb"] = True
        except Exception as e:
            out["errors"]["tmdb"] = str(e)
    return jsonify(out)

@app.route("/api/start-initial-scan", methods=["POST"])
def api_start_initial_scan():
    if not scan_running:
        threading.Thread(target=do_initial_scan, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/list")
def api_series_list():
    try:
        out = []
        for s in sonarr_get("series"):
            stats = s.get("statistics", {})
            out.append({
                "id":               s["id"],
                "title":            s["title"],
                "tvdbId":           s.get("tvdbId"),
                "status":           s.get("status", ""),
                "monitored":        s.get("monitored", False),
                "episodeCount":     stats.get("episodeCount", 0),
                "episodeFileCount": stats.get("episodeFileCount", 0),
                "seasonCount":      s.get("seasonCount", 0),
                "poster":           next((i["remoteUrl"] for i in s.get("images", []) if i.get("coverType") == "poster"), None),
            })
        out.sort(key=lambda x: x["title"])
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/series/<int:sonarr_id>/link", methods=["POST"])
def api_series_link(sonarr_id):
    def run():
        try:
            series = sonarr_get(f"series/{sonarr_id}")
            logger.info(f"🔗 Symlinking (no Sonarr trigger): {series['title']}")
            symlink_series(series)
            logger.info(f"✅ Symlinks done for '{series['title']}' — no Sonarr actions triggered")
        except Exception as e:
            logger.error(f"Symlink failed for {sonarr_id}: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/link-all", methods=["POST"])
def api_series_link_all():
    def run():
        try:
            symlink_all(get_sonarr_series_map())
        except Exception as e:
            logger.error(f"Bulk symlink failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/<int:sonarr_id>/rescan", methods=["POST"])
def api_series_rescan(sonarr_id):
    """Trigger Sonarr refresh + rescan for a single series."""
    def run():
        try:
            series = sonarr_get(f"series/{sonarr_id}")
            title  = series["title"]
            logger.info(f"🔄 Triggering refresh + rescan for '{title}'")
            refresh_and_rescan_series(sonarr_id, title)
            logger.info(f"✅ Refresh + rescan complete for '{title}'")
        except Exception as e:
            logger.error(f"Rescan failed for {sonarr_id}: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/<int:sonarr_id>/delete", methods=["POST"])
def api_series_delete(sonarr_id):
    try:
        series  = sonarr_get(f"series/{sonarr_id}")
        tvdb_id = series.get("tvdbId")
        title   = series["title"]
        requests.delete(f"{CONFIG['SONARR_URL']}/api/v3/series/{sonarr_id}",
                        headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]},
                        params={"deleteFiles": "false", "addImportListExclusion": "false"}, timeout=10).raise_for_status()
        ignored = CONFIG.get("IGNORED_SERIES", [])
        if tvdb_id and tvdb_id not in ignored:
            ignored.append(tvdb_id)
            CONFIG["IGNORED_SERIES"] = ignored
            save_config()
        logger.info(f"🗑 Deleted '{title}' and added to ignore list")
        return jsonify({"ok": True, "title": title})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/pending-readd")
def api_pending_readd():
    return jsonify(pending_readd)

@app.route("/api/pending-readd/confirm", methods=["POST"])
def api_pending_readd_confirm():
    global pending_readd
    data    = request.json or {}
    tvdb_id = data.get("tvdbId")
    title   = data.get("title", "")
    ignored = CONFIG.get("IGNORED_SERIES", [])
    if tvdb_id in ignored:
        ignored.remove(tvdb_id)
        CONFIG["IGNORED_SERIES"] = ignored
        save_config()
    pending_readd = [p for p in pending_readd if p["tvdbId"] != tvdb_id]
    try:
        add_series_to_sonarr(tvdb_id, title, get_quality_profile_id())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})

@app.route("/api/pending-readd/dismiss", methods=["POST"])
def api_pending_readd_dismiss():
    global pending_readd
    tvdb_id = (request.json or {}).get("tvdbId")
    pending_readd = [p for p in pending_readd if p["tvdbId"] != tvdb_id]
    return jsonify({"ok": True})

@app.route("/api/watchdog/stop", methods=["POST"])
def api_watchdog_stop():
    global watchdog_running
    watchdog_running = False
    logger.info("🛑 Watchdog stopped")
    return jsonify({"ok": True})

@app.route("/api/logs/history")
def api_logs_history():
    return jsonify(log_history)

@app.route("/api/logs/stream")
def api_logs_stream():
    def generate():
        while True:
            try:
                entry = log_queue.get(timeout=1)
                yield f"data: {json.dumps(entry)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'ping': True})}\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/quality-profiles")
def api_quality_profiles():
    try:
        return jsonify(sonarr_get("qualityprofile"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    if CONFIG.get("INITIAL_SCAN_DONE") and CONFIG.get("SETUP_COMPLETE"):
        logger.info("🔁 Previous scan detected — auto-starting...")
        threading.Thread(target=auto_start, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
