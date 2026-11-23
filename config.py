"""
config.py - RepostFlow Application Configuration

Loads settings from environment variables (via .env).
Three configuration classes:
  - BaseConfig:       shared defaults
  - DevelopmentConfig: local development (default)
  - ProductionConfig:  VPS / production deployment
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (same directory as this file).
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class BaseConfig:
    # ------------------------------------------------------------------ #
    # Flask Core                                                           #
    # ------------------------------------------------------------------ #
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-insecure-key-change-me")
    if SECRET_KEY == "dev-insecure-key-change-me":
        import warnings
        warnings.warn(
            "SECRET_KEY is not set. Using an insecure default. "
            "Set SECRET_KEY in your .env file before running in production.",
            stacklevel=2,
        )

    # ------------------------------------------------------------------ #
    # Database                                                             #
    # ------------------------------------------------------------------ #
    # SQLite default keeps the database inside /instance/ (not in source).
    _default_db = f"sqlite:///{(BASE_DIR / 'instance' / 'repostflow.db').as_posix()}"
    SQLALCHEMY_DATABASE_URI: str = os.environ.get("DATABASE_URL", _default_db)
    SQLALCHEMY_TRACK_MODIFICATIONS: bool = False
    # Enables FK enforcement for SQLite. Ignored by PostgreSQL (always on).
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "connect_args": {"check_same_thread": False},
    }

    # ------------------------------------------------------------------ #
    # Google OAuth / YouTube API                                           #
    # ------------------------------------------------------------------ #
    GOOGLE_CLIENT_SECRETS_FILE: str = os.environ.get(
        "GOOGLE_CLIENT_SECRETS_FILE",
        str(BASE_DIR / "storage" / "credentials" / "client_secrets.json"),
    )
    OAUTH_REDIRECT_URI: str = os.environ.get(
        "OAUTH_REDIRECT_URI", "http://localhost:5000/oauth/callback"
    )
    # Scopes requested during OAuth. Least privilege: only what is needed.
    YOUTUBE_SCOPES: list = [
        "https://www.googleapis.com/auth/youtube.readonly",   # read channel/video data
        "https://www.googleapis.com/auth/youtube.upload",     # upload videos
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]

    # ------------------------------------------------------------------ #
    # Token Encryption                                                     #
    # ------------------------------------------------------------------ #
    # Fernet key used to encrypt OAuth tokens at rest in the database.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    TOKEN_ENCRYPTION_KEY: str = os.environ.get("TOKEN_ENCRYPTION_KEY", "")

    # ------------------------------------------------------------------ #
    # Paths                                                                #
    # ------------------------------------------------------------------ #
    BASE_DIR: Path = BASE_DIR
    STORAGE_DIR: Path = BASE_DIR / "storage"
    VIDEOS_DIR: Path = STORAGE_DIR / "videos"
    THUMBNAILS_DIR: Path = STORAGE_DIR / "thumbnails"
    TEMP_DIR: Path = STORAGE_DIR / "temp"
    CREDENTIALS_DIR: Path = STORAGE_DIR / "credentials"
    LOGS_DIR: Path = BASE_DIR / "logs"

    # ------------------------------------------------------------------ #
    # Upload Settings                                                      #
    # ------------------------------------------------------------------ #
    MAX_CONCURRENT_UPLOADS: int = int(os.environ.get("MAX_CONCURRENT_UPLOADS", 2))
    DEFAULT_PRIVACY: str = os.environ.get("DEFAULT_PRIVACY", "private")
    MAX_UPLOAD_RETRIES: int = int(os.environ.get("MAX_UPLOAD_RETRIES", 3))

    # ------------------------------------------------------------------ #
    # Scheduler / Monitoring                                               #
    # ------------------------------------------------------------------ #
    YOUTUBE_POLL_INTERVAL: int = int(os.environ.get("YOUTUBE_POLL_INTERVAL", 300))
    FOLDER_STABILITY_WAIT: int = int(os.environ.get("FOLDER_STABILITY_WAIT", 10))

    # ------------------------------------------------------------------ #
    # Logging                                                              #
    # ------------------------------------------------------------------ #
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    # ------------------------------------------------------------------ #
    # Security                                                             #
    # ------------------------------------------------------------------ #
    WTF_CSRF_ENABLED: bool = True
    SESSION_COOKIE_HTTPONLY: bool = True
    SESSION_COOKIE_SAMESITE: str = "Lax"

    # ------------------------------------------------------------------ #
    # Supported video extensions                                           #
    # ------------------------------------------------------------------ #
    ALLOWED_VIDEO_EXTENSIONS: set = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


class DevelopmentConfig(BaseConfig):
    DEBUG: bool = True
    SESSION_COOKIE_SECURE: bool = False  # HTTP is fine locally
    # More verbose SQL logging in development
    SQLALCHEMY_ECHO: bool = False


class ProductionConfig(BaseConfig):
    DEBUG: bool = False
    SESSION_COOKIE_SECURE: bool = True   # Requires HTTPS
    WTF_CSRF_SSL_STRICT: bool = True


# Map string names to config classes for easy switching.
config_map: dict = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}


def get_config() -> BaseConfig:
    """Return the config class selected by FLASK_ENV (default: development)."""
    env = os.environ.get("FLASK_ENV", "development").lower()
    return config_map.get(env, DevelopmentConfig)
