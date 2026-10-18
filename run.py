"""Run the file server on this PC, share it on a public HTTPS link, and keep it updated.

    python run.py            (or double-click start.bat on Windows)

* First start asks you for a password and creates .env.
* A Cloudflare quick tunnel gives you a public https link (no account, no router setup).
* Every few minutes it checks GitHub for a newer VERSION and updates itself.
  The public link stays the same during an update; only the server restarts.
"""
import getpass
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from shutil import which

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / ".env"


# ---------------------------------------------------------------- first-run setup

def ensure_env():
    if ENV_FILE.exists() and "FILE_SERVER_PASSWORD" in ENV_FILE.read_text():
        return
    print("First start: creating your settings.\n")
    if sys.stdin and sys.stdin.isatty():
        while True:
            pw = getpass.getpass("Choose a password for the site (10+ characters): ")
            if len(pw) < 10:
                print("Too short. Use a longer passphrase.")
                continue
            if pw != getpass.getpass("Repeat it: "):
                print("They do not match.")
                continue
            break
    else:
        pw = secrets.token_urlsafe(16)
        print(f"Generated password: {pw}")
    ENV_FILE.write_text(
        f"FILE_SERVER_PASSWORD={pw}\nFLASK_SECRET={secrets.token_hex(32)}\n")
    print("Saved to .env (this file stays on your PC and is never uploaded).\n")


ensure_env()
os.environ["HTTPS_ENABLED"] = "1"  # secure cookies; the public link is HTTPS

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ENV_FILE)
PORT = int(os.environ.get("PORT", "8000"))

import updater  # noqa: E402


def ensure_optional():
    """Pillow makes small thumbnails. Nice to have, never required."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pillow"],
                       check=False, timeout=300)


try:
    ensure_optional()
except Exception:
    pass

# ---------------------------------------------------------------- server process

_server = None
_lock = threading.Lock()


def start_server():
    global _server
    code = ("from waitress import serve; from app import app; "
            f"serve(app, host='127.0.0.1', port={PORT}, threads=8, ident='files')")
    _server = subprocess.Popen([sys.executable, "-c", code], cwd=BASE)


def stop_server():
    if _server and _server.poll() is None:
        _server.terminate()
        try:
            _server.wait(10)
        except subprocess.TimeoutExpired:
            _server.kill()


def port_open():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def wait_for_port(seconds=15):
    for _ in range(int(seconds * 10)):
        if _server.poll() is not None:
            return False
        if port_open():
            return True
        time.sleep(0.1)
    return False


def restart_server():
    stop_server()
    start_server()
    return wait_for_port()


# ---------------------------------------------------------------- auto-update

def log(msg):
    print(time.strftime("[%H:%M] ") + msg, flush=True)


def update_once():
    try:
        new = updater.check()
    except Exception as e:  # offline, GitHub down, etc.
        log(f"update check skipped ({e.__class__.__name__})")
        return
    if not new:
        return

    old = updater.local_version()
    log(f"new version {new} found (running {old}). Updating...")
    with _lock:
        try:
            changed, launcher = updater.apply()
        except Exception as e:
            log(f"update failed: {e}")
            updater.rollback(new)
            return

        if restart_server():
            log(f"updated to {updater.local_version()}.")
            if launcher:
                log("start.bat / run.py also changed: close and reopen it when convenient "
                    "(the link will change when you do).")
        else:
            log("new version did not start. Going back to the previous one.")
            updater.rollback(new)
            restart_server()


def update_loop():
    minutes = float(os.environ.get("UPDATE_MINUTES", "10"))
    while True:
        update_once()
        time.sleep(max(minutes, 1) * 60)


# ---------------------------------------------------------------- tunnel

DL = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def asset_for(system, machine):
    """Name of the cloudflared download for this OS/CPU, or None."""
    m = machine.lower()
    arm64 = m in ("arm64", "aarch64")
    x64 = m in ("amd64", "x86_64", "x64")
    if system == "Windows":
        return "cloudflared-windows-386.exe" if m in ("x86", "i386", "i686") \
            else "cloudflared-windows-amd64.exe"  # ARM Windows runs the x64 build
    if system == "Darwin":
        return "cloudflared-darwin-arm64.tgz" if arm64 else "cloudflared-darwin-amd64.tgz"
    if system == "Linux":
        if arm64:
            return "cloudflared-linux-arm64"
        if x64:
            return "cloudflared-linux-amd64"
        if m.startswith("armv7") or m.startswith("armv6") or m == "arm":
            return "cloudflared-linux-armhf"
        if m in ("i386", "i686", "x86"):
            return "cloudflared-linux-386"
    return None


def find_cloudflared():
    local = BASE / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if local.exists():
        return str(local)
    if which("cloudflared"):
        return which("cloudflared")
    asset = asset_for(platform.system(), platform.machine())
    if not asset:
        sys.exit("Install cloudflared first: https://developers.cloudflare.com/"
                 "cloudflare-one/connections/connect-networks/downloads/")
    print("Downloading cloudflared (one time)...")
    if asset.endswith(".tgz"):  # macOS ships as a tarball
        import tarfile
        tgz = BASE / "cloudflared.tgz"
        urllib.request.urlretrieve(DL + asset, tgz)
        with tarfile.open(tgz) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("cloudflared"))
            member.name = "cloudflared"
            try:
                t.extract(member, BASE, filter="data")
            except TypeError:  # older Python without the filter argument
                t.extract(member, BASE)
        tgz.unlink()
    else:
        urllib.request.urlretrieve(DL + asset, local)
    local.chmod(0o755)
    return str(local)


def copy_to_clipboard(text):
    """Best effort. Returns True if copied."""
    if os.name == "nt":
        cmds = [["clip"]]
    elif platform.system() == "Darwin":
        cmds = [["pbcopy"]]
    else:
        cmds = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]]
    for cmd in cmds:
        if which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def show(url):
    line = "=" * 60
    print(f"\n{line}\n  Share this link:\n\n    {url}\n\n"
          f"  Version {updater.local_version()}. Keep this window open.\n"
          f"  Ctrl+C to stop.\n{line}\n", flush=True)
    if copy_to_clipboard(url):
        print("  (link copied to clipboard)\n")


def main():
    if port_open():
        sys.exit(f"Port {PORT} is already in use (is the server already running?). "
                 f"Close the other window, or set PORT=8001 in .env.")
    start_server()
    if not wait_for_port():
        sys.exit("Server did not start. Check the messages above.")
    print(f"Server running locally on http://127.0.0.1:{PORT}")

    if os.environ.get("AUTO_UPDATE", "1") != "0":
        threading.Thread(target=update_loop, daemon=True).start()

    exe = find_cloudflared()
    token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
    if token:
        cmd = [exe, "tunnel", "--no-autoupdate", "run", "--token", token]
    else:
        cmd = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    shown = False
    try:
        for out in proc.stdout:
            if token:
                if not shown and "Registered tunnel connection" in out:
                    shown = True
                    show(os.environ.get("PUBLIC_URL", "(your tunnel domain)"))
                continue
            m = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", out)
            if m and not shown:
                shown = True
                show(m.group(0))
            elif not shown and ("ERR" in out or "failed" in out.lower()):
                print(out.rstrip())
        if not shown:
            print("\nCould not open the public link (no internet, or Cloudflare is blocked).\n"
                  f"The site still works on this PC: http://127.0.0.1:{PORT}\n"
                  "Press Ctrl+C to stop.", flush=True)
            _server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        stop_server()


if __name__ == "__main__":
    main()
