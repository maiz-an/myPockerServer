# myPocketServer

A small private file server you run on your own PC. Pick the folders you want to share, send one
password-protected link, and anyone can browse and download the files in them.

## Run it

Works on Windows, macOS and Linux.

1. Install [Python 3.10+](https://www.python.org/downloads/). Git is optional but makes updates cleaner.
2. Get the code: `git clone https://github.com/maiz-an/myPockerServer.git` (or download the zip).
3. Start it:
   - **Windows:** double-click `start.bat`
   - **macOS:** double-click `start.command` (first time: right-click, Open)
   - **Linux:** `./start.sh` (Debian/Ubuntu may need `sudo apt install python3-venv`)
4. First start asks you to choose a password. Then it prints a public `https://...trycloudflare.com` link.
5. Choose what to share: open `http://127.0.0.1:8000` on the same PC, sign in, and click the **gear icon**
   to add folders. See below.

On macOS or Linux, if the launcher says permission denied: `chmod +x start.sh start.command`.

Keep the window open while you want the link to work. The link changes each time you start it.
For a permanent link use a named Cloudflare tunnel and set `CLOUDFLARE_TUNNEL_TOKEN` and `PUBLIC_URL` in `.env`.

## Choosing folders

### From the page (easiest)

1. On the PC running the app, open `http://127.0.0.1:8000` (the address is also printed in the window).
2. Sign in and click the **gear icon** at the top right.
3. Under **Add a folder**, browse your PC (Home, Desktop, drives, USB disks) and press **Add** next to
   a folder, or **Add this folder** for the one you are in. You can type or paste a path too.
4. Repeat for as many folders as you like. They are saved in `folders.json`, so you pick them once and
   they are remembered every time you start the app. Use **Remove** to stop sharing one, or edit its name.

Folders show up as tabs for your visitors immediately, with no restart. Removing a folder only stops
sharing it; your files are never deleted.

The gear icon and the Settings page exist **only when you open the app directly on the PC running it**
(`http://127.0.0.1:8000`). Visitors using your public link never see them and cannot browse your PC or
change what is shared, even though they have the password.

### By editing `folders.json`

Everything the Settings page does is stored in `folders.json`, next to `app.py`. You can also edit it by
hand; save, and refresh the browser. Folders appear as tabs at the top.

```json
{
  "folders": [
    { "name": "Movies",    "path": "D:/Movies" },
    { "name": "Photos",    "path": "/home/me/Photos" },
    { "name": "Documents", "path": "~/Documents/Share" },
    "/mnt/usb/Music"
  ]
}
```

- `path` can be absolute, relative to this folder, or start with `~`. Environment variables work too.
- `name` is optional and defaults to the folder's own name. A plain string is also fine.
- Use forward slashes on every OS (`D:/Movies`). Single backslashes are tolerated, but `\\` is safer.
- A folder that is missing or on an unplugged drive shows a notice and returns when it is available again.
- If the file has a typo the site keeps serving the last working list and tells you in the console.
- Only the files directly inside each folder are listed (not subfolders). Hidden files and links are skipped.
- Updating from an older version? Your existing `shared` folder is added automatically.
- `folders.json` is specific to your PC, so it is ignored by git and never changed by updates.

## Using the site

- Tick files (or press **Select all**) and use **Download zip**, or **Download all** for everything.
- Images show small thumbnails; images, PDFs, video, audio and text open in the browser.
- Press `/` to search, `Esc` to clear.
- Hidden and temporary files (`.DS_Store`, `Thumbs.db`, `*.part`) are not listed.

## Updates

Every time you start the app it first checks the `VERSION` file in this repo. If it is higher than
yours, the new code is installed before the server starts, so you always begin on the latest version.
Offline? It skips the check and starts normally. While it keeps running it checks again every 10 minutes
and restarts the server on its own (the public link stays the same).

If a new version does not start, the previous one is put back automatically and the bad version is
skipped. Your `.env` and `folders.json` are never touched. Turn updates off with `AUTO_UPDATE=0` in `.env`.

## Publishing a new version (maintainer)

```
python release.py patch "what changed"     # 1.1.0 -> 1.1.1
python release.py minor "new feature"      # 1.1.0 -> 1.2.0
```

This bumps `VERSION`, updates `CHANGELOG.md`, commits, tags and pushes.

## Settings (`.env`)

See `.env.example`. `.env` holds your password, so it is ignored by git and must never be committed.

## Security notes

- Anyone with the link and password can download every file in the folders listed in `folders.json`.
  Share dedicated folders, not your home folder or a whole drive. Use a long password.
- After 5 wrong passwords a visitor is locked out for 15 minutes.
- Updates run code from this repository on every machine using it. Keep 2-factor login on the GitHub account that owns it.
