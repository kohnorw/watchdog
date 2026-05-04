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

# ── Config ─────────────────────────────────────────────────────────────────────
CONFIG_FILE = "/data/config.json"
DEFAULTS = {
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
    cfg = dict(DEFAULTS)
    try:
        os.makedirs("/data", exist_ok=True)
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save config: {e}")

CONFIG = load_config()

# ── State ──────────────────────────────────────────────────────────────────────
log_queue        = queue.Queue()
log_history      = []
watchdog_running = False
watchdog_thread  = None
scan_running     = False
pending_readd    = []

# ── Logging ────────────────────────────────────────────────────────────────────
class QueueHandler(logging.Handler):
    def emit(self, record):
        entry = {"time": datetime.now().strftime("%H:%M:%S"),
                 "level": record.levelname, "msg": self.format(record)}
        log_queue.put(entry)
        log_history.append(entry)
        if len(log_history) > 2000:
            log_history.pop(0)

logger = logging.getLogger("watchdog")
logger.setLevel(logging.DEBUG)
qh = QueueHandler()
qh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(qh)
sh = logging.StreamHandler()
sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(sh)

VIDEO_EXTS    = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}
ONGOING_STATI = {"returning series", "in production", "planned", "pilot", "continuing"}

# ── Sonarr helpers ─────────────────────────────────────────────────────────────
def sonarr_get(path):
    r = requests.get(
        f"{CONFIG['SONARR_URL']}/api/v3/{path}",
        headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]}, timeout=10)
    r.raise_for_status()
    return r.json()

def sonarr_post(path, payload):
    r = requests.post(
        f"{CONFIG['SONARR_URL']}/api/v3/{path}",
        headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]},
        json=payload, timeout=10)
    r.raise_for_status()
    return r.json()

def get_quality_profile_id():
    profiles = sonarr_get("qualityprofile")
    for p in profiles:
        if p["name"].lower() == CONFIG["SONARR_QUALITY_PROFILE"].lower():
            return p["id"]
    return profiles[0]["id"] if profiles else 1

def get_sonarr_series():
    """Returns list of all series in Sonarr."""
    return sonarr_get("series")

def build_sonarr_map(series_list):
    """Build {tvdbId: series} from a series list."""
    result = {}
    for s in series_list:
        tid = s.get("tvdbId")
        if tid:
            result[tid] = s
    logger.info(f"📋 Sonarr map: {len(result)} series")
    return result

def rescan_series(sonarr_id, title):
    try:
        sonarr_post("command", {"name": "RescanSeries", "seriesId": sonarr_id})
        logger.info(f"  🔍 Rescan triggered: {title}")
    except Exception as e:
        logger.warning(f"  ⚠ Rescan failed for '{title}': {e}")

def refresh_and_rescan(sonarr_id, title):
    try:
        sonarr_post("command", {"name": "RefreshSeries", "seriesId": sonarr_id})
        logger.info(f"  🔄 Refresh triggered: {title}")
    except Exception as e:
        logger.warning(f"  ⚠ Refresh failed: {e}")
    time.sleep(2)
    rescan_series(sonarr_id, title)

# ── TMDB ───────────────────────────────────────────────────────────────────────
def get_show_status(tvdb_id, title):
    key = CONFIG.get("TMDB_API_KEY", "").strip()
    if key:
        try:
            r = requests.get(
                f"https://api.themoviedb.org/3/find/{tvdb_id}",
                params={"api_key": key, "external_source": "tvdb_id"}, timeout=8)
            results = r.json().get("tv_results", [])
            if results:
                r2 = requests.get(
                    f"https://api.themoviedb.org/3/tv/{results[0]['id']}",
                    params={"api_key": key}, timeout=8)
                status = r2.json().get("status", "").lower()
                return status in ONGOING_STATI, status
        except Exception as e:
            logger.debug(f"TMDB failed for '{title}': {e}")
    try:
        results = sonarr_get(f"series/lookup?term=tvdb:{tvdb_id}")
        if results:
            status = results[0].get("status", "").lower()
            return status in {"continuing", "upcoming"}, status
    except Exception:
        pass
    return True, "unknown"

