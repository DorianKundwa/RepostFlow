"""
app/routes/oauth.py - Google OAuth 2.0 routes.

Endpoints:
  GET  /oauth/start    — Build authorisation URL and redirect to Google.
  GET  /oauth/callback — Exchange code for tokens, store account, discover channels.
  POST /oauth/disconnect/<account_id> — Disconnect (optionally revoke) an account.

Security:
  - State parameter prevents CSRF on the callback.
  - OAUTHLIB_INSECURE_TRANSPORT is set only for localhost (HTTP).
    Remove this for production HTTPS deployments.
  - Tokens are encrypted before database storage.
  - Redirect URIs are validated by the OAuth library against the
    registered value in Google Cloud Console.

Google developer notes:
  - The OAuth consent screen must be in "Testing" mode with your
    Google account listed as a test user while the app is unverified.
  - After granting access you will see an "unverified app" warning
    from Google. This is expected during development.
  - To remove the warning the app must pass Google's OAuth verification
    review (required for production use with other users).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import ConnectedAccount, NotificationType
from app.services.google_auth import (
    build_authorisation_url,
    credentials_to_dict,
    decrypt_credentials,
    encrypt_credentials,
    exchange_code_for_credentials,
    revoke_credentials,
)
from app.services.channel_service import (
    fetch_channels_for_account,
    get_user_info_from_credentials,
    register_channel,
)
from app.services.notification_service import create_notification

logger = logging.getLogger(__name__)

oauth_bp = Blueprint("oauth", __name__)

# Session key for the CSRF state token
_OAUTH_STATE_KEY = "oauth_state"
# Session key remembering the target role chosen before OAuth
_OAUTH_ROLE_KEY = "oauth_role"


@oauth_bp.route("/start")
@login_required
def start():
    """
    Begin the OAuth flow.
    Builds the Google authorisation URL, stores the state in the session,
    and redirects the browser to Google's consent screen.

    Query params:
      role (optional): 'source' | 'destination' | 'both'  (default: destination)
    """
    # Allow caller to hint at the intended role
    role = request.args.get("role", "destination")
    if role not in ("source", "destination", "both"):
        role = "destination"

    try:
        auth_url, state = build_authorisation_url()
    except FileNotFoundError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("channels.index"))
    except Exception as exc:
        logger.error("OAuth start error: %s", exc)
        flash(f"Could not start Google authorisation: {exc}", "danger")
        return redirect(url_for("channels.index"))

    # Allow HTTP on localhost (dev only)
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    session[_OAUTH_STATE_KEY] = state
    session[_OAUTH_ROLE_KEY] = role
    session.modified = True

    logger.info("OAuth flow started for user %s, role=%s", current_user.id, role)
    return redirect(auth_url)


@oauth_bp.route("/callback")
@login_required
def callback():
    """
    Handle Google's OAuth callback.

    1. Validate the state parameter (CSRF guard).
    2. Exchange the authorisation code for credentials.
    3. Fetch the Google user profile.
    4. Create or update a ConnectedAccount row.
    5. Discover accessible YouTube channels.
    6. Register discovered channels in the database.
    7. Redirect to the channel management page.
    """
    # --- CSRF state check ---
    expected_state = session.pop(_OAUTH_STATE_KEY, None)
    intended_role = session.pop(_OAUTH_ROLE_KEY, "destination")
    received_state = request.args.get("state")

    if not expected_state or expected_state != received_state:
        logger.warning(
            "OAuth state mismatch: expected=%s received=%s",
            expected_state,
            received_state,
        )
        flash("OAuth authorisation failed: invalid state. Please try again.", "danger")
        return redirect(url_for("channels.index"))

    # --- Error from Google? ---
    error = request.args.get("error")
    if error:
        flash(f"Google returned an error: {error}", "warning")
        return redirect(url_for("channels.index"))

    # --- Exchange code for credentials ---
    try:
        creds = exchange_code_for_credentials(
            authorisation_response_url=request.url,
            state=received_state,
        )
    except Exception as exc:
        logger.error("OAuth code exchange failed: %s", exc)
        flash(f"Could not complete Google authorisation: {exc}", "danger")
        return redirect(url_for("channels.index"))

    # --- Fetch user profile ---
    user_info = get_user_info_from_credentials(creds)
    if not user_info or not user_info.get("id"):
        flash("Could not retrieve Google account profile. Please try again.", "danger")
        return redirect(url_for("channels.index"))

    google_account_id = user_info["id"]
    email = user_info.get("email", "")
    display_name = user_info.get("name", email)

    # --- Store / update ConnectedAccount ---
    creds_dict = credentials_to_dict(creds)
    encrypted = encrypt_credentials(creds_dict)
    expiry_dt = creds.expiry
    if expiry_dt and expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

    account = ConnectedAccount.query.filter_by(
        user_id=current_user.id,
        google_account_id=google_account_id,
    ).first()

    if account:
        account.email = email
        account.display_name = display_name
        account.profile_picture_url = user_info.get("picture")
        account.encrypted_credentials = encrypted
        account.scopes_granted = ",".join(creds_dict.get("scopes", []))
        account.is_active = True
        account.last_refreshed = datetime.now(timezone.utc)
        account.token_expires_at = expiry_dt
        logger.info("Updated ConnectedAccount for %s", email)
    else:
        account = ConnectedAccount(
            user_id=current_user.id,
            google_account_id=google_account_id,
            email=email,
            display_name=display_name,
            profile_picture_url=user_info.get("picture"),
            encrypted_credentials=encrypted,
            scopes_granted=",".join(creds_dict.get("scopes", [])),
            is_active=True,
            connected_at=datetime.now(timezone.utc),
            last_refreshed=datetime.now(timezone.utc),
            token_expires_at=expiry_dt,
        )
        db.session.add(account)
        logger.info("Created new ConnectedAccount for %s", email)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to save ConnectedAccount: %s", exc)
        flash("Database error while saving account. Please try again.", "danger")
        return redirect(url_for("channels.index"))

    # --- Discover YouTube channels ---
    channels_data, err = fetch_channels_for_account(account)

    if err:
        flash(
            f"Google account connected, but could not retrieve YouTube channels: {err}",
            "warning",
        )
        return redirect(url_for("channels.index"))

    if not channels_data:
        flash(
            "Google account connected, but no YouTube channels were found. "
            "Make sure this Google account owns or manages a YouTube channel.",
            "warning",
        )
        return redirect(url_for("channels.index"))

    # --- Register discovered channels ---
    from app.models import ChannelRole
    role_map = {
        "source": ChannelRole.SOURCE,
        "destination": ChannelRole.DESTINATION,
        "both": ChannelRole.BOTH,
    }
    role = role_map.get(intended_role, ChannelRole.DESTINATION)

    registered_names = []
    for ch_data in channels_data:
        ch, reg_err = register_channel(account, ch_data, role=role)
        if ch:
            registered_names.append(ch.channel_name)
        else:
            logger.warning("Failed to register channel %s: %s", ch_data.get("channel_id"), reg_err)

    if registered_names:
        channel_list = ", ".join(registered_names)
        flash(
            f"Successfully connected {len(registered_names)} channel(s): {channel_list}",
            "success",
        )
        create_notification(
            user_id=current_user.id,
            title="Channels connected",
            message=f"Connected: {channel_list}",
            type=NotificationType.SUCCESS,
            related_url=url_for("channels.index"),
        )
    else:
        flash("Google account connected, but no channels could be registered.", "warning")

    return redirect(url_for("channels.index"))


@oauth_bp.route("/disconnect-account/<int:account_id>", methods=["POST"])
@login_required
def disconnect_account(account_id: int):
    """
    Disconnect a ConnectedAccount (and optionally revoke the token).
    All channels linked to this account are marked inactive.
    History records are preserved.
    """
    account = ConnectedAccount.query.filter_by(
        id=account_id, user_id=current_user.id
    ).first_or_404()

    # Revoke token with Google
    revoke_credentials(account)

    # Mark all channels inactive
    for ch in account.youtube_channels:
        ch.is_active = False
        ch.monitoring_enabled = False

    account.is_active = False
    account.encrypted_credentials = None

    try:
        db.session.commit()
        flash(f"Google account {account.email} disconnected.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error disconnecting account: {exc}", "danger")

    return redirect(url_for("channels.index"))
