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

Sign in with the owner account (`admin_email` in `cloud_config.json`). It reads
the `downloads` and `errors` collections, so it shows who downloaded which
playlist, track counts, failures, and every reported error. Anyone else who
signs in is refused — see [`firestore.rules`](firestore.rules).

Redeploy after editing `admin/`:

```sh
firebase deploy --only hosting --project playlist-downloader-99ad0
```

Publish the security rules only when they change:

```sh
firebase deploy --only firestore:rules --project playlist-downloader-99ad0
```

