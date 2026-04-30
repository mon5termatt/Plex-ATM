# Plex ATM

> Disclaimer: This project was developed with substantial AI assistance. Review, test, and validate behavior in your own environment before production use.

Plex ATM (Plex Anime Theme Manager) is a Flask web app for finding and applying anime themes to shows in your Plex library.

It integrates with:

- **Plex** (library + show listing)
- **Sonarr** (title/path matching and alternate titles)
- **AnimeThemes API** (theme discovery)

## What It Does

- Caches Plex shows locally (fast startup, no forced scan on launch)
- Supports manual **Rescan Library**
- Looks up themes from AnimeThemes using:
  - primary title query
  - title variants
  - slug fallbacks
  - direct slug endpoint fallback
- Uses **audio-only** candidate links (no video fallback)
- Lets you preview candidates in-app and apply selected theme
- Supports custom upload fallback
- Re-encodes applied themes to **MP3 128kbps** via ffmpeg for consistent Plex theme playback

## Current Workflow

1. Configure Plex/Sonarr in **Settings**
2. Rescan Plex library from **Shows**
3. Open a show page
4. Adjust query or pick alternate titles if needed
5. Find themes, preview audio, and apply

## Requirements

- Python 3.11+ (3.12 recommended)
- Plex server + token
- Optional: Sonarr URL + API key

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open: `http://127.0.0.1:5000`

## Docker Setup

See `DOCKER.md` for full details.

Quick start:

```bash
git clone https://github.com/mon5termatt/Plex-ATM.git
cd Plex-ATM
docker compose up -d --build
```

or if already cloned:

```powershell
docker compose up -d --build
```

Open: `http://localhost:5000`

### Important: Path Mapping

Preferred: use container-native library paths like `/tv/...` and `/anime/...` in Plex/Sonarr, and mount host media folders to those exact paths in `docker-compose.yml`.

Example:

```yaml
volumes:
  - ./data:/app/data
  - "D:/TV:/tv"
  - "D:/Anime:/anime"
```

Only if needed (path mismatch): if source paths use a different prefix than your container mount (for example `/plex/ANIME/...`), set:

```yaml
environment:
  APP_PATH_MAPPINGS: "/plex/ANIME=/media/anime;/plex/TV=/media/tv"
```

## AnimeThemes API Notes

- Rate limit target: **90 requests/minute**
- App uses conservative request pacing + retry behavior
- Includes support for slug and direct anime endpoint fallback when title filters miss

Docs:
- https://api-docs.animethemes.moe/content/anime/
- https://api-docs.animethemes.moe/intro/ratelimiting/

## Project Structure

- `app.py` - Flask entrypoint
- `src/web/` - routes
- `src/services/` - API and apply logic
- `src/db/` - SQLite schema and helpers
- `templates/` - Jinja templates
- `static/` - CSS and UI assets

## Status

This project is actively evolving. The current version focuses on practical manual workflows with strong debug visibility and safe file write checks.
