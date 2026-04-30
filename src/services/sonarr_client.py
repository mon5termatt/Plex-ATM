from typing import Any
import re
import unicodedata
import requests


class SonarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def validate(self) -> tuple[bool, str]:
        if not self.base_url or not self.api_key:
            return (False, "Sonarr URL or API key missing.")
        try:
            response = requests.get(
                f"{self.base_url}/api/v3/system/status",
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
            return (response.ok, response.text[:300])
        except Exception as exc:
            return (False, str(exc))

    def list_series(self) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/api/v3/series",
            headers={"X-Api-Key": self.api_key},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def find_series_for_show(self, show_title: str, tvdb_id: int | None = None, tmdb_id: int | None = None) -> dict[str, Any] | None:
        series = self.list_series()
        if tvdb_id is not None:
            for item in series:
                if item.get("tvdbId") == tvdb_id:
                    return item
        if tmdb_id is not None:
            for item in series:
                if item.get("tmdbId") == tmdb_id:
                    return item
        normalized_show_title = self._normalize_title(show_title)
        for item in series:
            title = str(item.get("title", ""))
            if self._normalize_title(title) == normalized_show_title:
                return item
        return None

    def build_path_index(self) -> dict[str, dict[str, str]]:
        series = self.list_series()
        by_tvdb: dict[str, str] = {}
        by_tmdb: dict[str, str] = {}
        by_title: dict[str, str] = {}
        query_by_tvdb: dict[str, str] = {}
        query_by_tmdb: dict[str, str] = {}
        query_by_title: dict[str, str] = {}
        for item in series:
            path = item.get("path")
            if not path:
                continue
            preferred_query = self._preferred_query(item)
            tvdb_id = item.get("tvdbId")
            if tvdb_id:
                by_tvdb[str(tvdb_id)] = path
                if preferred_query:
                    query_by_tvdb[str(tvdb_id)] = preferred_query
            tmdb_id = item.get("tmdbId")
            if tmdb_id:
                by_tmdb[str(tmdb_id)] = path
                if preferred_query:
                    query_by_tmdb[str(tmdb_id)] = preferred_query
            title = item.get("title", "")
            if title:
                normalized = self._normalize_title(title)
                by_title[normalized] = path
                if preferred_query:
                    query_by_title[normalized] = preferred_query
        return {
            "by_tvdb": by_tvdb,
            "by_tmdb": by_tmdb,
            "by_title": by_title,
            "query_by_tvdb": query_by_tvdb,
            "query_by_tmdb": query_by_tmdb,
            "query_by_title": query_by_title,
        }

    @classmethod
    def _preferred_query(cls, item: dict[str, Any]) -> str:
        # Use Sonarr canonical metadata for all shows (TVDB/TMDB-backed).
        candidates: list[str] = []
        original_title = str(item.get("originalTitle", "")).strip()
        if original_title:
            candidates.append(original_title)
        for alias in item.get("alternateTitles", []) or []:
            alias_title = str(alias.get("title", "")).strip()
            if alias_title:
                candidates.append(alias_title)
        title_slug = str(item.get("titleSlug", "")).strip()
        if title_slug:
            candidates.append(title_slug.replace("-", " "))
        title = str(item.get("title", "")).strip()
        if title:
            candidates.append(title)

        normalized_candidates = [cls._romanized_query(c) for c in candidates]
        unique = []
        seen = set()
        for c in normalized_candidates:
            key = c.casefold()
            if key and key not in seen:
                unique.append(c)
                seen.add(key)
        return unique[0] if unique else ""

    @staticmethod
    def _normalize_title(value: str) -> str:
        return "".join(ch.lower() for ch in value if ch.isalnum())

    @staticmethod
    def _romanized_query(value: str) -> str:
        # Keep ASCII-latin-ish query text so AnimeThemes matching is consistent.
        normalized = unicodedata.normalize("NFKD", value)
        ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
        ascii_only = ascii_only.replace("-", " ")
        ascii_only = re.sub(r"[^A-Za-z0-9\s\.\']", " ", ascii_only)
        return re.sub(r"\s+", " ", ascii_only).strip()

