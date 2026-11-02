# DEVELOPER SETUP AND DEPLOYMENT

This document is for a developer setting this repo up from scratch (or restoring
it to a working state). It describes how the desktop app runs, how the hosted
admin dashboard and app Google sign-in work, what Firebase/GitHub config is
needed, and how to build/update/deploy.

> For the product README see [`README.md`](README.md).

## 1. Repository layout

| Path | Purpose |
|---|---|
| `app/` | Python backend (HTTP server, API, downloader, DB, cloud/telemetry, updater). |
| `web/` | Static frontend for the desktop app (served by `app.server` from `web_dir()`). |
| `docs/` | GitHub Pages site: owner dashboard (`index.html`) + `login.html` for Google handoff. |
| `launcher/macos/` | macOS menu bar app source (`MenuBarApp.swift`) + `backend_entry.py`. |
| `launcher/windows/` | Windows tray app source (.NET 8 WinForms) + `build.ps1`, `install.ps1`, `uninstall.ps1`, `README.txt`. |
| `scripts/` | `build_backend.py` (PyInstaller), `build_macos_app.sh`. |
| `Contents/`, `Info.plist`, `AppIcon.icns` | Legacy macOS bundle assets (kept for reference). |
| `cloud_config.json` | Firebase project config read by `app.cloud` (safe to ship; web API key is public). |
| `firebase.json`, `.firebaserc`, `firestore.rules` | Firebase Hosting/Firestore rules. |
| `requirements.txt` | Python deps for the app. |
| `.github/workflows/build-apps.yml` | CI: builds macOS + Windows apps and publishes a release on tag push. |

`app.bak/` is a backup copy checked into the repo (not imported). `app/__pycache__/`
should not be committed — `.gitignore` ignores it and `.firebase/`.

## 2. Local development (source)

### Prerequisites
- Python 3.10+ (tested on 3.14 locally)
- `yt-dlp`, `ffmpeg` for downloads (the app warns if missing)
- Optional: Firebase CLI (`firebase`) for hosting/rules, `gh` for releases, .NET SDK 10 for Windows builds

### Install deps
```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.txt`:
```text
yt-dlp
mutagen
rapidfuzz
yt-dlp-ejs
```

### Run the app
```sh
python -m app.server            # opens http://127.0.0.1:8765/
# or
python -m app.server --port 9000 --no-browser
# or
python -m app                  # same as app.server.main
```

Behavior (see `app/server.py`):
- Binds to `127.0.0.1` only by default, finds a free port if `8765` taken.
- Single-instance guard (`app/instance.py`) only applies to the **packaged Windows
  build** (named mutex). Running `python -m app.server` alongside an installed
  copy is allowed and is the normal dev workflow.
- Runs a background worker/manager, serves `web/` as static files, and exposes
  the JSON API under `/api/...` via `app/api.py`.
- On quit, stops manager/telemetry, shuts down the server cleanly.

### Data paths
`app/paths.py`:
- `app_root()` — repo root when running from source (`Path(__file__).parent.parent`)
- `data_dir()` — per-user: macOS `~/Library/Application Support/PlaylistDownloader`,
  Windows `%APPDATA%\\PlaylistDownloader`, Linux `~/.config/PlaylistDownloader`
  (override with `PLAYLIST_DOWNLOADER_HOME`)
- Writes: `config.json`, `queue.db` (SQLite), `download-archive.txt`,
  `cloud_session.json`, logs in `data_dir()`

### Config
`app/config.py` defaults: `download_dir` defaults to `~/Downloads/Playlist Downloader`
(macOS/Linux logic in `paths.default_download_dir()`), `update_repo` defaults to
`brianbcyangmusic-pixel/playlist-downloader`, `audio_format`/`video_format`/qualities,
`yt_cookies_browser`, etc. Config is loaded/saved to `data_dir()/config.json`.

## 3. Cloud accounts, telemetry, and auth (app side)

Configured via `cloud_config.json` (loaded by `app.cloud.load_config()`, env
`PLAYLIST_DOWNLOADER_CLOUD` takes precedence).

Current `cloud_config.json`:
```json
{
  "api_key": "AIzaSyCSCx1rMiR2bQKFH6PXwGRLUxg0__pkmdo",
  "project_id": "playlist-downloader-99ad0",
  "auth_domain": "playlist-downloader-99ad0.firebaseapp.com",
  "login_url": "https://brianbcyangmusic-pixel.github.io/playlist-downloader/login.html",
  "admin_email": "brian.bcyang27@gmail.com",
  "enabled": true
}
```

Notes:
- The **Firebase web API key is public by design**. Security is enforced by
  Firebase Auth + Firestore rules, not by hiding this key.
