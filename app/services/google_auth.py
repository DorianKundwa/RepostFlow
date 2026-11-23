"""
app/services/google_auth.py - Google OAuth 2.0 service for RepostFlow.

Responsibilities:
  - Build the OAuth authorisation URL.
  - Exchange the authorisation code for credentials.
  - Encrypt and decrypt OAuth tokens for database storage.
  - Refresh expired tokens.
  - Return a google-auth Credentials object ready for API calls.
  - Validate the OAuth state parameter to prevent CSRF.

Google OAuth notes:
  - RepostFlow requests incremental authorisation per channel.
  - Each Google account that owns (or manages) a YouTube channel
    requires its own OAuth grant.
  - Brand Account channels appear as separate channels under the
    same Google account; the user must choose which channel to use
    after granting access.
  - Scopes are defined in config.YOUTUBE_SCOPES (least-privilege).
  - During development, the OAuth consent screen must be in
    "Testing" mode and your Google account must be listed as a
    test user. Google will show an "unverified app" warning.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, session, url_for
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# Encryption helpers                                                  #
# ------------------------------------------------------------------ #

def _get_fernet() -> Optional[Fernet]:
    """
    Return a Fernet instance using TOKEN_ENCRYPTION_KEY from config.
    Returns None if no key is configured (tokens stored unencrypted
    with a warning — only acceptable in local dev with no real tokens).
    """
    key = current_app.config.get("TOKEN_ENCRYPTION_KEY", "")
    if not key:
        logger.warning(
            "TOKEN_ENCRYPTION_KEY is not set. "
            "OAuth tokens will be stored unencrypted. "
            "Set TOKEN_ENCRYPTION_KEY in your .env before connecting real channels."
        )
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:
        logger.error("Invalid TOKEN_ENCRYPTION_KEY: %s", exc)
        return None


def encrypt_credentials(creds_dict: dict) -> str:
    """
    Serialise and encrypt a credentials dictionary.
    Returns a string safe to store in the database.
    If encryption is unavailable, returns JSON plaintext (dev only).
    """
    payload = json.dumps(creds_dict).encode()
    fernet = _get_fernet()
    if fernet:
        return fernet.encrypt(payload).decode()
    # No encryption key — store as plain JSON (dev only, warning already logged)
    return payload.decode()


def decrypt_credentials(stored: str) -> Optional[dict]:
    """
    Decrypt a stored credentials string back to a dict.
    Returns None on failure.
    """
    if not stored:
        return None
    fernet = _get_fernet()
    try:
        if fernet:
            payload = fernet.decrypt(stored.encode())
        else:
            payload = stored.encode()
        return json.loads(payload)
    except (InvalidToken, json.JSONDecodeError, Exception) as exc:
        logger.error("Failed to decrypt credentials: %s", exc)
        return None


# ------------------------------------------------------------------ #
# OAuth flow                                                          #
# ------------------------------------------------------------------ #

def _build_flow(state: Optional[str] = None) -> Flow:
    """
    Construct a google_auth_oauthlib Flow from the client secrets file.
    """
    secrets_file = current_app.config["GOOGLE_CLIENT_SECRETS_FILE"]
    redirect_uri = current_app.config["OAUTH_REDIRECT_URI"]
    scopes = current_app.config["YOUTUBE_SCOPES"]

    if not os.path.isfile(secrets_file):
        raise FileNotFoundError(
            f"Google OAuth client secrets file not found: {secrets_file}\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials "
            "and place it at the path specified by GOOGLE_CLIENT_SECRETS_FILE in .env"
        )

    flow = Flow.from_client_secrets_file(
        secrets_file,
        scopes=scopes,
        redirect_uri=redirect_uri,
        state=state,
    )
    return flow


def build_authorisation_url() -> Tuple[str, str]:
    """
    Build a Google OAuth authorisation URL and return (url, state).
    The state token is a CSRF guard; store it in the Flask session
    before redirecting the user.
    """
    state = secrets.token_urlsafe(32)
    flow = _build_flow()

    auth_url, _ = flow.authorization_url(
        access_type="offline",       # Request a refresh token
        include_granted_scopes="true",
        prompt="consent",            # Always show consent screen to get refresh_token
        state=state,
    )
    return auth_url, state


def exchange_code_for_credentials(
    authorisation_response_url: str,
    state: str,
) -> Credentials:
    """
    Exchange the OAuth callback URL for a Credentials object.

    Args:
        authorisation_response_url: The full callback URL including
                                    the ``code`` and ``state`` params.
        state: The state value returned in the callback (must match
               the value stored in the session).

    Returns:
        google.oauth2.credentials.Credentials

    Raises:
        ValueError: If the state does not match (CSRF protection).
        Exception: If the token exchange fails.
    """
    flow = _build_flow(state=state)

    # Disable HTTPS requirement for localhost development.
    # In production this must be removed and HTTPS enforced.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    flow.fetch_token(authorization_response=authorisation_response_url)
    return flow.credentials


def credentials_to_dict(creds: Credentials) -> dict:
    """Serialise a Credentials object to a plain dict for storage."""
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def dict_to_credentials(creds_dict: dict) -> Credentials:
    """Reconstruct a Credentials object from a stored dict."""
    expiry = None
    if creds_dict.get("expiry"):
        try:
            expiry = datetime.fromisoformat(creds_dict["expiry"])
            # google.auth.credentials requires a NAIVE UTC datetime for expiry.
            # If fromisoformat produced an aware datetime, convert to UTC and strip tzinfo.
            if expiry.tzinfo is not None:
                expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            pass

    return Credentials(
        token=creds_dict.get("token"),
        refresh_token=creds_dict.get("refresh_token"),
        token_uri=creds_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=creds_dict.get("client_id"),
        client_secret=creds_dict.get("client_secret"),
        scopes=creds_dict.get("scopes", []),
        expiry=expiry,
    )


# ------------------------------------------------------------------ #
# Token management                                                    #
# ------------------------------------------------------------------ #

def get_valid_credentials(connected_account) -> Optional[Credentials]:
    """
    Return valid (possibly refreshed) Credentials for a ConnectedAccount.

    - Decrypts the stored token blob.
    - If the token is expired and a refresh_token exists, refreshes it.
    - Re-encrypts and saves the refreshed token back to the account row.
    - Returns None if credentials are unavailable or cannot be refreshed.

    The caller must commit the SQLAlchemy session after this call if
    the credentials were refreshed (the account row is modified in place).
    """
    if not connected_account or not connected_account.encrypted_credentials:
        return None

    creds_dict = decrypt_credentials(connected_account.encrypted_credentials)
    if not creds_dict:
        logger.error(
            "Could not decrypt credentials for account %s",
            connected_account.id,
        )
        return None

    creds = dict_to_credentials(creds_dict)

    # Refresh if expired and a refresh token is available.
    try:
        is_expired = creds.expired
    except Exception as exc:
        logger.warning(
            "Could not check creds.expired for account %s (%s): %s",
            connected_account.id,
            connected_account.email,
            exc,
        )
        is_expired = False

    if is_expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info(
                "Refreshed OAuth token for account %s (%s)",
                connected_account.id,
                connected_account.email,
            )
            # Persist the refreshed token.
            refreshed_dict = credentials_to_dict(creds)
            connected_account.encrypted_credentials = encrypt_credentials(refreshed_dict)
            connected_account.last_refreshed = datetime.now(timezone.utc)
            if creds.expiry:
                connected_account.token_expires_at = (
                    creds.expiry if creds.expiry.tzinfo
                    else creds.expiry.replace(tzinfo=timezone.utc)
                )
        except Exception as exc:
            logger.error(
                "Failed to refresh token for account %s: %s",
                connected_account.id,
                exc,
            )
            return None

    if not creds.valid:
        logger.warning(
            "Credentials for account %s are invalid and cannot be refreshed.",
            connected_account.id,
        )
        return None

    return creds


def revoke_credentials(connected_account) -> bool:
    """
    Attempt to revoke the OAuth token with Google.
    Returns True on success, False on failure.
    Does NOT delete the account row — that is the caller's responsibility.
    """
    import requests as req_lib

    creds_dict = decrypt_credentials(connected_account.encrypted_credentials)
    if not creds_dict or not creds_dict.get("token"):
        return False

    try:
        resp = req_lib.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": creds_dict["token"]},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("Revoked token for account %s", connected_account.id)
            return True
        logger.warning(
            "Token revocation returned %s for account %s",
            resp.status_code,
            connected_account.id,
        )
        return False
    except Exception as exc:
        logger.error("Error revoking token for account %s: %s", connected_account.id, exc)
        return False
