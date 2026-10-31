"""Entry point so the app can be launched three ways:

    python -m app                 # package form
    python -m app.server          # module form
    PyInstaller bundle            # frozen executable

All of them funnel into `app.server.main`.
"""

from __future__ import annotations

import sys


def _bind_std_streams() -> None:
    """A windowed (no-console) Windows build starts with stdout/stderr = None.

    Anything that prints would then raise ``AttributeError: 'NoneType' object
    has no attribute 'write'``, including the startup banner and any traceback.
    Point both at the app's log file instead so the packaged build fails
    quietly rather than instantly.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from app import paths

        log_path = paths.data_dir() / "app.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover - last-resort safety
        import io

        stream = io.StringIO()
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_bind_std_streams()

from app import updater  # noqa: E402  (must follow the stream guard)

# Windows self-update: the freshly downloaded exe runs with --apply-update and
# swaps itself over the old build before any server work happens.
_apply = updater.apply_update(sys.argv[1:])
if _apply is not None:
    raise SystemExit(_apply)

from app.server import main  # noqa: E402  (must follow the stream guard)

if __name__ == "__main__":
    raise SystemExit(main())
