# Playlist Downloader

A modern desktop app to download playlists from YouTube, YouTube Music, and Spotify as MP3/MP4.

## Features
- Download playlists from YouTube Music, YouTube, and Spotify
- Convert to MP3, M4A, Opus, FLAC, or keep original
- Compare local files with playlists
- Auto-updates
- Menu bar app for easy access
- Optional accounts; signed-in runs are logged to Firestore

## Admin dashboard

The owner dashboard lives in [`admin/`](admin/) and is deployed with Firebase
Hosting (same Firebase project the app writes to):

**https://playlist-downloader-99ad0.web.app**

Sign in with **Google** as `brian.bcyang27@gmail.com` (the `admin_email` in
`cloud_config.json`). There is no password on that account — it was created
with Google Sign-In. It reads the `downloads` and `errors` collections, so it
shows who downloaded which playlist, track counts, failures, and every reported
error. Anyone else who signs in is refused — see [`firestore.rules`](firestore.rules).

The same site hosts `login.html`, the browser page the desktop app opens for
**Sign in with Google** (`login_url` in `cloud_config.json`). It hands the
Firebase session back to the app's loopback server, so no secret ships in the app.

Redeploy after editing `admin/`:

```sh
firebase deploy --only hosting --project playlist-downloader-99ad0
```

Publish the security rules only when they change:

```sh
firebase deploy --only firestore:rules --project playlist-downloader-99ad0
```

