import os
import time
import threading
import logging
import json
import queue
import requests
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response, stream_with_context
from plexapi.server import PlexServer

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
    "POLL_INTERVAL":          30,
    "SETUP_COMPLETE":         False,
    "INITIAL_SCAN_DONE":      False,
    "AUTO_LINK":              False,
    "IGNORED_SERIES":         [],   # tvdbIds the user has deleted and chosen not to re-add
}

def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
            cfg.update(saved)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg

def save_config_to_disk(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
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
pending_readd    = []   # shows deleted but found again in Plex, waiting user confirmation

# ── Logging ───────────────────────────────────────────────────────────────────
class QueueHandler(logging.Handler):
    def emit(self, record):
        msg   = self.format(record)
        level = record.levelname
        entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
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

# ── Constants ─────────────────────────────────────────────────────────────────
ONGOING_STATUSES = {"returning series", "in production", "planned", "pilot", "continuing"}

# ── Sonarr helpers ────────────────────────────────────────────────────────────
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

def sonarr_put(path, payload):
    r = requests.put(f"{CONFIG['SONARR_URL']}/api/v3/{path}",
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

def get_sonarr_series_map():
    """Returns {tvdbId: series_dict} for all series in Sonarr."""
    series = sonarr_get("series")
    return {s["tvdbId"]: s for s in series if s.get("tvdbId")}

# ── TMDB helpers ──────────────────────────────────────────────────────────────
def get_show_status(tvdb_id, title, tmdb_id=None):
    """Returns (is_ongoing, status_str). Checks TMDB then Sonarr lookup."""
    key = CONFIG.get("TMDB_API_KEY", "")
    if key:
        try:
            sid = tmdb_id
            if not sid and tvdb_id:
                r = requests.get(f"https://api.themoviedb.org/3/find/{tvdb_id}",
                                 params={"api_key": key, "external_source": "tvdb_id"}, timeout=8)
                results = r.json().get("tv_results", [])
                if results:
                    sid = results[0]["id"]
            if sid:
                r = requests.get(f"https://api.themoviedb.org/3/tv/{sid}",
                                 params={"api_key": key}, timeout=8)
                data   = r.json()
                status = data.get("status", "").lower()
                return status in ONGOING_STATUSES, status
        except Exception as e:
            logger.debug(f"TMDB status check failed for '{title}': {e}")
    # Fallback: Sonarr lookup
    try:
        results = sonarr_get(f"series/lookup?term=tvdb:{tvdb_id}")
        if results:
            status = results[0].get("status", "").lower()
            return status in {"continuing", "upcoming"}, status
    except Exception:
        pass
    return True, "unknown"

# ── Add series to Sonarr ──────────────────────────────────────────────────────
def add_series_to_sonarr(tvdb_id, title, quality_profile_id, tmdb_id=None):
    """Add a continuing show to Sonarr monitoring future episodes only."""
    try:
        results = sonarr_get(f"series/lookup?term=tvdb:{tvdb_id}")
        if not results:
            logger.warning(f"  ⚠ No Sonarr lookup result for '{title}' (TVDB:{tvdb_id})")
            return False
        lookup = results[0]
        # Only monitor future — don't search for missing back catalogue
        seasons = [dict(s, monitored=True) for s in lookup.get("seasons", [])]
        payload = {
            "title":            lookup["title"],
            "tvdbId":           tvdb_id,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath":   CONFIG["SONARR_ROOT_FOLDER"],
            "seasonFolder":     True,
            "monitored":        True,
            "addOptions": {
                "searchForMissingEpisodes":     False,
                "searchForCutoffUnmetEpisodes": False,
                "monitor":                      "future",
            },
            "seasons": seasons,
            "images":  lookup.get("images", []),
            "path":    f"{CONFIG['SONARR_ROOT_FOLDER']}/{lookup['title']}",
        }
        sonarr_post("series", payload)
        logger.info(f"  ✅ Added to Sonarr: {lookup['title']} (TVDB:{tvdb_id}) — monitoring future episodes")
        return True
    except requests.HTTPError as e:
        if e.response.status_code == 400:
            logger.debug(f"  Already in Sonarr: {title}")
        else:
            logger.error(f"  ❌ Failed to add '{title}': {e}")
        return False

# ── Symlink health check ─────────────────────────────────────────────────────
def check_and_repair_symlinks(sonarr_map):
    """
    Walk every Sonarr series folder, find symlinks, and verify they point
    to a real file. Broken ones are removed so they can be recreated on the
    next symlink pass.
    """
    repaired = 0
    removed  = 0
    for tvdb_id, series in sonarr_map.items():
        path = series.get("path", "")
        if not path or not os.path.isdir(path):
            continue
        for root, dirs, files in os.walk(path):
            for fname in files:
                fpath = os.path.join(root, fname)
                if os.path.islink(fpath):
                    target = os.readlink(fpath)
                    if not os.path.exists(fpath):
                        # Broken symlink — remove it
                        try:
                            os.remove(fpath)
                            removed += 1
                            logger.info(f"  🔧 Removed broken symlink: {fname} → {target}")
                        except Exception as e:
                            logger.warning(f"  ⚠ Could not remove broken symlink {fname}: {e}")
    if removed:
        logger.info(f"🔧 Symlink health check — {removed} broken symlinks removed")
    else:
        logger.debug("🔧 Symlink health check — all symlinks OK")
    return removed

# ── Symlink + Rescan ──────────────────────────────────────────────────────────
def symlink_series(plex_show, sonarr_series):
    """
    For each episode in Plex, create a symlink inside the Sonarr series folder
    so Sonarr can find and mark the file as downloaded on rescan.
    """
    title        = sonarr_series["title"]
    series_path  = sonarr_series.get("path", "")
    if not series_path:
        logger.warning(f"  ⚠ No path set in Sonarr for '{title}' — skipping symlink")
        return 0

    created = 0
    skipped = 0

    for plex_season in plex_show.seasons():
        snum = plex_season.seasonNumber
        if snum == 0:
            continue
        season_dir = os.path.join(series_path, f"Season {snum:02}")
        os.makedirs(season_dir, exist_ok=True)

        for plex_ep in plex_season.episodes():
            try:
                src = plex_ep.media[0].parts[0].file
            except (IndexError, AttributeError):
                continue

            filename = os.path.basename(src)
            dst = os.path.join(season_dir, filename)

            # If symlink exists and is valid — skip
            if os.path.islink(dst) and os.path.exists(dst):
                skipped += 1
                continue

            # If symlink is broken — remove and recreate
            if os.path.islink(dst) and not os.path.exists(dst):
                logger.info(f"    🔧 Repairing broken symlink S{snum:02}E{plex_ep.index:02}")
                os.remove(dst)

            # If a real file already exists there — skip
            if os.path.exists(dst):
                skipped += 1
                continue

            try:
                os.symlink(src, dst)
                created += 1
                logger.debug(f"    🔗 Symlinked S{snum:02}E{plex_ep.index:02} → {dst}")
            except Exception as e:
                logger.warning(f"    ⚠ Symlink failed for S{snum:02}E{plex_ep.index:02}: {e}")

    if created:
        logger.info(f"  🔗 '{title}' — {created} symlinks created, {skipped} already existed")
    return created

def rescan_series(sonarr_series_id, title):
    """Tell Sonarr to rescan disk for a series."""
    try:
        sonarr_post("command", {"name": "RescanSeries", "seriesId": sonarr_series_id})
        logger.info(f"  🔍 Rescan triggered for '{title}'")
        return True
    except Exception as e:
        logger.warning(f"  ⚠ Rescan failed for '{title}': {e}")
        return False

def symlink_and_rescan_all(plex, sonarr_map):
    """Symlink Plex files into Sonarr folders then trigger rescan."""
    logger.info("🔗 Starting symlink pass...")
    tv_lib    = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
    total_new = 0
    for show in tv_lib.all():
        tvdb_id = None
        for guid in show.guids:
            if "tvdb://" in guid.id:
                try: tvdb_id = int(guid.id.replace("tvdb://", ""))
                except ValueError: pass
        if not tvdb_id or tvdb_id not in sonarr_map:
            continue
        sonarr_series = sonarr_map[tvdb_id]
        created = symlink_series(show, sonarr_series)
        total_new += created
        if created:
            rescan_series(sonarr_series["id"], sonarr_series["title"])
            time.sleep(0.3)
    logger.info(f"✅ Symlink pass complete — {total_new} new symlinks created")

def rescan_all_series(sonarr_map):
    """Trigger disk rescan for all series without symlinking."""
    logger.info("🔍 Triggering rescan for all series...")
    for tvdb_id, series in sonarr_map.items():
        rescan_series(series["id"], series["title"])
        time.sleep(0.2)
    logger.info(f"✅ Rescan triggered for {len(sonarr_map)} series")

# ── Initial Plex scan ─────────────────────────────────────────────────────────
def do_initial_scan():
    """Scan Plex, add only continuing shows to Sonarr, then start watchdog."""
    global watchdog_running, watchdog_thread, scan_running
    if scan_running:
        logger.warning("⚠ Scan already in progress — ignoring duplicate request")
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
    except Exception as e:
        logger.error(f"❌ Sonarr connection failed: {e}")
        scan_running = False
        return

    shows = tv_lib.all()
    logger.info(f"📺 Found {len(shows)} shows in Plex")
    added = skipped_ended = skipped_exists = 0

    for show in shows:
        tvdb_id = None
        for guid in show.guids:
            if "tvdb://" in guid.id:
                try: tvdb_id = int(guid.id.replace("tvdb://", ""))
                except ValueError: pass

        if not tvdb_id:
            logger.debug(f"  ⚠ No TVDB ID for '{show.title}', skipping")
            continue

        if tvdb_id in sonarr_map:
            skipped_exists += 1
            continue

        is_ongoing, status = get_show_status(tvdb_id, show.title)
        if not is_ongoing:
            logger.debug(f"  ⏭ '{show.title}' is {status} — skipping")
            skipped_ended += 1
            continue

        if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
            logger.debug(f"  ⏭ '{show.title}' is in ignore list — skipping")
            continue

        logger.info(f"  📡 Adding continuing show: {show.title} [{status}]")
        add_series_to_sonarr(tvdb_id, show.title, quality_profile_id)
        added += 1
        time.sleep(0.3)

    logger.info(f"✅ Initial scan done — {added} added, {skipped_exists} already in Sonarr, {skipped_ended} ended/skipped")

    # Link episodes for all added series
    if CONFIG.get("AUTO_LINK", True):
        logger.info("🔗 Linking episodes for all series...")
        sonarr_map = get_sonarr_series_map()
        rescan_all_series(sonarr_map)
    else:
        logger.info("⏭ Auto-link is disabled — skipping episode linking")

    CONFIG["INITIAL_SCAN_DONE"] = True
    save_config_to_disk(CONFIG)
    scan_running = False
    logger.info("🐕 Starting watchdog loop...")
    watchdog_running = True
    watchdog_thread  = threading.Thread(target=do_watchdog_loop, daemon=True)
    watchdog_thread.start()

# ── Watchdog loop ─────────────────────────────────────────────────────────────
def do_watchdog_loop():
    """
    Every POLL_INTERVAL seconds:
    1. Check Plex for continuing shows not yet in Sonarr → add them
    2. Link any new episodes for all series already in Sonarr
    """
    logger.info(f"🐕 Watchdog loop running every {CONFIG['POLL_INTERVAL']}s")
    while watchdog_running:
        try:
            plex   = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
            tv_lib = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
            sonarr_map         = get_sonarr_series_map()
            quality_profile_id = get_quality_profile_id()

            # Step 1: find new continuing shows
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
                # Check if user previously deleted this series
                if tvdb_id in CONFIG.get("IGNORED_SERIES", []):
                    already_pending = any(p["tvdbId"] == tvdb_id for p in pending_readd)
                    if not already_pending:
                        logger.info(f"⚠ '{show.title}' was previously deleted — awaiting user confirmation to re-add")
                        pending_readd.append({"tvdbId": tvdb_id, "title": show.title, "status": status})
                    continue
                logger.info(f"🆕 New continuing show in Plex: {show.title} [{status}]")
                add_series_to_sonarr(tvdb_id, show.title, quality_profile_id)

            # Step 2: always check and repair broken symlinks
            sonarr_map = get_sonarr_series_map()
            broken = check_and_repair_symlinks(sonarr_map)

            # Step 3: if auto-link on, symlink new files and rescan
            if CONFIG.get("AUTO_LINK", True):
                symlink_and_rescan_all(plex, sonarr_map)
            elif broken:
                # Even if auto-link is off, rescan series where symlinks were repaired
                logger.info("🔍 Rescanning series with repaired symlinks...")
                for tvdb_id, series in sonarr_map.items():
                    path = series.get("path", "")
                    if path and os.path.isdir(path):
                        rescan_series(series["id"], series["title"])
                        time.sleep(0.2)

        except Exception as e:
            logger.error(f"Watchdog error: {e}")

        logger.debug(f"💤 Sleeping {CONFIG['POLL_INTERVAL']}s...")
        time.sleep(CONFIG["POLL_INTERVAL"])

# ── Auto-start on restart ─────────────────────────────────────────────────────
def auto_start():
    """Called on container restart when INITIAL_SCAN_DONE is already True."""
    global watchdog_running, watchdog_thread
    logger.info("🔁 Restarted — auto-scanning Plex for new continuing shows...")
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
            if not is_ongoing:
                continue
            logger.info(f"  🆕 New continuing show: {show.title} [{status}]")
            add_series_to_sonarr(tvdb_id, show.title, quality_profile_id)
            added += 1
            time.sleep(0.3)
        if CONFIG.get("AUTO_LINK", True):
            sonarr_map = get_sonarr_series_map()
            symlink_and_rescan_all(plex, sonarr_map)
        logger.info(f"✅ Auto-start complete — {added} new shows added")
    except Exception as e:
        logger.error(f"Auto-start error: {e}")
    logger.info("🐕 Starting watchdog loop...")
    watchdog_running = True
    watchdog_thread  = threading.Thread(target=do_watchdog_loop, daemon=True)
    watchdog_thread.start()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", config=CONFIG)

@app.route("/api/config", methods=["GET","POST"])
def api_config():
    if request.method == "POST":
        for k, v in (request.json or {}).items():
            if k in CONFIG:
                CONFIG[k] = v
        save_config_to_disk(CONFIG)
        return jsonify({"ok": True})
    return jsonify(CONFIG)

@app.route("/api/setup", methods=["POST"])
def api_setup():
    data = request.json or {}
    for k, v in data.items():
        if k in CONFIG:
            CONFIG[k] = v
    CONFIG["SETUP_COMPLETE"] = True
    save_config_to_disk(CONFIG)
    logger.info("✅ Setup complete — config saved")
    return jsonify({"ok": True})

@app.route("/api/test-connections", methods=["POST"])
def api_test_connections():
    data = request.json or {}
    results = {"plex": False, "sonarr": False, "tmdb": False, "errors": {}}
    try:
        plex = PlexServer(data.get("PLEX_URL"), data.get("PLEX_TOKEN"))
        plex.library.section(data.get("PLEX_TV_LIBRARY","TV Shows"))
        results["plex"] = True
    except Exception as e:
        results["errors"]["plex"] = str(e)
    try:
        r = requests.get(f"{data.get('SONARR_URL')}/api/v3/system/status",
                         headers={"X-Api-Key": data.get("SONARR_API_KEY")}, timeout=8)
        r.raise_for_status()
        results["sonarr"] = True
    except Exception as e:
        results["errors"]["sonarr"] = str(e)
    if data.get("TMDB_API_KEY"):
        try:
            r = requests.get("https://api.themoviedb.org/3/configuration",
                             params={"api_key": data.get("TMDB_API_KEY")}, timeout=8)
            r.raise_for_status()
            results["tmdb"] = True
        except Exception as e:
            results["errors"]["tmdb"] = str(e)
    return jsonify(results)

@app.route("/api/start-initial-scan", methods=["POST"])
def api_start_initial_scan():
    t = threading.Thread(target=do_initial_scan, daemon=True)
    t.start()
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify({
        "setupComplete":    CONFIG.get("SETUP_COMPLETE", False),
        "initialScanDone":  CONFIG.get("INITIAL_SCAN_DONE", False),
        "watchdogRunning":  watchdog_running,
        "scanRunning":      scan_running,
    })

@app.route("/api/series/list")
def api_series_list():
    try:
        series = sonarr_get("series")
        out = []
        for s in series:
            stats = s.get("statistics", {})
            out.append({
                "id":               s["id"],
                "title":            s["title"],
                "tvdbId":           s.get("tvdbId"),
                "status":           s.get("status",""),
                "monitored":        s.get("monitored", False),
                "episodeCount":     stats.get("episodeCount", 0),
                "episodeFileCount": stats.get("episodeFileCount", 0),
                "seasonCount":      s.get("seasonCount", 0),
                "poster":           next((i["remoteUrl"] for i in s.get("images",[]) if i.get("coverType")=="poster"), None),
            })
        out.sort(key=lambda x: x["title"])
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/series/<int:sonarr_id>/link", methods=["POST"])
def api_series_link(sonarr_id):
    """Symlink Plex files into Sonarr folder then trigger rescan."""
    def run():
        try:
            series  = sonarr_get(f"series/{sonarr_id}")
            title   = series["title"]
            tvdb_id = series.get("tvdbId")
            logger.info(f"🔗 Symlinking files for: {title}")
            plex   = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
            tv_lib = plex.library.section(CONFIG["PLEX_TV_LIBRARY"])
            plex_show = None
            for show in tv_lib.all():
                for guid in show.guids:
                    if "tvdb://" in guid.id:
                        try:
                            if int(guid.id.replace("tvdb://", "")) == tvdb_id:
                                plex_show = show; break
                        except ValueError: pass
                if plex_show: break
            if not plex_show:
                logger.warning(f"  ⚠ '{title}' not found in Plex")
                return
            symlink_series(plex_show, series)
            rescan_series(sonarr_id, title)
        except Exception as e:
            logger.error(f"Symlink/rescan failed for series {sonarr_id}: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/link-all", methods=["POST"])
def api_series_link_all():
    """Symlink all Plex files into Sonarr folders then rescan."""
    def run():
        try:
            plex       = PlexServer(CONFIG["PLEX_URL"], CONFIG["PLEX_TOKEN"])
            sonarr_map = get_sonarr_series_map()
            symlink_and_rescan_all(plex, sonarr_map)
        except Exception as e:
            logger.error(f"Bulk symlink failed: {e}")
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/series/<int:sonarr_id>/delete", methods=["POST"])
def api_series_delete(sonarr_id):
    """Delete a series from Sonarr and add its tvdbId to the ignored list."""
    try:
        series  = sonarr_get(f"series/{sonarr_id}")
        tvdb_id = series.get("tvdbId")
        title   = series["title"]
        # Delete from Sonarr (deleteFiles=False — don't touch actual files/symlinks)
        requests.delete(
            f"{CONFIG['SONARR_URL']}/api/v3/series/{sonarr_id}",
            headers={"X-Api-Key": CONFIG["SONARR_API_KEY"]},
            params={"deleteFiles": "false", "addImportListExclusion": "false"},
            timeout=10
        ).raise_for_status()
        # Add to ignored list
        ignored = CONFIG.get("IGNORED_SERIES", [])
        if tvdb_id and tvdb_id not in ignored:
            ignored.append(tvdb_id)
            CONFIG["IGNORED_SERIES"] = ignored
            save_config_to_disk(CONFIG)
        logger.info(f"🗑 Deleted '{title}' from Sonarr and added to ignore list (TVDB:{tvdb_id})")
        return jsonify({"ok": True, "title": title})
    except Exception as e:
        logger.error(f"Delete failed for series {sonarr_id}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/pending-readd", methods=["GET"])
def api_pending_readd():
    return jsonify(pending_readd)

@app.route("/api/pending-readd/confirm", methods=["POST"])
def api_pending_readd_confirm():
    """User confirms re-adding a previously deleted series."""
    global pending_readd
    data    = request.json or {}
    tvdb_id = data.get("tvdbId")
    title   = data.get("title", "")
    # Remove from ignored list
    ignored = CONFIG.get("IGNORED_SERIES", [])
    if tvdb_id in ignored:
        ignored.remove(tvdb_id)
        CONFIG["IGNORED_SERIES"] = ignored
        save_config_to_disk(CONFIG)
    # Remove from pending
    pending_readd = [p for p in pending_readd if p["tvdbId"] != tvdb_id]
    # Add to Sonarr
    try:
        qp_id = get_quality_profile_id()
        add_series_to_sonarr(tvdb_id, title, qp_id)
        logger.info(f"✅ Re-added '{title}' to Sonarr after user confirmation")
    except Exception as e:
        logger.error(f"Re-add failed for '{title}': {e}")
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})

@app.route("/api/pending-readd/dismiss", methods=["POST"])
def api_pending_readd_dismiss():
    """User dismisses re-add prompt — keeps series ignored."""
    global pending_readd
    data    = request.json or {}
    tvdb_id = data.get("tvdbId")
    pending_readd = [p for p in pending_readd if p["tvdbId"] != tvdb_id]
    logger.info(f"⏭ Dismissed re-add prompt for TVDB:{tvdb_id}")
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
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

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
