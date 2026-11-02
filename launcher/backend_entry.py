"""Entry point used by PyInstaller to build the bundled backend executable.

Keeping this file tiny means PyInstaller has one obvious script to freeze; the
real work still lives in :mod:`app.server` so running from source and running
from the bundle take exactly the same code path.
"""

from app.server import main

if __name__ == "__main__":
    raise SystemExit(main())
