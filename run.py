"""Launcher: python run.py, then open http://127.0.0.1:8770.

Set READAFP_DEBUG=1 to enable the Werkzeug reloader/debugger for local
development.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from readafp.app import create_app

if __name__ == "__main__":
    # The Werkzeug debugger is a remote-code-execution console for anyone who
    # can reach the port, so it stays OFF by default and is opt-in via
    # READAFP_DEBUG=1 — never shipped on just because the file was copied.
    # Even then this binds loopback only; never widen host= here. Production
    # entry points are the Dockerfile (gunicorn) and desktop.py (waitress),
    # both without debug.
    debug = os.environ.get("READAFP_DEBUG") == "1"
    create_app().run(host="127.0.0.1", port=8770, debug=debug)
