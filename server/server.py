"""
Local helper server for the yt-dlp Chrome extension.

Binds 127.0.0.1 only. The Chrome extension talks to it over HTTP:

  GET  /ping                      liveness + ffmpeg/provider health
  GET  /health                    verbose diagnostics
  GET  /formats?url=...           available formats + metadata (cached briefly)
  POST /download                  start a download, returns job_id
  GET  /progress/<job_id>         progress of one job
  POST /cancel   {job_id|id}      cancel a queued or running download
  GET  /history                   every past download
  POST /open_file {id}            open a finished file
  POST /open_folder {id}          reveal a finished file in the file manager
  POST /open_downloads_folder     open the download directory
  POST /delete {id}               delete a record (and optionally its file)
  POST /redownload {id}           re-run a past download
  POST /clear_history {status?}   drop records (optionally only one status)
  GET  /version                   yt-dlp version + update state
  GET  /extension                 extension version + checkout sync state
  POST /update                    upgrade yt-dlp + extension now (restarts when idle)
  POST /update_extension          fast-forward the extension checkout now

Start with:  python server/server.py
Requires:    pip install yt-dlp   (plus ffmpeg on PATH, or set YTDLP_FFMPEG)

Environment overrides:
  YTDLP_SERVER_PORT       default 4599
  YTDLP_DOWNLOAD_DIR      default ~/Downloads/yt-dlp-extension
  YTDLP_FFMPEG            explicit path to ffmpeg(.exe) or its directory
  YTDLP_MAX_CONCURRENT    simultaneous downloads, default 2
  YTDLP_POT_PROVIDER_URL  default http://127.0.0.1:4416
  YTDLP_POT_SERVER_HOME   bgutil provider checkout, for the script provider
  YTDLP_AUTO_UPDATE       0 to disable background yt-dlp upgrades
  YTDLP_EXTENSION_AUTO_UPDATE  0 to stop fast-forwarding the extension checkout
  YTDLP_EXTRA_ORIGINS     comma-separated extra allowed CORS origins
"""

import copy
import itertools
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import yt_dlp
from yt_dlp.utils import DownloadCancelled


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

PORT = int(os.environ.get("YTDLP_SERVER_PORT", "4599"))
DOWNLOAD_DIR = os.environ.get(
    "YTDLP_DOWNLOAD_DIR",
    os.path.join(os.path.expanduser("~"), "Downloads", "yt-dlp-extension"),
)
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
HISTORY_LIMIT = 500

MAX_CONCURRENT = max(1, int(os.environ.get("YTDLP_MAX_CONCURRENT", "2")))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)


# Two problems with stdout here, both fixed before anything prints:
#  - A Windows console defaults to cp1252 and raises UnicodeEncodeError on any
#    non-ASCII byte we log, and video titles are full of them.
#  - Redirected to a file, stdout is block-buffered, so the startup banner --
#    which is how you find out whether ffmpeg was located -- sits invisible in
#    the buffer instead of appearing in the log.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass


def log(*parts):
    print(f"[{time.strftime('%H:%M:%S')}]", *parts, flush=True)


class YdlLogger:
    """Silence yt-dlp's chatter, keep its errors.

    yt-dlp writes a Python-version deprecation notice straight to stderr every
    time a YoutubeDL is constructed, and neither `quiet` nor `no_warnings`
    covers it. Resolving the ten presets for one video constructs ten of them,
    so opening the popup once printed a dozen identical lines and buried the
    messages that actually matter. Handing yt-dlp a logger routes all of that
    here instead. Errors still propagate as exceptions -- this only decides
    what reaches the console.
    """

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        text = ANSI_RE.sub("", str(msg)).strip()
        if not text or text.startswith("Deprecated Feature:"):
            # yt-dlp reports its Python-version deprecation through the error
            # channel. It is not an error, it is not actionable per download,
            # and the README already says which Python to run.
            return
        log(f"yt-dlp: {text[:300]}")


# --------------------------------------------------------------------------- #
# ffmpeg discovery
#
# This is the single most common reason downloads fail: any "best quality"
# download on YouTube means merging a video-only and an audio-only stream, and
# that needs ffmpeg. Relying on PATH inheritance is fragile -- a server started
# from a shell whose PATH lacks ffmpeg (or started before ffmpeg was installed,
# since a live process never sees later PATH edits) fails every download with a
# cryptic "ffmpeg is not installed". So resolve it once, explicitly, at startup
# and hand yt-dlp an absolute location.
# --------------------------------------------------------------------------- #