- `app/cloud.py` talks to Firebase REST APIs (Identity Toolkit, Firestore) with
  stdlib `urllib`. It stores the refresh token in `cloud_session.json` with
  `0600` permissions.
- `app/telemetry.py` listens to events on `app.events` and writes documents to
  Firestore collections `downloads` and `errors` (see Firestore rules). It never
  blocks downloads; full queue is dropped if backing up.
- Google sign-in in the desktop app uses the **browser handoff flow** (see below).
- `app/api.py` enforces auth on API calls if cloud is enabled and a session is
  required.

## 4. Firebase project setup (playlist-downloader-99ad0)

Project: `playlist-downloader-99ad0` (Firebase web app `1:800139508246:web:5032688053764cd0871337`).

### Authentication
- Email/password enabled
- Google provider enabled (client ID/secret configured)
- Authorized domains include: `localhost`, `playlist-downloader-99ad0.firebaseapp.com`,
  `playlist-downloader-99ad0.web.app`, `brianbcyangmusic-pixel.github.io`
- Users (as of last check): `brian.bcyang27@gmail.com` (Google, verified, owner),
  `stonytonybb@gmail.com` (Google), `ohnapek@gmail.com` (Google). None have
  password providers attached.

### Firestore
Database in Native mode. Collections written by the app:
- `downloads` — one document per playlist download/run (fields like `uid`,
  `user_email`, `playlist`, `platform`, `link`, `tracks_total/done/failed/skipped`,
  `status`, `started_at`, `finished_at`, `duration_s`, `os`, `app_version`,
  `sample_tracks`). See `app/telemetry.py`.
- `errors` — failures/errors worth reporting (e.g. `auth_failed` can be created
  anonymously with tight constraints; others require auth). `notified` is used
  by the dashboard.

Rules are in `firestore.rules` (deployed). Key points:
- Owner is `brian.bcyang27@gmail.com` (email match). Can read all `downloads`
  and `errors`, can update `errors` (mark reviewed), can update/delete as
  allowed.
- A signed-in user can create/update their own `downloads` documents (must
  match `request.auth.uid` and email).
- `errors` allow anonymous create only when `kind == 'auth_failed'`, not
  notified, message/email length-capped; otherwise require ownership.
- No client deletes.

Deploy rules:
```sh
firebase deploy --only firestore:rules --project playlist-downloader-99ad0
```

### Recreating the Firebase project from scratch

Only needed if the project is lost or you are standing up a second environment.

1. `firebase login` (as the project owner), then `firebase projects:create <id>`.
2. Firebase Console → **Add app → Web**; copy `apiKey`, `appId`, `authDomain`,
   `projectId`, `messagingSenderId` into `cloud_config.json` (keep the same key
   names) and into the `CONFIG` objects in `docs/index.html` and
   `docs/login.html`.
3. **Authentication → Sign-in method**: enable *Email/Password* and *Google*
   (Google needs an OAuth client; the console creates/links one automatically).
4. **Authentication → Settings → Authorized domains**: add `localhost`,
   `<project>.firebaseapp.com`, `<project>.web.app`, and any custom domain.
   If the admin page is hosted on GitHub Pages, add `<account>.github.io` too.
5. **Firestore Database → Create database** in Native mode, then
   `firebase deploy --only firestore:rules`.
6. Edit `firestore.rules` so `owner()` matches the intended owner email, deploy
   again, and set the same address as `admin_email` in `cloud_config.json` and
   `ADMIN_EMAIL` in `docs/index.html` (admin.js).
7. Create the owner account (sign up in the app, Google or email/password).
   The first account must use the owner email if you want it to read the
   dashboard.
8. Firebase Hosting is still used as a mirror of the admin site (optional):
   `firebase deploy --only hosting --project playlist-downloader-99ad0`.
9. Put the hosting login URL into `cloud_config.json` as `login_url`
   (GitHub Pages: `https://<account>.github.io/<repo>/login.html`).
10. Run the app, sign in, download one playlist, and confirm documents appear
    in `downloads`/`errors` and render in the dashboard.

### Adding or changing an admin

Two places must agree, or the page loads but Firestore refuses the read:
- `docs/admin.js` → `ADMIN_EMAIL`
- `firestore.rules` → `owner()` email

Deploy both (hosting + rules). Adding multiple admins means changing `owner()` to
`request.auth.token.email in ['a@x', 'b@y']`.

### Firebase Hosting (optional mirror)
Hosts the admin dashboard and the app Google login page as a fallback:
- Site: `playlist-downloader-99ad0.web.app` / `playlist-downloader-99ad0.firebaseapp.com`
- Public dir: `docs/` (see `firebase.json`)
- Cache headers set to `no-cache` for `.html` and `.js`

