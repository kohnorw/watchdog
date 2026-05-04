import os
import re
import time
import threading
import logging
import json
import queue
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
    "PLEX_SCAN_INTERVAL":     120,
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
    with open(CONFIG_FILE, "w") as f:
        json.dump(CONFIG, f, indent=2)

CONFIG = load_config()

# ── State ──────────────────────────────────────────────────────────────────────
log_queue        = queue.Queue()
log_history      = []
watchdog_running = False
scan_running     = False
pending_readd    = []
debrid_cache     = {}   # {filename: full_path} — rebuilt each scan cycle

# ── Logging ────────────────────────────────────────────────────────────────────
class QH(logging.Handler):
    def emit(self, record):
        e = {"time": datetime.now().strftime("%H:%M:%S"),
             "level": record.levelname, "msg": self.format(record)}
        log_queue.put(e)
        log_history.append(e)
        if len(log_history) > 2000:
            log_history.pop(0)

logger = logging.getLogger("wd")
logger.setLevel(logging.DEBUG)
qh = QH(); qh.setFormatter(logging.Formatter("%(message)s")); logger.addHandler(qh)
sh = logging.StreamHandler(); sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s")); logger.addHandler(sh)

VIDEO_EXTS    = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov"}
ONGOING_STATI = {"returning series", "in production", "planned", "pilot", "continuing"}

# ── Debrid cache ───────────────────────────────────────────────────────────────
def build_debrid_cache():
    """Walk debrid path and build {filename: full_path} index."""
    global debrid_cache
    debrid = CONFIG.get("DEBRID_PATH", "").strip()
    if not debrid or not os.path.isdir(debrid):
        logger.warning(f"⚠ Debrid path not found: {debrid}")
        return {}
    cache = {}
    count = 0
    for root, dirs, files in os.walk(debrid):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
                cache[fname] = os.path.join(root, fname)
                count += 1
    debrid_cache = cache
    logger.info(f"📦 Debrid cache: {count} files indexed")
    return cache

def find_in_debrid(plex_path, cache):
    """
    Given a Plex file path, find the matching file in debrid cache.
    1. Exact filename match
    2. SxxExx pattern match as fallback
    """
    fname = os.path.basename(plex_path)

    # Exact match
    if fname in cache:
        return cache[fname]

    # Pattern match: find SxxExx in plex filename then search cache
    m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", fname)
    if m:
        pattern = re.compile(
            re.escape(f"S{int(m.group(1)):02}E{int(m.group(2)):02}"), re.IGNORECASE)
        for cname, cpath in cache.items():
            if pattern.search(cname):
                return cpath

    return None

# ── Symlink ────────────────────────────────────────────────────────────────────
def make_symlink(src, dst):
    """Create or update a symlink. Returns True if created/updated."""
    # Already correct symlink — skip
    if os.path.islink(dst) and os.readlink(dst) == src:
        return False
    # Remove stale/broken symlink or existing file
    if os.path.islink(dst) or os.path.exists(dst):
        os.remove(dst)
    try:
        # Ensure parent directory exists first
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(src, dst)
        return True
    except Exception as e:
        logger.warning(f"    ⚠ Symlink failed: {e}")
        return False

def symlink_series_from_plex(plex_show, sonarr_series, cache):
    """
    For each episode in Plex, find the file in debrid cache and symlink
    it into the Sonarr series folder.
    """
    title       = sonarr_series.get("title", plex_show.title)
    series_path = sonarr_series.get("path", "")
    if not series_path:
        logger.warning(f"  ⚠ No Sonarr path for '{title}'")
        return 0

    os.makedirs(series_path, exist_ok=True)
    os.chmod(series_path, 0o777)

    created = skipped = not_found = 0

    for season in plex_show.seasons():
        snum = season.seasonNumber
        if snum == 0:
            continue
        for ep in season.episodes():
            try:
                plex_path = ep.media[0].parts[0].file
            except (IndexError, AttributeError):
                continue

            debrid_file = find_in_debrid(plex_path, cache)
            if not debrid_file:
                not_found += 1
                logger.debug(f"    ❌ Not in debrid: {os.path.basename(plex_path)}")
                continue

            fname    = os.path.basename(debrid_file)
            sdir     = os.path.join(series_path, f"Season {snum:02}")
            os.makedirs(sdir, exist_ok=True)
            os.chmod(sdir, 0o777)
            dst = os.path.join(sdir, fname)

            if make_symlink(debrid_file, dst):
                created += 1
                logger.info(f"    🆕 New episode linked: S{snum:02}E{ep.index:02} — {fname}")
            else:
                skipped += 1

    if created:
        logger.info(f"  ✅ '{title}': {created} linked, {skipped} unchanged, {not_found} not in debrid")
    else:
        logger.debug(f"  ✓ '{title}': {skipped} unchanged, {not_found} not in debrid")
    return created