# ── Add series ─────────────────────────────────────────────────────────────────
def add_series_to_sonarr(tvdb_id, title, quality_profile_id):
    """Add a series to Sonarr. Returns the created series dict or None."""
    try:
        results = sonarr_get(f"series/lookup?term=tvdb:{tvdb_id}")
        if not results:
            logger.warning(f"  ⚠ No Sonarr lookup result for '{title}' (TVDB:{tvdb_id})")
            return None
        lookup = results[0]
        seasons = [dict(s, monitored=True) for s in lookup.get("seasons", [])]
        payload = {
            "title":            lookup["title"],
            "tvdbId":           tvdb_id,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath":   CONFIG["SONARR_ROOT_FOLDER"],
            "seasonFolder":     True,
            "monitored":        True,
            "addOptions": {
                "monitor": "all",
                "searchForMissingEpisodes":     True,
                "searchForCutoffUnmetEpisodes": False,
            },
            "seasons": seasons,
            "images":  lookup.get("images", []),
            "path":    f"{CONFIG['SONARR_ROOT_FOLDER']}/{lookup['title']}",
        }
        created = sonarr_post("series", payload)
        logger.info(f"  ✅ Added to Sonarr: {lookup['title']} (TVDB:{tvdb_id})")
        return created
    except requests.HTTPError as e:
        if e.response.status_code == 400:
            try:
                body = e.response.json()
            except Exception:
                body = e.response.text[:200]
            logger.warning(f"  ⚠ Sonarr rejected '{title}': {body}")
        else:
            logger.error(f"  ❌ Failed to add '{title}': {e.response.status_code}")
        return None

