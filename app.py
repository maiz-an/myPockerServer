import hashlib
import json
import os
import re
import secrets
import string
import threading
import time
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from markupsafe import Markup, escape
from flask import (
    Flask, Response, request, session, redirect, url_for,
    send_from_directory, abort, render_template_string
)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# The app name is fixed here on purpose (no .env setting needed).
APP_NAME = "myPocketServer"

PASSWORD = os.environ.get("FILE_SERVER_PASSWORD", "")
SECRET_KEY = os.environ.get("FLASK_SECRET", "")

try:
    VERSION = (BASE_DIR / "VERSION").read_text().strip()
except OSError:
    VERSION = "dev"

if not PASSWORD:
    raise RuntimeError(
        "FILE_SERVER_PASSWORD is missing. Add it to your .env file."
    )

if len(SECRET_KEY) < 32:
    raise RuntimeError(
        "FLASK_SECRET must be at least 32 characters. "
        "Generate one using Python secrets.token_hex(32)."
    )

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ENABLED") == "1",
    MAX_CONTENT_LENGTH=1024 * 1024,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)

# --------------------------------------------------
# FOLDERS (folders.json)
# --------------------------------------------------
#
# folders.json lists the folders to show on the page. It is read again whenever it
# changes, so edit it, save, refresh the browser: no restart needed.

FOLDERS_FILE = BASE_DIR / "folders.json"
LEGACY_SHARED = BASE_DIR / "shared"  # older versions served this one folder

FOLDERS_HELP = (
    "One entry per folder. Use forward slashes on every OS, for example "
    "D:/Movies or /home/me/Photos. 'name' is optional. "
    "Save the file and refresh the page: no restart needed."
)


def ensure_folders_file():
    """Create folders.json on first use. Existing installs keep serving their shared folder."""
    if FOLDERS_FILE.exists():
        return
    entries = [{"name": "Shared", "path": "shared"}] if LEGACY_SHARED.is_dir() else []
    try:
        FOLDERS_FILE.write_text(
            json.dumps({"_help": FOLDERS_HELP, "folders": entries}, indent=2) + "\n",
            encoding="utf-8")
    except OSError:
        pass


def _read_folder_entries():
    """Return [(name, raw_path), ...] from folders.json. Raises ValueError if it is unusable."""
    ensure_folders_file()
    if not FOLDERS_FILE.exists():  # could not be created: fall back to the old folder
        return [("Shared", "shared")] if LEGACY_SHARED.is_dir() else []

    text = FOLDERS_FILE.read_text(encoding="utf-8-sig")  # utf-8-sig: Notepad may add a BOM
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Common Windows mistake: "D:\Movies" written with single backslashes.
        try:
            data = json.loads(text.replace("\\", "\\\\"))
        except json.JSONDecodeError as e:
            raise ValueError(f"not valid JSON (line {e.lineno})") from None

    items = data.get("folders", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError('"folders" must be a list')

    out = []
    for item in items:
        if isinstance(item, str):
            name, raw = "", item
        elif isinstance(item, dict):
            name, raw = str(item.get("name") or "").strip(), item.get("path")
        else:
            continue
        if isinstance(raw, str) and raw.strip():
            out.append((name, raw.strip()))
    return out


def _resolve_folder(raw):
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _folder_id(resolved):
    return hashlib.sha1(str(resolved).encode("utf-8", "replace")).hexdigest()[:10]


_folder_cache = {"stamp": object(), "entries": [], "error": None}
_folders_lock = threading.Lock()


def get_folders():
    """Current folders as a list of dicts, plus an error message if folders.json is broken.

    The file is only re-parsed when it changes. Whether each folder exists is checked on
    every call, so a drive that gets plugged in later just appears.
    """
    try:
        stamp = FOLDERS_FILE.stat().st_mtime_ns
    except OSError:
        stamp = None

    cache = _folder_cache
    if cache["stamp"] != stamp:
        cache["stamp"] = stamp
        try:
            cache["entries"] = _read_folder_entries()
            cache["error"] = None
        except (ValueError, OSError) as e:
            # keep serving the last working list instead of taking the site down
            cache["error"] = f"folders.json problem: {e}"
            print(cache["error"], flush=True)

    folders, seen = [], set()
    for name, raw in cache["entries"]:
        path = _resolve_folder(raw)
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            continue
        fid = _folder_id(resolved)
        if fid in seen:
            continue
        seen.add(fid)
        try:
            # the app's own folder is never shared (it holds the code)
            ok = resolved.is_dir() and resolved != BASE_DIR
        except OSError:
            ok = False
        folders.append({
            "id": fid,
            "name": name or resolved.name or f"Folder {len(folders) + 1}",
            "path": resolved,
            "ok": ok,
        })
    return folders, cache["error"]


def _write_entries(entries):
    """Save [(name, raw_path), ...] to folders.json atomically (never leaves a half-written file)."""
    items = [({"name": n, "path": r} if n else {"path": r}) for n, r in entries]
    data = {"_help": FOLDERS_HELP, "folders": items}
    tmp = FOLDERS_FILE.with_name(FOLDERS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, FOLDERS_FILE)
    _folder_cache["stamp"] = object()  # force a re-read right away


def _entry_id(raw):
    try:
        return _folder_id(_resolve_folder(raw).resolve())
    except (OSError, RuntimeError):
        return None


def change_folders(action, fid=None, path=None, name=""):
    """Add, remove or rename a shared folder. Returns (ok, message).

    Only the folders.json entries involved are touched; everything else is kept as written.
    If the file currently has an error it is left alone so nothing hand-written is lost.
    """
    with _folders_lock:
        try:
            entries = _read_folder_entries()
        except (ValueError, OSError) as e:
            return False, f"folders.json has a problem ({e}). Fix it first, then try again."

        if action == "add":
            target = Path(os.path.abspath(path))
            try:
                resolved = target.resolve()
                if not resolved.is_dir():
                    return False, "That folder does not exist."
            except (OSError, RuntimeError):
                return False, "That folder can't be opened."
            if resolved == BASE_DIR:
                return False, "The app's own folder can't be shared."
            new_id = _folder_id(resolved)
            if any(_entry_id(raw) == new_id for _, raw in entries):
                return False, "That folder is already shared."
            label = name or ""
            entries.append((label, target.as_posix()))
            _write_entries(entries)
            return True, f"Now sharing {label or resolved.name or target.as_posix()}."

        matches = [i for i, (_, raw) in enumerate(entries) if _entry_id(raw) == fid]
        if not matches:
            return False, "That folder is no longer in the list."
        if action == "remove":
            entries = [e for i, e in enumerate(entries) if i not in matches]
            _write_entries(entries)
            return True, "Stopped sharing that folder. Its files were not touched."
        if action == "rename":
            entries = [((name if i in matches else n), raw) for i, (n, raw) in enumerate(entries)]
            _write_entries(entries)
            return True, "Name saved."
    return False, "Unknown action."


def find_folder(fid):
    for folder in get_folders()[0]:
        if folder["id"] == fid:
            return folder
    return None


def print_folders():
    folders, error = get_folders()
    if not folders:
        print("No folders yet. Open the site on this PC and use the gear icon to add some.", flush=True)
    for f in folders:
        print(f"  {f['name']}: {f['path']}" + ("" if f["ok"] else "  (not found)"), flush=True)


# --------------------------------------------------
# LOGIN THROTTLE (the site can be reachable from the internet)
# --------------------------------------------------

MAX_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60
_failures = {}  # ip -> (count, first_failure_time)


def client_ip():
    # Cloudflare sets this header at its edge; the app only listens on
    # localhost when tunnelled, so it cannot be set by a visitor directly.
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"


def locked_seconds(ip):
    entry = _failures.get(ip)
    if not entry:
        return 0
    count, first = entry
    if time.time() - first > LOCK_SECONDS:
        _failures.pop(ip, None)
        return 0
    if count >= MAX_ATTEMPTS:
        return int(LOCK_SECONDS - (time.time() - first))
    return 0


def record_failure(ip):
    count, first = _failures.get(ip, (0, time.time()))
    if time.time() - first > LOCK_SECONDS:
        count, first = 0, time.time()
    _failures[ip] = (count + 1, first)


# --------------------------------------------------
# UI
# --------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="robots" content="noindex, nofollow">
<title>{{ page_title }}</title>
<style>
:root {
    --bg: #f4f2ec;
    --surface: #fbfaf7;
    --line: #e3dfd5;
    --line-soft: #ebe8df;
    --ink: #2d2c28;
    --muted: #7d7970;
    --accent: #5b7565;
    --accent-ink: #fff;
    --accent-soft: #e5ebe6;
    --danger: #a4524a;
    --danger-soft: #f4e4e1;
    --shadow: 0 1px 2px rgba(60,55,40,.05), 0 6px 18px rgba(60,55,40,.05);
    --radius: 12px;
    --k-image-bg: #e8e3f0; --k-image-fg: #6d5f8c;
    --k-video-bg: #f1e2dd; --k-video-fg: #9a5d4e;
    --k-audio-bg: #dfeceb; --k-audio-fg: #4e7f7b;
    --k-document-bg: #dfe8f0; --k-document-fg: #4f6f8f;
    --k-archive-bg: #efe8d6; --k-archive-fg: #8c7440;
    --k-other-bg: #e9e7e0; --k-other-fg: #77746b;
    --k-folder-bg: #dfe9e1; --k-folder-fg: #5b7565;
}
@media (prefers-color-scheme: dark) {
    :root {
        --bg: #1a1b19; --surface: #232421; --line: #33352f; --line-soft: #2b2d28;
        --ink: #e7e5de; --muted: #8d9186;
        --accent: #8fae9a; --accent-ink: #172019; --accent-soft: #2a342d;
        --danger: #d98f86; --danger-soft: #3a2825;
        --shadow: 0 1px 2px rgba(0,0,0,.3), 0 6px 18px rgba(0,0,0,.2);
        --k-image-bg: #2f2b3b; --k-image-fg: #b3a6d4;
        --k-video-bg: #3a2c28; --k-video-fg: #d9a392;
        --k-audio-bg: #26363a; --k-audio-fg: #8fc3be;
        --k-document-bg: #27323d; --k-document-fg: #98b6d3;
        --k-archive-bg: #38332383; --k-archive-fg: #cfb877;
        --k-other-bg: #2d2e2a; --k-other-fg: #a09d92;
        --k-folder-bg: #28332c; --k-folder-fg: #8fae9a;
    }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
}
button, input, select { font: inherit; color: inherit; }
a { color: inherit; text-decoration: none; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.wrap { max-width: 1080px; margin: 0 auto; padding-left: 24px; padding-right: 24px; }

.bar { background: var(--surface); border-bottom: 1px solid var(--line); }
.bar .wrap { display: flex; align-items: center; justify-content: space-between; height: 62px; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 600; font-size: 16px; }
.mark {
    width: 30px; height: 30px; border-radius: 8px; display: grid; place-items: center;
    background: var(--accent-soft); color: var(--accent);
}
.linkbtn {
    background: none; border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 12px; cursor: pointer; color: var(--muted); font-size: 14px;
}
.linkbtn:hover { color: var(--ink); border-color: var(--muted); }

main.wrap { padding-top: 36px; padding-bottom: 110px; }
.head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
h1 { font-size: 26px; font-weight: 600; letter-spacing: -.02em; margin: 0; }
.count { color: var(--muted); font-size: 14px; margin-top: 2px; }
.search { position: relative; width: min(320px, 100%); }
.search svg { position: absolute; left: 12px; top: 11px; color: var(--muted); pointer-events: none; }
.search input {
    width: 100%; padding: 9px 14px 9px 38px; background: var(--surface);
    border: 1px solid var(--line); border-radius: 10px; outline: none;
}
.search input::placeholder { color: var(--muted); }
.search input:focus { border-color: var(--accent); }

.toolbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 14px; flex-wrap: wrap; margin: 24px 0 20px;
}
.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip {
    border: 1px solid var(--line); background: transparent; color: var(--muted);
    border-radius: 999px; padding: 5px 14px; font-size: 13px; cursor: pointer;
}
.chip b { font-weight: 500; opacity: .7; margin-left: 4px; }
.chip:hover { color: var(--ink); }
.chip.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.ctrls { display: flex; gap: 10px; align-items: center; }
select {
    background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
    padding: 6px 10px; font-size: 13px; color: var(--muted);
}
.seg { display: flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--surface); }
.seg button {
    background: none; border: 0; padding: 6px 10px; cursor: pointer; color: var(--muted);
    display: grid; place-items: center;
}
.seg button.on { background: var(--accent-soft); color: var(--accent); }

