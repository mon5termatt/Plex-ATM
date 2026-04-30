# Docker Run Guide

## 1) Clone on your server

```bash
git clone https://github.com/mon5termatt/Plex-ATM.git
cd Plex-ATM
```

## 2) Configure media path mounts

Edit `docker-compose.yml` and add your host media folders under `volumes`.

Example on Windows:

```yaml
volumes:
  - plex_atm_data:/app/data
  - "D:/TV:/tv"
  - "D:/Anime:/anime"
```

## 3) Build and start locally on server

```powershell
docker compose up -d --build
```

Open: `http://localhost:5000`

Note: the container includes `ffmpeg` and will re-encode applied themes to MP3 128kbps.

`/app/data` stores app settings + SQLite DB and now persists in the named Docker volume `plex_atm_data`.

## 4) Set Plex/Sonarr paths to container-visible paths

Preferred approach: keep Plex/Sonarr/library paths container-native (for example `/tv/...` or `/anime/...`) and mount host media into those same container paths.

If Sonarr returns `/tv/...`, map your host folder to `/tv` in compose:

```yaml
- "D:/TV:/tv"
```

Optional fallback only: if your incoming path prefix differs from the mounted container path (example `/plex/ANIME/...` from Sonarr, but container mount is `/media/anime/...`), set `APP_PATH_MAPPINGS` in `docker-compose.yml`:

```yaml
environment:
  APP_PATH_MAPPINGS: "/plex/ANIME=/media/anime;/plex/TV=/media/tv"
```

## 5) View logs

```powershell
docker compose logs -f plex-theme-manager
```

## 6) Stop

```powershell
docker compose down
```
