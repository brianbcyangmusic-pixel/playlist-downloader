#!/usr/bin/env python3
"""Build the self-contained backend with PyInstaller.

The launcher apps (macOS menu bar / Windows tray) start this executable instead
of asking the user to install Python. PyInstaller cannot cross-compile, so run
this on every OS you ship: it produces an ``--onedir`` folder because onefile
unpacks ~120 MB to a temp directory on every launch and makes startup slow.

Usage:
    python scripts/build_backend.py [--out build/backend]

Result:
    build/backend/PlaylistDownloaderBackend/            (macOS / Linux)
    build/backend/PlaylistDownloaderBackend.exe + dir   (Windows)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "PlaylistDownloaderBackend"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "build" / "backend"),
                        help="folder that receives the frozen backend")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    work = ROOT / "build" / "pyinstaller"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    sep = os.pathsep  # ':' on macOS/Linux, ';' on Windows
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir",
        "--name", NAME,
        "--distpath", str(out),
        "--workpath", str(work / "work"),
        "--specpath", str(work),
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'web'}{sep}web",
        "--add-data", f"{ROOT / 'cloud_config.json'}{sep}.",
        # yt-dlp loads extractors lazily; freezing only the imported modules
        # would drop most sites.
        "--collect-submodules", "yt_dlp",
        "--collect-data", "yt_dlp",
        "--collect-all", "yt_dlp_ejs",
        "--hidden-import", "mutagen",
        "--hidden-import", "rapidfuzz",
        str(ROOT / "launcher" / "backend_entry.py"),
    ]
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    exe = out / NAME / (NAME + (".exe" if os.name == "nt" else ""))
    if not exe.exists():
        print(f"ERROR: expected {exe} to exist", file=sys.stderr)
        return 1
    print(f"\nBackend ready: {exe}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
