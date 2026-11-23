"""
wsgi.py - Production WSGI Entry Point for RepostFlow.

Compatible with:
  - Waitress (Windows / Linux)
  - Gunicorn (Linux / VPS)
  - uWSGI

Usage:
  Windows:  python deploy/start_windows.py
  Linux:    gunicorn --workers 4 --threads 2 --bind 127.0.0.1:5000 wsgi:app
"""

import atexit
import os
import sys
from pathlib import Path

# Ensure application root directory is on Python path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app import create_app
from app.services.folder_monitor import folder_monitor
from app.services.scheduler_service import scheduler_service

app = create_app()

# Initialize background services for production worker
folder_monitor.start(app)
atexit.register(folder_monitor.stop)

scheduler_service.start(app)
atexit.register(scheduler_service.stop)

# Standard WSGI aliases
application = app

if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  [RepostFlow Production WSGI] Serving on http://127.0.0.1:{port} (Waitress)\n")
    serve(app, host="127.0.0.1", port=port, threads=8)
