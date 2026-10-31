"""Metadata tagging and cover-art embedding, for every format we produce.

yt-dlp writes its own tags, but for Spotify/text playlists the accurate
title/artist/album live in our queue rather than in the YouTube video, so we
re-write them here after the download finishes.
"""

from __future__ import annotations

import mimetypes
import urllib.request
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, TXXX
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

_UA = {"User-Agent": "Mozilla/5.0 (compatible; PlaylistDownloader/1.0)"}


def fetch_image(url: str, timeout: int = 30) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _mime_for(data: bytes, fallback: str = "image/jpeg") -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def write_tags(
    path: str | Path,
    *,
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    albumartist: str | None = None,
    track_number: int | None = None,
    date: str | None = None,
    genre: str | None = None,
    cover_url: str | None = None,
    cover_bytes: bytes | None = None,
    comment: str | None = None,
) -> dict[str, Any]:
    """Write tags in place. Returns a small report; never raises on tag failure."""
    path = Path(path)
    if not path.exists():
        return {"ok": False, "error": "file not found"}

    suffix = path.suffix.lower()
    report: dict[str, Any] = {"ok": True, "tagged": False, "cover": False}

    image = cover_bytes or fetch_image(cover_url or "")
    if image and len(image) < 100:
        image = None

    try:
        if suffix == ".mp3":
            _tag_mp3(path, title, artist, album, albumartist, track_number, date, genre, image, comment)
        elif suffix in (".m4a", ".mp4", ".m4b", ".aac"):
            _tag_mp4(path, title, artist, album, albumartist, track_number, date, genre, image, comment)
        elif suffix == ".flac":
            _tag_flac(path, title, artist, album, albumartist, track_number, date, genre, image, comment)
        elif suffix in (".opus", ".ogg"):
            _tag_ogg(path, title, artist, album, albumartist, track_number, date, genre, image, comment)
        else:
            return {"ok": False, "error": f"unsupported format {suffix}"}
        report["tagged"] = True
        report["cover"] = bool(image)
    except Exception as exc:  # keep the file; tagging is best-effort
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


# ---------------------------------------------------------------------------
# Format writers
# ---------------------------------------------------------------------------

def _tag_mp3(path, title, artist, album, albumartist, track_number, date, genre, image, comment):
    try:
        tags = ID3(str(path))
    except Exception:
        tags = ID3()
    if title:
        tags.setall("TIT2", [TIT2(encoding=3, text=[title])])
    if artist:
        tags.setall("TPE1", [TPE1(encoding=3, text=[artist])])
    if albumartist:
        tags.setall("TPE2", [TPE2(encoding=3, text=[albumartist])])
    if album:
        tags.setall("TALB", [TALB(encoding=3, text=[album])])
    if track_number:
        tags.setall("TRCK", [TRCK(encoding=3, text=[str(track_number)])])
    if date:
        tags.setall("TDRC", [TDRC(encoding=3, text=[date])])
    if genre:
        tags.setall("TCON", [TCON(encoding=3, text=[genre])])
    if comment:
        tags.setall("TXXX:comment", [TXXX(encoding=3, desc="comment", text=[comment])])
    if image:
        tags.setall("APIC", [APIC(encoding=3, mime=_mime_for(image), type=3, desc="Cover", data=image)])
    tags.save(str(path), v2_version=3)


def _tag_mp4(path, title, artist, album, albumartist, track_number, date, genre, image, comment):
    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    t = audio.tags
    if title:
        t["\xa9nam"] = [title]
    if artist:
        t["\xa9ART"] = [artist]
    if albumartist:
        t["aART"] = [albumartist]
    if album:
        t["\xa9alb"] = [album]
    if track_number:
        t["trkn"] = [(track_number, 0)]
    if date:
        t["\xa9day"] = [date]
    if genre:
        t["\xa9gen"] = [genre]
    if comment:
        t["\xa9cmt"] = [comment]
    if image:
        fmt = MP4Cover.FORMAT_PNG if image[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(image, imageformat=fmt)]
    audio.save()


