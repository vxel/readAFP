"""Launcher: python run.py, then open http://127.0.0.1:8770."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from readafp.app import create_app

if __name__ == "__main__":
    # debug=True exposes the Werkzeug debugger (remote code execution) to
    # anyone who can reach the port — safe ONLY because this binds loopback.
    # Never widen host= here; production entry points are the Dockerfile
    # (gunicorn) and desktop.py (waitress), both without debug.
    create_app().run(host="127.0.0.1", port=8770, debug=True)
