# Docker Run Guide

## 1) Configure media path mounts

Edit `docker-compose.yml` and add your host media folders under `volumes`.

Example on Windows:

```yaml
volumes:
  - ./data:/app/data
  - "D:/TV:/media/tv"
  - "D:/Anime:/media/anime"
```

## 2) Start container

```powershell
docker compose up -d --build
```

Open: `http://localhost:5000`

## 3) Set Plex/Sonarr paths to container-visible paths

In app settings/workflow, ensure writable show paths resolve to mounted container paths (for example `/media/tv/...`), not host-only paths.

If Sonarr returns `/tv/...`, map your host folder to `/tv` in compose:

```yaml
- "D:/TV:/tv"
```

## 4) View logs

```powershell
docker compose logs -f plex-theme-manager
```

## 5) Stop

```powershell
docker compose down
```
