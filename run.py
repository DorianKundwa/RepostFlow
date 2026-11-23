"""
run.py - RepostFlow application entry point.

Usage:
    python run.py

The Flask development server binds to localhost only.
Do NOT expose this directly to the internet without a production
WSGI server (Gunicorn / Waitress) and a reverse proxy (Nginx).
"""

import os
import sys
from pathlib import Path

# Ensure the project root is on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import create_app
from app.services.folder_monitor import folder_monitor
from app.services.scheduler_service import scheduler_service
import atexit

app = create_app()

if __name__ == "__main__":
    # Start background folder monitor
    folder_monitor.start(app)
    atexit.register(folder_monitor.stop)

    # Start background task scheduler (APScheduler)
    scheduler_service.start(app)
    atexit.register(scheduler_service.stop)

    # Only bind to localhost in development.
    # Set PORT environment variable to override.
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    print(f"\n  RepostFlow is running at http://localhost:{port}/\n")

    app.run(
        host="127.0.0.1",
        port=port,
        debug=debug,
        # Prevent APScheduler and Watchdog from starting twice
        # when Flask's reloader forks the process.
        use_reloader=False,
    )