# ── Debrid ─────────────────────────────────────────────────────────────────────
def normalize(s):
    s = s.lower()
    s = re.sub(r'[:\-"!&.,\(\)]', " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_readable(path):
    try:
        with open(path, "rb") as f:
            f.read(1024)
        return True
    except (IOError, OSError):
        return False

def find_debrid_files(title):
    debrid = CONFIG.get("DEBRID_PATH", "").strip()
    if not debrid or not os.path.isdir(debrid):
        logger.warning(f"  ⚠ Debrid path not found: {debrid}")
        return []

    clean = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip()
    norm  = normalize(clean)
    logger.info(f"  🔍 Searching debrid for '{clean}'")

    try:
        all_folders = os.listdir(debrid)
    except Exception as e:
        logger.warning(f"  ⚠ Cannot list debrid: {e}")
        return []

    # Score every folder
    norm_words = [w for w in norm.split() if len(w) >= 2]
    scored = []
    for folder in all_folders:
        norm_f = normalize(folder)
        score  = sum(1 for w in norm_words if w in norm_f)
        if score > 0:
            scored.append((score, folder))
    scored.sort(reverse=True)

    if not scored:
        logger.info(f"  ❌ No debrid folder found for '{clean}'")
        return []

    best = scored[0][0]
    matched = [f for s, f in scored if s >= max(1, best - 1)]
    logger.info(f"  📁 Matched: {', '.join(matched)}")

    files = []
    for folder in matched:
        fp = os.path.join(debrid, folder)
        if not os.path.isdir(fp):
            continue
        count = 0
        for root, dirs, fnames in os.walk(fp):
            for fname in fnames:
                if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
                    full = os.path.join(root, fname)
                    if is_readable(full):
                        files.append(full)
                        count += 1
        logger.info(f"    📼 {folder}: {count} readable files")

    logger.info(f"  📦 Total: {len(files)} files for '{clean}'")
    return files

def symlink_series(sonarr_series):
    title       = sonarr_series.get("title", "?")
    series_path = sonarr_series.get("path", "")
    if not series_path:
        logger.warning(f"  ⚠ No path for '{title}'")
        return 0

    files = find_debrid_files(title)
    if not files:
        return 0

    os.makedirs(series_path, exist_ok=True)
    os.chmod(series_path, 0o777)

    created = repaired = skipped = 0
    for src in files:
        fname = os.path.basename(src)
        snum  = None
        for part in src.replace("\\", "/").split("/"):
            if part.lower().startswith("season "):
                try: snum = int(part.lower().replace("season ", ""))
                except ValueError: pass
        if snum is None:
            m = re.search(r"[Ss](\d{1,2})[Ee]\d{1,2}", fname)
            if m: snum = int(m.group(1))

        sdir = os.path.join(series_path, f"Season {snum:02}") if snum else series_path
        os.makedirs(sdir, exist_ok=True)
        os.chmod(sdir, 0o777)
        dst = os.path.join(sdir, fname)

        if os.path.islink(dst):
            if os.path.exists(dst) and os.readlink(dst) == src:
                skipped += 1
                continue
            os.remove(dst)
            repaired += 1
        elif os.path.exists(dst):
            skipped += 1
            continue

        try:
            os.symlink(src, dst)
            created += 1
            logger.info(f"    🔗 {'Repaired' if repaired else 'Linked'}: {fname}")
        except Exception as e:
            logger.warning(f"    ⚠ Symlink failed '{fname}': {e}")

    total = created + repaired
    if total:
        logger.info(f"  ✅ '{title}': {created} new, {repaired} repaired, {skipped} unchanged")
    return created

def symlink_all(sonarr_map):
    logger.info("🔗 Symlinking all series...")
    total = 0
    for tvdb_id, series in sonarr_map.items():
        total += symlink_series(series)
        time.sleep(0.1)
    logger.info(f"✅ Symlink pass done — {total} new")

def repair_symlinks(sonarr_map):
    debrid = CONFIG.get("DEBRID_PATH", "").strip()
    if not debrid or not os.path.isdir(debrid):
        return 0
    # Build index
    index = {}
    try:
        for folder in os.listdir(debrid):
            fp = os.path.join(debrid, folder)
            if os.path.isdir(fp):
                for root, dirs, files in os.walk(fp):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                            full = os.path.join(root, f)
                            if is_readable(full):
                                index[f] = full
    except Exception as e:
        logger.warning(f"⚠ Cannot build debrid index: {e}")
        return 0

    repaired = removed = 0
    for series in sonarr_map.values():
        path = series.get("path", "")
        if not path or not os.path.isdir(path):
            continue
        for root, dirs, files in os.walk(path):
            for fname in files:
                fpath = os.path.join(root, fname)
                if not os.path.islink(fpath):
                    continue
                if is_readable(fpath):
                    continue
                old_target = os.readlink(fpath)
                try:
                    os.remove(fpath)
                    removed += 1
                except Exception:
                    continue
                new_target = index.get(fname)
                if new_target:
                    try:
                        os.symlink(new_target, fpath)
                        repaired += 1
                        logger.info(f"  🔧 Repaired: {fname}")
                    except Exception as e:
                        logger.warning(f"  ⚠ Repair failed {fname}: {e}")
                else:
                    logger.info(f"  🗑 Removed broken symlink: {fname}")
    if removed:
        logger.info(f"🔧 Symlink repair: {repaired} repaired, {removed-repaired} removed")
    return removed

def remove_missing_ep_files(sonarr_map):
    total = 0
    for series in sonarr_map.values():
        sid = series["id"]
        try:
            ep_files = sonarr_get(f"episodefile?seriesId={sid}")
        except Exception:
            continue
        for ef in ep_files:
            path = ef.get("path", "")
            if not path:
                continue
            if is_readable(path):
                continue
            try:
                requests.delete(
                    f"{CONFIG['SONARR_URL']}/api/v3/episodefile/{ef['id']}",
                    headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]}, timeout=10
                ).raise_for_status()
                total += 1
                logger.info(f"  🗑 Removed missing ep file: {os.path.basename(path)}")
            except Exception as e:
                logger.warning(f"  ⚠ Could not remove ep file: {e}")
    if total:
        logger.info(f"🗑 Removed {total} missing episode files from Sonarr")
    return total

# ── Plex helpers ───────────────────────────────────────────────────────────────
def get_plex():
    return PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])

def get_plex_shows(plex):
    return plex.library.section(CONFIG["PLEX_TV_LIBRARY"]).all()

def get_tvdb_id(show):
    for guid in show.guids:
        if "tvdb://" in guid.id:
            try: return int(guid.id.replace("tvdb://", ""))
            except ValueError: pass
    return None

