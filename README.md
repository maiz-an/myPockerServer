# myPocketServer

A small private file server you run on your own PC. Put files in the `shared` folder, share one
password-protected link, and anyone can browse and download them.

## Run it

Works on Windows, macOS and Linux.

1. Install [Python 3.10+](https://www.python.org/downloads/). Git is optional but makes updates cleaner.
2. Get the code: `git clone https://github.com/maiz-an/myPockerServer.git` (or download the zip).
3. Start it:
   - **Windows:** double-click `start.bat`
   - **macOS:** double-click `start.command` (first time: right-click, Open)
   - **Linux:** `./start.sh` (Debian/Ubuntu may need `sudo apt install python3-venv`)
4. First start asks you to choose a password. Then it prints a public `https://...trycloudflare.com` link.
5. Copy files into the `shared` folder. Refresh the page.

On macOS or Linux, if the launcher says permission denied: `chmod +x start.sh start.command`.

Keep the window open while you want the link to work. The link changes each time you start it.
For a permanent link use a named Cloudflare tunnel and set `CLOUDFLARE_TUNNEL_TOKEN` and `PUBLIC_URL` in `.env`.

## Using the site

- Tick files (or press **Select all**) and use **Download zip**, or **Download all** for everything.
- Images show small thumbnails; images, PDFs, video, audio and text open in the browser.
- Press `/` to search, `Esc` to clear.
- Hidden and temporary files (`.DS_Store`, `Thumbs.db`, `*.part`) are not listed.

## Updates

The app checks the `VERSION` file in this repo every 10 minutes. When it is higher than yours it
downloads the new code, restarts the server (the public link stays the same) and goes back to the old
version by itself if the new one does not start. Your `.env` and `shared` folder are never touched.
Turn it off with `AUTO_UPDATE=0` in `.env`.

## Publishing a new version (maintainer)

```
python release.py patch "what changed"     # 1.1.0 -> 1.1.1
python release.py minor "new feature"      # 1.1.0 -> 1.2.0
```

This bumps `VERSION`, updates `CHANGELOG.md`, commits, tags and pushes.

## Settings (`.env`)

See `.env.example`. `.env` holds your password, so it is ignored by git and must never be committed.

## Security notes

- Anyone with the link and password can download every file in `shared`. Use a long password.
- After 5 wrong passwords a visitor is locked out for 15 minutes.
- Updates run code from this repository on every machine using it. Keep 2-factor login on the GitHub account that owns it.