# ── Sonarr ─────────────────────────────────────────────────────────────────────
def sonarr_get(path):
    r = requests.get(f"{CONFIG['SONARR_URL']}/api/v3/{path}",
                     headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]}, timeout=10)
    r.raise_for_status()
    return r.json()

def sonarr_post(path, payload):
    r = requests.post(f"{CONFIG['SONARR_URL']}/api/v3/{path}",
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

def get_sonarr_map():
    """Returns {tvdbId: series_dict}"""
    result = {}
    for s in sonarr_get("series"):
        tid = s.get("tvdbId")
        if tid:
            result[tid] = s
    return result

def add_to_sonarr(tvdb_id, title, qp_id):
    """Add series to Sonarr. Returns created series dict or None."""
    try:
        results = sonarr_get(f"series/lookup?term=tvdb:{tvdb_id}")
        if not results:
            logger.warning(f"  ⚠ No lookup result for '{title}'")
            return None
        lookup  = results[0]
        seasons = [dict(s, monitored=True) for s in lookup.get("seasons", [])]
        payload = {
            "title":            lookup["title"],
            "tvdbId":           tvdb_id,
            "qualityProfileId": qp_id,
            "rootFolderPath":   CONFIG["SONARR_ROOT_FOLDER"],
            "seasonFolder":     True,
            "monitored":        True,
            "seasons":          seasons,
            "images":           lookup.get("images", []),
            "path":             f"{CONFIG['SONARR_ROOT_FOLDER']}/{lookup['title']}",
        }
        created = sonarr_post("series", payload)
        logger.info(f"  ✅ Added: {lookup['title']} (TVDB:{tvdb_id})")
        return created
    except requests.HTTPError as e:
        if e.response.status_code == 400:
            logger.warning(f"  ⚠ Sonarr rejected '{title}': {e.response.text[:150]}")
        else:
            logger.error(f"  ❌ Failed to add '{title}': {e.response.status_code}")
        return None

def refresh_rescan(sonarr_id, title):
    try:
        sonarr_post("command", {"name": "RefreshSeries", "seriesId": sonarr_id})
        logger.info(f"  🔄 Refresh triggered: {title}")
    except Exception as e:
        logger.warning(f"  ⚠ Refresh failed: {e}")
    time.sleep(2)
    try:
        sonarr_post("command", {"name": "RescanSeries", "seriesId": sonarr_id})
        logger.info(f"  🔍 Rescan triggered: {title}")
    except Exception as e:
        logger.warning(f"  ⚠ Rescan failed: {e}")

# ── TMDB ───────────────────────────────────────────────────────────────────────
def is_ongoing(tvdb_id, title):
    key = CONFIG.get("TMDB_API_KEY", "").strip()
    if key:
        try:
            r = requests.get(f"https://api.themoviedb.org/3/find/{tvdb_id}",
                             params={"api_key": key, "external_source": "tvdb_id"}, timeout=8)
            hits = r.json().get("tv_results", [])
            if hits:
                r2 = requests.get(f"https://api.themoviedb.org/3/tv/{hits[0]['id']}",
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

# ── Plex helpers ───────────────────────────────────────────────────────────────
def get_tvdb_id(show):
    for guid in show.guids:
        if "tvdb://" in guid.id:
            try:
                return int(guid.id.replace("tvdb://", ""))
            except ValueError:
                pass
    return None

# ── Main scan ──────────────────────────────────────────────────────────────────
def do_scan():
    global scan_running, watchdog_running

    if scan_running:
        logger.warning("⚠ Scan already running")
        return
    scan_running = True

    logger.info("🔍 Starting scan...")

    # Connect
    try:
        plex   = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
        tv_lib = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
    except Exception as e:
        logger.error(f"❌ Plex error: {e}")
        scan_running = False
        return

    try:
        qp_id      = get_quality_profile_id()
        sonarr_map = get_sonarr_map()
    except Exception as e:
        logger.error(f"❌ Sonarr error: {e}")
        scan_running = False
        return

    # Build debrid cache
    cache = build_debrid_cache()

    shows = tv_lib.all()
    logger.info(f"📺 {len(shows)} Plex shows | {len(sonarr_map)} in Sonarr | {len(cache)} debrid files")

    added = existed = skipped_ended = 0

    for show in shows:
        tvdb_id = get_tvdb_id(show)
        if not tvdb_id:
            continue
        if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
            continue

        ongoing, status = is_ongoing(tvdb_id, show.title)
        if not ongoing:
            skipped_ended += 1
            continue

        # Add to Sonarr if not there
        if tvdb_id not in sonarr_map:
            logger.info(f"  📡 Adding: {show.title} [{status}]")
            created = add_to_sonarr(tvdb_id, show.title, qp_id)
            if created:
                sonarr_map[tvdb_id] = created
                added += 1
            time.sleep(0.3)
        else:
            existed += 1

        # Symlink episodes from Plex → debrid → Sonarr folder
        if tvdb_id in sonarr_map:
            symlink_series_from_plex(show, sonarr_map[tvdb_id], cache)

    logger.info(f"✅ Scan done — {added} added, {existed} existed, {skipped_ended} ended")

    CONFIG["INITIAL_SCAN_DONE"] = True
    scan_running = False
    save_config()

    # Start watchdog loop
    watchdog_running = True
    threading.Thread(target=watchdog_loop, daemon=True).start()

# ── Watchdog loop ──────────────────────────────────────────────────────────────
def watchdog_loop():
    scan_interval = int(CONFIG.get("PLEX_SCAN_INTERVAL", 120))
    logger.info(f"🐕 Watchdog: scanning Plex every {scan_interval}s")
    last_scan = 0

    while watchdog_running:
        now_ts = time.time()
        if now_ts - last_scan >= scan_interval:
            last_scan = now_ts
            try:
                logger.info("🔍 Scanning Plex for new shows...")
                plex       = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
                tv_lib     = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
                sonarr_map = get_sonarr_map()
                qp_id      = get_quality_profile_id()
                cache      = build_debrid_cache()
                shows      = tv_lib.all()

                for show in shows:
                    tvdb_id = get_tvdb_id(show)
                    if not tvdb_id or tvdb_id in CONFIG.get("IGNORED_SERIES", []):
                        continue
                    ongoing, status = is_ongoing(tvdb_id, show.title)
                    if not ongoing:
                        continue
                    if tvdb_id not in sonarr_map:
                        logger.info(f"🆕 New: {show.title} [{status}]")
                        created = add_to_sonarr(tvdb_id, show.title, qp_id)
                        if created:
                            sonarr_map[tvdb_id] = created
                        time.sleep(0.3)
                    if tvdb_id in sonarr_map:
                        symlink_series_from_plex(show, sonarr_map[tvdb_id], cache)

                # Update interval in case user changed it
                scan_interval = int(CONFIG.get("PLEX_SCAN_INTERVAL", 120))
                logger.debug(f"✅ Scan done. Next in {scan_interval}s")
            except Exception as e:
                logger.error(f"Watchdog error: {e}")

        time.sleep(10)  # check every 10s if interval has elapsed

# ── Auto-start ─────────────────────────────────────────────────────────────────
def auto_start():
    global watchdog_running
    logger.info("🔁 Auto-starting...")
    # Run one full scan cycle then hand off to loop
    try:
        plex       = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
        tv_lib     = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
        sonarr_map = get_sonarr_map()
        qp_id      = get_quality_profile_id()
        cache      = build_debrid_cache()
        shows      = tv_lib.all()
        added = 0
        for show in shows:
            tvdb_id = get_tvdb_id(show)
            if not tvdb_id or tvdb_id in CONFIG.get("IGNORED_SERIES", []):
                continue
            ongoing, status = is_ongoing(tvdb_id, show.title)
            if not ongoing:
                continue
            if tvdb_id not in sonarr_map:
                logger.info(f"  🆕 New: {show.title} [{status}]")
                created = add_to_sonarr(tvdb_id, show.title, qp_id)
                if created:
                    sonarr_map[tvdb_id] = created
                    added += 1
                time.sleep(0.3)
            if tvdb_id in sonarr_map:
                symlink_series_from_plex(show, sonarr_map[tvdb_id], cache)
        logger.info(f"✅ Auto-start done — {added} new shows")
    except Exception as e:
        logger.error(f"Auto-start error: {e}")
    watchdog_running = True
    threading.Thread(target=watchdog_loop, daemon=True).start()

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    p = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(p) as f:
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
    return jsonify({"ok": True})

@app.route("/api/test-connections", methods=["POST"])
def api_test():
    data = request.json or {}
    out  = {"plex": False, "sonarr": False, "tmdb": False, "errors": {}}
    try:
        p = PlexServer(data.get("PLEX_URL"), data.get("PLEX_TOKEN"))
        p.library.section(data.get("PLEX_TV_LIBRARY", "TV Shows"))
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

@app.route("/api/start-scan", methods=["POST"])
def api_start_scan():
    if not scan_running:
        threading.Thread(target=do_scan, daemon=True).start()
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
                "poster":           next((i["remoteUrl"] for i in s.get("images", [])
                                          if i.get("coverType") == "poster"), None),
            })
        out.sort(key=lambda x: x["title"])
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/series/<int:sid>/sync", methods=["POST"])
def api_series_sync(sid):
    """Symlink one series from Plex → debrid → Sonarr."""
    def run():
        try:
            sonarr_series = sonarr_get(f"series/{sid}")
            tvdb_id       = sonarr_series.get("tvdbId")
            title         = sonarr_series["title"]
            logger.info(f"🔗 Syncing: {title}")
            plex   = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
            tv_lib = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
            cache  = build_debrid_cache()
            for show in tv_lib.all():
                if get_tvdb_id(show) == tvdb_id:
                    symlink_series_from_plex(show, sonarr_series, cache)
                    break
            logger.info(f"✅ Sync done: {title}")
        except Exception as e:
            logger.error(f"Sync failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/sync-all", methods=["POST"])
def api_series_sync_all():
    """Symlink all series."""
    def run():
        try:
            plex       = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
            tv_lib     = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
            sonarr_map = get_sonarr_map()
            cache      = build_debrid_cache()
            for show in tv_lib.all():
                tvdb_id = get_tvdb_id(show)
                if tvdb_id and tvdb_id in sonarr_map:
                    symlink_series_from_plex(show, sonarr_map[tvdb_id], cache)
        except Exception as e:
            logger.error(f"Sync all failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/<int:sid>/rescan", methods=["POST"])
def api_series_rescan(sid):
    def run():
        try:
            s = sonarr_get(f"series/{sid}")
            refresh_rescan(sid, s["title"])
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
        logger.info(f"🗑 Deleted: {title}")
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
    created = add_to_sonarr(tvdb_id, title, get_quality_profile_id())
    if created:
        logger.info(f"✅ Re-added: {title}")
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
                e = log_queue.get(timeout=1)
                yield f"data: {json.dumps(e)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'ping': True})}\n\n"
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    if CONFIG.get("INITIAL_SCAN_DONE") and CONFIG.get("SETUP_COMPLETE"):
        logger.info("🔁 Auto-starting...")
        threading.Thread(target=auto_start, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
