"""readAFP desktop launcher.

Runs readAFP locally and opens it in your browser. Your AFP files are
processed on your own machine and never leave it — this is the build that
ships as the standalone .exe for environments that block uploads.

Built into a single Windows executable with PyInstaller (see
.github/workflows/release.yml). For local runs, the package is on sys.path
via ``src``; when frozen, PyInstaller bundles it.
"""

import os
import sys
import threading
import webbrowser
from pathlib import Path

# When running from source (not frozen), put src/ on the path.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).parent / "src"))

from readafp.app import create_app  # noqa: E402


def main() -> None:
    port = int(os.environ.get("READAFP_PORT", "8770"))
    url = f"http://127.0.0.1:{port}/"
    app = create_app()

    if os.environ.get("READAFP_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"\n  readAFP is running at {url}")
    print("  Your files stay on this computer. Close this window to stop.\n")

    # waitress: a small, pure-Python production WSGI server that works on
    # Windows (gunicorn does not). Bundles cleanly into the .exe.
    from waitress import serve

    serve(app, host="127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    main()
