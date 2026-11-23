"""
app/__init__.py - Application factory for RepostFlow.

Creates and configures the Flask application, registers extensions,
blueprints, and sets up logging and storage directories.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask

from config import get_config
from app.extensions import db, login_manager, csrf


def create_app(config_override=None) -> Flask:
    """
    Application factory.

    Args:
        config_override: Optional config class to use instead of the
                         environment-selected one. Useful in tests.

    Returns:
        Configured Flask application instance.
    """
    app = Flask(__name__, instance_relative_config=False)

    # ---------------------------------------------------------------- #
    # Configuration                                                      #
    # ---------------------------------------------------------------- #
    cfg = config_override or get_config()
    app.config.from_object(cfg)

    # ---------------------------------------------------------------- #
    # Ensure required directories exist                                  #
    # ---------------------------------------------------------------- #
    _ensure_directories(app)

    # ---------------------------------------------------------------- #
    # Logging                                                            #
    # ---------------------------------------------------------------- #
    _configure_logging(app)

    # ---------------------------------------------------------------- #
    # Extensions                                                         #
    # ---------------------------------------------------------------- #
    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Flask-Login user loader
    from app.models import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    # ---------------------------------------------------------------- #
    # Blueprints                                                         #
    # ---------------------------------------------------------------- #
    _register_blueprints(app)

    # ---------------------------------------------------------------- #
    # Database initialisation                                            #
    # ---------------------------------------------------------------- #
    with app.app_context():
        db.create_all()
        _seed_default_settings(app)

    app.logger.info("RepostFlow started (env=%s)", os.environ.get("FLASK_ENV", "development"))
    return app


# ------------------------------------------------------------------ #
# Private helpers                                                     #
# ------------------------------------------------------------------ #

def _ensure_directories(app: Flask) -> None:
    """Create storage and log directories if they do not exist."""
    dirs = [
        app.config["STORAGE_DIR"],
        app.config["VIDEOS_DIR"],
        app.config["THUMBNAILS_DIR"],
        app.config["TEMP_DIR"],
        app.config["CREDENTIALS_DIR"],
        app.config["LOGS_DIR"],
        app.config["BASE_DIR"] / "instance",
    ]
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)


def _configure_logging(app: Flask) -> None:
    """Set up console + rotating-file logging."""
    log_level = getattr(logging, app.config.get("LOG_LEVEL", "INFO"), logging.INFO)
    log_file = Path(app.config["LOGS_DIR"]) / "repostflow.log"

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler (10 MB per file, keep 5)
    file_handler = RotatingFileHandler(log_file, maxBytes=10_485_760, backupCount=5)
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)

    root = logging.getLogger()
    root.setLevel(log_level)
    # Avoid duplicate handlers when Flask reloader restarts the process.
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console_handler)

    app.logger.setLevel(log_level)


def _register_blueprints(app: Flask) -> None:
    """Import and register all route blueprints."""
    from app.routes.dashboard import dashboard_bp
    from app.routes.channels import channels_bp
    from app.routes.videos import videos_bp
    from app.routes.automation import automation_bp
    from app.routes.uploads import uploads_bp
    from app.routes.settings import settings_bp
    from app.routes.auth import auth_bp
    from app.routes.oauth import oauth_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(channels_bp, url_prefix="/channels")
    app.register_blueprint(videos_bp, url_prefix="/videos")
    app.register_blueprint(automation_bp, url_prefix="/automation")
    app.register_blueprint(uploads_bp, url_prefix="/uploads")
    app.register_blueprint(settings_bp, url_prefix="/settings")
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(oauth_bp, url_prefix="/oauth")


def _seed_default_settings(app: Flask) -> None:
    """
    Insert default ApplicationSetting rows on first run.
    Uses INSERT OR IGNORE semantics: safe to call on every startup.
    """
    from app.models import ApplicationSetting

    defaults = [
        ("app_name", "RepostFlow", "Application display name", "string"),
        ("default_privacy", app.config.get("DEFAULT_PRIVACY", "private"), "Default upload privacy", "string"),
        ("max_concurrent_uploads", str(app.config.get("MAX_CONCURRENT_UPLOADS", 2)), "Max simultaneous uploads", "int"),
        ("youtube_poll_interval", str(app.config.get("YOUTUBE_POLL_INTERVAL", 300)), "YouTube poll interval (seconds)", "int"),
        ("auto_copy_metadata", "true", "Automatically copy metadata when reposting", "bool"),
        ("auto_copy_thumbnail", "true", "Automatically copy thumbnail when reposting", "bool"),
        ("max_upload_retries", "3", "Maximum upload retry attempts", "int"),
        ("log_level", app.config.get("LOG_LEVEL", "INFO"), "Logging level", "string"),
    ]

    for key, value, description, vtype in defaults:
        existing = ApplicationSetting.query.filter_by(key=key).first()
        if existing is None:
            db.session.add(ApplicationSetting(
                key=key, value=value, description=description, value_type=vtype
            ))

    db.session.commit()