/* items */
.items { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 18px; }
.item {
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
    overflow: hidden; display: flex; flex-direction: column;
    transition: box-shadow .15s, border-color .15s, transform .15s;
}
.item:hover { box-shadow: var(--shadow); border-color: var(--muted); transform: translateY(-1px); }
.thumb {
    position: relative; aspect-ratio: 4 / 3; display: grid; place-items: center;
    background: var(--tb); color: var(--tf); overflow: hidden;
}
.thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.ico { display: grid; place-items: center; gap: 8px; }
.ext { font-size: 12px; font-weight: 700; letter-spacing: .08em; }
.k-image { --tb: var(--k-image-bg); --tf: var(--k-image-fg); }
.k-video { --tb: var(--k-video-bg); --tf: var(--k-video-fg); }
.k-audio { --tb: var(--k-audio-bg); --tf: var(--k-audio-fg); }
.k-document { --tb: var(--k-document-bg); --tf: var(--k-document-fg); }
.k-archive { --tb: var(--k-archive-bg); --tf: var(--k-archive-fg); }
.k-other { --tb: var(--k-other-bg); --tf: var(--k-other-fg); }
.body { padding: 12px 14px 14px; display: flex; flex-direction: column; gap: 10px; flex: 1; }
.name { font-weight: 500; line-height: 1.35; overflow-wrap: anywhere;
        display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.meta { color: var(--muted); font-size: 13px; margin-top: 2px; }
.acts { display: flex; gap: 8px; margin-top: auto; }
.btn {
    flex: 1; display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    padding: 7px 10px; border-radius: 8px; font-size: 13px; font-weight: 500;
    border: 1px solid var(--line); color: var(--muted);
}
.btn:hover { color: var(--ink); border-color: var(--muted); }
.btn.main { background: var(--accent-soft); border-color: transparent; color: var(--accent); }
.btn.main:hover { background: var(--accent); color: var(--accent-ink); }

/* list view */
.items.list { grid-template-columns: 1fr; gap: 0; background: var(--surface);
              border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
.list .item { flex-direction: row; align-items: center; border: 0; border-top: 1px solid var(--line-soft);
              border-radius: 0; padding: 10px 16px; gap: 14px; }
.list .item:first-child { border-top: 0; }
.list .item:hover { transform: none; box-shadow: none; background: var(--accent-soft); }
.list .thumb { width: 46px; height: 46px; aspect-ratio: auto; border-radius: 9px; flex: none; }
.list .ico svg { display: none; }
.list .body { flex-direction: row; align-items: center; padding: 0; gap: 14px; }
.list .txt { flex: 1; min-width: 0; }
.list .name { -webkit-line-clamp: 1; }
.list .acts { margin: 0; }
.list .btn { flex: none; }

.empty { padding: 70px 20px; text-align: center; color: var(--muted); grid-column: 1 / -1; }
.empty code { background: var(--accent-soft); padding: 2px 6px; border-radius: 5px; color: var(--ink); font-size: 13px; }
.none { display: none; }
.item { position: relative; }
.pick { position: absolute; top: 10px; left: 10px; z-index: 2; width: 26px; height: 26px;
        display: grid; place-items: center; border-radius: 7px; cursor: pointer;
        background: color-mix(in srgb, var(--surface) 88%, transparent);
        border: 1px solid var(--line); opacity: 0; transition: opacity .12s; }
.item:hover .pick, .pick:has(input:checked), .picking .pick, .pick:focus-within { opacity: 1; }
.pick input { appearance: none; -webkit-appearance: none; margin: 0; width: 16px; height: 16px;
              border: 1.5px solid var(--muted); border-radius: 4px; cursor: pointer; display: grid; place-items: center; }
.pick input:checked { background: var(--accent); border-color: var(--accent); }
.pick input:checked::after { content: ""; width: 4px; height: 8px; margin-top: -2px;
        border: solid var(--accent-ink); border-width: 0 2px 2px 0; transform: rotate(45deg); }
.item.sel { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.list .item.sel { box-shadow: none; background: var(--accent-soft); }
.list .item { padding-left: 54px; }
.list .pick { top: 50%; left: 16px; transform: translateY(-50%); opacity: 1; }
@media (hover: none) { .pick { opacity: 1; } }
.selbar { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); z-index: 10;
          display: flex; align-items: center; gap: 12px; padding: 10px 12px 10px 18px;
          background: var(--ink); color: var(--bg); border-radius: 14px; box-shadow: var(--shadow);
          max-width: calc(100vw - 24px); white-space: nowrap; }
.selbar button { background: none; border: 0; color: inherit; opacity: .75; cursor: pointer; font-size: 14px; padding: 4px 2px; }
.selbar button:hover { opacity: 1; }
.selbar .go { background: var(--accent); color: var(--accent-ink); opacity: 1; font-weight: 600;
              border-radius: 9px; padding: 8px 14px; }
.allbtn { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px;
          font-size: 13px; color: var(--muted); cursor: pointer; }
.allbtn:hover { color: var(--ink); border-color: var(--muted); }

.hbtns { display: flex; align-items: center; gap: 8px; }
.hbtns form { margin: 0; }
.linkbtn.icon { display: inline-grid; place-items: center; padding: 6px 8px; }
.linkbtn.on { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }

/* settings */
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
         padding: 20px 22px; margin-top: 22px; }
.panel h2 { margin: 0 0 12px; font-size: 17px; font-weight: 600; }
.muted { color: var(--muted); font-size: 14px; margin: 0 0 14px; }
.flash { padding: 10px 14px; border-radius: 8px; font-size: 14px; margin-top: 18px; }
.flash.ok { background: var(--accent-soft); color: var(--accent); }
.flash.err { background: var(--danger-soft); color: var(--danger); }
.mini { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 6px 12px;
        font-size: 13px; font-weight: 500; color: var(--muted); cursor: pointer; white-space: nowrap; }
.mini:hover { color: var(--ink); border-color: var(--muted); }
.mini.main { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.mini.main:hover { opacity: .9; color: var(--accent-ink); }
.mini.danger:hover { color: var(--danger); border-color: var(--danger); }
.field.sm { padding: 6px 10px; font-weight: 500; }
.frow { display: flex; align-items: center; justify-content: space-between; gap: 14px;
        padding: 12px 0; border-top: 1px solid var(--line-soft); }
.frow:first-of-type { border-top: 0; }
.finfo { min-width: 0; flex: 1; }
.rename { display: flex; gap: 8px; margin: 0; max-width: 420px; }
.rename .field { flex: 1; min-width: 0; }
.path { color: var(--muted); font-size: 13px; margin-top: 6px; overflow-wrap: anywhere; }
.bad { color: var(--danger); font-weight: 600; margin-left: 6px; }
.places { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.gobar { display: flex; gap: 8px; margin: 0 0 12px; }
.gobar .field { flex: 1; min-width: 0; }
.crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 6px; font-size: 14px; margin-bottom: 12px; color: var(--muted); }
.crumbs a { padding: 2px 4px; border-radius: 5px; }
.crumbs a:hover { background: var(--accent-soft); color: var(--accent); }
.note { background: var(--accent-soft); color: var(--ink); border-radius: 8px; padding: 9px 12px; font-size: 14px; margin-bottom: 12px; }
.here { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
        padding: 12px 14px; background: var(--bg); border: 1px solid var(--line); border-radius: 10px; margin-bottom: 12px; }
.here form, .drow form { margin: 0; }
.tag { font-size: 12px; font-weight: 600; color: var(--accent); background: var(--accent-soft); border-radius: 999px; padding: 3px 10px; }
.dirs { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; max-height: 420px; overflow-y: auto; }
.drow { display: flex; align-items: center; justify-content: space-between; gap: 12px;
        padding: 8px 14px; border-top: 1px solid var(--line-soft); }
.drow:first-child { border-top: 0; }
.drow:hover { background: var(--accent-soft); }
.dname { flex: 1; min-width: 0; display: flex; align-items: center; gap: 10px; overflow-wrap: anywhere; padding: 4px 0; }
.dname svg { flex: none; color: var(--accent); }
.drow.up { color: var(--muted); display: block; }
.pad { padding: 14px; margin: 0; }

.tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 26px; }
.tab { border: 1px solid var(--line); background: var(--surface); color: var(--muted);
       border-radius: 10px; padding: 7px 14px; font-size: 14px; font-weight: 500; }
.tab:hover { color: var(--ink); border-color: var(--muted); }
.tab.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.tab.off:not(.on) { opacity: .55; }
.notice { margin-bottom: 20px; }

/* folders, breadcrumbs, pager */
.k-folder { --tb: var(--k-folder-bg); --tf: var(--k-folder-fg); }
a.chip { display: inline-block; }
.trail { margin: 18px 0 0; align-items: center; }
.crumbs .here-now { color: var(--ink); font-weight: 500; padding: 2px 4px; }
.crumbs .clear { margin-left: auto; color: var(--accent); }
.where { text-decoration: underline; text-underline-offset: 2px; }
.where:hover { color: var(--accent); }
a.name:hover { color: var(--accent); }
.list .dir .ico svg { display: block; width: 24px; height: 24px; }
.pager { display: flex; align-items: center; justify-content: space-between; gap: 14px;
         flex-wrap: wrap; margin: 22px 0; color: var(--muted); font-size: 14px; }
.pnav { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.pnav a, .pnav span { min-width: 34px; height: 34px; display: grid; place-items: center;
                      border-radius: 8px; padding: 0 8px; }
.pnav a { border: 1px solid var(--line); background: var(--surface); }
.pnav a:hover { border-color: var(--muted); color: var(--ink); }
.pnav .cur { background: var(--accent); color: var(--accent-ink); font-weight: 600; }
.pnav .dis { opacity: .4; border: 1px solid var(--line-soft); }
.pnav .gap { min-width: 20px; padding: 0; }
.perform { margin: 0; }
.perform label { display: flex; align-items: center; gap: 8px; margin: 0; }
.selbar.none, .none { display: none !important; }

.foot { margin-top: 40px; color: var(--muted); font-size: 12px; text-align: center; opacity: .7; }

/* sign in */
.center { min-height: 100vh; display: grid; place-items: center; padding: 20px; }
.card { width: min(380px, 100%); background: var(--surface); border: 1px solid var(--line);
        border-radius: 16px; padding: 32px 28px; box-shadow: var(--shadow); }
.card .brand { margin-bottom: 24px; }
label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.field { width: 100%; padding: 10px 12px; background: var(--bg); border: 1px solid var(--line);
         border-radius: 8px; outline: none; }
.field:focus { border-color: var(--accent); }
.primary { width: 100%; margin-top: 16px; padding: 10px; background: var(--accent); color: var(--accent-ink);
           border: 0; border-radius: 8px; font-weight: 600; cursor: pointer; }
.primary:hover { opacity: .92; }
.error { background: var(--danger-soft); color: var(--danger); border-radius: 8px;
         padding: 10px 12px; font-size: 14px; margin-bottom: 16px; }

@media (max-width: 560px) {
    .wrap { padding-left: 16px; padding-right: 16px; }
    main.wrap { padding-top: 24px; }
    .items { grid-template-columns: repeat(2, 1fr); gap: 12px; }
    .body { padding: 10px 11px 12px; }
    .btn span { display: none; }
    .list .item { padding: 10px 12px; }
    .list .body { gap: 8px; }
    .list .btn.view { display: none; }
    .panel { padding: 16px 14px; }
    .frow { align-items: flex-start; flex-direction: column; }
    .rename { max-width: none; width: 100%; }
}
</style>
</head>
<body>

{% macro logo() %}
<span class="mark"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
 stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span>
{% endmacro %}

{% if not logged_in %}
<div class="center">
    <form class="card" method="post" action="{{ url_for('login') }}">
        <div class="brand">{{ logo() }}{{ site_name }}</div>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <label for="password">Password</label>
        <input class="field" id="password" type="password" name="password"
               autocomplete="current-password" required autofocus>
        <button class="primary" type="submit">Sign in</button>
    </form>
</div>

{% else %}
<header class="bar">
    <div class="wrap">
        <a class="brand" href="{{ url_for('index') }}">{{ logo() }}{{ site_name }}</a>
        <div class="hbtns">
            {% if is_owner %}
            <a class="linkbtn icon{{ ' on' if view == 'settings' }}" href="{{ url_for('settings') }}" title="Settings: choose shared folders" aria-label="Settings">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            </a>
            {% endif %}
            <form method="post" action="{{ url_for('logout') }}">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <button class="linkbtn" type="submit">Sign out</button>
            </form>
        </div>
    </div>
</header>

{% if view == 'settings' %}
<main class="wrap">
    <div class="head">
        <div>
            <h1>Settings</h1>
            <div class="count">Choose which folders on this PC are shared</div>
        </div>
        <a class="allbtn" href="{{ url_for('index') }}">&larr; Back to files</a>
    </div>

    {% if flash %}<div class="flash {{ flash.kind }}">{{ flash.text }}</div>{% endif %}
    {% if config_error %}<div class="flash err">folders.json has a problem, so the last working list is shown. Fix the file, or remove it to start fresh.</div>{% endif %}

    <section class="panel">
        <h2>Shared folders</h2>
        {% if not folders %}<p class="muted" style="margin:0">Nothing is shared yet. Add your first folder below.</p>{% endif %}
        {% for fo in folders %}
        <div class="frow">
            <div class="finfo">
                <form class="rename" method="post" action="{{ url_for('settings_rename') }}">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="hidden" name="id" value="{{ fo.id }}">
                    <input type="hidden" name="back" value="{{ b.path }}">
                    <input class="field sm" name="name" value="{{ fo.name }}" maxlength="60" aria-label="Name of {{ fo.name }}">
                    <button class="mini" type="submit">Save name</button>
                </form>
                <div class="path">{{ fo.path }}{% if not fo.ok %}<span class="bad">not found</span>{% endif %}</div>
            </div>
            <form method="post" action="{{ url_for('settings_remove') }}" onsubmit="return confirm('Stop sharing this folder? Your files are not deleted.')">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <input type="hidden" name="id" value="{{ fo.id }}">
                <input type="hidden" name="back" value="{{ b.path }}">
                <button class="mini danger" type="submit">Remove</button>
            </form>
        </div>
        {% endfor %}
    </section>

    <section class="panel">
        <h2>Add a folder</h2>
        <p class="muted">Open folders below and press <b>Add</b>. Add as many as you like, one after another;
            they are remembered. Anyone with your link and password can browse and download everything inside a shared folder, including its subfolders.</p>

        <div class="places">
            {% for pl in b.places %}<a class="chip" href="{{ url_for('settings', b=pl.path) }}">{{ pl.label }}</a>{% endfor %}
        </div>

        <form class="gobar" method="get" action="{{ url_for('settings') }}">
            <input class="field" name="b" value="{{ b.path }}" aria-label="Folder path" spellcheck="false" autocomplete="off"
                   placeholder="Type or paste a folder path">
            <button class="mini" type="submit">Go</button>
        </form>

        <nav class="crumbs" aria-label="Current location">
            {% for c in b.crumbs %}<a href="{{ url_for('settings', b=c.path) }}">{{ c.label }}</a>{% if not loop.last %}<span>&rsaquo;</span>{% endif %}{% endfor %}
        </nav>

        {% if b.error %}<div class="note">{{ b.error }}</div>{% endif %}

        <div class="here">
            <div><b>This folder</b> &middot; {{ b.files }} file{{ '' if b.files == 1 else 's' }}</div>
            {% if b.shared %}
            <span class="tag">Already shared</span>
            {% else %}
            <form method="post" action="{{ url_for('settings_add') }}">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <input type="hidden" name="path" value="{{ b.path }}">
                <input type="hidden" name="back" value="{{ b.path }}">
                <button class="mini main" type="submit">Add this folder</button>
            </form>
            {% endif %}
        </div>

        <div class="dirs">
            {% if b.up %}<a class="drow up" href="{{ url_for('settings', b=b.up) }}">&uarr; Up one level</a>{% endif %}
            {% for d in b.dirs %}
            <div class="drow">
                <a class="dname" href="{{ url_for('settings', b=d.path) }}">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
                    <span>{{ d.name }}</span>
                </a>
                {% if d.shared %}
                <span class="tag">Shared</span>
                {% else %}
                <form method="post" action="{{ url_for('settings_add') }}">
                    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                    <input type="hidden" name="path" value="{{ d.path }}">
                    <input type="hidden" name="back" value="{{ b.path }}">
                    <button class="mini" type="submit" aria-label="Add {{ d.name }}">Add</button>
                </form>
                {% endif %}
            </div>
            {% endfor %}
            {% if not b.dirs %}<p class="muted pad">No subfolders here.</p>{% endif %}
            {% if b.more %}<p class="muted pad">&hellip;and {{ b.more }} more. Type a path above to jump straight to one.</p>{% endif %}
        </div>
    </section>
    <footer class="foot">v{{ version }}</footer>
</main>
{% else %}
{% macro pager(b, nav, hidden, per, per_choices) %}
{% if b.total > per_choices[0] or b.pages > 1 %}
<nav class="pager" aria-label="Pages">
    <div class="pinfo">{{ b.first }}&ndash;{{ b.last }} of {{ b.total }}</div>
    {% if b.pages > 1 %}
    <div class="pnav">
        {% if b.page > 1 %}<a href="{{ nav(page=b.page - 1) }}" rel="prev" aria-label="Previous page">&lsaquo;</a>{% else %}<span class="dis">&lsaquo;</span>{% endif %}
        {% for n in b.page_links %}
            {% if n is none %}<span class="gap">&hellip;</span>
            {% elif n == b.page %}<span class="cur" aria-current="page">{{ n }}</span>
            {% else %}<a href="{{ nav(page=n) }}">{{ n }}</a>{% endif %}
        {% endfor %}
        {% if b.page < b.pages %}<a href="{{ nav(page=b.page + 1) }}" rel="next" aria-label="Next page">&rsaquo;</a>{% else %}<span class="dis">&rsaquo;</span>{% endif %}
    </div>
    {% endif %}
    <form class="perform" method="get" action="{{ url_for('index') }}">
        {{ hidden('per') }}
        <label>Per page
            <select name="per" data-auto>
                {% for n in per_choices %}<option value="{{ n }}"{{ ' selected' if n == per }}>{{ n }}</option>{% endfor %}
            </select>
        </label>
    </form>
</nav>
{% endif %}
{% endmacro %}
<main class="wrap">
    {% if config_error and is_owner %}
    <div class="error notice">The folder list (folders.json) could not be read, so the last working list is shown.</div>
    {% endif %}
    {% if folders|length > 1 %}
    <nav class="tabs" aria-label="Folders">
        {% for fo in folders %}
        <a class="tab{{ ' on' if fo.id == current.id }}{{ ' off' if not fo.ok }}" href="{{ url_for('index', f=fo.id) }}"
           {% if fo.id == current.id %}aria-current="page"{% endif %}>{{ fo.name }}</a>
        {% endfor %}
    </nav>
    {% endif %}
    {% set title = b.crumbs[-1].label if (b and b.crumbs) else (current.name if current else 'Files') %}
    <div class="head">
        <div>
            <h1>{{ title }}</h1>
            {% if b and current.ok %}
            <div class="count">
                {% if query %}{{ b.total_all }} result{{ '' if b.total_all == 1 else 's' }} for &ldquo;{{ query }}&rdquo;
                {% else %}{{ b.n_dirs }} folder{{ '' if b.n_dirs == 1 else 's' }} &middot; {{ b.n_files }} file{{ '' if b.n_files == 1 else 's' }}{% if b.total_size %} &middot; {{ b.total_size }}{% endif %}{% endif %}
            </div>
            {% endif %}
        </div>
        {% if b and current.ok %}
        <form class="search" method="get" action="{{ url_for('index') }}" role="search">
            {{ hidden('q', 't') }}
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m16 16 4 4"/></svg>
            <input id="search" name="q" value="{{ query }}" type="search" placeholder="Search in {{ title }}" aria-label="Search in {{ title }}" autocomplete="off">
        </form>
        {% endif %}
    </div>

    {% if b and current.ok %}
    <nav class="crumbs trail" aria-label="Folder path">
        {% for c in b.crumbs %}
            {% if not loop.last or query %}<a href="{{ nav(p=c.rel, page=None, q=None, t=None) }}">{{ c.label }}</a>{% else %}<span class="here-now">{{ c.label }}</span>{% endif %}
            {% if not loop.last %}<span>&rsaquo;</span>{% endif %}
        {% endfor %}
        {% if query %}<span>&rsaquo;</span><span class="here-now">Search</span>
        <a class="clear" href="{{ nav(q=None, page=None, t=None) }}">Clear search</a>{% endif %}
    </nav>
    {% if b.error %}<div class="note">{{ b.error }}</div>{% endif %}
    {% if b.stopped %}<div class="note">This search stopped early to keep the PC responsive. Open a smaller folder, or add more of the name, to narrow it down.</div>{% endif %}
    {% endif %}

    {% if b and current.ok and (b.total_all or query) %}
    <div class="toolbar">
        <div class="chips" id="chips">
            <a class="chip{{ ' on' if not kind }}" href="{{ nav(t=None, page=None) }}">All<b>{{ b.total_all }}</b></a>
            {% if b.n_dirs %}<a class="chip{{ ' on' if kind == 'folder' }}" href="{{ nav(t='folder', page=None) }}">Folders<b>{{ b.n_dirs }}</b></a>{% endif %}
            {% for k, label, n in b.kinds %}
            <a class="chip{{ ' on' if kind == k }}" href="{{ nav(t=k, page=None) }}">{{ label }}<b>{{ n }}</b></a>
            {% endfor %}
        </div>
        <div class="ctrls">
            {% if b.n_files and not query %}
            <form method="post" action="{{ url_for('zip_files') }}" style="margin:0">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <input type="hidden" name="f" value="{{ current.id }}">
                <input type="hidden" name="d" value="{{ b.rel }}">
                <button class="allbtn" type="submit" title="Download the files in this folder as one zip (not the subfolders)">Download all</button>
            </form>
            {% endif %}
            <form method="get" action="{{ url_for('index') }}" style="margin:0">
                {{ hidden('sort') }}
                <select name="sort" data-auto aria-label="Sort">
                    {% for value, label in sort_choices %}<option value="{{ value }}"{{ ' selected' if value == sort }}>{{ label }}</option>{% endfor %}
                </select>
            </form>
            <div class="seg">
                <button id="v-grid" class="on" aria-label="Grid view" title="Grid">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
                </button>
                <button id="v-list" aria-label="List view" title="List">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
                </button>
            </div>
        </div>
    </div>

    {{ pager(b, nav, hidden, per, per_choices) if b.pages > 1 }}

    {% if b.rows %}
    <div class="items" id="items">
    {% for it in b.rows %}
        {% if it.dir %}
        <article class="item dir" data-kind="folder">
            <label class="pick" title="Select"><input type="checkbox" name="name" value="{{ it.rel }}" form="zipform" aria-label="Select {{ it.name }}"></label>
            <a class="thumb k-folder" href="{{ nav(p=it.rel, page=None, q=None, t=None) }}" tabindex="-1" aria-hidden="true">
                <span class="ico"><svg width="34" height="34" viewBox="0 0 24 24" fill="currentColor" fill-opacity=".18" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span>
            </a>
            <div class="body">
                <div class="txt">
                    <a class="name" href="{{ nav(p=it.rel, page=None, q=None, t=None) }}" title="{{ it.name }}">{{ it.name }}</a>
                    <div class="meta">Folder{% if it.date %} &middot; {{ it.date }}{% endif %}{% if query %} &middot; <a class="where" href="{{ nav(p=it.parent, page=None, q=None, t=None) }}">in {{ it.parent or current.name }}</a>{% endif %}</div>
                </div>
                <div class="acts">
                    <a class="btn view" href="{{ nav(p=it.rel, page=None, q=None, t=None) }}">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span>Open</span></a>
                    <button type="button" class="btn main zipdir" data-rel="{{ it.rel }}" aria-label="Download {{ it.name }} as a zip" title="Download this folder and everything in it as a zip">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/></svg><span>Zip</span></button>
                </div>
            </div>
        </article>
        {% else %}
        <article class="item" data-kind="{{ it.kind }}">
            <label class="pick" title="Select"><input type="checkbox" name="name" value="{{ it.rel }}" form="zipform" aria-label="Select {{ it.name }}"></label>
            <a class="thumb k-{{ it.kind }}" href="{{ url_for('view_file', fid=current.id, filename=it.rel) if it.viewable else url_for('download_file', fid=current.id, filename=it.rel) }}"
               {% if it.viewable %}target="_blank" rel="noopener"{% endif %} tabindex="-1" aria-hidden="true">
                {% if it.kind == 'image' and it.viewable %}
                    <img src="{{ url_for('thumb', fid=current.id, filename=it.rel) }}" alt="" loading="lazy" decoding="async">
                {% endif %}
                <span class="ico">
                    {% if it.kind == 'image' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-9 9"/></svg>
                    {% elif it.kind == 'video' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/></svg>
                    {% elif it.kind == 'audio' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>
                    {% elif it.kind == 'archive' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/></svg>
                    {% else %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>
                    {% endif %}
                    <span class="ext">{{ it.ext }}</span>
                </span>
            </a>
            <div class="body">
                <div class="txt">
                    <div class="name" title="{{ it.name }}">{{ it.name }}</div>
                    <div class="meta">{{ it.size }} &middot; {{ it.date }}{% if query %} &middot; <a class="where" href="{{ nav(p=it.parent, page=None, q=None, t=None) }}">in {{ it.parent or current.name }}</a>{% endif %}</div>
                </div>
                <div class="acts">
                    {% if it.viewable %}
                    <a class="btn view" href="{{ url_for('view_file', fid=current.id, filename=it.rel) }}" target="_blank" rel="noopener">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg><span>Open</span></a>
                    {% endif %}
                    <a class="btn main" href="{{ url_for('download_file', fid=current.id, filename=it.rel) }}" aria-label="Download {{ it.name }}">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/></svg><span>Download</span></a>
                </div>
            </div>
        </article>
        {% endif %}
    {% endfor %}
    </div>
    {{ pager(b, nav, hidden, per, per_choices) }}
    {% else %}
    <div class="items" style="margin-top:26px">
        <div class="empty">
            {% if query %}Nothing named &ldquo;{{ query }}&rdquo; in this folder or the folders inside it.
            {% elif kind %}Nothing of this type here. <a class="where" href="{{ nav(t=None, page=None) }}">Show everything</a>
            {% else %}This folder is empty.{% endif %}
        </div>
    </div>
    {% endif %}
    {% else %}
    <div class="items" style="margin-top:26px">
        <div class="empty">
            {% if not current %}
                {% if is_owner %}No folders are shared yet.<br><br><a class="allbtn" href="{{ url_for('settings') }}">Choose folders</a>
                {% else %}Nothing is shared yet.{% endif %}
            {% elif not current.ok %}
                {% if is_owner %}This folder was not found.<br><br><a class="allbtn" href="{{ url_for('settings') }}">Open settings</a>
                {% else %}This folder isn't available right now.{% endif %}
            {% else %}This folder is empty.
            {% endif %}
        </div>
    </div>
    {% endif %}
    {% if current and b %}
    <form id="zipform" method="post" action="{{ url_for('zip_files') }}" class="selbar none">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="f" value="{{ current.id }}">
        <input type="hidden" name="d" value="{{ b.rel }}">
        <span id="selcount"></span>
        <button type="button" id="selall">Select page</button>
        <button type="button" id="selclear">Clear</button>
        <button type="submit" class="go">Download zip</button>
    </form>
    <form id="zip1" method="post" action="{{ url_for('zip_files') }}" class="none">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="f" value="{{ current.id }}">
        <input type="hidden" name="d" value="{{ b.rel }}">
        <input type="hidden" name="name" value="">
    </form>
    {% endif %}
    <footer class="foot">v{{ version }}</footer>
</main>

<script>
(function () {
    document.querySelectorAll("select[data-auto]").forEach(s =>
        s.addEventListener("change", () => s.form.submit()));

    const search = document.getElementById("search");
    const box = document.getElementById("items");
    const bar = document.getElementById("zipform");
    const boxes = () => box ? [...box.querySelectorAll(".pick input")] : [];

    function picked() {
        const on = boxes().filter(b => b.checked);
        boxes().forEach(b => b.closest(".item").classList.toggle("sel", b.checked));
        if (bar) bar.classList.toggle("none", on.length === 0);
        if (box) box.classList.toggle("picking", on.length > 0);
        const label = document.getElementById("selcount");
        if (label) label.textContent = on.length + " selected";
    }
    function clearPicks() { boxes().forEach(b => b.checked = false); picked(); }

    document.addEventListener("keydown", e => {
        if (e.key === "/" && search && document.activeElement !== search &&
                !/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) {
            e.preventDefault(); search.focus();
        } else if (e.key === "Escape") {
            if (search && document.activeElement === search) search.blur();
            else clearPicks();
        }
    });

    // one folder: download it as a zip
    const zip1 = document.getElementById("zip1");
    document.addEventListener("click", e => {
        const b = e.target.closest(".zipdir");
        if (!b || !zip1) return;
        zip1.querySelector('input[name="name"]').value = b.dataset.rel;
        zip1.submit();
    });

    if (!box) return;
    box.addEventListener("change", e => { if (e.target.matches(".pick input")) picked(); });
    const selclear = document.getElementById("selclear");
    const selall = document.getElementById("selall");
    if (selclear) selclear.onclick = clearPicks;
    if (selall) selall.onclick = () => { boxes().forEach(b => b.checked = true); picked(); };

    function view(v) {
        box.classList.toggle("list", v === "list");
        const l = document.getElementById("v-list"), g = document.getElementById("v-grid");
        if (l) l.classList.toggle("on", v === "list");
        if (g) g.classList.toggle("on", v !== "list");
        try { localStorage.setItem("view", v); } catch (e) {}
    }
    const g = document.getElementById("v-grid"), l = document.getElementById("v-list");
    if (g) g.onclick = () => view("grid");
    if (l) l.onclick = () => view("list");

    let saved = "grid";
    try { saved = localStorage.getItem("view") || "grid"; } catch (e) {}
    view(saved);
})();
</script>
{% endif %}
{% endif %}
</body>
</html>
"""


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


KINDS = {
    "image": ("Images", {"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "heic", "avif"}),
    "video": ("Videos", {"mp4", "mkv", "mov", "avi", "webm", "m4v"}),
    "audio": ("Audio", {"mp3", "wav", "flac", "aac", "ogg", "m4a"}),
    "document": ("Documents", {"pdf", "doc", "docx", "txt", "rtf", "xls", "xlsx",
                               "ppt", "pptx", "csv", "md"}),
    "archive": ("Archives", {"zip", "rar", "7z", "tar", "gz", "bz2"}),
}

# Types safe to show inside the browser (no HTML/SVG, which could run script).
VIEWABLE = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "avif": "image/avif",
    "pdf": "application/pdf", "mp4": "video/mp4", "webm": "video/webm",
    "m4v": "video/mp4", "mp3": "audio/mpeg", "wav": "audio/wav",
    "ogg": "audio/ogg", "m4a": "audio/mp4", "txt": "text/plain; charset=utf-8",
}


def kind_of(ext):
    for kind, (_, exts) in KINDS.items():
        if ext in exts:
            return kind
    return "other"


JUNK_NAMES = {"thumbs.db", "desktop.ini", "ehthumbs.db"}
JUNK_SUFFIXES = (".tmp", ".part", ".crdownload", ".download", ".swp", "~")


def is_hidden(name):
    low = name.lower()
    return low.startswith(".") or low in JUNK_NAMES or low.endswith(JUNK_SUFFIXES)


_BASE_DIR_KEY = os.path.normcase(str(BASE_DIR))  # the app's own folder is never shown or served
_SYSTEM_DIR_NAMES = {"lost+found", "system volume information"}
_WIN_BAD_CHARS = set('\\:*?"<>|')
_REPARSE = 0x400  # Windows junction / mount point / cloud placeholder
_HIDDEN_SYSTEM = 0x6  # Windows HIDDEN | SYSTEM attributes

PER_PAGE_CHOICES = (24, 48, 96, 192)
DEFAULT_PER_PAGE = 48
SORT_CHOICES = (
    ("name", "Name A-Z"), ("name_desc", "Name Z-A"),
    ("new", "Newest"), ("old", "Oldest"),
    ("big", "Largest"), ("small", "Smallest"),
)
STAT_ALL_LIMIT = 2000  # folders up to this size are fully measured (total size, date sorting)

SEARCH_MAX_SECONDS = 4.0
SEARCH_MAX_VISITED = 200_000
SEARCH_MAX_RESULTS = 5_000
MAX_ZIP_FILES = 50_000


def _is_system_name(name):
    return name.startswith("$") or name.lower() in _SYSTEM_DIR_NAMES


def _bad_part(part):
    """True if one piece of a requested path can never be a valid visible name."""
    if part in ("", ".", "..") or "\x00" in part or is_hidden(part):
        return True
    return os.name == "nt" and any(c in _WIN_BAD_CHARS for c in part)


def split_rel(rel):
    """'a/b/c' -> ['a', 'b', 'c'];  '' -> [];  anything suspicious -> None."""
    rel = (rel or "").strip("/")
    if not rel:
        return []
    parts = rel.split("/")
    return None if any(_bad_part(p) for p in parts) else parts


def _inside(path, root_real):
    try:
        Path(path).resolve().relative_to(root_real)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def resolve_inside(root, rel, kind="any"):
    """Path of a visible file/folder at `rel` inside the shared folder `root`, or None.

    kind is "file", "dir" or "any". No part of the way there may be a hidden name or a
    symlink, and the final location must still be inside `root` once everything is resolved.
    """
    parts = split_rel(rel)
    if parts is None:
        return None
    path = root
    try:
        root_real = root.resolve()
        for i, part in enumerate(parts):
            path = path / part
            if path.is_symlink():
                return None
            if _is_system_name(part) and (i < len(parts) - 1 or path.is_dir()):
                return None
        if kind == "file" and not path.is_file():
            return None
        if kind == "dir" and not path.is_dir():
            return None
        if kind == "any" and not (path.is_file() or path.is_dir()):
            return None
        real = path.resolve()
        real.relative_to(root_real)
        if real == BASE_DIR or BASE_DIR in real.parents:
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path


def safe_path(root, filename):
    """Path of a visible regular file inside the folder `root` (subfolders allowed), or None."""
    return resolve_inside(root, filename, "file")


def _entry_info(entry, root_real):
    """Light description of one directory entry, or None if it must not be shown."""
    name = entry.name
    if is_hidden(name):
        return None
    try:
        if entry.is_symlink():
            return None
        is_dir = entry.is_dir(follow_symlinks=False)
        if not is_dir and not entry.is_file(follow_symlinks=False):
            return None
    except OSError:
        return None
    if is_dir and (_is_system_name(name) or os.path.normcase(entry.path) == _BASE_DIR_KEY):
        return None

    st = None
    if os.name == "nt":  # free on Windows: the attributes come with the directory scan
        try:
            st = entry.stat()
            attrs = st.st_file_attributes
        except (OSError, AttributeError):
            return None
        if attrs & _HIDDEN_SYSTEM:
            return None
        if is_dir and attrs & _REPARSE and not _inside(entry.path, root_real):
            return None  # junction pointing outside the shared folder

    ext = "" if is_dir else os.path.splitext(name)[1].lower().lstrip(".")
    return {
        "name": name, "path": entry.path, "dir": is_dir, "st": st,
        "ext": ext[:4].upper() or ("" if is_dir else "FILE"),
        "kind": "folder" if is_dir else kind_of(ext),
        "viewable": (not is_dir) and ext in VIEWABLE,
        "filled": False,
    }


def _fill(item):
    """Add size and date (one stat call) to an item."""
    if item["filled"]:
        return item
    try:
        st = item["st"] or os.stat(item["path"])
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0
    if item["dir"]:
        size = 0
    item.update(
        filled=True, bytes=size, mtime=int(mtime),
        size="" if item["dir"] else human_size(size),
        date=datetime.fromtimestamp(mtime).strftime("%d %b %Y") if mtime else "",
    )
    return item


def scan_dir(root_real, directory, rel):
    items = []
    with os.scandir(directory) as it:
        for entry in it:
            info = _entry_info(entry, root_real)
            if info:
                info["rel"] = f"{rel}/{info['name']}" if rel else info["name"]
                info["parent"] = rel
                items.append(info)
    return items


def search_tree(root_real, directory, rel, query):
    """Find names containing `query` in `directory` and everything below it.

    Bounded by time, entries visited and results, so a huge drive can't hang the server.
    Returns (items, stopped_early).
    """
    query = query.lower()
    found, visited = [], 0
    deadline = time.monotonic() + SEARCH_MAX_SECONDS
    queue = deque([(directory, rel)])
    while queue:
        current, current_rel = queue.popleft()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    visited += 1
                    if visited > SEARCH_MAX_VISITED or (
                            visited % 256 == 0 and time.monotonic() > deadline):
                        return found, True
                    info = _entry_info(entry, root_real)
                    if not info:
                        continue
                    info["rel"] = f"{current_rel}/{info['name']}" if current_rel else info["name"]
                    info["parent"] = current_rel
                    if info["dir"]:
                        queue.append((info["path"], info["rel"]))
                    if query in info["name"].lower():
                        found.append(info)
                        if len(found) >= SEARCH_MAX_RESULTS:
                            return found, True
        except OSError:
            continue
    return found, False


def kind_counts(items):
    """[(kind, label, count)] for the filter chips (folders are handled separately)."""
    out = []
    for kind, (label, _) in KINDS.items():
        n = sum(1 for f in items if f["kind"] == kind)
        if n:
            out.append((kind, label, n))
    n = sum(1 for f in items if f["kind"] == "other")
    if n:
        out.append(("other", "Other", n))
    return out


def _sorted(items, sort):
    dirs = [i for i in items if i["dir"]]
    files = [i for i in items if not i["dir"]]
    by_name = lambda i: i["name"].lower()
    if sort == "name_desc":
        dirs.sort(key=by_name, reverse=True)
        files.sort(key=by_name, reverse=True)
    elif sort in ("new", "old"):
        for group in (dirs, files):
            group.sort(key=by_name)
            group.sort(key=lambda i: i["mtime"], reverse=(sort == "new"))
    elif sort in ("big", "small"):
        dirs.sort(key=by_name)
        files.sort(key=by_name)
        files.sort(key=lambda i: i["bytes"], reverse=(sort == "big"))
    else:
        dirs.sort(key=by_name)
        files.sort(key=by_name)
    return dirs + files


def _page_links(page, pages):
    """[1, None, 4, 5, 6, None, 20] where None is an ellipsis."""
    wanted = {1, pages} | set(range(max(1, page - 2), min(pages, page + 2) + 1))
    out, last = [], 0
    for n in sorted(wanted):
        if n - last > 1:
            out.append(None)
        out.append(n)
        last = n
    return out


def browse_folder(folder, rel, query, kind, sort, page, per):
    """Everything the page needs to show one directory (or search results) of a shared folder."""
    root = folder["path"]
    result = {
        "rel": "", "rows": [], "total": 0, "total_all": 0, "n_dirs": 0, "n_files": 0,
        "total_size": None, "kinds": [], "page": 1, "pages": 1, "first": 0, "last": 0,
        "page_links": [], "crumbs": [], "up": None, "error": None, "stopped": False,
    }
    if not folder["ok"]:
        return result

    parts = split_rel(rel) or []
    directory = None
    while True:  # fall back to the closest folder that still exists
        directory = resolve_inside(root, "/".join(parts), "dir") if parts else root
        if directory is not None:
            break
        parts.pop()
    if "/".join(parts) != (rel or "").strip("/"):
        result["error"] = "That folder was not found, so the closest one is shown."
    rel = "/".join(parts)
    result["rel"] = rel
    result["up"] = "/".join(parts[:-1]) if parts else None

    crumbs = [{"label": folder["name"], "rel": ""}]
    for i, part in enumerate(parts):
        crumbs.append({"label": part, "rel": "/".join(parts[:i + 1])})
    result["crumbs"] = crumbs

    root_real = root.resolve()
    try:
        if query:
            items, result["stopped"] = search_tree(root_real, str(directory), rel, query)
        else:
            items = scan_dir(root_real, str(directory), rel)
    except OSError:
        items = []
        result["error"] = "This folder can't be opened (no permission)."

    result["total_all"] = len(items)
    result["n_dirs"] = sum(1 for i in items if i["dir"])
    result["n_files"] = len(items) - result["n_dirs"]
    result["kinds"] = kind_counts(items)

    if sort not in dict(SORT_CHOICES):
        sort = "name"
    if sort in ("new", "old", "big", "small") or len(items) <= STAT_ALL_LIMIT:
        for item in items:
            _fill(item)
        result["total_size"] = human_size(sum(i["bytes"] for i in items if not i["dir"]))

    if kind == "folder":
        items = [i for i in items if i["dir"]]
    elif kind in KINDS or kind == "other":
        items = [i for i in items if i["kind"] == kind]
    items = _sorted(items, sort)

    total = len(items)
    pages = max(1, -(-total // per))
    page = min(max(page, 1), pages)
    shown = items[(page - 1) * per: page * per]
    for item in shown:
        _fill(item)

    result.update(
        rows=shown, total=total, page=page, pages=pages,
        first=(page - 1) * per + 1 if total else 0,
        last=min(page * per, total), page_links=_page_links(page, pages),
    )
    return result


# ---- owner-only settings -------------------------------------------------------------
#
# The public link is protected by one shared password, so a visitor must never be able to
# browse this PC or change what is shared. Settings therefore only work when the page is
# opened directly on this PC (http://127.0.0.1:PORT). Anything that came through the
# Cloudflare tunnel carries proxy headers and a public Host name, and is refused.

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_PROXY_HEADERS = {"forwarded", "x-real-ip", "true-client-ip", "cdn-loop", "via"}


def is_owner_request():
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return False
    host = request.host.lower()
    host = host[:host.find("]") + 1] if host.startswith("[") else host.split(":")[0]
    if host not in _LOCAL_HOSTS:  # also blocks DNS-rebinding from a malicious web page
        return False
    for header in request.headers.keys():
        h = header.lower()
        if h.startswith(("cf-", "x-forwarded")) or h in _PROXY_HEADERS:
            return False
    return True


def owner_gate():
    """None when the request may use settings, otherwise the response to send instead."""
    if not is_owner_request():
        abort(404)  # do not even reveal that the page exists
    if not session.get("authenticated"):
        return redirect(url_for("index"))
    return None


def flash(kind, text):
    session["flash"] = {"kind": kind, "text": text}


def _is_hidden_dir(entry):
    if is_hidden(entry.name) or entry.name.startswith("$"):
        return True
    if os.name == "nt":  # Windows hidden/system folders (Recycle Bin, System Volume Information)
        try:
            return bool(entry.stat().st_file_attributes & 0x6)  # HIDDEN | SYSTEM
        except (OSError, AttributeError):
            return False
    return False


def browse_places():
    """Quick-jump locations for the folder browser, different on each OS."""
    places = []
    try:
        home = Path.home()
        places.append(("Home", home))
        for n in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Movies", "Music"):
            if (home / n).is_dir():
                places.append((n, home / n))
    except (OSError, RuntimeError):
        pass

    if os.name == "nt":
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()  # fast; no waiting on empty drives
            for i, letter in enumerate(string.ascii_uppercase):
                if mask & (1 << i):
                    places.append((f"{letter}:", Path(f"{letter}:/")))
        except Exception:
            pass
    else:
        places.append(("Computer", Path("/")))
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        for base in ("/Volumes", "/mnt", f"/media/{user}", f"/run/media/{user}"):
            try:
                for child in sorted(Path(base).iterdir(), key=lambda c: c.name.lower())[:8]:
                    if not child.name.startswith(".") and child.is_dir():
                        places.append((child.name, child))
            except OSError:
                continue
    return places[:24]


MAX_BROWSE_ROWS = 500


def browse(arg, shared_ids):
    """What the folder browser shows for the location `arg` (a path typed or clicked)."""
    error = None
    raw = (arg or "").strip()
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = Path(BASE_DIR.anchor)
    target = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))) if raw else home
    if raw and not Path(os.path.expandvars(os.path.expanduser(raw))).is_absolute():
        error = "Type a full path, for example D:/Movies or /home/me/Photos."
        target = home

    try:
        while not target.is_dir() and target.parent != target:
            error = error or "That folder was not found, so the closest one is shown."
            target = target.parent
    except OSError:
        target = home

    dirs, files = [], 0
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        if not _is_hidden_dir(entry):
                            dirs.append(entry.name)
                    elif entry.is_file() and not is_hidden(entry.name):
                        files += 1
                except OSError:
                    continue
    except OSError:
        error = "This folder can't be opened (no permission)."
    dirs.sort(key=str.lower)
    more = max(0, len(dirs) - MAX_BROWSE_ROWS)

    rows = []
    for name in dirs[:MAX_BROWSE_ROWS]:
        child = target / name
        rows.append({"name": name, "path": str(child),
                     "shared": _folder_id(Path(os.path.realpath(child))) in shared_ids})

    crumbs, acc = [], None
    for part in target.parts:
        acc = Path(part) if acc is None else acc / part
        crumbs.append({"label": part.rstrip("\\/") or "/", "path": str(acc)})

    return {
        "path": str(target),
        "shared": _folder_id(Path(os.path.realpath(target))) in shared_ids,
        "files": files,
        "dirs": rows,
        "more": more,
        "crumbs": crumbs,
        "up": str(target.parent) if target.parent != target else None,
        "error": error,
        "places": [{"label": label, "path": str(p)} for label, p in browse_places()],
    }


def ensure_csrf():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def check_csrf():
    supplied = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(supplied, expected):
        abort(400, "Invalid form token. Refresh the page and try again.")


def render_login(error=None, status=200):
    return render_template_string(
        PAGE,
        page_title="Sign in",
        site_name=APP_NAME,
        logged_in=False,
        error=error,
        csrf_token=ensure_csrf(),
    ), status


# --------------------------------------------------
# ROUTES
# --------------------------------------------------

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.endpoint in ("view_file", "thumb"):
        response.headers["Cache-Control"] = "private, max-age=3600"
    else:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if request.endpoint not in ("view_file", "thumb"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
    return response


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@app.get("/")
def index():
    if not session.get("authenticated"):
        return render_login()

    folders, config_error = get_folders()
    current = next((f for f in folders if f["id"] == request.args.get("f")), None)
    if current is None and folders:
        current = folders[0]

    per = _int_arg("per", DEFAULT_PER_PAGE)
    if per not in PER_PAGE_CHOICES:
        per = DEFAULT_PER_PAGE
    sort = request.args.get("sort", "name")
    if sort not in dict(SORT_CHOICES):
        sort = "name"
    query = re.sub(r"\s+", " ", request.args.get("q", "")).strip()[:100]
    kind = request.args.get("t", "")

    view = browse_folder(current, request.args.get("p", ""), query, kind, sort,
                         _int_arg("page", 1), per) if current else None

    state = {
        "f": current["id"] if current else None,
        "p": view["rel"] if view else "",
        "q": query, "t": kind, "sort": sort, "per": per,
        "page": view["page"] if view else 1,
    }
    defaults = {"p": "", "q": "", "t": "", "sort": "name", "per": DEFAULT_PER_PAGE, "page": 1}

    def nav(**over):
        """URL of this page with some settings changed. nav(page=None) drops a setting."""
        merged = {**state, **over}
        args = {k: v for k, v in merged.items()
                if v is not None and (k == "f" or v != defaults.get(k))}
        return url_for("index", **args)

    def hidden(*drop):
        """Hidden form fields that carry the current settings (except the ones in `drop`)."""
        fields = {k: v for k, v in state.items()
                  if k not in drop and k != "page" and v not in (None, "") and v != defaults.get(k)}
        return Markup("".join(
            f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">'
            for k, v in fields.items()))

    return render_template_string(
        PAGE,
        page_title=APP_NAME,
        site_name=APP_NAME,
        logged_in=True,
        view="files",
        is_owner=is_owner_request(),
        folders=folders,
        current=current,
        config_error=config_error,
        b=view,
        query=query,
        kind=kind,
        sort=sort,
        per=per,
        sort_choices=SORT_CHOICES,
        per_choices=PER_PAGE_CHOICES,
        nav=nav,
        hidden=hidden,
        version=VERSION,
        csrf_token=ensure_csrf(),
    )


def _clean_name(value):
    return re.sub(r"\s+", " ", (value or "")).strip()[:60]


@app.get("/settings")
def settings():
    gate = owner_gate()
    if gate:
        return gate
    folders, config_error = get_folders()
    return render_template_string(
        PAGE,
        page_title="Settings · " + APP_NAME,
        site_name=APP_NAME,
        logged_in=True,
        view="settings",
        is_owner=True,
        folders=folders,
        config_error=config_error,
        flash=session.pop("flash", None),
        b=browse(request.args.get("b"), {f["id"] for f in folders}),
        version=VERSION,
        csrf_token=ensure_csrf(),
    )


def _back_to_settings():
    return redirect(url_for("settings", b=request.form.get("back") or None))


@app.post("/settings/add")
def settings_add():
    gate = owner_gate()
    if gate:
        return gate
    check_csrf()
    path = request.form.get("path", "")
    if not path or not Path(path).is_absolute():
        flash("err", "Choose a folder from the list first.")
    else:
        ok, msg = change_folders("add", path=path, name=_clean_name(request.form.get("name")))
        flash("ok" if ok else "err", msg)
    return _back_to_settings()


@app.post("/settings/remove")
def settings_remove():
    gate = owner_gate()
    if gate:
        return gate
    check_csrf()
    ok, msg = change_folders("remove", fid=request.form.get("id", ""))
    flash("ok" if ok else "err", msg)
    return _back_to_settings()


@app.post("/settings/rename")
def settings_rename():
    gate = owner_gate()
    if gate:
        return gate
    check_csrf()
    ok, msg = change_folders("rename", fid=request.form.get("id", ""),
                             name=_clean_name(request.form.get("name")))
    flash("ok" if ok else "err", msg)
    return _back_to_settings()


@app.post("/login")
def login():
    check_csrf()
    ip = client_ip()

    wait = locked_seconds(ip)
    if wait:
        return render_login(
            f"Too many attempts. Try again in {wait // 60 + 1} minutes.", 429
        )

    supplied = request.form.get("password", "").encode()
    if secrets.compare_digest(supplied, PASSWORD.encode()):
        _failures.pop(ip, None)
        session.clear()
        session.permanent = True
        session["authenticated"] = True
        ensure_csrf()
        return redirect(url_for("index"))

    record_failure(ip)
    session.pop("csrf_token", None)
    return render_login("Incorrect password.", 401)


def _folder_or_404(fid):
    folder = find_folder(fid)
    if not folder or not folder["ok"]:
        abort(404)
    return folder


@app.get("/f/<fid>/download/<path:filename>")
def download_file(fid, filename):
    if not session.get("authenticated"):
        return redirect(url_for("index"))
    folder = _folder_or_404(fid)
    if not safe_path(folder["path"], filename):
        abort(404)
    return send_from_directory(
        str(folder["path"]), filename, as_attachment=True, conditional=True,
    )


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/f/<fid>/view/<path:filename>")
def view_file(fid, filename):
    if not session.get("authenticated"):
        return redirect(url_for("index"))
    folder = _folder_or_404(fid)
    mimetype = VIEWABLE.get(Path(filename).suffix.lower().lstrip("."))
    if not mimetype or not safe_path(folder["path"], filename):
        abort(404)
    return send_from_directory(
        str(folder["path"]), filename, mimetype=mimetype,
        as_attachment=False, conditional=True,
    )


THUMB_DIR = BASE_DIR / ".thumbs"
THUMB_TYPES = {"png", "jpg", "jpeg", "gif", "webp", "avif"}


@app.get("/f/<fid>/thumb/<path:filename>")
def thumb(fid, filename):
    """Small cached preview of an image. Falls back to the full image without Pillow."""
    if not session.get("authenticated"):
        return redirect(url_for("index"))
    folder = _folder_or_404(fid)
    path = safe_path(folder["path"], filename)
    if not path or path.suffix.lower().lstrip(".") not in THUMB_TYPES:
        abort(404)
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return redirect(url_for("view_file", fid=fid, filename=filename))

    stat = path.stat()
    key = hashlib.sha1(
        f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8", "replace")).hexdigest()
    cached = THUMB_DIR / f"{key}.jpg"
    if not cached.exists():
        try:
            THUMB_DIR.mkdir(exist_ok=True)
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((520, 520))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                tmp = cached.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=80, optimize=True)
                os.replace(tmp, cached)
        except Exception:
            return redirect(url_for("view_file", fid=fid, filename=filename))
    return send_from_directory(str(THUMB_DIR), cached.name, mimetype="image/jpeg", conditional=True)


class _Sink:
    """Write-only buffer so a zip can be streamed without a temp file."""

    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def drain(self):
        data, self.parts = b"".join(self.parts), []
        return data


def stream_zip(items):
    """Stream a zip of [(Path, name_inside_zip), ...] without building it in memory."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as z:
        for path, arcname in items:
            try:
                stamp = time.localtime(max(path.stat().st_mtime, 315532800))[:6]
                info = zipfile.ZipInfo(arcname, date_time=stamp)
                info.compress_type = zipfile.ZIP_STORED
                with path.open("rb") as src, z.open(info, "w", force_zip64=True) as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        data = sink.drain()
                        if data:
                            yield data
            except (OSError, ValueError):
                continue  # file vanished or unreadable: skip it
        # leaving the with-block writes the zip directory
    yield sink.drain()


class TooManyFiles(Exception):
    pass


def _zip_name(parts, base_parts):
    """Name inside the zip: relative to the folder being viewed when possible."""
    if len(parts) > len(base_parts) and parts[:len(base_parts)] == base_parts:
        return "/".join(parts[len(base_parts):])
    return parts[-1]


def collect_zip_items(root, base_rel, rels):
    """[(path, arcname)] for the chosen files and folders (folders include everything inside)."""
    root_real = root.resolve()
    base_parts = split_rel(base_rel) or []
    chosen, seen = [], set()

    def add(path, arcname):
        if arcname in seen:
            return
        if len(chosen) >= MAX_ZIP_FILES:
            raise TooManyFiles
        seen.add(arcname)
        chosen.append((path, arcname))

    for rel in dict.fromkeys(rels):
        path = resolve_inside(root, rel, "any")
        if path is None:
            continue
        parts = split_rel(rel)
        arc = _zip_name(parts, base_parts)
        if path.is_file():
            add(path, arc)
            continue
        stack = [(str(path), arc)]
        while stack:
            current, prefix = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        info = _entry_info(entry, root_real)
                        if not info:
                            continue
                        name = f"{prefix}/{info['name']}"
                        if info["dir"]:
                            stack.append((info["path"], name))
                        else:
                            add(Path(info["path"]), name)
            except OSError:
                continue
    return chosen


@app.post("/zip")
def zip_files():
    if not session.get("authenticated"):
        return redirect(url_for("index"))
    check_csrf()

    folder = _folder_or_404(request.form.get("f", ""))
    root = folder["path"]
    base_rel = (request.form.get("d") or "").strip("/")
    base = resolve_inside(root, base_rel, "dir") if base_rel else root
    if base is None:
        abort(404)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", folder["name"]).strip("-") or "files"

    names = request.form.getlist("name")
    try:
        if names:
            items = collect_zip_items(root, base_rel, names)
            label = f"{slug}-selected"
            if len(names) == 1:  # one folder picked: name the zip after it
                single = re.sub(r"[^A-Za-z0-9._-]+", "-", names[0].rsplit("/", 1)[-1]).strip("-")
                label = single or label
        else:
            # "Download all": the files directly in the folder being viewed
            root_real = root.resolve()
            files = []
            with os.scandir(base) as it:
                for entry in it:
                    info = _entry_info(entry, root_real)
                    if info and not info["dir"]:
                        files.append((Path(info["path"]), info["name"]))
            items = files
            label = f"{slug}-all"
    except OSError:
        abort(404)
    except TooManyFiles:
        abort(413, f"That is more than {MAX_ZIP_FILES:,} files. Pick a smaller folder.")
    if not items:
        abort(404)

    return Response(
        stream_zip(items),
        mimetype="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{label}-{datetime.now():%Y%m%d}.zip"'},
    )


@app.post("/logout")
def logout():
    if session.get("authenticated"):
        check_csrf()
    session.clear()
    return redirect(url_for("index"))


# --------------------------------------------------
# START (local only; use run.py to share over the internet)
# --------------------------------------------------

ensure_folders_file()
print("Sharing:", flush=True)
print_folders()

if __name__ == "__main__":
    print("Local site: http://127.0.0.1:8000")
    app.run(host="127.0.0.1", port=8000, debug=False, threaded=True)
