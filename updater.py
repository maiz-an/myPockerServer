"""Check GitHub for a newer VERSION and apply it.

Two ways to apply an update:
  * the folder is a git clone  -> git pull --ff-only
  * the folder is a plain download -> fetch the repo zip and copy the code files over

Your .env and the shared/ folder are never touched.
"""
import io
import os
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent

REPO = os.environ.get("UPDATE_REPO", "maiz-an/myPockerServer")
BRANCH = os.environ.get("UPDATE_BRANCH", "main")
RAW_VERSION_URL = os.environ.get(
    "UPDATE_RAW_URL",
    f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION")
ZIP_URL = os.environ.get(
    "UPDATE_ZIP_URL",
    f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip")

# Only these files are ever replaced by an update.
CODE_FILES = [
    "app.py", "run.py", "updater.py", "release.py", "requirements.txt",
    "start.bat", "start.sh", "start.command", ".gitattributes", "VERSION", "README.md", "CHANGELOG.md", ".env.example",
    ".gitignore",
]
LAUNCHER_FILES = {"run.py", "updater.py", "start.bat", "start.sh", "start.command"}

BACKUP = BASE / ".backup"
SKIP_FILE = BASE / ".skip_version"


def parse(version):
    parts = []
    for p in version.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def local_version():
    try:
        return (BASE / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


API_VERSION_URL = f"https://api.github.com/repos/{REPO}/contents/VERSION?ref={BRANCH}"


def remote_version(timeout=10):
    try:
        req = urllib.request.Request(RAW_VERSION_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode().strip()
    except Exception:
        if "UPDATE_RAW_URL" in os.environ:
            raise
        # raw.githubusercontent.com unreachable: ask the GitHub API instead
        req = urllib.request.Request(
            API_VERSION_URL, headers={"Accept": "application/vnd.github.raw+json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode().strip()


def skipped_version():
    try:
        return SKIP_FILE.read_text().strip()
    except OSError:
        return ""


def check(timeout=10):
    """Return the remote version string if it is newer (and not a known-bad one), else None."""
    remote = remote_version(timeout)
    if parse(remote) > parse(local_version()) and remote != skipped_version():
        return remote
    return None


def _backup():
    if BACKUP.exists():
        shutil.rmtree(BACKUP)
    BACKUP.mkdir()
    for name in CODE_FILES:
        if (BASE / name).exists():
            shutil.copy2(BASE / name, BACKUP / name)


HEAD_NOTE = BACKUP / ".head"  # git commit we were on before the update


def _git_head():
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BASE, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def rollback(bad_version=None):
    """Put the previous code back."""
    restored = False
    if HEAD_NOTE.exists() and _is_git_clone():
        # Go back to the old commit itself, not just the old files. Otherwise the clone is
        # left with local changes and a later fixed release could never be pulled.
        # --keep never overwrites uncommitted work; if it refuses, we copy files instead.
        r = subprocess.run(["git", "reset", "--keep", HEAD_NOTE.read_text().strip()],
                           cwd=BASE, capture_output=True)
        restored = r.returncode == 0
    if not restored and BACKUP.exists():
        for f in BACKUP.iterdir():
            if f.is_file() and f != HEAD_NOTE:
                shutil.copy2(f, BASE / f.name)
    if bad_version:
        SKIP_FILE.write_text(bad_version)


def _is_git_clone():
    return (BASE / ".git").exists() and shutil.which("git") is not None


def _snapshot():
    out = {}
    for name in CODE_FILES:
        p = BASE / name
        out[name] = p.read_bytes() if p.exists() else None
    return out


def _apply_git():
    # If .env was ever tracked upstream, local edits would block the pull and the
    # pull could delete it. Set it aside, pull, then put it back.
    env = BASE / ".env"
    env_bytes = env.read_bytes() if env.exists() else None
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=BASE,
                             capture_output=True).returncode == 0
    if tracked:
        subprocess.run(["git", "checkout", "--", ".env"], cwd=BASE, capture_output=True)
    try:
        _git_pull()
    finally:
        if env_bytes is not None:
            env.write_bytes(env_bytes)


def _git_pull():
    # GIT_TERMINAL_PROMPT=0: never sit waiting for a password at startup.
    r = subprocess.run(["git", "pull", "--ff-only", "origin", BRANCH], cwd=BASE,
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        raise RuntimeError("git pull failed: " + (r.stderr or r.stdout).strip()[:300])


def _apply_zip():
    with urllib.request.urlopen(ZIP_URL, timeout=60) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        prefix = z.namelist()[0].split("/")[0] + "/"
        for name in CODE_FILES:
            member = prefix + name
            if member in z.namelist():
                tmp = BASE / (name + ".new")
                tmp.write_bytes(z.read(member))
                os.replace(tmp, BASE / name)
                if name.endswith((".sh", ".command")):
                    os.chmod(BASE / name, 0o755)


def apply():
    """Install the update. Returns (changed_files, launcher_changed, requirements_changed)."""
    _backup()
    before = _snapshot()
    if _is_git_clone():
        head = _git_head()
        if head:
            HEAD_NOTE.write_text(head)
        _apply_git()
    else:
        _apply_zip()
    after = _snapshot()
    changed = [n for n in CODE_FILES if before[n] != after[n]]

    if "requirements.txt" in changed:
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r",
                        str(BASE / "requirements.txt")], timeout=600, check=False)

    return changed, bool(LAUNCHER_FILES & set(changed))
