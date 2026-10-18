"""Publish a new version.

    python release.py patch "fix download button"
    python release.py minor "add folders"
    python release.py major "new design"

Bumps VERSION, adds a CHANGELOG line, commits, tags vX.Y.Z and pushes.
Everyone running the server picks it up within a few minutes.
"""
import subprocess
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent


def git(*args):
    r = subprocess.run(["git", *args], cwd=BASE, text=True, capture_output=True)
    if r.returncode:
        sys.exit(f"git {' '.join(args)} failed:\n{r.stderr or r.stdout}")
    return r.stdout.strip()


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("patch", "minor", "major"):
        sys.exit(__doc__)
    kind, note = sys.argv[1], " ".join(sys.argv[2:])

    major, minor, patch = (int(x) for x in (BASE / "VERSION").read_text().strip().split("."))
    if kind == "major":
        major, minor, patch = major + 1, 0, 0
    elif kind == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    new = f"{major}.{minor}.{patch}"

    (BASE / "VERSION").write_text(new + "\n")
    log = BASE / "CHANGELOG.md"
    text = log.read_text() if log.exists() else "# Changelog\n"
    head, _, rest = text.partition("\n")
    log.write_text(f"{head}\n\n## {new} - {date.today()}\n- {note}\n{rest}")

    git("add", "-A")
    git("commit", "-m", f"v{new}: {note}")
    git("tag", f"v{new}")
    git("push", "origin", "HEAD", "--follow-tags")
    print(f"Released {new}.")


if __name__ == "__main__":
    main()
