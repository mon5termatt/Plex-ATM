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
  - ./data:/app/data
  - "D:/TV:/media/tv"
  - "D:/Anime:/media/anime"
```

## 3) Build and start locally on server

```powershell
docker compose up -d --build
```

Open: `http://localhost:5000`

## 4) Set Plex/Sonarr paths to container-visible paths

In app settings/workflow, ensure writable show paths resolve to mounted container paths (for example `/media/tv/...`), not host-only paths.

If Sonarr returns `/tv/...`, map your host folder to `/tv` in compose:

```yaml
- "D:/TV:/tv"
```

## 5) View logs

```powershell
docker compose logs -f plex-theme-manager
```

## 6) Stop

```powershell
docker compose down
```
