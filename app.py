import hashlib
import json
import os
import re
import secrets
import time
import zipfile
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
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


_folder_cache = {"stamp": object(), "entries": [], "error": None}


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
        fid = hashlib.sha1(str(resolved).encode("utf-8", "replace")).hexdigest()[:10]
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


def find_folder(fid):
    for folder in get_folders()[0]:
        if folder["id"] == fid:
            return folder
    return None


def print_folders():
    folders, error = get_folders()
    if not folders:
        print("No folders yet. Add some to folders.json, then refresh the page.", flush=True)
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

.tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 26px; }
.tab { border: 1px solid var(--line); background: var(--surface); color: var(--muted);
       border-radius: 10px; padding: 7px 14px; font-size: 14px; font-weight: 500; }
.tab:hover { color: var(--ink); border-color: var(--muted); }
.tab.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.tab.off:not(.on) { opacity: .55; }
.notice { margin-bottom: 20px; }

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
        <div class="brand">{{ logo() }}{{ site_name }}</div>
        <form method="post" action="{{ url_for('logout') }}">
            <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
            <button class="linkbtn" type="submit">Sign out</button>
        </form>
    </div>
</header>

<main class="wrap">
    {% if config_error %}
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
    <div class="head">
        <div>
            <h1>{{ current.name if current else 'Files' }}</h1>
            {% if current %}<div class="count">{{ total_files }} file{{ '' if total_files == 1 else 's' }} · {{ total_size }}</div>{% endif %}
        </div>
        {% if files %}
        <div class="search">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m16 16 4 4"/></svg>
            <input id="search" type="search" placeholder="Search files" aria-label="Search files" autocomplete="off">
        </div>
        {% endif %}
    </div>

    {% if files %}
    <div class="toolbar">
        <div class="chips" id="chips">
            <button class="chip on" data-kind="all">All<b>{{ total_files }}</b></button>
            {% for k, label, n in kinds %}
            <button class="chip" data-kind="{{ k }}">{{ label }}<b>{{ n }}</b></button>
            {% endfor %}
        </div>
        <div class="ctrls">
            <form method="post" action="{{ url_for('zip_files') }}" style="margin:0">
                <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
                <input type="hidden" name="f" value="{{ current.id }}">
                <button class="allbtn" type="submit" title="Download every file as one zip">Download all</button>
            </form>
            <select id="sort" aria-label="Sort">
                <option value="name">Name</option>
                <option value="new">Newest</option>
                <option value="big">Largest</option>
            </select>
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

    <div class="items" id="items">
    {% for f in files %}
        <article class="item" data-name="{{ f.name|lower }}" data-kind="{{ f.kind }}"
                 data-size="{{ f.bytes }}" data-time="{{ f.mtime }}">
            <label class="pick" title="Select"><input type="checkbox" name="name" value="{{ f.name }}" form="zipform" aria-label="Select {{ f.name }}"></label>
            <a class="thumb k-{{ f.kind }}" href="{{ url_for('view_file', fid=current.id, filename=f.name) if f.viewable else url_for('download_file', fid=current.id, filename=f.name) }}"
               {% if f.viewable %}target="_blank" rel="noopener"{% endif %} tabindex="-1" aria-hidden="true">
                {% if f.kind == 'image' and f.viewable %}
                    <img src="{{ url_for('thumb', fid=current.id, filename=f.name) }}" alt="" loading="lazy" decoding="async">
                {% endif %}
                <span class="ico">
                    {% if f.kind == 'image' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m21 16-5-5-9 9"/></svg>
                    {% elif f.kind == 'video' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/></svg>
                    {% elif f.kind == 'audio' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/></svg>
                    {% elif f.kind == 'archive' %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4"/></svg>
                    {% else %}<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>
                    {% endif %}
                    <span class="ext">{{ f.ext }}</span>
                </span>
            </a>
            <div class="body">
                <div class="txt">
                    <div class="name" title="{{ f.name }}">{{ f.name }}</div>
                    <div class="meta">{{ f.size }} · {{ f.date }}</div>
                </div>
                <div class="acts">
                    {% if f.viewable %}
                    <a class="btn view" href="{{ url_for('view_file', fid=current.id, filename=f.name) }}" target="_blank" rel="noopener">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg><span>Open</span></a>
                    {% endif %}
                    <a class="btn main" href="{{ url_for('download_file', fid=current.id, filename=f.name) }}" aria-label="Download {{ f.name }}">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5M12 15V3"/></svg><span>Download</span></a>
                </div>
            </div>
        </article>
    {% endfor %}
        <div id="no-results" class="empty none">No matching files.</div>
    </div>
    {% else %}
    <div class="items" style="margin-top:26px">
        <div class="empty">
            {% if not current %}No folders yet. Add one to <code>folders.json</code>, then refresh.
            {% elif not current.ok %}This folder was not found. Check its path in <code>folders.json</code>.
            {% else %}No files in this folder yet.{% endif %}
        </div>
    </div>
    {% endif %}
    <form id="zipform" method="post" action="{{ url_for('zip_files') }}" class="selbar none">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        {% if current %}<input type="hidden" name="f" value="{{ current.id }}">{% endif %}
        <span id="selcount"></span>
        <button type="button" id="selall">Select all</button>
        <button type="button" id="selclear">Clear</button>
        <button type="submit" class="go">Download zip</button>
    </form>
    <footer class="foot">v{{ version }}</footer>
</main>

<script>
(function () {
    const box = document.getElementById("items");
    if (!box) return;
    const items = [...box.querySelectorAll(".item")];
    const search = document.getElementById("search");
    const sort = document.getElementById("sort");
    let kind = "all";

    function apply() {
        const term = search.value.toLowerCase().trim();
        let n = 0;
        items.forEach(el => {
            const ok = (kind === "all" || el.dataset.kind === kind) && el.dataset.name.includes(term);
            el.style.display = ok ? "" : "none";
            if (ok) n++;
        });
        document.getElementById("no-results").classList.toggle("none", n > 0);
    }
    function order() {
        const by = sort.value;
        const key = {
            name: (a, b) => a.dataset.name.localeCompare(b.dataset.name),
            new: (a, b) => b.dataset.time - a.dataset.time,
            big: (a, b) => b.dataset.size - a.dataset.size,
        }[by];
        items.sort(key).forEach(el => box.insertBefore(el, document.getElementById("no-results")));
    }
    function view(v) {
        box.classList.toggle("list", v === "list");
        document.getElementById("v-list").classList.toggle("on", v === "list");
        document.getElementById("v-grid").classList.toggle("on", v !== "list");
        try { localStorage.setItem("view", v); } catch (e) {}
    }

    const bar = document.getElementById("zipform");
    const boxes = () => [...box.querySelectorAll(".pick input")];
    function picked() {
        const on = boxes().filter(b => b.checked);
        boxes().forEach(b => b.closest(".item").classList.toggle("sel", b.checked));
        bar.classList.toggle("none", on.length === 0);
        box.classList.toggle("picking", on.length > 0);
        document.getElementById("selcount").textContent =
            on.length + " selected";
    }
    box.addEventListener("change", e => { if (e.target.matches(".pick input")) picked(); });
    document.getElementById("selclear").onclick = () => { boxes().forEach(b => b.checked = false); picked(); };
    document.getElementById("selall").onclick = () => {
        boxes().forEach(b => { if (b.closest(".item").style.display !== "none") b.checked = true; });
        picked();
    };
    document.addEventListener("keydown", e => {
        if (e.key === "/" && document.activeElement !== search) { e.preventDefault(); search.focus(); }
        else if (e.key === "Escape") {
            if (search.value) { search.value = ""; apply(); }
            else { document.getElementById("selclear").click(); }
            search.blur();
        }
    });

    search.addEventListener("input", apply);
    sort.addEventListener("change", order);
    document.getElementById("chips").addEventListener("click", e => {
        const b = e.target.closest(".chip");
        if (!b) return;
        kind = b.dataset.kind;
        document.querySelectorAll(".chip").forEach(c => c.classList.toggle("on", c === b));
        apply();
    });
    document.getElementById("v-grid").onclick = () => view("grid");
    document.getElementById("v-list").onclick = () => view("list");

    let saved = "grid";
    try { saved = localStorage.getItem("view") || "grid"; } catch (e) {}
    view(saved);
})();
</script>
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


def safe_path(root, filename):
    """Path of a visible regular file directly inside the folder `root`, or None."""
    if "/" in filename or "\\" in filename or filename in ("", ".", "..") or is_hidden(filename):
        return None
    path = root / filename
    try:
        if (path.is_symlink() or not path.is_file()
                or path.parent.resolve() != root.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return path


def list_files(folder):
    """Visible files directly inside one folder (not subfolders)."""
    files = []
    root = folder["path"]
    if not folder["ok"]:
        return files
    try:
        entries = list(root.iterdir())
    except OSError:  # no permission, drive went away, ...
        return files

    for path in entries:
        try:
            if is_hidden(path.name) or not path.is_file() or path.is_symlink():
                continue
            if path.parent.resolve() != root:
                continue

            stat = path.stat()
            ext = path.suffix.lower().lstrip(".")
            files.append({
                "name": path.name,
                "size": human_size(stat.st_size),
                "bytes": stat.st_size,
                "ext": ext[:4].upper() or "FILE",
                "kind": kind_of(ext),
                "viewable": ext in VIEWABLE,
                "mtime": int(stat.st_mtime),
                "date": datetime.fromtimestamp(stat.st_mtime).strftime("%d %b %Y"),
            })
        except (OSError, ValueError):
            continue

    return sorted(files, key=lambda item: item["name"].lower())


def kind_counts(files):
    out = []
    for kind, (label, _) in KINDS.items():
        n = sum(1 for f in files if f["kind"] == kind)
        if n:
            out.append((kind, label, n))
    n = sum(1 for f in files if f["kind"] == "other")
    if n:
        out.append(("other", "Other", n))
    return out


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


@app.get("/")
def index():
    if not session.get("authenticated"):
        return render_login()

    folders, config_error = get_folders()
    current = next((f for f in folders if f["id"] == request.args.get("f")), None)
    if current is None and folders:
        current = folders[0]

    files = list_files(current) if current else []
    total_bytes = sum(item["bytes"] for item in files)

    return render_template_string(
        PAGE,
        page_title=APP_NAME,
        site_name=APP_NAME,
        logged_in=True,
        folders=folders,
        current=current,
        config_error=config_error,
        files=files,
        total_files=len(files),
        total_size=human_size(total_bytes),
        kinds=kind_counts(files),
        version=VERSION,
        csrf_token=ensure_csrf(),
    )


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


def stream_zip(paths):
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as z:
        for p in paths:
            stamp = time.localtime(max(p.stat().st_mtime, 315532800))[:6]
            info = zipfile.ZipInfo(p.name, date_time=stamp)
            info.compress_type = zipfile.ZIP_STORED
            try:
                with p.open("rb") as src, z.open(info, "w", force_zip64=True) as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        data = sink.drain()
                        if data:
                            yield data
            except OSError:
                continue  # file vanished or unreadable: skip it
        # leaving the with-block writes the zip directory
    yield sink.drain()


@app.post("/zip")
def zip_files():
    if not session.get("authenticated"):
        return redirect(url_for("index"))
    check_csrf()

    folder = _folder_or_404(request.form.get("f", ""))
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", folder["name"]).strip("-") or "files"

    names = request.form.getlist("name")
    if names:
        paths = [p for p in (safe_path(folder["path"], n) for n in dict.fromkeys(names)) if p]
        label = f"{slug}-selected"
    else:
        paths = [folder["path"] / f["name"] for f in list_files(folder)]
        label = f"{slug}-all"
    if not paths:
        abort(404)

    return Response(
        stream_zip(paths),
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
