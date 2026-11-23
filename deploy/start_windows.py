"""
deploy/start_windows.py - Production WSGI runner for Windows host using Waitress.

Runs RepostFlow using multi-threaded Waitress WSGI server bound to localhost.
For external access, configure a reverse proxy such as Nginx, Caddy, or IIS.
"""

import os
import sys
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from wsgi import app
from waitress import serve


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", 5000))
    threads = int(os.environ.get("WAITRESS_THREADS", 8))

    print("=" * 60)
    print("  RepostFlow Production Server (Windows / Waitress)")
    print("=" * 60)
    print(f"  Address:   http://{host}:{port}/")
    print(f"  Threads:   {threads}")
    print(f"  Env:       {app.config.get('ENV', 'production')}")
    print("=" * 60)
    print("  Press Ctrl+C to stop the server.\n")

    serve(app, host=host, port=port, threads=threads, channel_timeout=120)


if __name__ == "__main__":
    main()