def _tag_flac(path, title, artist, album, albumartist, track_number, date, genre, image, comment):
    audio = FLAC(str(path))
    _set_vorbis(audio, title, artist, album, albumartist, track_number, date, genre, comment)
    if image:
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = _mime_for(image)
        pic.desc = "Cover"
        pic.data = image
        audio.add_picture(pic)
    audio.save()


def _tag_ogg(path, title, artist, album, albumartist, track_number, date, genre, image, comment):
    audio = OggOpus(str(path)) if path.suffix.lower() == ".opus" else OggVorbis(str(path))
    _set_vorbis(audio, title, artist, album, albumartist, track_number, date, genre, comment)
    # Opus/Vorbis store cover art as a base64 METADATA_BLOCK_PICTURE comment.
    if image:
        import base64

        pic = Picture()
        pic.type = 3
        pic.mime = _mime_for(image)
        pic.desc = "Cover"
        pic.data = image
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


def _set_vorbis(audio, title, artist, album, albumartist, track_number, date, genre, comment):
    if title:
        audio["title"] = [title]
    if artist:
        audio["artist"] = [artist]
    if albumartist:
        audio["albumartist"] = [albumartist]
    if album:
        audio["album"] = [album]
    if track_number:
        audio["tracknumber"] = [str(track_number)]
    if date:
        audio["date"] = [date]
    if genre:
        audio["genre"] = [genre]
    if comment:
        audio["comment"] = [comment]


# ---------------------------------------------------------------------------
# Reading (used by the folder-vs-playlist comparison)
# ---------------------------------------------------------------------------

def read_tags(path: str | Path) -> dict[str, Any]:
    """Return whatever we can learn about a media file: tags + duration."""
    path = Path(path)
    info: dict[str, Any] = {
        "path": str(path),
        "name": path.stem,
        "title": None,
        "artist": None,
        "album": None,
        "albumartist": None,
        "track_number": None,
        "duration": None,
        "_mtime": path.stat().st_mtime,
    }
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            tags = ID3(str(path))
            info["title"] = _first(tags, "TIT2")
            info["artist"] = _first(tags, "TPE1")
            info["album"] = _first(tags, "TALB")
            info["albumartist"] = _first(tags, "TPE2")
            info["track_number"] = _first(tags, "TRCK")
            if tags.get("TLEN"):
                info["duration"] = float(tags["TLEN"].text[0]) / 1000
        elif suffix in (".m4a", ".mp4", ".m4b", ".aac"):
            audio = MP4(str(path))
            info["duration"] = audio.info.length if audio.info else None
            t = audio.tags or {}
            info["title"] = _first_list(t.get("\xa9nam"))
            info["artist"] = _first_list(t.get("\xa9ART"))
            info["album"] = _first_list(t.get("\xa9alb"))
            info["albumartist"] = _first_list(t.get("aART"))
            trkn = t.get("trkn")
            info["track_number"] = str(trkn[0][0]) if trkn else None
        elif suffix == ".flac":
            audio = FLAC(str(path))
            info["duration"] = audio.info.length if audio.info else None
            _read_vorbis(audio, info)
        elif suffix in (".opus", ".ogg"):
            audio = OggOpus(str(path)) if suffix == ".opus" else OggVorbis(str(path))
            info["duration"] = audio.info.length if audio.info else None
            _read_vorbis(audio, info)
        elif suffix in (".wav",):
            info["duration"] = None
    except Exception as exc:
        info["_error"] = f"{type(exc).__name__}: {exc}"
    return info


def _first(tags, key):
    val = tags.get(key)
    text = getattr(val, "text", None)
    return str(text[0]) if text else None


def _first_list(val):
    if isinstance(val, list) and val:
        return str(val[0])
    return None


def _read_vorbis(audio, info):
    def one(key):
        val = audio.get(key)
        if isinstance(val, list) and val:
            return str(val[0])
        return None

    info["title"] = one("title")
    info["artist"] = one("artist")
    info["album"] = one("album")
    info["albumartist"] = one("albumartist")
    info["track_number"] = one("tracknumber")


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"