Deploy hosting:
```sh
firebase deploy --only hosting --project playlist-downloader-99ad0
```

Firebase CLI auth: logged in as `brian.bcyang27@gmail.com` (config in
`~/.config/configstore/firebase-tools.json` in this environment).

## 5. Admin dashboard

URL: **https://brianbcyangmusic-pixel.github.io/playlist-downloader/admin.html** (GitHub Pages)
Mirror: https://playlist-downloader-99ad0.web.app

### Login
- **Continue with Google** — use `brian.bcyang27@gmail.com`. All existing
  accounts use Google, so email/password usually won't work unless a password
  was added.
- **Sign in with email** — fallback if the account has a password provider.
- The page checks auth state, signs out anyone who is not the owner
  (`ADMIN_EMAIL` in `docs/admin.js`), and refuses to load data.
- Uses Firebase compat SDK (app/auth/firestore).

### Features
- Overview: totals, status breakdown, tracks downloaded, user count, unread
  errors, recent downloads/errors.
- Downloads: filter by search/status/platform, expandable row (link, UID,
  timings, sample tracks), CSV export.
- Errors: filter unread/reviewed, mark reviewed (single/bulk), CSV export
  (formula injection guarded).
- Users: aggregated from downloads (runs, tracks, platforms, OS, last activity).
- Auto-refresh every 60s when tab visible.

### Safety
- Links use `safeUrl()` (only `http(s)://`).
- CSV cells escaping formulas (`= + - @` prefixed with `'`).
- Reads only; errors marked reviewed update `notified: true` (owner can do this
  per rules).

## 6. App Google sign-in (browser handoff)

Flow (desktop app):
1. User clicks **Sign in with Google** in the app UI (`web/auth.js` calls
   `/api/auth/google`).
2. `app/api.py:_api_auth_google()` creates a short-lived `state`, builds
   `login_url + ?port=<loopback>&state=<state>`, opens browser via
   `app.server.open_browser()`.
3. Browser opens the hosted login page (`docs/login.html`). User clicks
   **Continue with Google**, signs in with Firebase popup, gets `id_token` +
   `refresh_token`, redirects to `http://127.0.0.1:<port>/auth/handoff#<params>`
   (fragment, never sent to server).
4. `app/server.py` serves `/auth/handoff` as `web/handoff.html` (static). That
   page POSTs tokens to `/api/auth/handoff`.
5. `app/api.py:_api_auth_handoff()` validates `state` (consumed, expires after
   900s), calls `app.cloud.adopt_tokens()`, stores session in
   `cloud_session.json`, returns auth state. UI polls `/api/auth/state` until
   signed in.
6. If `login_url` missing/unconfigured, the endpoint returns 501.

If Google sign-in fails in popup with `auth/unauthorized-domain`, add the domain
to Firebase Auth → Settings → Authorized domains.

## 7. Desktop apps (macOS menu bar + Windows tray)

### macOS menu bar app
- Source: `launcher/macos/MenuBarApp.swift` (Swift, Cocoa, no Xcode project).
- Lives in the menu bar (`LSUIElement`), no Dock icon.
- Starts the backend on launch, stops it on quit; shows live status:
  backend running/attached/stopped, port, queue (done/pending/failed), tools
  (yt-dlp, FFmpeg, JS runtime), signed-in account, available update.
- Actions: Open web page, Start/Stop backend, Open data folder, Check for
  updates now, Quit.
- If no Python backend is found, shows setup instructions and copies the
  needed commands to the clipboard.

Build:
```sh
bash scripts/build_macos_app.sh          # bundled backend (PyInstaller)
bash scripts/build_macos_app.sh --dev    # dev build, uses repo .venv
```
Output: `dist/Playlist Downloader.app` and `dist/PlaylistDownloader-macOS.zip`.

### Windows tray app
- Source: `launcher/windows/PlaylistDownloaderTray.csproj` (WinForms, .NET 10).
- Sits in the notification area; menu mirrors the macOS app.
- Installs per-user under `%LOCALAPPDATA%\Programs\PlaylistDownloader` via
  `launcher/windows/install.ps1` (Start Menu shortcut; optional autostart).
- `uninstall.ps1` removes it while keeping settings/downloads.

Build (Windows):
```powershell
.\launcher\windows\build.ps1
```
Output: `dist\PlaylistDownloader-Windows-x64.zip` (tray exe + backend folder +
install/uninstall scripts + README).

