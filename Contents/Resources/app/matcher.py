"""Compare a local folder against a playlist and report what is missing.

Matching is intentionally forgiving, because real-world names are messy:

    playlist: "Daft Punk - Get Lucky (feat. Pharrell Williams)"
    file:     "03 - Get Lucky.mp3"   (tagged artist "Daft Punk")

How it stays fast at scale
--------------------------
A naive implementation compares every track against every file. With 1,500
tracks and 3,000 files that is 4.5 million fuzzy comparisons and takes minutes.

Instead:

1. Normalise both sides once (accent folding, bracket stripping, "feat." noise).
2. Pass 1 — exact normalised lookups. When the app downloaded the files itself
   this resolves the vast majority in O(1).
3. Pass 2 — token inverted index. Only files sharing a word with the track are
   considered, ranked by overlap, and the top few hundred are fuzzy scored.
4. Greedy best-first assignment, so one file satisfies at most one track.

`rapidfuzz` (a C extension) is used when present and the pure-Python
`difflib` otherwise, so the app works either way.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import tagging

try:  # optional accelerator; ~50x faster than difflib
    from rapidfuzz import fuzz as _rf

    def _ratio(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        return _rf.ratio(a, b) / 100.0

    def _soft_ratio(a: str, b: str) -> float:
        """Ratio that tolerates extra words (channel junk, "Official Video")."""
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        return _rf.token_set_ratio(a, b) / 100.0

except ImportError:  # pragma: no cover - fallback path
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        return SequenceMatcher(None, a, b).ratio()

    def _soft_ratio(a: str, b: str) -> float:
        return _ratio(a, b)


AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".opus", ".ogg", ".wav", ".wma", ".aiff"}
DEFAULT_THRESHOLD = 0.72
MAX_FUZZY_CANDIDATES = 150

# Words that carry no identity and only add noise.
_NOISE = {
    "feat", "ft", "featuring", "with", "prod", "remix", "remaster", "remastered",
    "official", "video", "videos", "audio", "lyrics", "lyric", "hd", "hq", "4k",
    "mv", "m", "v", "music", "the", "a", "an", "and", "or", "of", "to", "in",
    "full", "song", "album", "version", "explicit", "clean", "clip", "visualizer",
    "audioonly", "topic", "vevo", "records", "recordings", "tv", "live",
}

_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_WS = re.compile(r"\s+")
_LEADING_TRACK = re.compile(r"^\s*\d{1,4}\s*[-._)\]]\s*")
_NORM_CACHE: dict[str, str] = {}


def normalize(text: str, keep_noise: bool = False) -> str:
    """Aggressively normalise a title for comparison."""
    if not text:
        return ""
    key = text if keep_noise else "\x00" + text
    cached = _NORM_CACHE.get(key)
    if cached is not None:
        return cached

    out = unicodedata.normalize("NFKD", text)
    out = "".join(c for c in out if not unicodedata.combining(c))
    out = out.lower()
    out = _BRACKETS.sub(" ", out)
    out = out.replace("&", " and ")
    out = _PUNCT.sub(" ", out)
    out = _WS.sub(" ", out).strip()
    if not keep_noise:
        out = " ".join(t for t in out.split() if t not in _NOISE).strip()

    if len(_NORM_CACHE) > 200_000:      # keep the cache bounded
        _NORM_CACHE.clear()
    _NORM_CACHE[key] = out
    return out


def tokens_of(text: str) -> set[str]:
    return {t for t in normalize(text).split() if len(t) >= 2}


def _token_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    return len(inter) / min(len(a), len(b))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class FileRecord:
    path: str
    name: str
    title: str | None
    artist: str | None
    album: str | None
    duration: float | None
    norm_title: str = ""
    norm_artist: str = ""
    norm_combined: str = ""
    tokens: set[str] = field(default_factory=set)
    cand_norms: list[str] = field(default_factory=list)
    error: str | None = None

    def finalize(self) -> None:
        """Precompute everything the scorer needs, exactly once."""
        self.norm_title = normalize(self.title or self.name)
        self.norm_artist = normalize(self.artist or "")
        self.norm_combined = normalize(f"{self.artist or ''} {self.title or self.name}")
        self.tokens = tokens_of(f"{self.artist or ''} {self.title or self.name}")
        seen: list[str] = []
        for cand in self.candidates():
            n = normalize(cand)
            if n and n not in seen:
                seen.append(n)
        self.cand_norms = seen

    def candidates(self) -> list[str]:
        out = []
        if self.artist and self.title:
            out.append(f"{self.artist} {self.title}")
            out.append(f"{self.title} {self.artist}")
        if self.title:
            out.append(self.title)
        out.append(self.name)
        return out


def scan_folder(folder: str, extensions: set[str] | None = None,
                recursive: bool = True, read_tags: bool = True,
                progress=None) -> list[FileRecord]:
    """Collect audio files and their metadata (tags preferred, filename fallback)."""
    exts = extensions or AUDIO_EXTS
    root = Path(folder).expanduser()
    if not root.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")

    walker = root.rglob("*") if recursive else root.glob("*")
    records: list[FileRecord] = []
    for path in walker:
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        rec = FileRecord(path=str(path), name=path.stem, title=None, artist=None,
                         album=None, duration=None)
        if read_tags:
            try:
                meta = tagging.read_tags(path)
                rec.title = meta.get("title")
                rec.artist = meta.get("artist") or meta.get("albumartist")
                rec.album = meta.get("album")
                rec.duration = meta.get("duration")
                rec.error = meta.get("_error")
            except Exception as exc:
                rec.error = f"{type(exc).__name__}: {exc}"

        # Fall back to the filename when tags are absent or partial.
        if not rec.title or not rec.artist:
            guess_artist, guess_title = parse_filename(path.stem)
            if not rec.title:
                rec.title = guess_title
            if not rec.artist:
                rec.artist = guess_artist

        rec.finalize()
        records.append(rec)
        if progress and len(records) % 250 == 0:
            progress(len(records))

    return records


def parse_filename(stem: str) -> tuple[str, str]:
    """Split 'Artist - Title' style filenames. Returns (artist, title)."""
    stem = _LEADING_TRACK.sub("", stem)
    stem = _BRACKETS.sub(" ", stem).strip()
    for sep in (" - ", " – ", " — ", "_-_", "-"):
        if sep in stem:
            left, _, right = stem.partition(sep)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
    return "", stem.strip()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class _TrackKey:
    """Precomputed normalised form of a playlist track."""
    norm_full: str
    norm_title: str
    norm_artist: str
    tokens: set[str]


def _track_key(track: dict) -> _TrackKey:
    title = track.get("title") or ""
    artist = track.get("artist") or ""
    return _TrackKey(
        norm_full=normalize(f"{artist} {title}"),
        norm_title=normalize(title),
        norm_artist=normalize(artist),
        tokens=tokens_of(f"{artist} {title}"),
    )


_GENERIC_ARTISTS = {
    "various artists", "various", "va", "v a", "unknown", "unknown artist",
    "n a", "none", "soundtrack", "ost", "original soundtrack", "compilation",
}


def _is_generic_artist(norm_artist: str) -> bool:
    return not norm_artist or norm_artist in _GENERIC_ARTISTS


def _score(key: _TrackKey, rec: FileRecord, duration: float | None) -> float:
    best = 0.0
    for cand in rec.cand_norms:
        if cand == key.norm_full:
            best = 1.0
            break
        best = max(best, _ratio(key.norm_full, cand))
        # `token_set_ratio` tolerates extra junk words, but it reports ~100%
        # when one string is merely a subset of the other, so scale it by how
        # similar the lengths are. Without this, "Hold On" matches
        # "Artist One Hold On".
        soft = _soft_ratio(key.norm_full, cand)
        if soft:
            la, lb = len(key.norm_full), len(cand)
            length_ratio = min(la, lb) / max(la, lb, 1)
            best = max(best, soft * (0.55 + 0.45 * length_ratio) * 0.95)

    # "Various Artists" style tags carry no identity, so treat them as absent.
    both_artists = not _is_generic_artist(key.norm_artist) and \
        not _is_generic_artist(rec.norm_artist)
    artist_sim = (
        max(_ratio(key.norm_artist, rec.norm_artist),
            _soft_ratio(key.norm_artist, rec.norm_artist))
        if both_artists else 0.0
    )
    # Two clearly different, named artists mean two different recordings. Every
    # signal above is blind to that: the combined-string ratio and the shared
    # tokens both reward a generic shared title like "Hold On". So fall back to
    # the title alone and hard-cap it below the default threshold. Duration must
    # not rescue it either, so these surface as "missing" for a human to review.
    if both_artists and artist_sim < 0.4:
        title_sim = _ratio(key.norm_title, rec.norm_title) \
            if (key.norm_title and rec.norm_title) else 0.0
        return round(title_sim * 0.68, 4)

    if key.norm_title and rec.norm_title:
        title_sim = _ratio(key.norm_title, rec.norm_title)
        if artist_sim > 0.8 or not both_artists:
            best = max(best, 0.5 + 0.5 * title_sim)
        else:
            best = max(best, title_sim * 0.85)

    overlap = _token_overlap(key.tokens, rec.tokens)
    if overlap:
        best = max(best, overlap * 0.95)

    # Duration is a tie-breaker, never a rescue for a weak name match.
    if duration and rec.duration and best >= 0.6:
        delta = abs(duration - rec.duration)
        if delta <= 3:
            best = min(1.0, best + 0.10)
        elif delta <= 8:
            best = min(1.0, best + 0.04)
        elif delta > 45:
            best = max(0.0, best - 0.25)
    return round(best, 4)


@dataclass
class MatchResult:
    matched: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    extra: list[dict] = field(default_factory=list)
    threshold: float = DEFAULT_THRESHOLD
    counts: dict = field(default_factory=dict)


class _Index:
    """Token inverted index plus a 4-char prefix index for typo tolerance.

    Both are built once, so candidate lookup is cheap regardless of folder size.
    """

    def __init__(self, records: list[FileRecord]) -> None:
        self.token_index: dict[str, list[int]] = {}
        self.prefix_index: dict[str, list[str]] = {}
        self.exact: dict[str, int] = {}
        for i, rec in enumerate(records):
            for tok in rec.tokens:
                self.token_index.setdefault(tok, []).append(i)
                if len(tok) >= 5:
                    self.prefix_index.setdefault(tok[:4], []).append(tok)
            for cand in rec.cand_norms:
                self.exact.setdefault(cand, i)

    def top_candidates(self, tokens: set[str],
                       limit: int = MAX_FUZZY_CANDIDATES) -> list[int]:
        """Rank files by how many meaningful words they share with the track."""
        if not tokens:
            return []
        counts: dict[int, float] = {}
        for tok in tokens:
            for fi in self.token_index.get(tok, ()):
                counts[fi] = counts.get(fi, 0) + 1
        # Probe 4-char prefixes so typos / suffixes still find candidates.
        # Buckets are tiny, so this stays cheap.
        seen_tokens: set[str] = set()
        for tok in tokens:
            if len(tok) < 5:
                continue
            for full in self.prefix_index.get(tok[:4], ()):
                if full in tokens or full in seen_tokens:
                    continue
                seen_tokens.add(full)
                for fi in self.token_index.get(full, ()):
                    counts[fi] = counts.get(fi, 0) + 0.4
        if not counts:
            return []
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [fi for fi, _ in ranked[:limit]]


def compare_playlist_to_folder(
    tracks: list[dict],
    folder: str,
    threshold: float = DEFAULT_THRESHOLD,
    recursive: bool = True,
    read_tags: bool = True,
    progress=None,
) -> MatchResult:
    """tracks: list of {'title','artist','album','duration'} (from playlist.py)."""
    if progress:
        progress(0, f"Scanning {folder}…")
    records = scan_folder(folder, recursive=recursive, read_tags=read_tags,
                          progress=lambda n: progress and progress(0, f"Read {n} files…"))
    if progress:
        progress(0, f"Scanned {len(records)} files. Matching…")

    keys = [_track_key(t) for t in tracks]
    index = _Index(records)

    used_files: set[int] = set()
    assigned: dict[int, tuple[int, float]] = {}

    # --- Pass 1: exact matches (O(1) each) --------------------------------
    for ti, key in enumerate(keys):
        for probe in (key.norm_full, key.norm_title):
            if not probe:
                continue
            fi = index.exact.get(probe)
            if fi is not None and fi not in used_files:
                used_files.add(fi)
                assigned[ti] = (fi, 1.0)
                break

    # --- Pass 2: fuzzy scoring on a small candidate set -------------------
    pairs: list[tuple[float, int, int]] = []
    for ti, key in enumerate(keys):
        if ti in assigned:
            continue
        for fi in index.top_candidates(key.tokens):
            if fi in used_files:
                continue
            s = _score(key, records[fi], tracks[ti].get("duration"))
            if s >= threshold * 0.6:
                pairs.append((s, ti, fi))

    pairs.sort(key=lambda p: p[0], reverse=True)
    for score, ti, fi in pairs:
        if ti in assigned or fi in used_files:
            continue
        used_files.add(fi)
        assigned[ti] = (fi, score)

    # --- Build the report -------------------------------------------------
    result = MatchResult(threshold=threshold)
    pending_missing: list[tuple[dict, int, _TrackKey]] = []
    for ti, track in enumerate(tracks):
        entry = {
            "index": ti,
            "title": track.get("title", ""),
            "artist": track.get("artist", ""),
            "album": track.get("album", ""),
            "duration": track.get("duration"),
            "url": track.get("url", ""),
        }
        if ti in assigned:
            fi, score = assigned[ti]
            entry.update({
                "score": score,
                "file": records[fi].path,
                "file_name": records[fi].name,
                "file_title": records[fi].title,
                "file_artist": records[fi].artist,
            })
            if score >= threshold:
                result.matched.append(entry)
                continue
            entry["note"] = "weak match"
        result.missing.append(entry)
        pending_missing.append((entry, ti, keys[ti]))

    # Closest-on-disk hints, only for genuinely missing tracks.
    if progress:
        progress(0, "Looking for near misses…")
    for entry, ti, key in pending_missing:
        best_score, best_rec = 0.0, None
        for fi in index.top_candidates(key.tokens, limit=60):
            s = _score(key, records[fi], tracks[ti].get("duration"))
            if s > best_score:
                best_score, best_rec = s, records[fi]
        if best_rec is not None and best_score > 0.4:
            entry["closest"] = {
                "file": best_rec.path, "name": best_rec.name,
                "score": round(best_score, 3),
            }

    for fi, rec in enumerate(records):
        if fi not in used_files:
            result.extra.append({
                "file": rec.path, "name": rec.name, "title": rec.title,
                "artist": rec.artist, "album": rec.album,
            })

    result.counts = {
        "playlist": len(tracks),
        "folder": len(records),
        "matched": len(result.matched),
        "missing": len(result.missing),
        "extra": len(result.extra),
    }
    if progress:
        progress(0, "Done.")
    return result
