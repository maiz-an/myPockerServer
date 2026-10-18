# Changelog

## 1.4.0 - 2026-10-19
- Settings page (gear icon): browse this PC and add, rename or remove shared folders one by one; saved permanently in `folders.json`
- Works the same on Windows, macOS and Linux (drives, USB disks, Home shortcuts)
- Settings only exist when opened directly on the host PC; visitors on the public link cannot see or use them

## 1.3.0 - 2026-10-19
- Share any number of folders: list them in `folders.json` and they show as tabs, no `shared` folder needed (picked up live, no restart)
- Existing `shared` folders keep working: they are added to `folders.json` automatically
- App name is built in (`SITE_NAME` setting removed)
- Checks for updates on every launch, before the server starts; skips quietly when offline
- Fix: after a rolled-back update, a later fixed release could not be installed on git clones

## 1.2.0 - 2026-10-18
- Windows, macOS and Linux launchers; zip download of selected files; thumbnails; hides junk files; clearer tunnel errors

## 1.1.1 - 2026-10-18
- Update check falls back to the GitHub API if raw.githubusercontent.com is blocked

## 1.1.0 - 2026-10-18
- New calm interface: grid and list views, type filters, sorting, thumbnails, preview in browser
- Public HTTPS link from your own PC (Cloudflare tunnel), login lockout after repeated failures
- Automatic updates from GitHub, with rollback if the new version does not start
- `release.py` to publish a new version in one command

## 1.0.0
- First version
