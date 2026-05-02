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
- Lets you preview candidates in-app and **apply** a selected theme (or **upload** your own audio)
- **Apply** and **custom upload** both run the same pipeline: re-encode to **MP3 128 kbps** with **ffmpeg**, write beside the show folder, verify on disk
- In-browser **install progress** (stepper + status) for apply and upload when the UI uses fetch (no full-page reload for those actions)
- After a successful apply or upload, the **Apply Candidate** sidebar refreshes so **Current Local Theme** appears with a preview player without reloading the page

## Current Workflow

1. Configure Plex/Sonarr in **Settings**
2. Rescan Plex library from **Shows**
3. Open a **show** detail page
4. Adjust query or pick alternate titles if needed
5. **Find themes**, preview audio, and **Apply** (or use **Upload custom theme** in the top toolbar)
6. Use **Prev show** / **Next show** (full list order) or **Next (no theme)** (shows missing a theme file, same sort/search context) as shortcuts

## Show detail page

- **Header**: poster, title, and folder path on the first row; **navigation and upload** on the row below.
- **Toolbar**: Back to Shows, Prev/Next show (same order as the Shows list for your current filter/sort/search), Next (no theme), **Upload custom theme** (opens a file picker; supports common audio extensions, then the same install progress as apply).
- **←→ target** toggle (**All** vs **No theme**): chooses which list **left/right arrow keys** use when jumping between shows. The choice is stored in `localStorage` (`plexATM_showDetailArrowNav`). **No theme** is disabled when there are no eligible neighbors.
- **Find AnimeThemes Candidates** and **Apply Candidate** sidebar behave as before; find results replace the candidate list and can add a local-theme block when the server reports a theme on disk.

## Keyboard shortcuts (show detail)

| Keys | Action |
|------|--------|
| **←** **→** | Previous / next show (list depends on **All** vs **No theme** toggle above the hints) |
| **Shift** + **←** **→** | Seek the theme preview ±5 seconds (playing track, else the focused candidate row) |
| **↑** **↓** | Move focus between candidate rows (plays preview when moving) |
| **Space** | Play / pause focused candidate preview (or submit Find when the sidebar is empty) |
| **Enter** | Submit **Apply** for the focused candidate (or submit Find when the sidebar is empty) |

When the sidebar only shows alternate titles (no candidates yet), **Space** / **Enter** can trigger **Find**, and **↑** / **↓** move among alternate title chips.

## Requirements

- Python 3.11+ (3.12 recommended)
- Plex server + token
- Optional: Sonarr URL + API key
- **ffmpeg** on `PATH` for theme re-encode (included in the Docker image)

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
  - plex_atm_data:/app/data
  - "D:/TV:/tv"
  - "D:/Anime:/anime"
```

The app database/settings persist in the Docker named volume `plex_atm_data` (so recreating the container does not reset settings).

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

This project is actively evolving. The current version focuses on practical manual workflows with strong debug visibility, safe file write checks, and responsive show-detail UX (streaming install progress, keyboard navigation, and sidebar refresh after installs).