# ── Initial scan ───────────────────────────────────────────────────────────────
def do_initial_scan():
    global watchdog_running, watchdog_thread, scan_running
    if scan_running:
        logger.warning("⚠ Scan already running")
        return
    scan_running = True
    logger.info("🔍 Starting initial Plex scan...")

    try:
        plex = get_plex()
        tv_lib = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
        logger.info(f"✅ Connected to Plex: {CONFIG['PLEX_TV_LIBRARY']}")
    except Exception as e:
        logger.error(f"❌ Plex connection failed: {e}")
        scan_running = False
        return

    try:
        qp_id      = get_quality_profile_id()
        sonarr_map = build_sonarr_map(get_sonarr_series())
    except Exception as e:
        logger.error(f"❌ Sonarr connection failed: {e}")
        scan_running = False
        return

    shows = tv_lib.all()
    logger.info(f"📺 {len(shows)} shows in Plex | {len(sonarr_map)} already in Sonarr")

    added = skipped_exists = skipped_ended = skipped_ignored = skipped_no_id = 0

    for show in shows:
        tvdb_id = get_tvdb_id(show)

        if not tvdb_id:
            skipped_no_id += 1
            continue

        if tvdb_id in sonarr_map:
            skipped_exists += 1
            continue

        if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
            skipped_ignored += 1
            continue

        is_ongoing, status = get_show_status(tvdb_id, show.title)
        if not is_ongoing:
            skipped_ended += 1
            continue

        logger.info(f"  📡 Adding: {show.title} (TVDB:{tvdb_id}) [{status}]")
        created = add_series_to_sonarr(tvdb_id, show.title, qp_id)
        if created:
            added += 1
            sonarr_map[tvdb_id] = created  # update local map immediately
        time.sleep(0.5)

    logger.info(
        f"✅ Scan complete — {added} added | {skipped_exists} already in Sonarr | "
        f"{skipped_ended} ended | {skipped_ignored} ignored | {skipped_no_id} no TVDB ID"
    )

    # Symlink pass
    logger.info("🔗 Creating symlinks...")
    sonarr_map = build_sonarr_map(get_sonarr_series())
    symlink_all(sonarr_map)

    CONFIG["INITIAL_SCAN_DONE"] = True
    scan_running = False
    save_config()

    logger.info("🐕 Starting watchdog loop...")
    watchdog_running = True
    watchdog_thread  = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog_thread.start()

# ── Watchdog loop ──────────────────────────────────────────────────────────────
def watchdog_loop():
    logger.info(f"🐕 Watchdog running every {CONFIG['POLL_INTERVAL']}s")
    while watchdog_running:
        try:
            plex       = get_plex()
            shows      = get_plex_shows(plex)
            sonarr_map = build_sonarr_map(get_sonarr_series())
            qp_id      = get_quality_profile_id()

            # Check for new shows
            for show in shows:
                tvdb_id = get_tvdb_id(show)
                if not tvdb_id or tvdb_id in sonarr_map:
                    continue
                if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
                    if not any(p["tvdbId"] == tvdb_id for p in pending_readd):
                        logger.info(f"⚠ '{show.title}' was deleted — awaiting confirmation")
                        pending_readd.append({"tvdbId": tvdb_id, "title": show.title})
                    continue
                is_ongoing, status = get_show_status(tvdb_id, show.title)
                if not is_ongoing:
                    continue
                logger.info(f"🆕 New show: {show.title} [{status}]")
                created = add_series_to_sonarr(tvdb_id, show.title, qp_id)
                if created:
                    sonarr_map[tvdb_id] = created

            # Refresh map
            sonarr_map = build_sonarr_map(get_sonarr_series())

            # Remove missing ep files
            remove_missing_ep_files(sonarr_map)

            # Repair broken symlinks
            repair_symlinks(sonarr_map)

            # Auto-symlink if enabled
            if CONFIG.get("AUTO_SYMLINK", False):
                symlink_all(sonarr_map)

        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        logger.debug(f"💤 Sleeping {CONFIG['POLL_INTERVAL']}s...")
        time.sleep(CONFIG["POLL_INTERVAL"])

