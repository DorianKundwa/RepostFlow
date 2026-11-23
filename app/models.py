"""
app/models.py - SQLAlchemy database models for RepostFlow.

Stage 1: All tables are defined here from the start so migrations
can be tracked from day one. Services in later stages fill in the logic.

Design notes:
- Uses SQLAlchemy 2.x mapped_column / Mapped style where possible.
- All timestamps stored as UTC.
- OAuth tokens encrypted at rest (encryption applied in services layer).
- Foreign keys use ondelete cascade where child records should be removed
  automatically when the parent is deleted.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    BigInteger,
    Float,
)
from sqlalchemy.orm import relationship

from app.extensions import db


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def utcnow() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ #
# Enumerations                                                        #
# ------------------------------------------------------------------ #

class ChannelRole(str, enum.Enum):
    SOURCE = "source"
    DESTINATION = "destination"
    BOTH = "both"


class UploadStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    AWAITING_FILE = "awaiting_file"


class PrivacyStatus(str, enum.Enum):
    PRIVATE = "private"
    UNLISTED = "unlisted"
    PUBLIC = "public"


class VideoStatus(str, enum.Enum):
    DETECTED = "detected"
    READY = "ready"
    QUEUED = "queued"
    UPLOADED = "uploaded"
    FAILED = "failed"
    AWAITING_FILE = "awaiting_file"


class SourceType(str, enum.Enum):
    YOUTUBE = "youtube"
    LOCAL_FOLDER = "local_folder"


class NotificationType(str, enum.Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


# ------------------------------------------------------------------ #
# 1. User                                                             #
# ------------------------------------------------------------------ #

class User(db.Model):
    """
    Application user (local account).
    In Stage 1 this is a simple local user.
    Google OAuth links connect through ConnectedAccount.
    """
    __tablename__ = "user"

    id = db.Column(Integer, primary_key=True)
    username = db.Column(String(80), unique=True, nullable=False, index=True)
    email = db.Column(String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(String(256), nullable=True)  # None = OAuth-only user
    is_active = db.Column(Boolean, default=True, nullable=False)
    is_admin = db.Column(Boolean, default=False, nullable=False)
    created_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login = db.Column(DateTime(timezone=True), nullable=True)

    # Flask-Login interface
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    def get_id(self) -> str:
        return str(self.id)

    # Relationships
    connected_accounts = relationship(
        "ConnectedAccount", back_populates="user", cascade="all, delete-orphan"
    )
    notifications = relationship(
        "Notification", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User {self.username}>"


# ------------------------------------------------------------------ #
# 2. ConnectedAccount                                                 #
# ------------------------------------------------------------------ #

class ConnectedAccount(db.Model):
    """
    A Google account authenticated via OAuth 2.0.
    One user can connect multiple Google accounts.
    Tokens are stored encrypted (encryption handled in google_auth service).
    """
    __tablename__ = "connected_account"

    id = db.Column(Integer, primary_key=True)
    user_id = db.Column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    google_account_id = db.Column(String(255), nullable=False)   # Google's 'sub' claim
    email = db.Column(String(254), nullable=False)
    display_name = db.Column(String(255), nullable=True)
    profile_picture_url = db.Column(String(2048), nullable=True)
    # Fernet-encrypted JSON blob of the OAuth credentials dict
    encrypted_credentials = db.Column(Text, nullable=True)
    scopes_granted = db.Column(Text, nullable=True)   # Comma-separated scope list
    is_active = db.Column(Boolean, default=True, nullable=False)
    connected_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_refreshed = db.Column(DateTime(timezone=True), nullable=True)
    token_expires_at = db.Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "google_account_id", name="uq_user_google_account"),
    )

    # Relationships
    user = relationship("User", back_populates="connected_accounts")
    youtube_channels = relationship(
        "YouTubeChannel", back_populates="connected_account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<ConnectedAccount {self.email}>"


# ------------------------------------------------------------------ #
# 3. YouTubeChannel                                                   #
# ------------------------------------------------------------------ #

class YouTubeChannel(db.Model):
    """
    A specific YouTube channel accessible through a ConnectedAccount.
    One Google account may manage multiple channels (Brand Accounts).
    """
    __tablename__ = "youtube_channel"

    id = db.Column(Integer, primary_key=True)
    connected_account_id = db.Column(
        Integer,
        ForeignKey("connected_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_id = db.Column(String(255), unique=True, nullable=False, index=True)
    channel_name = db.Column(String(255), nullable=False)
    channel_url = db.Column(String(2048), nullable=True)
    thumbnail_url = db.Column(String(2048), nullable=True)
    subscriber_count = db.Column(BigInteger, nullable=True)
    uploads_playlist_id = db.Column(String(255), nullable=True)  # UC... -> UU... playlist
    role = db.Column(
        Enum(ChannelRole), default=ChannelRole.DESTINATION, nullable=False
    )
    is_active = db.Column(Boolean, default=True, nullable=False)
    monitoring_enabled = db.Column(Boolean, default=False, nullable=False)
    last_checked_at = db.Column(DateTime(timezone=True), nullable=True)
    last_video_id = db.Column(String(255), nullable=True)   # Newest known video ID
    added_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    # Relationships
    connected_account = relationship("ConnectedAccount", back_populates="youtube_channels")
    videos = relationship("Video", back_populates="source_channel", foreign_keys="Video.source_channel_id")
    automation_sources = relationship(
        "AutomationRule", back_populates="source_channel", foreign_keys="AutomationRule.source_channel_id"
    )
    automation_destinations = relationship(
        "AutomationDestination", back_populates="channel"
    )

    def __repr__(self) -> str:
        return f"<YouTubeChannel {self.channel_name} ({self.channel_id})>"


# ------------------------------------------------------------------ #
# 4. WatchedFolder                                                    #
# ------------------------------------------------------------------ #

class WatchedFolder(db.Model):
    """
    A local filesystem path monitored by Watchdog for new video files.
    """
    __tablename__ = "watched_folder"

    id = db.Column(Integer, primary_key=True)
    user_id = db.Column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    path = db.Column(String(2048), unique=True, nullable=False)
    label = db.Column(String(255), nullable=True)
    is_active = db.Column(Boolean, default=True, nullable=False)
    recursive = db.Column(Boolean, default=False, nullable=False)
    added_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_scanned_at = db.Column(DateTime(timezone=True), nullable=True)

    # Relationships
    videos = relationship("Video", back_populates="watched_folder")
    automation_sources = relationship(
        "AutomationRule",
        back_populates="source_folder",
        foreign_keys="AutomationRule.source_folder_id",
    )

    def __repr__(self) -> str:
        return f"<WatchedFolder {self.path}>"


# ------------------------------------------------------------------ #
# 5. Video                                                            #
# ------------------------------------------------------------------ #

class Video(db.Model):
    """
    Central video library entry.
    Covers both YouTube-detected and locally-detected videos.
    """
    __tablename__ = "video"

    id = db.Column(Integer, primary_key=True)
    # Source tracking
    source_type = db.Column(Enum(SourceType), nullable=False)
    source_channel_id = db.Column(
        Integer, ForeignKey("youtube_channel.id", ondelete="SET NULL"), nullable=True, index=True
    )
    watched_folder_id = db.Column(
        Integer, ForeignKey("watched_folder.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # YouTube identifiers (if applicable)
    youtube_video_id = db.Column(String(255), unique=True, nullable=True, index=True)
    youtube_published_at = db.Column(DateTime(timezone=True), nullable=True)
    # Local file info
    original_filename = db.Column(String(1024), nullable=True)
    local_file_path = db.Column(String(2048), nullable=True)
    file_size_bytes = db.Column(BigInteger, nullable=True)
    # Media properties (from FFprobe)
    duration_seconds = db.Column(Float, nullable=True)
    width = db.Column(Integer, nullable=True)
    height = db.Column(Integer, nullable=True)
    fps = db.Column(Float, nullable=True)
    codec = db.Column(String(64), nullable=True)
    # Metadata
    title = db.Column(String(500), nullable=True)
    description = db.Column(Text, nullable=True)
    tags = db.Column(Text, nullable=True)       # JSON list stored as text
    category_id = db.Column(String(10), nullable=True)
    thumbnail_url = db.Column(String(2048), nullable=True)
    local_thumbnail_path = db.Column(String(2048), nullable=True)
    privacy_status = db.Column(
        Enum(PrivacyStatus), default=PrivacyStatus.PRIVATE, nullable=False
    )
    # Status
    status = db.Column(
        Enum(VideoStatus), default=VideoStatus.DETECTED, nullable=False, index=True
    )
    detected_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    notes = db.Column(Text, nullable=True)

    # Relationships
    source_channel = relationship(
        "YouTubeChannel", back_populates="videos", foreign_keys=[source_channel_id]
    )
    watched_folder = relationship("WatchedFolder", back_populates="videos")
    upload_jobs = relationship("UploadJob", back_populates="video", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Video {self.id}: {self.title or self.original_filename or 'untitled'}>"


# ------------------------------------------------------------------ #
# 6. AutomationRule                                                   #
# ------------------------------------------------------------------ #

class AutomationRule(db.Model):
    """
    An automation rule that pairs a source (channel or folder) with
    one or more destination channels and defines upload behaviour.
    """
    __tablename__ = "automation_rule"

    id = db.Column(Integer, primary_key=True)
    user_id = db.Column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(String(255), nullable=False)
    is_enabled = db.Column(Boolean, default=True, nullable=False)
    source_type = db.Column(Enum(SourceType), nullable=False)
    source_channel_id = db.Column(
        Integer, ForeignKey("youtube_channel.id", ondelete="SET NULL"), nullable=True
    )
    source_folder_id = db.Column(
        Integer, ForeignKey("watched_folder.id", ondelete="SET NULL"), nullable=True
    )
    # Metadata behaviour
    copy_title = db.Column(Boolean, default=True, nullable=False)
    copy_description = db.Column(Boolean, default=True, nullable=False)
    copy_tags = db.Column(Boolean, default=True, nullable=False)
    copy_thumbnail = db.Column(Boolean, default=True, nullable=False)
    title_prefix = db.Column(String(255), nullable=True)
    title_suffix = db.Column(String(255), nullable=True)
    description_prepend = db.Column(Text, nullable=True)
    description_append = db.Column(Text, nullable=True)
    extra_tags = db.Column(Text, nullable=True)   # JSON list
    # Upload behaviour
    default_privacy = db.Column(
        Enum(PrivacyStatus), default=PrivacyStatus.PRIVATE, nullable=False
    )
    # Timestamps
    created_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    # Relationships
    source_channel = relationship(
        "YouTubeChannel", back_populates="automation_sources", foreign_keys=[source_channel_id]
    )
    source_folder = relationship(
        "WatchedFolder", back_populates="automation_sources", foreign_keys=[source_folder_id]
    )
    destinations = relationship(
        "AutomationDestination", back_populates="rule", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<AutomationRule {self.name}>"


# ------------------------------------------------------------------ #
# 7. AutomationDestination                                            #
# ------------------------------------------------------------------ #

class AutomationDestination(db.Model):
    """
    Maps an AutomationRule to one destination YouTubeChannel.
    A rule can have multiple destination rows.
    """
    __tablename__ = "automation_destination"
    __table_args__ = (
        UniqueConstraint("rule_id", "channel_id", name="uq_rule_channel"),
    )

    id = db.Column(Integer, primary_key=True)
    rule_id = db.Column(
        Integer, ForeignKey("automation_rule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_id = db.Column(
        Integer, ForeignKey("youtube_channel.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Relationships
    rule = relationship("AutomationRule", back_populates="destinations")
    channel = relationship("YouTubeChannel", back_populates="automation_destinations")

    def __repr__(self) -> str:
        return f"<AutomationDestination rule={self.rule_id} channel={self.channel_id}>"


# ------------------------------------------------------------------ #
# 8. UploadJob                                                        #
# ------------------------------------------------------------------ #

class UploadJob(db.Model):
    """
    One upload task: a single video being uploaded to a single channel.
    A video uploaded to 3 channels produces 3 UploadJob rows.
    """
    __tablename__ = "upload_job"
    __table_args__ = (
        # Prevent duplicate queuing of the same video→channel pair.
        UniqueConstraint("video_id", "destination_channel_id", "idempotency_key",
                         name="uq_upload_job_idempotency"),
    )

    id = db.Column(Integer, primary_key=True)
    video_id = db.Column(
        Integer, ForeignKey("video.id", ondelete="CASCADE"), nullable=False, index=True
    )
    destination_channel_id = db.Column(
        Integer, ForeignKey("youtube_channel.id", ondelete="CASCADE"), nullable=False, index=True
    )
    automation_rule_id = db.Column(
        Integer, ForeignKey("automation_rule.id", ondelete="SET NULL"), nullable=True
    )
    # A short hash to allow intentional re-queuing while still preventing accidental duplicates.
    idempotency_key = db.Column(String(64), nullable=False, default="default")
    status = db.Column(
        Enum(UploadStatus), default=UploadStatus.PENDING, nullable=False, index=True
    )
    progress_percent = db.Column(Integer, default=0, nullable=False)
    retry_count = db.Column(Integer, default=0, nullable=False)
    max_retries = db.Column(Integer, default=3, nullable=False)
    # Metadata snapshot (what will actually be uploaded)
    upload_title = db.Column(String(500), nullable=True)
    upload_description = db.Column(Text, nullable=True)
    upload_tags = db.Column(Text, nullable=True)
    upload_privacy = db.Column(
        Enum(PrivacyStatus), default=PrivacyStatus.PRIVATE, nullable=False
    )
    upload_thumbnail_path = db.Column(String(2048), nullable=True)
    # Result
    destination_video_id = db.Column(String(255), nullable=True)
    destination_video_url = db.Column(String(2048), nullable=True)
    error_message = db.Column(Text, nullable=True)
    # Timestamps
    created_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at = db.Column(DateTime(timezone=True), nullable=True)
    completed_at = db.Column(DateTime(timezone=True), nullable=True)
    last_attempt_at = db.Column(DateTime(timezone=True), nullable=True)

    # Relationships
    video = relationship("Video", back_populates="upload_jobs")
    destination_channel = relationship("YouTubeChannel")
    automation_rule = relationship("AutomationRule")

    def __repr__(self) -> str:
        return f"<UploadJob {self.id} video={self.video_id} → channel={self.destination_channel_id} [{self.status}]>"


# ------------------------------------------------------------------ #
# 9. PublishingHistory                                                #
# ------------------------------------------------------------------ #

class PublishingHistory(db.Model):
    """
    Immutable record of a completed (or permanently failed) upload.
    Written when an UploadJob reaches Completed or Failed (after retries).
    """
    __tablename__ = "publishing_history"

    id = db.Column(Integer, primary_key=True)
    upload_job_id = db.Column(Integer, ForeignKey("upload_job.id", ondelete="SET NULL"), nullable=True)
    video_id = db.Column(Integer, ForeignKey("video.id", ondelete="SET NULL"), nullable=True, index=True)
    source_channel_id = db.Column(Integer, ForeignKey("youtube_channel.id", ondelete="SET NULL"), nullable=True)
    destination_channel_id = db.Column(Integer, ForeignKey("youtube_channel.id", ondelete="SET NULL"), nullable=True, index=True)
    video_title = db.Column(String(500), nullable=True)
    upload_status = db.Column(String(50), nullable=False)
    privacy_status = db.Column(String(50), nullable=True)
    destination_video_id = db.Column(String(255), nullable=True)
    destination_video_url = db.Column(String(2048), nullable=True)
    error_message = db.Column(Text, nullable=True)
    uploaded_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    # Relationships
    video = relationship("Video")
    source_channel = relationship("YouTubeChannel", foreign_keys=[source_channel_id])
    destination_channel = relationship("YouTubeChannel", foreign_keys=[destination_channel_id])

    def __repr__(self) -> str:
        return f"<PublishingHistory {self.id} [{self.upload_status}]>"


# ------------------------------------------------------------------ #
# 10. ApplicationSetting                                              #
# ------------------------------------------------------------------ #

class ApplicationSetting(db.Model):
    """
    Key-value store for application-wide settings configurable via the UI.
    Typed values are stored as strings and cast at read time.
    """
    __tablename__ = "application_setting"

    id = db.Column(Integer, primary_key=True)
    key = db.Column(String(255), unique=True, nullable=False, index=True)
    value = db.Column(Text, nullable=True)
    description = db.Column(Text, nullable=True)
    value_type = db.Column(String(20), default="string", nullable=False)  # string|int|bool|json
    updated_at = db.Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    @classmethod
    def get(cls, key: str, default=None):
        """Retrieve a setting value by key."""
        row = cls.query.filter_by(key=key).first()
        if row is None:
            return default
        return row.value

    @classmethod
    def set(cls, key: str, value, description: str = None):
        """Create or update a setting."""
        row = cls.query.filter_by(key=key).first()
        if row is None:
            row = cls(key=key, value=str(value), description=description)
            db.session.add(row)
        else:
            row.value = str(value)
            if description:
                row.description = description
        db.session.commit()

    def __repr__(self) -> str:
        return f"<ApplicationSetting {self.key}={self.value}>"


# ------------------------------------------------------------------ #
# 11. Notification                                                    #
# ------------------------------------------------------------------ #

class Notification(db.Model):
    """
    In-dashboard notification for the user.
    """
    __tablename__ = "notification"

    id = db.Column(Integer, primary_key=True)
    user_id = db.Column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type = db.Column(Enum(NotificationType), default=NotificationType.INFO, nullable=False)
    title = db.Column(String(255), nullable=False)
    message = db.Column(Text, nullable=True)
    is_read = db.Column(Boolean, default=False, nullable=False)
    created_at = db.Column(DateTime(timezone=True), default=utcnow, nullable=False)
    related_url = db.Column(String(2048), nullable=True)

    # Relationships
    user = relationship("User", back_populates="notifications")

    def __repr__(self) -> str:
        return f"<Notification {self.id} [{self.type}] {self.title}>"
