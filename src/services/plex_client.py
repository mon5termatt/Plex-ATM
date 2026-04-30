from typing import Any
import requests


class PlexClient:
    def __init__(self, base_url: str, token: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        query = params.copy() if params else {}
        query["X-Plex-Token"] = self.token
        return requests.get(
            f"{self.base_url}{path}",
            params=query,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )

    def validate(self) -> tuple[bool, str]:
        try:
            response = self._get("/")
            return (response.ok, response.text[:300])
        except Exception as exc:
            return (False, str(exc))

    def list_sections(self) -> list[dict[str, Any]]:
        response = self._get("/library/sections")
        response.raise_for_status()
        data = response.json()
        dirs = data.get("MediaContainer", {}).get("Directory", [])
        return [
            {
                "key": str(item.get("key")),
                "title": item.get("title"),
                "type": item.get("type"),
                "locations": [loc.get("path") for loc in item.get("Location", []) if loc.get("path")],
            }
            for item in dirs
        ]

    def list_show_metadata(self, section_key: str) -> list[dict[str, Any]]:
        response = self._get(f"/library/sections/{section_key}/all", params={"type": 2})
        response.raise_for_status()
        data = response.json()
        shows = data.get("MediaContainer", {}).get("Metadata", [])
        output: list[dict[str, Any]] = []
        for show in shows:
            locations = [loc.get("path") for loc in show.get("Location", []) if loc.get("path")]
            if not locations and show.get("ratingKey"):
                fallback = self.get_show_location(str(show.get("ratingKey")))
                if fallback:
                    locations = [fallback]
            tvdb_id = self._extract_guid_id(show.get("Guid", []), "tvdb")
            tmdb_id = self._extract_guid_id(show.get("Guid", []), "tmdb")
            output.append(
                {
                    "rating_key": str(show.get("ratingKey")),
                    "title": show.get("title", "Unknown"),
                    "year": show.get("year"),
                    "folder_path": locations[0] if locations else None,
                    "tvdb_id": tvdb_id,
                    "tmdb_id": tmdb_id,
                }
            )
        return output

    def get_show_location(self, rating_key: str) -> str | None:
        response = self._get(f"/library/metadata/{rating_key}")
        response.raise_for_status()
        data = response.json()
        metadata = data.get("MediaContainer", {}).get("Metadata", [])
        if not metadata:
            return None
        locations = [loc.get("path") for loc in metadata[0].get("Location", []) if loc.get("path")]
        return locations[0] if locations else None

    def get_show_metadata_raw(self, rating_key: str) -> dict[str, Any]:
        response = self._get(f"/library/metadata/{rating_key}")
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _extract_guid_id(guid_items: list[dict[str, Any]], provider: str) -> int | None:
        prefix = f"{provider}://"
        for item in guid_items:
            guid_value = str(item.get("id", ""))
            if guid_value.startswith(prefix):
                raw = guid_value.split(prefix, 1)[1]
                if raw.isdigit():
                    return int(raw)
        return None