### Backend (frozen, both apps)
`scripts/build_backend.py` freezes `app.server` with PyInstaller (`--onedir`).
The launcher apps start this executable instead of requiring Python:
- macOS: `Contents/Resources/backend/PlaylistDownloaderBackend`
- Windows: `backend/PlaylistDownloaderBackend.exe` (+ `_internal/`)

PyInstaller cannot cross-compile: run `build_backend.py` on every OS you ship.
The GitHub Actions workflow does this automatically on tag push.

## 8. Updates / releases

`app/updater.py`:
- Checks `https://api.github.com/repos/<repo>/releases/latest` (repo from config
  `update_repo`, default `brianbcyangmusic-pixel/playlist-downloader`).
- Downloads from allowlisted GitHub hosts only (HTTPS). Asset selection:
  - macOS: `.zip` asset whose name contains `macos` (the zipped `.app`)
  - Windows: `.exe` asset whose name contains `windows` (portable, if present)
- Self-replace requires a frozen build; from source `install()` refuses and
  points to the release page.
- Windows applies update via `--apply-update` in `app/__main__.py`.
- `__version__` in `app/__init__.py` (currently `1.5.0`) drives the comparison.

The GitHub repo is `brianbcyangmusic-pixel/playlist-downloader`. Releases are
published by `.github/workflows/build-apps.yml` on tag push (`v*`).

## 9. Environment / gotchas

- **Single instance**: second launch opens the existing browser tab.
- **Auth gate**: if cloud enabled and not signed in, the web UI shows the auth
  gate (`web/auth.js`). API calls return 401 and trigger re-auth.
- **Telemetry is best-effort**: network failures never block downloads.
- **Google popup blocked**: admin/login pages use `signInWithPopup`; if blocked,
  try a different browser or allow popups.
- **Authorized domains**: Google sign-in on GitHub Pages requires
  `<account>.github.io` in Firebase Auth → Authorized domains.
- **CORS**: app serves everything on localhost; Firebase REST calls go direct.
- **.gitignore** ignores `__pycache__/`, `.firebase/`, `firebase-debug.log`,
  `*.local`, `node_modules/`, `.venv/`.

## 10. Quick verification checklist (dev)

- [ ] `python -m pip install -r requirements.txt`
- [ ] `python -m app.server --no-browser` starts, banner shows URL and tool status
- [ ] Open UI, add a playlist (if tools present) and test download flow
- [ ] `firebase login:list` shows `brian.bcyang27@gmail.com`
- [ ] `firebase deploy --only hosting --project playlist-downloader-99ad0` succeeds
- [ ] `https://brianbcyangmusic-pixel.github.io/playlist-downloader/admin.html` loads, Google sign-in as owner works
- [ ] Desktop app Google sign-in opens the hosted login page and completes handoff
- [ ] Firestore has `downloads`/`errors` populated by a signed-in run
- [ ] Owner can read them in dashboard; non-owner cannot
- [ ] `bash scripts/build_macos_app.sh` produces a working `dist/Playlist Downloader.app`

## 11. Files to touch for common changes

| Change | Files |
|---|---|
| Add API endpoint | `app/api.py` (add route in handler + method) |
| Change telemetry fields | `app/telemetry.py`, check Firestore rules allow new fields |
| Update default repo/version | `app/__init__.py` (`__version__`), `app/updater.py` (`REPO_DEFAULT`) |
| Change dashboard UI | `docs/admin.html`, `docs/admin.js`, redeploy hosting |
| Change app UI | `web/*`, restarts pick up files from `web_dir()` |
| Change auth flow | `app/cloud.py`, `app/api.py`, `web/auth.js`, `docs/login.html` |
| Change Firebase config | `cloud_config.json` (and copy to `Contents/Resources/` if shipping bundle) |
| Change security rules | `firestore.rules`, deploy with `firebase deploy --only firestore:rules` |
| macOS menu bar app | `launcher/macos/MenuBarApp.swift` |
| Windows tray app | `launcher/windows/Program.cs` |
| Backend bundling | `scripts/build_backend.py` |
| Release pipeline | `.github/workflows/build-apps.yml` |

## 12. Current state (for context)

- GitHub Pages: https://brianbcyangmusic-pixel.github.io/playlist-downloader/admin.html (admin dashboard + login)
- Firebase Hosting mirror: https://playlist-downloader-99ad0.web.app
- Owner: `brian.bcyang27@gmail.com` (Google)
- `login_url` points to the GitHub Pages login page
- Firestore has real data (`downloads` / `errors`) visible to the owner
- GitHub account `brianbcyangmusic-pixel` is active again (push works)
- Version: 1.5.0
- macOS menu bar app builds from `launcher/macos/MenuBarApp.swift`
- Windows tray app builds from `launcher/windows/PlaylistDownloaderTray.csproj`
