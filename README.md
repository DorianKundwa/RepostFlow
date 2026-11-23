# RepostFlow

**Upload Once. Distribute Everywhere.**

RepostFlow is a self-hosted YouTube video distribution and automation platform. Connect multiple YouTube channels, monitor source channels and local folders for new videos, and automatically repost them to destination channels using the official YouTube Data API v3.

---

## Table of Contents

1. [Features](#features)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Google Cloud Configuration](#google-cloud-configuration)
5. [Database Initialisation](#database-initialisation)
6. [Running the Application](#running-the-application)
7. [Connecting YouTube Channels](#connecting-youtube-channels)
8. [Configuring Source Channels](#configuring-source-channels)
9. [Configuring Destination Channels](#configuring-destination-channels)
10. [Creating Automation Rules](#creating-automation-rules)
11. [Uploading Videos](#uploading-videos)
12. [Troubleshooting](#troubleshooting)
13. [Security Considerations](#security-considerations)
14. [Backup and Restore](#backup-and-restore)
15. [Development Stages](#development-stages)

---

## Features

**Current (Stages 1 through 8 Complete)**
- Project structure and Flask application factory
- Full database schema (11 tables: Users, Accounts, Channels, Folders, Videos, Rules, Destinations, UploadJobs, History, Settings, Notifications)
- Local user authentication (registration with admin grant, login, logout)
- Google OAuth 2.0 multi-account and multi-channel flow with CSRF state protection
- Encrypted OAuth credential storage using Fernet symmetric encryption
- Channel discovery from YouTube Data API (channels.list)
- Channel role management (Source, Destination, Both)
- Per-channel monitoring toggles and live connectivity testing
- Safe channel disconnection and token revocation
- In-dashboard notification service
- Central Video Library with search, status & source filtering, and pagination
- Automated FFprobe media inspection (resolution, duration, codecs, FPS, bitrate, file size)
- Automated FFmpeg frame capture and Pillow thumbnail optimization (1280x720 16:9, letterboxing, quality compression)
- Custom thumbnail uploads and replacements
- Full video metadata editing (Title, Description, Tags JSON, Category, Privacy Status)
- "Awaiting Original File" workflow support (linking master files to YouTube-detected entries)
- In-browser HTML5 video preview streaming and safe thumbnail serving
- Local folder monitoring via Watchdog with recursive subfolder watching
- Multi-phase asynchronous file stability verification (detects and waits for active file copy/render completions)
- Manual and automatic folder rescanning with duplicate detection
- Full Watched Folders and Application Preferences settings management interface
- YouTube Source Channel Monitoring via PlaylistItems and Videos API
- ISO 8601 duration parser and rich metadata ingestion (tags, categories, high-res thumbnails)
- Automatic local master file matcher linking original files to YouTube uploads
- Idempotent upload detection tracking last_video_id and last_checked_at timestamps
- One-click "Check Source Uploads" button for source channels
- Multi-channel Automation Rule Engine (Workflow A & Workflow B distribution)
- Infinite repost loop prevention (source channels strictly forbidden as destinations)
- Configurable metadata transformations (title prefixes/suffixes, description header/footers, tag merging)
- Granular inheritance toggles (copy title, description, tags, thumbnail)
- Automatic UploadJob generation and state management (Pending vs Awaiting File)
- Batch execution of rules against existing video library archives
- Official YouTube Resumable Upload Engine (MediaFileUpload, 5MB chunk streaming)
- Live upload progress percent tracking and database commits
- YouTube destination video ID and shortened URL capture (`https://youtu.be/<id>`)
- Custom thumbnail upload via YouTube Data API `thumbnails().set()`
- Quota error handling (HTTP 403 `quotaExceeded`) and automatic daily throttling
- Configurable retry mechanism with exponential failure counting
- Interactive Resumable Upload Queue dashboard with real-time status auto-polling
- Upload job controls: Instant execution, retry, pause, resume, cancel, and "Retry All Failed"
- Immutable Publishing History audit log with channel filtering and direct YouTube links
- Background task automation engine via APScheduler (`scheduler_service.py`)
- Automated YouTube source channel polling worker running at configured intervals
- Resumable upload queue worker dispatching pending jobs while enforcing `max_concurrent_uploads`
- Watched folder maintenance sweep periodically verifying directory stability
- Dynamic job rescheduling upon application settings changes without process restarts
- In-dashboard scheduler controls (Pause/Resume daemon, On-demand "Run Now" dispatch, schedule table)
- Dark SaaS-style responsive dashboard with Bootstrap 5
- Comprehensive test suite (119 tests, all passing)

**Planned (Next Stages)**
- Security hardening, production deployment guides & backup workflows (Stage 9)

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Python      | 3.9+    |
| FFmpeg      | Any recent build |
| FFprobe     | Included with FFmpeg |
| Windows     | 10 / 11 (initial target) |

---

## Installation

### 1. Clone or download the project

```cmd
cd C:\Users\YourName\Documents
git clone https://github.com/yourname/repostflow.git
cd repostflow
```

### 2. Create a virtual environment

```cmd
python -m venv venv
```

### 3. Activate the virtual environment

```cmd
venv\Scripts\activate
```

### 4. Install Python dependencies

```cmd
pip install -r requirements.txt
```

### 5. Copy the environment template

```cmd
copy .env.example .env
```

### 6. Generate a secret key

```cmd
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output and paste it as the value of `SECRET_KEY` in your `.env` file.

### 7. Generate a Fernet token encryption key

```cmd
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output and paste it as the value of `TOKEN_ENCRYPTION_KEY` in your `.env` file.

---

## Google Cloud Configuration

> **Important:** YouTube Data API v3 has a default quota of 10,000 units per day. Each video upload costs approximately 1,600 units. Google also requires OAuth apps in production to go through a verification process. During development, add your Google account as a test user.

### Step-by-step

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project named **RepostFlow** (or any name you choose).
3. In the left menu, go to **APIs & Services → Library**.
4. Search for **YouTube Data API v3** and click **Enable**.
5. Go to **APIs & Services → OAuth consent screen**.
   - Select **External** user type.
   - Fill in the application name, support email, and developer contact.
   - Add the scope `https://www.googleapis.com/auth/youtube.upload`.
   - Add the scope `https://www.googleapis.com/auth/youtube.readonly`.
   - Under **Test users**, add your Google account email address.
6. Go to **APIs & Services → Credentials**.
7. Click **Create Credentials → OAuth 2.0 Client IDs**.
   - Application type: **Web application**.
   - Name: `RepostFlow Local`.
   - Authorised redirect URIs: `http://localhost:5000/oauth/callback`
8. Click **Download JSON** and save the file as:
   ```
   storage\credentials\client_secrets.json
   ```
9. Set `GOOGLE_CLIENT_SECRETS_FILE=storage/credentials/client_secrets.json` in `.env`.

---

## Database Initialisation

The database is created automatically when the application starts.

```cmd
venv\Scripts\python run.py
```

The SQLite database is stored at `instance\repostflow.db`.

To initialise manually without starting the server:

```cmd
venv\Scripts\python -c "from app import create_app; app = create_app(); print('DB ready')"
```

---

## Running the Application

```cmd
venv\Scripts\activate
python run.py
```

Open your browser and go to: **http://localhost:5000/**

On first run, you will be redirected to the registration page to create your admin account. After that, registration is disabled.

To run on a different port:

```cmd
set PORT=8080
python run.py
```

---

## Connecting YouTube Channels

> **Stage 4 & 5** — Google OAuth and channel management will be implemented in those stages.

Once implemented:

1. Go to **Channels** in the sidebar.
2. Click **Connect Channel**.
3. You will be redirected to Google's OAuth consent screen.
4. Grant the requested permissions.
5. RepostFlow will retrieve your YouTube channel details.
6. Set the channel role: Source, Destination, or Both.

---

## Configuring Source Channels

> **Stage 8** — YouTube source monitoring.

1. Connect a YouTube channel and set its role to **Source** or **Both**.
2. Enable monitoring for that channel.
3. Configure the monitoring interval in **Settings**.

RepostFlow will periodically check the channel's uploads playlist for new videos.

**Important:** RepostFlow cannot download videos from YouTube. When a new upload is detected, it is marked as **Awaiting Original File**. You must supply the original video file by either:
- Placing it in a watched local folder (Workflow B), or
- Matching it manually in the video library.

---

## Configuring Destination Channels

1. Connect a YouTube channel and set its role to **Destination** or **Both**.
2. Add it as a destination in an automation rule.

---

## Creating Automation Rules

> **Stage 10** — Automation rule engine.

1. Go to **Automation Rules** in the sidebar.
2. Click **New Rule**.
3. Set the source (YouTube channel or local folder).
4. Select destination channels.
5. Configure metadata behaviour (copy title, description, tags, thumbnail).
6. Set a default privacy status (default: **Private**).
7. Enable the rule.

---

## Uploading Videos

### Workflow A: YouTube-to-YouTube

1. A source YouTube channel is monitored.
2. A new video upload is detected.
3. The video is marked **Awaiting Original File**.
4. Place the original video file in a watched folder, or match it manually.
5. Once matched, the upload job is queued.
6. RepostFlow uploads the video to the selected destinations via the YouTube Data API.

### Workflow B: Local Folder-to-YouTube

1. Configure a watched folder in **Settings**.
2. Place a video file in the folder.
3. RepostFlow detects the file and validates it with FFprobe.
4. The video is added to the library.
5. It is queued for upload to the configured destinations.

---

## Troubleshooting

### Application does not start

- Check that the virtual environment is activated: `venv\Scripts\activate`
- Check that all dependencies are installed: `pip install -r requirements.txt`
- Check the log file: `logs\repostflow.log`

### SECRET_KEY warning on startup

- Open `.env` and set `SECRET_KEY` to a long random string.

### Database errors

- Delete `instance\repostflow.db` and restart the application to recreate it.

### OAuth errors

- Verify that `storage\credentials\client_secrets.json` exists.
- Verify that the redirect URI in Google Cloud Console matches `OAUTH_REDIRECT_URI` in `.env`.
- Confirm you have added your Google account as a test user in the OAuth consent screen.

### YouTube quota errors

- The YouTube Data API has a 10,000 unit daily quota.
- Monitor your usage in [Google Cloud Console → APIs & Services → Quotas](https://console.cloud.google.com/).
- Reduce the monitoring interval if needed.

### FFprobe not found

- Ensure FFmpeg is installed and `ffprobe` is on your system PATH.
- Test: open CMD and run `ffprobe -version`.

---

## Security Considerations

- **Never commit `.env` to version control.** The `.gitignore` excludes it.
- **Never commit `storage\credentials\`.** The `.gitignore` excludes it.
- **Never expose the local server to the internet** without a production WSGI server and HTTPS.
- OAuth tokens are stored encrypted in the database using Fernet symmetric encryption.
- The encryption key (`TOKEN_ENCRYPTION_KEY`) must be stored separately from the database.
- If you lose the encryption key, all stored OAuth tokens become unreadable and you will need to re-authorise all channels.
- The development server binds to `127.0.0.1` only.

---

## Backup and Restore

### Backup

```cmd
copy instance\repostflow.db instance\repostflow.db.backup
```

Also back up:
- `.env`
- `storage\credentials\`

### Restore

```cmd
copy instance\repostflow.db.backup instance\repostflow.db
```

---

## Development Stages

| Stage | Description | Status |
|-------|-------------|--------|
| 1  | Project structure, environment, Flask app, database, auth, dashboard skeleton | ✅ Complete |
| 2  | Google OAuth 2.0 & Multi-channel management (encryption, roles, testing, UI) | ✅ Complete |
| 3  | Video library, FFprobe media inspection, Pillow thumbnail generation & metadata | ✅ Complete |
| 4  | Local folder monitoring (Watchdog, stability checks, rescanning & settings UI) | ✅ Complete |
| 5  | YouTube source monitoring (PlaylistItems & Videos API, auto-match & Awaiting File) | ✅ Complete |
| 6  | Automation rule engine (Loop prevention, metadata transformations & UploadJob queuing) | ✅ Complete |
| 7  | Resumable YouTube upload engine, upload queue UI, and publishing history | ✅ Complete |
| 8  | Background task scheduling via APScheduler & automated workers | ✅ Complete |
| 9  | Security hardening, production deployment guides & backup workflows | Planned |

---

## Running Tests

```cmd
venv\Scripts\activate
pytest tests\ -v
```

Expected output: **119 passed**.