def _ffmpeg_candidates():
    """Directories worth probing, most specific first."""
    env = os.environ.get("YTDLP_FFMPEG")
    if env:
        env = os.path.expanduser(env)
        yield env if os.path.isdir(env) else os.path.dirname(env)

    for exe in ("ffmpeg", "ffmpeg.exe"):
        found = shutil.which(exe)
        if found:
            yield os.path.dirname(found)

    home = os.path.expanduser("~")
    if sys.platform == "win32":
        # winget installs into a versioned directory, so glob for it.
        winget = os.path.join(home, "AppData", "Local", "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget):
            for entry in sorted(os.listdir(winget), reverse=True):
                if "ffmpeg" not in entry.lower():
                    continue
                pkg = os.path.join(winget, entry)
                for root, _dirs, files in os.walk(pkg):
                    if "ffmpeg.exe" in files:
                        yield root
                        break
        yield from (
            os.path.join(home, "anaconda3", "Library", "bin"),
            os.path.join(home, "miniconda3", "Library", "bin"),
            r"C:\ffmpeg\bin",
            r"C:\Program Files\ffmpeg\bin",
            r"C:\ProgramData\chocolatey\bin",
        )
        # Any conda env on this machine.
        for envs in (os.path.join(home, "anaconda3", "envs"),
                     os.path.join(home, "miniconda3", "envs")):
            if os.path.isdir(envs):
                for name in sorted(os.listdir(envs)):
                    yield os.path.join(envs, name, "Library", "bin")
    else:
        yield from ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/snap/bin")


# Encoders the presets rely on. A build without libmp3lame cannot satisfy the
# "Audio only (MP3)" preset -- it fails at the very end, after the whole file
# has downloaded, with "Encoder not found". Several common builds (notably the
# conda-forge one) ship without it, so prefer a build that has what we need
# rather than whichever happens to be first on PATH.
REQUIRED_ENCODERS = ("libmp3lame",)


def _probe_ffmpeg(exe):
    """Return (version, {encoder names}) for an ffmpeg executable, or None."""
    try:
        result = subprocess.run([exe, "-version"], capture_output=True,
                                text=True, timeout=15)
        if result.returncode != 0:
            return None
        first = (result.stdout or "").splitlines()[0] if result.stdout else ""
        version = first.split(" Copyright")[0].strip() or "ffmpeg (version unknown)"

        encoders = set()
        listing = subprocess.run([exe, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=20)
        for line in (listing.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0][:1] in ("V", "A", "S"):
                encoders.add(parts[1])
        return version, encoders
    except Exception:
        return None


def find_ffmpeg():
    """Return (directory, version_string) or (None, None).

    Prefers a build that has every encoder the presets need; falls back to the
    first working build so a partial ffmpeg is still better than none.
    """
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    fallback = (None, None)
    seen = set()
    for directory in _ffmpeg_candidates():
        if not directory or directory in seen:
            continue
        seen.add(directory)
        exe = os.path.join(directory, exe_name)
        if not os.path.isfile(exe):
            continue
        probed = _probe_ffmpeg(exe)
        if not probed:
            continue
        version, encoders = probed
        if all(enc in encoders for enc in REQUIRED_ENCODERS):
            return directory, version
        if fallback == (None, None):
            fallback = (directory, version)
    return fallback


FFMPEG_DIR, FFMPEG_VERSION = find_ffmpeg()


# --------------------------------------------------------------------------- #
# yt-dlp options
# --------------------------------------------------------------------------- #

# Do NOT pin a player client here.
#
# yt-dlp picks its client list per release to match whatever YouTube is
# currently doing, and that choice is the single biggest factor in whether a
# download works. Every pin this file has carried has aged badly: "mweb"
# required a PO token, so without a running provider YouTube served only
# format 18 (360p); a "default,android_vr" pin got the full format list but
# URLs that 403 after ~20 MB. Plain `yt-dlp URL` -- which pins nothing --
# downloads the same video start to finish at full speed.
#
# So the rule is: match the CLI. Anything this file overrides is a deviation
# from the configuration upstream actually tests, and needs to earn its place.
# The bgutil provider below is used when it happens to be running; it is not
# required. 127.0.0.1 beats [::1] because IPv6 loopback is not reachable on
# every Windows setup.
YOUTUBE_POT_PROVIDER_URL = os.environ.get("YTDLP_POT_PROVIDER_URL", "http://127.0.0.1:4416")
YOUTUBE_POT_SERVER_HOME = os.environ.get(
    "YTDLP_POT_SERVER_HOME",
    os.path.join(os.path.expanduser("~"), "bgutil-ytdlp-pot-provider", "server"),
)
def bgutil_script_usable(home):
    """Is the bgutil *script* provider actually runnable?

    Merely pointing at a checkout is not enough. If its npm dependencies were
    never installed, yt-dlp still tries it, spawns deno, and deno sits there
    downloading packages until the 15s probe timeout -- and that TimeoutExpired
    propagates out of extraction, so a half-installed provider breaks every
    download rather than just being skipped. Only offer it when it can run.
    """
    if not home or not os.path.isdir(home):
        return False
    if not os.path.isfile(os.path.join(home, "src", "generate_once.ts")):
        return False
    modules = os.path.join(home, "node_modules")
    try:
        return os.path.isdir(modules) and bool(os.listdir(modules))
    except OSError:
        return False


YOUTUBE_EXTRACTOR_ARGS = {
    "youtubepot-bgutilhttp": {"base_url": [YOUTUBE_POT_PROVIDER_URL]},
}
if bgutil_script_usable(YOUTUBE_POT_SERVER_HOME):
    YOUTUBE_EXTRACTOR_ARGS["youtubepot-bgutilscript"] = {
        "server_home": [YOUTUBE_POT_SERVER_HOME],
    }
else:
    # Simply omitting the argument is not enough: the plugin falls back to a
    # baked-in default (~/bgutil-ytdlp-pot-provider/server) and probes it
    # anyway. Its availability check does return early when the script file is
    # missing, so point it somewhere that definitely has none -- otherwise it
    # spawns deno, waits out the 15s probe timeout, and the resulting
    # TimeoutExpired escapes and kills the whole extraction.
    YOUTUBE_EXTRACTOR_ARGS["youtubepot-bgutilscript"] = {
        "server_home": [os.path.join(DATA_DIR, "bgutil-script-disabled")],
    }

def local_js_runtime():
    """A locally installed JS engine yt-dlp can use to solve YouTube's challenges."""
    for exe in ("deno", "bun", "node"):
        if shutil.which(exe):
            return exe
    return None


JS_RUNTIME = local_js_runtime()

COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "logger": YdlLogger(),
    "noplaylist": True,
    "noprogress": True,
    "color": "no_color",          # keep ANSI escapes out of progress/error strings
    "retries": 10,
    "fragment_retries": 10,
    "concurrent_fragment_downloads": 8,
    # No http_chunk_size. Setting it makes yt-dlp fetch the file as a series of
    # separate ranged requests, and googlevideo rejects those with a 403 once
    # past the first chunk or two -- so downloads died at ~10-20 MB while the
    # bare CLI, which does not chunk, streamed the whole file at full speed.
    "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
}

# "ejs:github" lets yt-dlp fetch its JS-challenge solver from GitHub releases at
# runtime. That download goes through Cloudflare, which blocks it unless the
# optional curl_cffi impersonation dependency is installed -- and the failed
# fetch is retried before extraction can continue, so every download starts
# minutes late for no benefit. A locally installed deno/bun/node solves the same
# challenges without leaving the machine, so only ask for the remote component
# when there is no local engine to fall back on.
if not JS_RUNTIME:
    COMMON_OPTS["remote_components"] = ["ejs:github"]
if FFMPEG_DIR:
    COMMON_OPTS["ffmpeg_location"] = FFMPEG_DIR

# Quality presets. "max" deliberately sorts on bitrate after resolution so the
# fattest stream wins; yt-dlp's default sort would prefer a smaller AV1 stream
# at the same resolution. "mp4" trades a little quality for a file that plays
# in anything.
QUALITY_PRESETS = {
    "max": {
        "label": "Best quality",
        "format": "bv*+ba/b",
        "format_sort": ["res", "fps", "hdr:12", "proto", "br", "vbr", "abr"],
    },
    "efficient": {
        # Same resolution as "max" but lets yt-dlp's default codec preference
        # win, which picks AV1/VP9 over a fatter stream -- typically a third of
        # the bytes for the same picture. The right default on a slow link.
        "label": "Best quality, smaller file",
        "format": "bv*+ba/b",
        "format_sort": None,
    },
    "mp4": {
        "label": "Best MP4 (most compatible)",
        "format": "bv*[vcodec~='^(avc1|h264)']+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        "format_sort": ["res", "fps", "proto", "br"],
    },
    "2160": {"label": "Up to 2160p", "format": "bv*[height<=2160]+ba/b[height<=2160]/b",
             "format_sort": ["res", "fps", "proto", "br"]},
    "1440": {"label": "Up to 1440p", "format": "bv*[height<=1440]+ba/b[height<=1440]/b",
             "format_sort": ["res", "fps", "proto", "br"]},
    "1080": {"label": "Up to 1080p", "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
             "format_sort": ["res", "fps", "proto", "br"]},
    "720": {"label": "Up to 720p", "format": "bv*[height<=720]+ba/b[height<=720]/b",
            "format_sort": ["res", "fps", "proto", "br"]},
    "480": {"label": "Up to 480p", "format": "bv*[height<=480]+ba/b[height<=480]/b",
            "format_sort": ["res", "fps", "proto", "br"]},
    "audio": {"label": "Audio only (original)", "format": "ba/b",
              "format_sort": ["abr", "proto", "br"]},
    "mp3": {"label": "Audio only (MP3)", "format": "ba/b",
            "format_sort": ["abr", "proto", "br"], "audio_only": "mp3"},
}
DEFAULT_QUALITY = "max"

AUTO_UPDATE_ENABLED = os.environ.get("YTDLP_AUTO_UPDATE", "1") != "0"
AUTO_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #

UPDATE_STATE_LOCK = threading.Lock()
UPDATE_STATE = {
    "current_version": getattr(yt_dlp.version, "__version__", "unknown"),
    "checking": False,
    "last_checked": None,
    "last_result": None,     # "up_to_date" | "updated" | "error"
    "last_error": None,
    "latest_available": None,
    "auto_update": AUTO_UPDATE_ENABLED,
    "restart_pending": False,
}

SERVER = None         # the live ThreadingHTTPServer, so a restart can close it

JOBS = {}
JOBS_LOCK = threading.Lock()

HISTORY_LOCK = threading.Lock()
HISTORY = []          # oldest-first internally; served newest-first

FORMATS_CACHE = {}    # url -> (expires_at, payload)
FORMATS_CACHE_LOCK = threading.Lock()
FORMATS_CACHE_TTL = 300

DOWNLOAD_QUEUE = queue.Queue()

ACTIVE_STATES = ("queued", "downloading", "processing")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# --------------------------------------------------------------------------- #
# History persistence
# --------------------------------------------------------------------------- #

def load_history():
    global HISTORY
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        HISTORY = [r for r in data if isinstance(r, dict) and r.get("id")]
    except Exception as e:
        log(f"could not read history ({e}); starting empty")
        HISTORY = []

    # Anything left mid-flight belongs to a server that is no longer running.
    for rec in HISTORY:
        if rec.get("status") in ACTIVE_STATES:
            rec["status"] = "error"
            rec["error"] = "Interrupted -- the server stopped while this was downloading."
            rec.pop("percent", None)
            rec.pop("speed", None)


def save_history():
    """Caller must hold HISTORY_LOCK."""
    global HISTORY
    if len(HISTORY) > HISTORY_LIMIT:
        del HISTORY[:len(HISTORY) - HISTORY_LIMIT]
    tmp = HISTORY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(HISTORY, f, ensure_ascii=False, indent=2)
        os.replace(tmp, HISTORY_FILE)
    except Exception as e:
        log(f"could not write history: {e}")


def find_record(record_id):
    """Caller must hold HISTORY_LOCK."""
    if not record_id:
        return None
    for r in HISTORY:
        if r.get("id") == record_id:
            return r
    return None


def update_record(record_id, **fields):
    with HISTORY_LOCK:
        rec = find_record(record_id)
        if rec is None:
            return
        for key, value in fields.items():
            if value is _DROP:
                rec.pop(key, None)
            else:
                rec[key] = value
        save_history()


class _Drop:
    pass


_DROP = _Drop()


# --------------------------------------------------------------------------- #
# Error messages
# --------------------------------------------------------------------------- #

FRIENDLY_ERRORS = (
    ("ffmpeg is not installed",
     "ffmpeg is required to combine video and audio, and the server could not find it. "
     "Install it (winget install ffmpeg) or set YTDLP_FFMPEG to its path, then restart the server."),
    ("confirm you.re not a bot",
     "YouTube asked the server to prove it isn't a bot. Make sure the bgutil PO-token provider "
     "is running on port 4416, then try again."),
    ("sign in to confirm your age",
     "This video is age-restricted and needs a signed-in session to download."),
    ("private video",
     "This video is private."),
    ("video unavailable",
     "YouTube says this video is unavailable (it may be region-blocked or removed)."),
    ("members-only",
     "This video is members-only."),
    ("requested format is not available",
     "That exact format is no longer offered by the site. Reload the formats and pick again."),
    ("http error 403",
     "The media server rejected the transfer (403). This is usually a stale signed URL -- "
     "retrying the download normally clears it."),
    ("http error 416",
     "The saved partial stream is no longer compatible with the media server. "
     "The downloader will reset that stream and retry from the beginning."),
    ("http error 429",
     "The site is rate-limiting this machine (429). Wait a few minutes before retrying."),
    ("is not a valid url",
     "That doesn't look like a downloadable page URL."),
    ("unsupported url",
     "yt-dlp has no extractor for this URL."),
    ("no space left",
     "The disk is full."),
    ("winerror 32",
     "A file in the download folder is locked by another program. Close it and retry."),
)


def clean_error(err):
    text = ANSI_RE.sub("", str(err)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"\s*Aborting due to --abort-on-error\.?", "", text)
    low = text.lower()
    for needle, friendly in FRIENDLY_ERRORS:
        if re.search(needle, low):
            return friendly
    return text[:600] or "Download failed for an unknown reason."


def is_range_error(err):
    """Return whether an error means the server rejected a byte range."""
    low = ANSI_RE.sub("", str(err)).lower()
    return bool(re.search(r"\b(?:http\s+error\s+)?416\b|requested range not satisfiable", low))


def redownload_overwrites(record):
    """Whether a history re-download should replace existing output files."""
    return record.get("status") == "done"


# --------------------------------------------------------------------------- #
# yt-dlp update checking
# --------------------------------------------------------------------------- #

def get_installed_ytdlp_version():
    """The version pip has on disk -- reflects an upgrade before we restart."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "yt-dlp"],
            capture_output=True, text=True, timeout=60,
        )
        for line in result.stdout.splitlines():
            if line.lower().startswith("version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def get_latest_pypi_version():
    req = urllib.request.Request(
        "https://pypi.org/pypi/yt-dlp/json",
        headers={"User-Agent": "yt-dlp-extension-helper"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)["info"]["version"]


def active_job_count():
    with JOBS_LOCK:
        return sum(1 for j in JOBS.values() if j.get("status") in ACTIVE_STATES)


def restart_process():
    """Replace this process, but never in the middle of a download."""
    with UPDATE_STATE_LOCK:
        UPDATE_STATE["restart_pending"] = True
    waited = 0
    while active_job_count() and waited < 3600:
        time.sleep(2)
        waited += 2
    time.sleep(1)  # let the in-flight HTTP response finish sending
    log("restarting to pick up the new yt-dlp")

    # Drop the listening socket before the replacement tries to claim the port.
    # Windows' SO_REUSEADDR does not queue the second bind, it *hijacks*: two
    # live sockets end up on 4599 and which one a connection reaches is
    # arbitrary, so an update could leave a server that answers every other
    # request. Closing here makes the handover clean, and the child's bind
    # retry (see main) covers the moment in between.
    if SERVER is not None:
        try:
            SERVER.server_close()
        except Exception:
            pass

    script = os.path.abspath(__file__)
    argv = [sys.executable, script] + sys.argv[1:]
    if sys.platform == "win32":
        # os.execv on Windows joins argv into a command line without quoting,
        # so any path containing a space (e.g. "F:\Fadi Hanna\...") gets cut
        # at the space and the relaunch fails immediately with a cryptic
        # "can't find '__main__' module" error, killing the server for good.
        # subprocess.Popen builds the command line correctly, so spawn the
        # replacement process and exit this one instead of exec'ing in place.
        subprocess.Popen(argv, close_fds=True)
        os._exit(0)
    else:
        os.execv(sys.executable, argv)


def check_for_ytdlp_update(restart_on_update=True):
    with UPDATE_STATE_LOCK:
        if UPDATE_STATE["checking"]:
            return
        UPDATE_STATE["checking"] = True

    try:
        installed = get_installed_ytdlp_version() or UPDATE_STATE["current_version"]
        latest = get_latest_pypi_version()

        with UPDATE_STATE_LOCK:
            UPDATE_STATE["latest_available"] = latest

        if latest == installed:
            with UPDATE_STATE_LOCK:
                UPDATE_STATE["last_checked"] = time.time()
                UPDATE_STATE["last_result"] = "up_to_date"
                UPDATE_STATE["last_error"] = None
            return

        log(f"upgrading yt-dlp {installed} -> {latest}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "pip install failed").strip()[-400:])

        after = get_installed_ytdlp_version() or installed
        with UPDATE_STATE_LOCK:
            UPDATE_STATE["last_checked"] = time.time()
            UPDATE_STATE["last_result"] = "updated" if after != installed else "up_to_date"
            UPDATE_STATE["last_error"] = None
            UPDATE_STATE["latest_available"] = after

        if after != installed and restart_on_update:
            threading.Thread(target=restart_process, daemon=True).start()
    except Exception as e:
        log(f"update check failed: {e}")
        with UPDATE_STATE_LOCK:
            UPDATE_STATE["last_checked"] = time.time()
            UPDATE_STATE["last_result"] = "error"
            UPDATE_STATE["last_error"] = str(e)[:400]
    finally:
        with UPDATE_STATE_LOCK:
            UPDATE_STATE["checking"] = False


def auto_update_loop():
    time.sleep(30)  # let the server finish booting before the first check
    while True:
        with UPDATE_STATE_LOCK:
            enabled = UPDATE_STATE["auto_update"]
        if enabled:
            # The extension first: a yt-dlp upgrade ends in a restart, and
            # anything queued after it would never run.
            sync_extension()
            check_for_ytdlp_update(restart_on_update=True)
        time.sleep(AUTO_UPDATE_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# Keeping the extension itself up to date
#
# The server already keeps yt-dlp current, which is what stops YouTube-side
# breakage from becoming a dead tool. The extension and this server are the
# other half of that: a fix for a new failure mode usually lands here, not in
# yt-dlp. But an unpacked extension is not updated by Chrome -- it is whatever
# is on disk, and it only re-reads that on a reload -- so without this it drifts
# until someone remembers to git pull and click "reload" in chrome://extensions.
#
# So the same loop that upgrades yt-dlp also fast-forwards this checkout, and
# the extension's service worker notices the new manifest version and reloads
# itself. Two deliberate limits: only ever a fast-forward (never a merge that
# could conflict or rewrite work), and never when the working tree is dirty, so
# local edits are always left alone.
# --------------------------------------------------------------------------- #

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION_DIR = os.path.join(REPO_DIR, "extension")
EXTENSION_MANIFEST = os.path.join(EXTENSION_DIR, "manifest.json")
EXTENSION_AUTO_UPDATE = os.environ.get("YTDLP_EXTENSION_AUTO_UPDATE", "1") != "0"

EXTENSION_STATE_LOCK = threading.Lock()
EXTENSION_STATE = {
    "version": None,          # manifest version currently on disk
    "commit": None,           # short sha of the checkout
    "syncing": False,
    "last_checked": None,
    "last_result": None,      # up_to_date | updated | dirty | unavailable | error
    "last_error": None,
    "changed": [],            # which parts changed on the last successful pull
    "auto_update": EXTENSION_AUTO_UPDATE,
}


def read_extension_version():
    try:
        with open(EXTENSION_MANIFEST, "r", encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None


def run_git(args, timeout=90):
    """Run a git command in the checkout. Returns (ok, output)."""
    try:
        result = subprocess.run(
            ["git", "-C", REPO_DIR] + args,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, "git is not installed"
    except subprocess.TimeoutExpired:
        return False, f"git {args[0]} timed out"
    except Exception as e:
        return False, str(e)
    out = (result.stdout or "").strip() or (result.stderr or "").strip()
    return result.returncode == 0, out


def _set_extension_state(**fields):
    with EXTENSION_STATE_LOCK:
        EXTENSION_STATE.update(fields)
        EXTENSION_STATE["version"] = read_extension_version()
        EXTENSION_STATE["last_checked"] = time.time()


def sync_extension(restart_on_server_change=True):
    """Fast-forward this checkout so the extension follows upstream.

    Returns the result string. Never raises: a machine with no git, no remote,
    or no network still has to keep downloading videos.
    """
    with EXTENSION_STATE_LOCK:
        if EXTENSION_STATE["syncing"] or not EXTENSION_STATE["auto_update"]:
            return EXTENSION_STATE["last_result"]
        EXTENSION_STATE["syncing"] = True
    try:
        ok, _ = run_git(["rev-parse", "--is-inside-work-tree"], timeout=20)
        if not ok:
            _set_extension_state(last_result="unavailable", last_error=None)
            return "unavailable"

        ok, branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
        if not ok or branch == "HEAD":
            _set_extension_state(last_result="unavailable",
                                 last_error="not on a branch")
            return "unavailable"

        ok, upstream = run_git(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"],
                               timeout=20)
        if not ok:
            _set_extension_state(last_result="unavailable",
                                 last_error="this branch tracks no remote")
            return "unavailable"

        # Uncommitted work is the one thing that must never be touched. A pull
        # over it either refuses noisily or, worse, half-applies.
        ok, dirty = run_git(["status", "--porcelain"], timeout=30)
        if ok and dirty:
            _set_extension_state(last_result="dirty", last_error=None)
            return "dirty"

        ok, err = run_git(["fetch", "--quiet", "origin"], timeout=120)
        if not ok:
            _set_extension_state(last_result="error", last_error=err[:300])
            return "error"

        ok, before = run_git(["rev-parse", "HEAD"], timeout=20)
        ok2, target = run_git(["rev-parse", upstream], timeout=20)
        if not (ok and ok2):
            _set_extension_state(last_result="error",
                                 last_error="could not resolve HEAD or upstream")
            return "error"
        if before == target:
            _set_extension_state(last_result="up_to_date", last_error=None,
                                 commit=before[:8], changed=[])
            return "up_to_date"

        ok, err = run_git(["merge", "--ff-only", target], timeout=90)
        if not ok:
            # Diverged local history: a fast-forward is impossible and anything
            # else would rewrite the user's commits. Say so and stop.
            _set_extension_state(last_result="error",
                                 last_error=f"cannot fast-forward: {err[:250]}")
            return "error"

        _, files = run_git(["diff", "--name-only", before, target], timeout=30)
        changed = [f for f in files.splitlines() if f.strip()]
        touched_extension = any(f.startswith("extension/") for f in changed)
        touched_server = any(f.startswith("server/") for f in changed)

        _set_extension_state(last_result="updated", last_error=None,
                             commit=target[:8], changed=changed)
        log(f"extension checkout updated {before[:8]} -> {target[:8]} "
            f"({len(changed)} file(s) changed)")

        if touched_server and restart_on_server_change:
            log("the pull changed the server itself -- restarting into it")
            threading.Thread(target=restart_process, daemon=True).start()
        elif touched_extension:
            log("extension files changed; Chrome will reload on the next check")
        return "updated"
    except Exception as e:
        _set_extension_state(last_result="error", last_error=str(e)[:300])
        return "error"
    finally:
        with EXTENSION_STATE_LOCK:
            EXTENSION_STATE["syncing"] = False


def run_full_update():
    """One "Check for Updates": the extension, then yt-dlp.

    In that order, because a yt-dlp upgrade ends in a restart and would cut the
    sync short. Both write their own state, so the UI can report either half.
    """
    sync_extension()
    check_for_ytdlp_update(restart_on_update=True)


def extension_status():
    with EXTENSION_STATE_LOCK:
        state = dict(EXTENSION_STATE)
    state["version"] = state.get("version") or read_extension_version()
    with UPDATE_STATE_LOCK:
        state["ytdlp_version"] = UPDATE_STATE["current_version"]
        state["ytdlp_latest"] = UPDATE_STATE["latest_available"]
        state["restart_pending"] = UPDATE_STATE["restart_pending"]
    state["extension_dir"] = EXTENSION_DIR
    return state


# --------------------------------------------------------------------------- #
# Format listing
# --------------------------------------------------------------------------- #

def human_size(num_bytes):
    if not num_bytes:
        return None
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def describe_format(f):
    vcodec = f.get("vcodec") or "none"
    acodec = f.get("acodec") or "none"
    has_video = vcodec != "none"
    has_audio = acodec != "none"
    if has_video and has_audio:
        kind = "video+audio"
    elif has_video:
        kind = "video only"
    else:
        kind = "audio only"

    height = f.get("height") or 0
    resolution = f.get("resolution") or f.get("format_note") or "-"
    if has_video and height:
        fps = f.get("fps")
        resolution = f"{height}p{int(fps) if fps and fps >= 50 else ''}"

    return {
        "format_id": f.get("format_id"),
        "ext": f.get("ext", ""),
        "resolution": resolution,
        "kind": kind,
        "height": height,
        "fps": f.get("fps"),
        "vcodec": vcodec.split(".")[0] if has_video else None,
        "acodec": acodec.split(".")[0] if has_audio else None,
        "tbr": f.get("tbr"),
        "abr": f.get("abr"),
        "dynamic_range": f.get("dynamic_range"),
        "filesize": f.get("filesize") or f.get("filesize_approx"),
        "needs_audio": has_video and not has_audio,
    }


def extract_raw_info(url, _depth=0):
    """Extract without running yt-dlp's format selection.

    This must be the *unprocessed* result. extract_info(process=True) bakes one
    format choice into the dict, and re-selecting from an already-processed dict
    silently returns that stale choice no matter what format string you ask for
    -- which made every preset report the same streams.
    """
    with yt_dlp.YoutubeDL({**COMMON_OPTS, "skip_download": True}) as ydl:
        raw = ydl.extract_info(url, download=False, process=False)

    if raw is None:
        raise RuntimeError("Nothing could be extracted from that URL.")

    kind = raw.get("_type")

    if kind in ("url", "url_transparent") and not raw.get("formats"):
        # A watch?v=...&list=... URL is handled by the playlist extractor, which
        # with noplaylist returns a bare pointer to the video rather than the
        # video itself. Resolve it here, once -- otherwise every preset below
        # would re-resolve it over the network, turning one extraction into ten.
        target = raw.get("url")
        if target and _depth < 3:
            return extract_raw_info(target, _depth + 1)
        raise RuntimeError("No downloadable video found at that URL.")

    if kind in ("playlist", "multi_video"):
        # A bare playlist URL still arrives as a playlist; take its first item.
        first = next(iter(itertools.islice(raw.get("entries") or [], 1)), None)
        if not first:
            raise RuntimeError("No downloadable video found at that URL.")
        if first.get("formats"):
            return first
        target = first.get("webpage_url") or first.get("url")
        if target and _depth < 3:
            return extract_raw_info(target, _depth + 1)
        raise RuntimeError("No downloadable video found at that URL.")

    if not raw.get("formats"):
        raise RuntimeError("That page has no downloadable media.")

    return raw


def pick_thumbnail(info):
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    if not thumbs:
        return None
    # Unprocessed info hasn't sorted these yet; prefer the largest.
    thumbs.sort(key=lambda t: (t.get("preference") or 0,
                               (t.get("width") or 0) * (t.get("height") or 0)))
    return thumbs[-1]["url"]


def resolve_presets(raw_info):
    """What each quality preset would actually pick, with real byte sizes.

    Runs yt-dlp's own format selection offline against the already-extracted
    info -- no extra network calls -- so the popup can show "1.1 GB, mkv" next
    to "Best quality" instead of making the user find out after an hour of
    downloading. On a throttled connection this is the difference between an
    informed choice and a wasted evening.
    """
    out = []
    for key, preset in QUALITY_PRESETS.items():
        entry = {"key": key, "label": preset["label"], "available": False}
        try:
            opts = {
                **COMMON_OPTS,
                "format": preset["format"],
                "merge_output_format": "mp4/mkv",
                "simulate": True,
                "skip_download": True,
            }
            if preset.get("format_sort"):
                opts["format_sort"] = preset["format_sort"]
            with yt_dlp.YoutubeDL(opts) as ydl:
                selected = ydl.process_ie_result(copy.deepcopy(raw_info), download=False)
            chosen = selected.get("requested_formats") or [selected]

            size = 0
            for f in chosen:
                part = f.get("filesize") or f.get("filesize_approx")
                if not part:
                    size = 0          # partial totals would understate the cost
                    break
                size += part

            video = next((f for f in chosen if (f.get("vcodec") or "none") != "none"), None)
            audio = next((f for f in chosen if (f.get("acodec") or "none") != "none"), None)
            bits = []
            if video:
                height = video.get("height")
                label = f"{height}p" if height else (video.get("resolution") or "video")
                fps = video.get("fps")
                if fps and fps >= 50:
                    label += str(int(fps))
                bits.append(label)
                if video.get("vcodec"):
                    bits.append(video["vcodec"].split(".")[0])
            elif audio:
                bits.append("audio only")
                if audio.get("abr"):
                    bits.append(f"{int(audio['abr'])}kbps")
                if audio.get("acodec"):
                    bits.append(audio["acodec"].split(".")[0])

            entry.update({
                "available": True,
                "format_ids": [f.get("format_id") for f in chosen],
                "detail": " · ".join(bits),
                "ext": preset.get("audio_only") or selected.get("ext"),
                "filesize": size or None,
                "filesize_human": human_size(size) if size else None,
                "height": video.get("height") if video else None,
            })
        except Exception:
            pass  # a preset this video can't satisfy is simply marked unavailable
        out.append(entry)
    return mark_duplicate_presets(out)


# Presets that always earn a slot in the list, even when another one resolves
# to the same streams: they say something about intent ("give me an mp4",
# "give me audio") rather than about a resolution ceiling.
DISTINCT_PRESETS = ("max", "efficient", "mp4", "audio", "mp3")


def mark_duplicate_presets(presets):
    """Flag capped presets that resolve to exactly what an earlier one offers.

    On a 240p video every one of "Up to 2160p" … "Up to 480p" picks the same
    two streams, so the dropdown offered seven identical-looking choices and
    the real ones were buried. A cap that changes nothing isn't a choice.
    """
    seen = {}
    for entry in presets:
        if not entry.get("available"):
            continue
        key = tuple(entry.get("format_ids") or ())
        if not key:
            continue
        if entry["key"] in DISTINCT_PRESETS or key not in seen:
            seen.setdefault(key, entry["key"])
            continue
        entry["duplicate_of"] = seen[key]
    return presets


def get_formats(url, use_cache=True):
    now = time.time()
    if use_cache:
        with FORMATS_CACHE_LOCK:
            hit = FORMATS_CACHE.get(url)
            if hit and hit[0] > now:
                return hit[1]

    info = extract_raw_info(url)

    formats = []
    for f in info.get("formats", []):
        if not f.get("url") or f.get("ext") == "mhtml":
            continue  # storyboards aren't downloadable media
        formats.append(describe_format(f))

    # Best first: video by height then bitrate, then audio by bitrate.
    formats.sort(
        key=lambda f: (
            0 if f["kind"] == "audio only" else 1,
            f["height"] or 0,
            f["tbr"] or f["abr"] or 0,
        ),
        reverse=True,
    )

    presets = resolve_presets(info)
    best = next((p for p in presets if p["key"] == "max" and p["available"]), None)

    payload = {
        "title": info.get("title") or "video",
        "thumbnail": pick_thumbnail(info),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "is_live": bool(info.get("is_live")),
        "best_label": (f"{best['detail']} · {best['filesize_human']}"
                       if best and best["filesize_human"] else
                       (best["detail"] if best else None)),
        "formats": formats,
        "presets": presets,
        "default_quality": DEFAULT_QUALITY,
        "ffmpeg": bool(FFMPEG_DIR),
    }

    with FORMATS_CACHE_LOCK:
        FORMATS_CACHE[url] = (now + FORMATS_CACHE_TTL, payload)
        for stale in [k for k, (exp, _) in FORMATS_CACHE.items() if exp <= now]:
            FORMATS_CACHE.pop(stale, None)
    return payload


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #

def build_ydl_opts(spec):
    """Turn a download request into yt-dlp options."""
    preset = QUALITY_PRESETS.get(spec.get("quality") or DEFAULT_QUALITY,
                                 QUALITY_PRESETS[DEFAULT_QUALITY])

    format_id = spec.get("format_id")
    if format_id:
        # A hand-picked video-only stream would otherwise download silent.
        fmt = f"{format_id}+bestaudio/{format_id}" if spec.get("needs_audio") else format_id
        format_sort = None
    else:
        fmt = preset["format"]
        format_sort = preset.get("format_sort")  # None = yt-dlp's own preference

    # Tag the filename with the quality that was asked for. Without this, every
    # quality of the same video maps to one filename, and yt-dlp -- which does
    # not overwrite an existing output -- silently hands back the file already
    # on disk. Asking for 1080p after downloading 720p then "succeeds" instantly
    # and records the 720p file as though it were the 1080p one.
    # The height alone is not unique: "Best quality" and "Best quality, smaller
    # file" are the *same* resolution in different codecs, so they would share a
    # filename -- and since yt-dlp refuses to overwrite, switching between them
    # silently returns the file already on disk instead of the one you asked
    # for. The selected format id is the only thing guaranteed to differ, so it
    # goes in the name too.
    audio_download = preset.get("audio_only") or preset["format"].startswith("ba")
    quality_tag = "audio %(format_id)s" if audio_download else "%(height)sp %(format_id)s"

    opts = {
        **COMMON_OPTS,
        "format": fmt,
        "outtmpl": os.path.join(
            DOWNLOAD_DIR, f"%(title).150B [%(id)s] {quality_tag}.%(ext)s"),
        # "/"-separated preference: use mp4 when the codecs allow it, else mkv.
        # Forcing mp4 outright makes VP9/Opus (the top YouTube streams) fail or
        # silently land in a different container than the recorded filepath.
        "merge_output_format": "mp4/mkv",
        "postprocessors": [],
    }
    if format_sort:
        opts["format_sort"] = format_sort

    if spec.get("overwrite"):
        # "Re-download" must actually fetch again -- otherwise it is a no-op on
        # a file that is already there, which is exactly when you'd click it.
        opts["overwrites"] = True

    audio_only = preset.get("audio_only")
    if audio_only:
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_only,
            "preferredquality": "0",
        })

    if spec.get("embed_metadata", True):
        opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})
    if spec.get("embed_thumbnail") and not preset.get("audio_only"):
        opts["writethumbnail"] = True
        opts["postprocessors"].append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if spec.get("subtitles"):
        opts["writesubtitles"] = True
        opts["subtitleslangs"] = ["en.*", "-live_chat"]
        opts["postprocessors"].append({"key": "FFmpegEmbedSubtitle"})

    if not FFMPEG_DIR:
        # Nothing here can run without ffmpeg; drop the post-processing so at
        # least single-stream downloads succeed instead of aborting.
        opts["postprocessors"] = []
        opts.pop("writethumbnail", None)
        opts.pop("writesubtitles", None)

    return opts


def should_resume_after_failure(attempt_saw_data):
    """After a failed attempt, should the retry resume the partial file?

    Only if that attempt actually moved bytes. A leftover .part from an earlier
    failed run makes yt-dlp resume with a Range request, and a freshly signed
    googlevideo URL rejects that with a 403 before a single byte arrives -- so a
    retry that also resumes fails identically, and the download can never
    recover on its own. Transferring nothing is the signal that the resume
    offset itself is the problem.
    """
    return bool(attempt_saw_data)


def requires_merge(opts):
    fmt = opts.get("format") or ""
    return "+" in fmt


def job_cancelled(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.get("cancel"))


def run_download(job_id, record_id, url, spec):
    opts = build_ydl_opts(spec)

    if requires_merge(opts) and not FFMPEG_DIR:
        fail(job_id, record_id, clean_error("ffmpeg is not installed"))
        return

    # Progress across several streams (video, then audio) so the UI shows one
    # number that only climbs, instead of jumping 0->100 once per stream.
    # A merged download fetches exactly two streams; anything else fetches one.
    expected_streams = 2 if requires_merge(opts) else 1
    streams = {}
    streams_lock = threading.Lock()
    partials = set()   # exact temp files this job wrote, for cleanup on cancel
    attempt_saw_data = [False]   # did this attempt actually transfer anything?

    def publish():
        with streams_lock:
            done = sum(s["downloaded"] for s in streams.values())
            known_total = sum(s["total"] for s in streams.values() if s["total"])
            known_count = sum(1 for s in streams.values() if s["total"])
            speed = sum(s["speed"] for s in streams.values() if s["speed"])
            eta = max((s["eta"] for s in streams.values() if s["eta"]), default=None)

        percent = None
        if known_total and known_count:
            # Scale up by the streams we haven't seen yet (assuming they're
            # about the average size of the ones we have), so the bar doesn't
            # sit at 100% while the audio track is still downloading.
            estimated_total = known_total * expected_streams / known_count
            percent = min(99.9, done / estimated_total * 100)
        return done, known_total, percent, speed, eta

    def hook(d):
        if job_cancelled(job_id):
            raise DownloadCancelled("cancelled by user")

        key = d.get("filename") or d.get("tmpfilename") or "stream"
        status = d.get("status")
        for name in (d.get("tmpfilename"), d.get("filename")):
            if name:
                partials.add(name)

        with streams_lock:
            slot = streams.setdefault(key, {"downloaded": 0, "total": 0, "speed": 0, "eta": None})
            if status == "downloading":
                slot["downloaded"] = d.get("downloaded_bytes") or 0
                slot["total"] = d.get("total_bytes") or d.get("total_bytes_estimate") or slot["total"]
                slot["speed"] = d.get("speed") or 0
                slot["eta"] = d.get("eta")
            elif status == "finished":
                slot["total"] = d.get("total_bytes") or slot["downloaded"] or slot["total"]
                slot["downloaded"] = slot["total"]
                slot["speed"] = 0
                slot["eta"] = None
                slot["finished"] = True
            finished_streams = sum(1 for s in streams.values() if s.get("finished"))

        if status == "downloading":
            attempt_saw_data[0] = True

        done, total, percent, speed, eta = publish()
        percent_str = f"{percent:.1f}%" if percent is not None else ""
        speed_str = f"{human_size(speed)}/s" if speed else ""

        # A merged download reports "finished" once per stream. Calling the
        # first one "processing" is wrong -- the audio track hasn't started yet
        # -- and it made the dashboard hide the progress bar and claim it was
        # merging for the whole second half of the transfer. Only the last
        # stream finishing means the download itself is over.
        still_downloading = (status == "downloading"
                             or finished_streams < expected_streams)

        fields = {
            "status": "downloading" if still_downloading else "processing",
            "percent": percent_str,
            "percent_value": round(percent, 1) if percent is not None else None,
            "speed": speed_str,
            "eta": eta,
            "downloaded_bytes": done,
            "total_bytes": total or None,
        }
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job.update(fields)
        update_record(record_id, **{k: v for k, v in fields.items()
                                    if k in ("status", "percent", "percent_value", "speed")})

    def pp_hook(d):
        if d.get("status") != "started":
            return
        name = {
            "Merger": "Merging video and audio…",
            "FFmpegMetadata": "Writing metadata…",
            "EmbedThumbnail": "Embedding thumbnail…",
            "ExtractAudio": "Extracting audio…",
            "FFmpegEmbedSubtitle": "Embedding subtitles…",
        }.get(d.get("postprocessor"), "Processing…")
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                job["status"] = "processing"
                job["stage"] = name
        update_record(record_id, status="processing", stage=name)

    opts["progress_hooks"] = [hook]
    opts["postprocessor_hooks"] = [pp_hook]

    # googlevideo URLs can be rate-limited mid-transfer, which surfaces as a
    # bare 403 that a same-URL retry can't fix. Re-extracting from scratch gets
    # a fresh signed URL, so retry the whole download before giving up.
    max_attempts = 3
    last_error = None
    filepath = None

    for attempt in range(1, max_attempts + 1):
        if job_cancelled(job_id):
            last_error = DownloadCancelled("cancelled by user")
            break
        attempt_saw_data[0] = False
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = resolve_output_path(ydl, info)
            last_error = None
            break
        except DownloadCancelled as e:
            last_error = e
            break
        except Exception as e:
            last_error = e
            log(f"attempt {attempt}/{max_attempts} failed: {clean_error(e)[:160]}")
            if attempt < max_attempts:
                # A 416 is specifically a bad byte range. Even if the attempt
                # transferred bytes before the server rejected its next range,
                # resuming at that offset will reproduce the same failure. Force
                # yt-dlp to open the .part in write mode on the next attempt;
                # this preserves the file until the retry actually starts and
                # avoids deleting a potentially useful partial pre-emptively.
                if is_range_error(e):
                    opts["continuedl"] = False
                    log("server rejected the resume range -- restarting this "
                        "stream from byte zero")
                else:
                    # A leftover .part from an earlier failed run can make a
                    # freshly signed URL reject a resume before any bytes move.
                    # Keep resuming only when this attempt actually transferred
                    # data; otherwise start over on the next attempt.
                    opts["continuedl"] = should_resume_after_failure(attempt_saw_data[0])
                    if not attempt_saw_data[0]:
                        log("nothing transferred -- restarting this download "
                            "from byte zero")
                streams.clear()
                update_record(record_id, status="downloading", percent="0%",
                              percent_value=0,
                              speed=f"retrying ({attempt}/{max_attempts - 1})…")
                time.sleep(2)

    if isinstance(last_error, DownloadCancelled):
        finish_cancelled(job_id, record_id, partials)
        return
    if last_error is not None:
        fail(job_id, record_id, clean_error(last_error))
        return

    filesize = os.path.getsize(filepath) if filepath and os.path.exists(filepath) else None
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update({"status": "done", "filepath": filepath, "percent": "100%",
                        "percent_value": 100, "speed": "", "stage": None})
    update_record(record_id, status="done", filepath=filepath, filesize=filesize,
                  filesize_human=human_size(filesize), finished_at=time.time(),
                  error=None, percent=_DROP, speed=_DROP, stage=_DROP)
    log(f"done: {os.path.basename(filepath) if filepath else '?'} ({human_size(filesize)})")


def resolve_output_path(ydl, info):
    """The real final path, after merging and post-processing.

    Never reconstruct this by hand: the container actually used depends on codec
    compatibility, and post-processors (audio extraction) change the extension.
    """
    for entry in (info.get("requested_downloads") or []):
        path = entry.get("filepath")
        if path:
            return path
    if info.get("filepath"):
        return info["filepath"]
    guess = ydl.prepare_filename(info)
    if guess and os.path.exists(guess):
        return guess
    # Last resort: match on the stem, whatever extension it ended up with.
    stem = os.path.splitext(guess or "")[0]
    if stem:
        for name in os.listdir(DOWNLOAD_DIR):
            candidate = os.path.join(DOWNLOAD_DIR, name)
            if os.path.splitext(candidate)[0] == stem:
                return candidate
    return guess


def fail(job_id, record_id, message):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update({"status": "error", "error": message, "stage": None})
    update_record(record_id, status="error", error=message, finished_at=time.time(),
                  percent=_DROP, speed=_DROP, stage=_DROP)
    log(f"failed: {message[:160]}")


def finish_cancelled(job_id, record_id, partials=None):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update({"status": "cancelled", "stage": None})
    update_record(record_id, status="cancelled", error="Cancelled.",
                  finished_at=time.time(), percent=_DROP, speed=_DROP, stage=_DROP)
    log("cancelled by user")
    cleanup_partials(partials)


def cleanup_partials(partials):
    """Remove the temp files this job wrote, so a cancel doesn't litter.

    Only ever touch paths yt-dlp reported for *this* job. Scanning the download
    folder for "*.part" and guessing which are abandoned is not safe: a
    concurrent download's temp file looks exactly the same, and mtime can't
    distinguish them reliably.
    """
    for path in sorted(partials or ()):
        if not inside_download_dir(path):
            continue
        for candidate in (path, path + ".part", path + ".ytdl"):
            try:
                if os.path.isfile(candidate):
                    os.remove(candidate)
            except OSError:
                pass  # still locked by a writer that hasn't unwound yet


def worker():
    while True:
        job_id, record_id, url, spec = DOWNLOAD_QUEUE.get()
        try:
            if job_cancelled(job_id):
                finish_cancelled(job_id, record_id)
                continue
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job:
                    job["status"] = "downloading"
            update_record(record_id, status="downloading")
            log(f"downloading {url} ({spec.get('quality') or spec.get('format_id')})")
            run_download(job_id, record_id, url, spec)
        except Exception as e:  # a worker must never die
            log(f"worker error: {e}")
            try:
                fail(job_id, record_id, clean_error(e))
            except Exception:
                pass
        finally:
            DOWNLOAD_QUEUE.task_done()


def progress_from_history(job_id):
    """Reconstruct a progress payload for a job the JOBS table no longer has.

    The in-memory table is lost on restart -- which the auto-updater does on
    purpose -- and old entries are evicted once it grows past 200. Either way a
    popup that was polling gets a 404 and sits forever on "lost contact with
    the server", even though the download finished perfectly and the history
    record says so. The record outlives the job, so answer from it.
    """
    if not job_id:
        return None
    with HISTORY_LOCK:
        rec = next((dict(r) for r in HISTORY if r.get("job_id") == job_id), None)
    if not rec:
        return None
    status = rec.get("status")
    if status in ACTIVE_STATES:
        # Nothing is running it any more; the process that owned it is gone.
        status = "error"
        rec["error"] = "Interrupted -- the server restarted while this was downloading."
    return {
        "status": status,
        "record_id": rec.get("id"),
        "percent": rec.get("percent"),
        "percent_value": rec.get("percent_value"),
        "speed": rec.get("speed"),
        "stage": rec.get("stage"),
        "filepath": rec.get("filepath"),
        "error": rec.get("error"),
        "from_history": True,
    }


def start_download_job(url, spec, meta):
    job_id = uuid.uuid4().hex
    record_id = uuid.uuid4().hex

    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "cancel": False, "record_id": record_id}
        # Keep the job table from growing without bound.
        if len(JOBS) > 200:
            for old in [k for k, v in list(JOBS.items())
                        if v.get("status") not in ACTIVE_STATES][:100]:
                JOBS.pop(old, None)

    quality = spec.get("quality") or DEFAULT_QUALITY
    with HISTORY_LOCK:
        HISTORY.append({
            "id": record_id,
            "job_id": job_id,
            "url": url,
            "format_id": spec.get("format_id"),
            "needs_audio": spec.get("needs_audio", False),
            "quality": quality,
            "title": meta.get("title") or "Untitled",
            "thumbnail": meta.get("thumbnail"),
            "uploader": meta.get("uploader"),
            "duration": meta.get("duration"),
            "resolution_label": meta.get("resolution_label")
                                or QUALITY_PRESETS.get(quality, {}).get("label"),
            "status": "queued",
            "filepath": None,
            "filesize": None,
            "filesize_human": None,
            "created_at": time.time(),
            "finished_at": None,
            "error": None,
        })
        save_history()

    DOWNLOAD_QUEUE.put((job_id, record_id, url, spec))
    return job_id, record_id


# --------------------------------------------------------------------------- #
# File manager integration
# --------------------------------------------------------------------------- #

def reveal_in_explorer(path):
    if sys.platform == "win32":
        # explorer.exe parses its own command line rather than using normal
        # argv rules: "/select," must sit outside any quotes, with only the
        # path itself quoted. Passing ["explorer", f"/select,{path}"] as a
        # list lets list2cmdline wrap the whole "/select,<path>" token in one
        # pair of quotes when the path has spaces, which explorer doesn't
        # understand -- it silently drops /select and opens its default
        # folder instead. Passing the exact command line as a string (Windows
        # sends it to CreateProcess verbatim, no shell involved) avoids that.
        norm = os.path.normpath(path)
        subprocess.run(f'explorer /select,"{norm}"')
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", path])
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)])


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


def inside_download_dir(path):
    """Guard the file-touching endpoints against paths outside the sandbox."""
    try:
        root = os.path.realpath(DOWNLOAD_DIR)
        target = os.path.realpath(path)
        return os.path.commonpath([root, target]) == root
    except (ValueError, OSError):
        return False


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

EXTRA_ORIGINS = [o.strip() for o in os.environ.get("YTDLP_EXTRA_ORIGINS", "").split(",") if o.strip()]


def origin_allowed(origin):
    """Only browser extensions and non-browser clients may drive this server.

    Without this, any web page you visit could POST to 127.0.0.1:4599 and start
    downloads, open files, or delete them -- CORS "*" hands that ability to
    every origin.
    """
    if not origin:
        return True  # curl, scripts, same-origin navigations
    if origin.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://")):
        return True
    return origin in EXTRA_ORIGINS


def request_allowed(origin, sec_fetch_site=None):
    """The origin check, plus the case where a page sends no Origin at all.

    Not every browser request carries one: <img src>, <script src> and plain
    navigations don't. A page can't *read* a JSON response fetched that way,
    but it can still cause the request, and GET /formats does real work (a full
    extraction) on the server's behalf. Chrome labels those loads with
    Sec-Fetch-Site, so a cross-site one can be refused without breaking curl,
    which sends neither header.
    """
    if origin:
        return origin_allowed(origin)
    return sec_fetch_site != "cross-site"


def cors_headers(origin):
    return {
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"   # keep-alive: the dashboard polls constantly,
                                    # and HTTP/1.0 left a socket in TIME_WAIT per poll
    server_version = "ytdlp-helper"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # requests are logged where it matters, not per socket

    # -- helpers ----------------------------------------------------------- #

    def _origin(self):
        return self.headers.get("Origin")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in cors_headers(self._origin()).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > 1_000_000:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _guard_origin(self):
        if request_allowed(self._origin(), self.headers.get("Sec-Fetch-Site")):
            return True
        self._send_json(403, {"error": "This server only accepts requests from the extension."})
        return False

    def _record_for(self, data, require_file=False):
        with HISTORY_LOCK:
            rec = find_record(data.get("id") if data else None)
            rec = dict(rec) if rec else None
        if not rec:
            self._send_json(404, {"error": "unknown record"})
            return None
        if require_file:
            path = rec.get("filepath")
            if not path or not os.path.exists(path):
                self._send_json(404, {"error": "That file is no longer on disk."})
                return None
            if not inside_download_dir(path):
                self._send_json(403, {"error": "refusing to touch a file outside the download folder"})
                return None
        return rec

    # -- routes ------------------------------------------------------------ #

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        for k, v in cors_headers(self._origin()).items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        if not self._guard_origin():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/ping", "/health"):
            with UPDATE_STATE_LOCK:
                version = UPDATE_STATE["current_version"]
            payload = {
                "ok": True,
                "ytdlp_version": version,
                "ffmpeg": bool(FFMPEG_DIR),
                "ffmpeg_version": FFMPEG_VERSION,
                "download_dir": DOWNLOAD_DIR,
                "active": active_job_count(),
                "max_concurrent": MAX_CONCURRENT,
            }
            if path == "/health":
                payload.update({
                    "ffmpeg_dir": FFMPEG_DIR,
                    "python": sys.version.split()[0],
                    "pot_provider": YOUTUBE_POT_PROVIDER_URL,
                    "pot_provider_reachable": pot_provider_reachable(),
                    "presets": {k: v["label"] for k, v in QUALITY_PRESETS.items()},
                })
            self._send_json(200, payload)
            return

        if path == "/formats":
            qs = parse_qs(parsed.query)
            url = (qs.get("url", [None])[0] or "").strip()
            if not url:
                self._send_json(400, {"error": "missing url"})
                return
            if not url.startswith(("http://", "https://")):
                self._send_json(400, {"error": "Only http(s) URLs can be downloaded."})
                return
            refresh = qs.get("refresh", ["0"])[0] == "1"
            try:
                self._send_json(200, get_formats(url, use_cache=not refresh))
            except Exception as e:
                self._send_json(500, {"error": clean_error(e)})
            return

        if path.startswith("/progress/"):
            job_id = path.split("/progress/", 1)[1]
            with JOBS_LOCK:
                job = dict(JOBS.get(job_id) or {})
            if not job:
                job = progress_from_history(job_id)
            if not job:
                self._send_json(404, {"error": "unknown job"})
                return
            job.pop("cancel", None)
            self._send_json(200, job)
            return

        if path == "/version":
            with UPDATE_STATE_LOCK:
                payload = dict(UPDATE_STATE)
            payload["extension_version"] = read_extension_version()
            self._send_json(200, payload)
            return

        if path == "/extension":
            self._send_json(200, extension_status())
            return

        if path == "/history":
            with HISTORY_LOCK:
                items = list(reversed(HISTORY))
            total_size = sum(r.get("filesize") or 0 for r in items)
            self._send_json(200, {
                "items": items,
                "count": len(items),
                "active": sum(1 for r in items if r.get("status") in ACTIVE_STATES),
                "total_size": total_size,
                "total_size_human": human_size(total_size),
                "download_dir": DOWNLOAD_DIR,
                "ffmpeg": bool(FFMPEG_DIR),
            })
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard_origin():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        data = self._read_json_body()
        if data is None:
            self._send_json(400, {"error": "invalid json body"})
            return

        if path == "/download":
            url = (data.get("url") or "").strip()
            if not url:
                self._send_json(400, {"error": "missing url"})
                return
            if not url.startswith(("http://", "https://")):
                self._send_json(400, {"error": "Only http(s) URLs can be downloaded."})
                return
            quality = data.get("quality") or DEFAULT_QUALITY
            if quality not in QUALITY_PRESETS:
                self._send_json(400, {"error": f"unknown quality preset '{quality}'"})
                return
            spec = {
                "quality": quality,
                "format_id": data.get("format_id") or None,
                "needs_audio": bool(data.get("needs_audio")),
                "embed_metadata": data.get("embed_metadata", True),
                "embed_thumbnail": bool(data.get("embed_thumbnail", False)),
                "subtitles": bool(data.get("subtitles")),
            }
            job_id, record_id = start_download_job(url, spec, {
                "title": data.get("title"),
                "thumbnail": data.get("thumbnail"),
                "uploader": data.get("uploader"),
                "duration": data.get("duration"),
                "resolution_label": data.get("resolution_label"),
            })
            self._send_json(200, {
                "job_id": job_id,
                "record_id": record_id,
                "download_dir": DOWNLOAD_DIR,
                "queued_behind": max(0, DOWNLOAD_QUEUE.qsize() - 1),
            })
            return

        if path == "/cancel":
            job_id = data.get("job_id")
            if not job_id and data.get("id"):
                with HISTORY_LOCK:
                    rec = find_record(data["id"])
                    job_id = rec.get("job_id") if rec else None
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job:
                    self._send_json(404, {"error": "unknown job"})
                    return
                if job.get("status") not in ACTIVE_STATES:
                    self._send_json(409, {"error": "that download already finished"})
                    return
                job["cancel"] = True
            self._send_json(200, {"ok": True})
            return

        if path == "/redownload":
            rec = self._record_for(data)
            if not rec:
                return
            # Failed/interrupted jobs should resume their .part files. A
            # completed job is an intentional re-download and should replace
            # its final output. The old implementation always set overwrite,
            # which destroyed a large resumable partial when the user clicked
            # the same button on a failed download.
            job_id, record_id = start_download_job(rec["url"], {
                "quality": rec.get("quality") or DEFAULT_QUALITY,
                "format_id": rec.get("format_id"),
                "needs_audio": rec.get("needs_audio", False),
                "overwrite": redownload_overwrites(rec),
            }, rec)
            self._send_json(200, {"job_id": job_id, "record_id": record_id})
            return

        if path in ("/open_file", "/open_folder"):
            rec = self._record_for(data, require_file=True)
            if not rec:
                return
            try:
                if path == "/open_file":
                    open_path(rec["filepath"])
                else:
                    reveal_in_explorer(rec["filepath"])
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": clean_error(e)})
            return

        if path == "/open_downloads_folder":
            try:
                open_path(DOWNLOAD_DIR)
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"error": clean_error(e)})
            return

        if path == "/delete":
            record_id = data.get("id")
            delete_file = data.get("delete_file", True)
            with HISTORY_LOCK:
                rec = find_record(record_id)
                if not rec:
                    self._send_json(404, {"error": "unknown record"})
                    return
                filepath = rec.get("filepath")
                if delete_file and filepath and os.path.exists(filepath):
                    if not inside_download_dir(filepath):
                        self._send_json(403, {"error": "refusing to delete outside the download folder"})
                        return
                    try:
                        os.remove(filepath)
                    except OSError as e:
                        self._send_json(500, {"error": f"could not delete the file: {e}"})
                        return
                HISTORY.remove(rec)
                save_history()
            self._send_json(200, {"ok": True})
            return

        if path == "/clear_history":
            status = data.get("status")
            with HISTORY_LOCK:
                keep = [r for r in HISTORY
                        if r.get("status") in ACTIVE_STATES
                        or (status and r.get("status") != status)]
                removed = len(HISTORY) - len(keep)
                HISTORY[:] = keep
                save_history()
            self._send_json(200, {"ok": True, "removed": removed})
            return

        if path == "/update":
            with UPDATE_STATE_LOCK:
                if UPDATE_STATE["checking"]:
                    self._send_json(409, {"error": "an update check is already running"})
                    return
            threading.Thread(target=run_full_update, daemon=True).start()
            self._send_json(200, {"ok": True, "message": "checking for updates"})
            return

        if path == "/update_extension":
            with EXTENSION_STATE_LOCK:
                if EXTENSION_STATE["syncing"]:
                    self._send_json(409, {"error": "a sync is already running"})
                    return
            threading.Thread(target=sync_extension, daemon=True).start()
            self._send_json(200, {"ok": True, "message": "syncing the extension"})
            return

        self._send_json(404, {"error": "not found"})


def warm_up_ytdlp():
    """Instantiate YoutubeDL once, single-threaded, before any worker runs.

    yt-dlp loads its plugin packages (the bgutil PO-token providers here) the
    first time a YoutubeDL is constructed, and that registration is not
    thread-safe: two workers starting a download at the same moment race and
    one of them dies with "PoTokenProvider ... already registered". Doing it
    once up front removes the race entirely.
    """
    try:
        yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                          "logger": YdlLogger()}).close()
    except Exception as e:
        log(f"yt-dlp warm-up warning: {e}")


def pot_provider_reachable():
    try:
        base = YOUTUBE_POT_PROVIDER_URL.rstrip("/")
        urllib.request.urlopen(f"{base}/ping", timeout=2)
        return True
    except Exception:
        try:
            urllib.request.urlopen(YOUTUBE_POT_PROVIDER_URL, timeout=2)
            return True
        except Exception:
            return False


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Not allow_reuse_address: on Windows that flag means "bind even though
    # another live socket already has this port", which is exactly the mistake
    # to avoid when a restarting server hands the port over. Two listeners then
    # split the incoming connections between them. Retrying the bind (below)
    # gives the same tolerance for the handover without the hijack.
    allow_reuse_address = False


BIND_RETRY_SECONDS = 20


def bind_server(port=None, retry_seconds=BIND_RETRY_SECONDS):
    """Bind the listening socket, waiting out a predecessor that is exiting.

    An auto-update restart spawns the replacement while this process is still
    unwinding, so the first bind can lose by a few hundred milliseconds. Giving
    up there would leave no server at all -- the update would take the tool
    down instead of keeping it current -- so retry briefly before reporting the
    port as genuinely occupied.
    """
    port = PORT if port is None else port
    deadline = time.time() + retry_seconds
    last = None
    first = True
    while True:
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError as e:
            last = e
            if time.time() >= deadline:
                break
            if first:
                log(f"port {port} is busy, waiting for it to free up…")
                first = False
            time.sleep(0.5)

    print(f"Could not bind 127.0.0.1:{port}: {last}", file=sys.stderr)
    print("Another copy of this server is probably already running.", file=sys.stderr)
    print("Stop it first, or set YTDLP_SERVER_PORT to a free port.", file=sys.stderr)
    return None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    load_history()
    warm_up_ytdlp()

    for _ in range(MAX_CONCURRENT):
        threading.Thread(target=worker, daemon=True).start()
    if AUTO_UPDATE_ENABLED:
        threading.Thread(target=auto_update_loop, daemon=True).start()

    global SERVER
    server = bind_server()
    if server is None:
        return 1
    SERVER = server

    print(f"yt-dlp helper server listening on http://127.0.0.1:{PORT}")
    print(f"  yt-dlp    {UPDATE_STATE['current_version']}"
          f"{'  (auto-update every %dh)' % (AUTO_UPDATE_INTERVAL_SECONDS // 3600) if AUTO_UPDATE_ENABLED else '  (auto-update off)'}")
    if FFMPEG_DIR:
        print(f"  ffmpeg    {FFMPEG_VERSION}")
        print(f"            {FFMPEG_DIR}")
    else:
        print("  ffmpeg    NOT FOUND - merged (best-quality) downloads will fail.")
        print("            Install it with:  winget install ffmpeg")
        print("            or point YTDLP_FFMPEG at the executable, then restart.")
    ext_version = read_extension_version()
    print(f"  extension {ext_version or 'unknown'}"
          f"{'  (auto-update on)' if EXTENSION_AUTO_UPDATE and AUTO_UPDATE_ENABLED else '  (auto-update off)'}")
    print(f"  saving to {DOWNLOAD_DIR}")
    print(f"  {MAX_CONCURRENT} concurrent download slot(s)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
