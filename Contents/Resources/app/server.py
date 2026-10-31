"""Application entry point.

    python -m app.server            # normal use
    python -m app.server --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from . import APP_NAME, __version__, instance, paths, tools, updater
from .api import AppContext, build_server
from .config import tool_status


def find_free_port(preferred: int) -> int:
    """Use `preferred` if free, otherwise let the OS pick one."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("Could not find a free port.")


def open_browser(url: str) -> bool:
    """Open `url` in the default browser using native commands.

    Python's `webbrowser` shells out to AppleScript on macOS, which fails
    noisily on some Python builds (e.g. 3.14 with no default browser set). The
    platform-native launchers below are more reliable, so try them first and
    only fall back to `webbrowser` as a last resort.
    """
    silent = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url], **silent)
            return True
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        if shutil.which("xdg-open"):
            subprocess.Popen(["xdg-open", url], **silent)
            return True
    except Exception:
        pass
    try:
        return webbrowser.open(url)
    except Exception:
        return False


def banner(port: int, ctx: AppContext) -> None:
    tools = tool_status(ctx.cfg)
    url = f"http://127.0.0.1:{port}/"
    lines = [
        "=" * 62,
        f"  {APP_NAME}  v{__version__}",
        "=" * 62,
        f"  Open in your browser:  {url}",
        f"  Downloads go to:       {ctx.cfg.download_dir()}",
        f"  Settings & data:       {paths.data_dir()}",
        f"  yt-dlp: {'OK' if tools['ytdlp_ok'] else 'MISSING'}"
        f"   ffmpeg: {'OK' if tools['ffmpeg_ok'] else 'MISSING'}",
    ]
    if not tools["ffmpeg_ok"]:
        lines.append("  ! ffmpeg not found - audio conversion needs it.")
        lines.append("    macOS: brew install ffmpeg     Windows: winget install Gyan.FFmpeg")
    lines += [
        "-" * 62,
        "  Press Ctrl+C to quit. Downloads are saved as they finish,",
        "  so it is always safe to close this window.",
        "=" * 62,
    ]
    # One flushed write: when stdout is redirected to a file (as the .app
    # launcher does) it is block-buffered, so the URL would otherwise never
    # appear and the launcher could not discover the port.
    print("\n".join(lines), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="playlist-downloader", description=APP_NAME)
    parser.add_argument("--port", type=int, default=8765, help="preferred HTTP port")
    parser.add_argument("--no-browser", action="store_true", help="do not auto-open a browser")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    args = parser.parse_args(argv)

    # One packaged copy per user. A second launch just shows the running one
    # instead of starting a rival server on the same port (see app/instance.py).
    if not instance.acquire():
        url = instance.running_url(args.port)
        print(f"{APP_NAME} is already running: {url}", flush=True)
        if not args.no_browser:
            open_browser(url)
        return 0

    # Only the copy that owns the mutex cleans up. A second launch can start
    # while the first is still writing a downloaded update beside the app, and
    # a sweep from that second launch would delete the download mid-flight.
    updater.cleanup_stale_update()

    # Managed tools (Windows) must be on PATH before anything looks them up.
    tools.ensure_path()

    ctx = AppContext()
    port = find_free_port(args.port)
    server = build_server(ctx, host=args.host, port=port)
    instance.publish(port)

    banner(port, ctx)

    if not args.no_browser:
        url = f"http://127.0.0.1:{port}/"
        threading.Thread(target=lambda: (time.sleep(0.6), open_browser(url)),
                         daemon=True).start()

    try:
        # The Quit button in the web UI sets ctx.shutdown; the server keeps
        # serving until something breaks the loop, so watch for that event.
        threading.Thread(
            target=lambda: (ctx.shutdown.wait(), server.shutdown()),
            daemon=True, name="quit-watch",
        ).start()

        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        # Stop the workers first: stopping the manager emits run_finished, and
        # the telemetry thread must still be alive to record it.
        ctx.manager.stop()
        ctx.telemetry.stop()
        ctx.shutdown.set()
        server.shutdown()
        server.server_close()
        ctx.db.close()
        instance.clear()
        print("Goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())