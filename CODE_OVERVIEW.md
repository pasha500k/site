# Code Overview

## Application purpose and stack
- `app.py` bootstraps a Flask application that serves a local video library with previews, account-based reactions, and content protection in a single file deployment. The module imports Flask, configures a secret key, and establishes helper dependencies for security, streaming, and database access.【F:app.py†L1-L34】

## Storage layout and runtime configuration
- Videos live under `VIDEO_ROOT` with derived directories for previews and uploads. Runtime behavior is driven by environment variables for secrets, ffmpeg timeouts, and short-form video parameters. JSON sidecar files store cached translations, durations, view counts, and folder protection metadata that are eagerly loaded on startup along with several in-memory caches for reactions, favorites, and recommendation data.【F:app.py†L35-L138】

## Persistent data model
- SQLite is used for user accounts, reactions, favorites, view fingerprints, author metadata overrides, watch sessions, transitions, folder grants, subscriptions, per-user statistics, and upload moderation. `init_db()` creates or migrates tables, ensures indices, and provisions a default admin account. `get_db()`/`close_db()` provide request-scoped connections with foreign keys enabled.【F:app.py†L140-L227】【F:app.py†L228-L302】

## Identity, rate limiting, and engagement tracking
- Request-scoped helpers provide lightweight rate limiting via per-user/per-IP deques, anonymous viewer fingerprints via cookies, and view registration that records unique impressions and updates user statistics. Folder access decisions reuse the same identity layer to persist grants in the database.【F:app.py†L304-L410】

## Access control tokens
- Protected folders can be opened with HMAC-signed access links. Helpers issue and verify signatures with expiry timestamps, detect the nearest protected ancestor for a path, and append grants to URLs when needed.【F:app.py†L1070-L1119】

## Video indexing and metadata enrichment
- Directory walking, preview generation, and an on-disk JSON index are used to keep metadata hot. Functions build author mappings, normalize entries, and refresh the in-memory catalog while avoiding preview/upload folders. Listing helpers enumerate subfolders, build video cards, and decorate them with cached reaction, favorite, and view counts before rendering.【F:app.py†L1321-L1500】【F:app.py†L1369-L1384】【F:app.py†L1394-L1462】【F:app.py†L2260-L2314】

## Core routes
- Browsing leverages the indexing helpers to render folders, videos, and breadcrumbs while enforcing password-protected folders. Additional routes manage access forms and random directory settings.【F:app.py†L4332-L4440】
- Video playback includes HTML watch pages, direct file serving with HTTP range support, preview thumbnails, and on-the-fly ffmpeg transcoding for streaming or downloads with access validation.【F:app.py†L4464-L4776】
- JSON APIs expose search, per-user state, reactions, favorites, watch progress, shorts feeds, and subscription toggles to the frontend.【F:app.py†L4780-L5089】
- Authenticated pages cover favorites, shorts browsing, uploads with moderation workflow, account statistics, and admin or moderator panels for reports, folder protection, and upload review.【F:app.py†L5091-L5599】
- Authentication routes provide rate-limited login, registration, and logout flows backed by the users table.【F:app.py†L5488-L5569】

## Command-line entry points
- The script can be invoked to initialize the database or re-index videos; CLI parsing wires options for background scans and preview generation, ultimately running the Flask development server when executed directly.【F:app.py†L5765-L5846】