# ── Auto-start on restart ──────────────────────────────────────────────────────
def auto_start():
    global watchdog_running, watchdog_thread
    logger.info("🔁 Auto-start: checking for new shows...")
    try:
        plex       = get_plex()
        shows      = get_plex_shows(plex)
        sonarr_map = build_sonarr_map(get_sonarr_series())
        qp_id      = get_quality_profile_id()
        added = 0
        for show in shows:
            tvdb_id = get_tvdb_id(show)
            if not tvdb_id or tvdb_id in sonarr_map:
                continue
            if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
                continue
            is_ongoing, status = get_show_status(tvdb_id, show.title)
            if not is_ongoing:
                continue
            logger.info(f"  🆕 New: {show.title} [{status}]")
            created = add_series_to_sonarr(tvdb_id, show.title, qp_id)
            if created:
                added += 1
                sonarr_map[tvdb_id] = created
            time.sleep(0.5)
        sonarr_map = build_sonarr_map(get_sonarr_series())
        symlink_all(sonarr_map)
        logger.info(f"✅ Auto-start done — {added} new shows")
    except Exception as e:
        logger.error(f"Auto-start error: {e}")
    logger.info("🐕 Starting watchdog loop...")
    watchdog_running = True
    watchdog_thread  = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog_thread.start()

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    tmpl_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(tmpl_path) as f:
        return Template(f.read()).render(config=CONFIG)

@app.route("/api/status")
def api_status():
    return jsonify({
        "setupComplete":   CONFIG.get("SETUP_COMPLETE", False),
        "initialScanDone": CONFIG.get("INITIAL_SCAN_DONE", False),
        "watchdogRunning": watchdog_running,
        "scanRunning":     scan_running,
    })

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
        p = PlexServer(data.get("PLEX_URL"), data.get("PLEX_TOKEN"))
        p.library.section(data.get("PLEX_TV_LIBRARY", "TV Shows"))
        out["plex"] = True
    except Exception as e:
        out["errors"]["plex"] = str(e)
    try:
        r = requests.get(
            f"{data.get('SONARR_URL')}/api/v3/system/status",
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
        for s in get_sonarr_series():
            stats = s.get("statistics", {})
            out.append({
                "id":               s["id"],
                "title":            s["title"],
                "tvdbId":           s.get("tvdbId"),
                "status":           s.get("status", ""),
                "monitored":        s.get("monitored", False),
                "episodeCount":     stats.get("episodeCount", 0),
                "episodeFileCount": stats.get("episodeFileCount", 0),
                "poster":           next((i["remoteUrl"] for i in s.get("images", [])
                                          if i.get("coverType") == "poster"), None),
            })
        out.sort(key=lambda x: x["title"])
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/series/<int:sid>/link", methods=["POST"])
def api_series_link(sid):
    def run():
        try:
            s = sonarr_get(f"series/{sid}")
            logger.info(f"🔗 Symlinking: {s['title']}")
            symlink_series(s)
            logger.info(f"✅ Symlink done for '{s['title']}'")
        except Exception as e:
            logger.error(f"Symlink failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/link-all", methods=["POST"])
def api_series_link_all():
    def run():
        try:
            symlink_all(build_sonarr_map(get_sonarr_series()))
        except Exception as e:
            logger.error(f"Bulk symlink failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/<int:sid>/rescan", methods=["POST"])
def api_series_rescan(sid):
    def run():
        try:
            s = sonarr_get(f"series/{sid}")
            refresh_and_rescan(sid, s["title"])
        except Exception as e:
            logger.error(f"Rescan failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/<int:sid>/delete", methods=["POST"])
def api_series_delete(sid):
    try:
        s       = sonarr_get(f"series/{sid}")
        tvdb_id = s.get("tvdbId")
        title   = s["title"]
        requests.delete(
            f"{CONFIG['SONARR_URL']}/api/v3/series/{sid}",
            headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]},
            params={"deleteFiles": "false", "addImportListExclusion": "false"},
            timeout=10).raise_for_status()
        ignored = CONFIG.get("IGNORED_SERIES", [])
        if tvdb_id and tvdb_id not in ignored:
            ignored.append(tvdb_id)
            CONFIG["IGNORED_SERIES"] = ignored
            save_config()
        logger.info(f"🗑 Deleted '{title}' and added to ignore list")
        return jsonify({"ok": True})
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
    created = add_series_to_sonarr(tvdb_id, title, get_quality_profile_id())
    if created:
        logger.info(f"✅ Re-added '{title}'")
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
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    if CONFIG.get("INITIAL_SCAN_DONE") and CONFIG.get("SETUP_COMPLETE"):
        logger.info("🔁 Previous scan detected — auto-starting...")
        threading.Thread(target=auto_start, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
