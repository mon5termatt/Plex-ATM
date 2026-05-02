from datetime import datetime, timezone

from src.db.models import get_conn
from src.services.plex_client import PlexClient


class PlexCacheService:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def get_cached_shows(self, section_key: str) -> list[dict]:
        with get_conn(self.database_path) as conn:
            rows = conn.execute(
                "SELECT rating_key, title, year, folder_path, library_section_id, cached_at, last_seen_at, is_missing "
                "FROM plex_shows_cache WHERE library_section_id = ? AND is_missing = 0 ORDER BY title COLLATE NOCASE",
                (section_key,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_cached_shows_filtered(self, section_key: str, query: str = "") -> list[dict]:
        where = "library_section_id = ? AND is_missing = 0"
        params: list = [section_key]
        q = (query or "").strip()
        if q:
            where += " AND (title LIKE ? OR folder_path LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like])
        with get_conn(self.database_path) as conn:
            rows = conn.execute(
                f"SELECT rating_key, title, year, folder_path, library_section_id, cached_at, last_seen_at, is_missing "
                f"FROM plex_shows_cache WHERE {where} ORDER BY title COLLATE NOCASE",
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_cached_shows_page(
        self,
        section_key: str,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        page = max(page, 1)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size
        where = "library_section_id = ? AND is_missing = 0"
        params: list = [section_key]
        q = (query or "").strip()
        if q:
            where += " AND (title LIKE ? OR folder_path LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like])

        with get_conn(self.database_path) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM plex_shows_cache WHERE {where}",
                tuple(params),
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT rating_key, title, year, folder_path, library_section_id, cached_at, last_seen_at, is_missing "
                f"FROM plex_shows_cache WHERE {where} "
                "ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                tuple(params + [page_size, offset]),
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def get_cached_shows_sorted_page(
        self,
        section_key: str,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "title",
        sort_dir: str = "asc",
    ) -> tuple[list[dict], int]:
        """
        Paginated shows from DB with server-side sort (no Python-side scan).
        sort_by: title | year | folder
        """
        page = max(page, 1)
        page_size = max(1, min(page_size, 200))
        offset = (page - 1) * page_size
        where = "library_section_id = ? AND is_missing = 0"
        params: list = [section_key]
        q = (query or "").strip()
        if q:
            where += " AND (title LIKE ? OR folder_path LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like])

        if sort_by not in {"title", "year", "folder"}:
            sort_by = "title"
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        direction = "DESC" if sort_dir == "desc" else "ASC"

        if sort_by == "title":
            order_sql = f"title COLLATE NOCASE {direction}"
        elif sort_by == "year":
            order_sql = f"COALESCE(year, 0) {direction}, title COLLATE NOCASE {direction}"
        else:
            order_sql = f"folder_path COLLATE NOCASE {direction}"

        count_sql = f"SELECT COUNT(*) AS c FROM plex_shows_cache WHERE {where}"
        page_sql = (
            f"SELECT rating_key, title, year, folder_path, library_section_id, cached_at, last_seen_at, is_missing "
            f"FROM plex_shows_cache WHERE {where} ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )

        with get_conn(self.database_path) as conn:
            total = conn.execute(count_sql, tuple(params)).fetchone()["c"]
            rows = conn.execute(page_sql, tuple(params + [page_size, offset])).fetchall()
        return [dict(r) for r in rows], int(total)

    def get_cached_shows_sorted_all(
        self,
        section_key: str,
        query: str = "",
        sort_by: str = "title",
        sort_dir: str = "asc",
    ) -> list[dict]:
        """All rows matching filter, in the same order as list_shows DB fast path (no LIMIT)."""
        where = "library_section_id = ? AND is_missing = 0"
        params: list = [section_key]
        q = (query or "").strip()
        if q:
            where += " AND (title LIKE ? OR folder_path LIKE ?)"
            like = f"%{q}%"
            params.extend([like, like])

        if sort_by not in {"title", "year", "folder"}:
            sort_by = "title"
        if sort_dir not in {"asc", "desc"}:
            sort_dir = "asc"
        direction = "DESC" if sort_dir == "desc" else "ASC"

        if sort_by == "title":
            order_sql = f"title COLLATE NOCASE {direction}"
        elif sort_by == "year":
            order_sql = f"COALESCE(year, 0) {direction}, title COLLATE NOCASE {direction}"
        else:
            order_sql = f"folder_path COLLATE NOCASE {direction}"

        sql = (
            f"SELECT rating_key, title, year, folder_path, library_section_id, cached_at, last_seen_at, is_missing "
            f"FROM plex_shows_cache WHERE {where} ORDER BY {order_sql}"
        )
        with get_conn(self.database_path) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _normalize_title(value: str) -> str:
        return "".join(ch.lower() for ch in value if ch.isalnum())

    def refresh_cache(
        self,
        plex: PlexClient,
        section_key: str,
        sonarr_index: dict | None = None,
    ) -> tuple[int, dict[str, str]]:
        now = datetime.now(timezone.utc).isoformat()
        fresh = plex.list_show_metadata(section_key)
        sonarr_by_tvdb = (sonarr_index or {}).get("by_tvdb", {})
        sonarr_by_tmdb = (sonarr_index or {}).get("by_tmdb", {})
        sonarr_by_title = (sonarr_index or {}).get("by_title", {})
        query_by_tvdb = (sonarr_index or {}).get("query_by_tvdb", {})
        query_by_tmdb = (sonarr_index or {}).get("query_by_tmdb", {})
        query_by_title = (sonarr_index or {}).get("query_by_title", {})
        preferred_queries: dict[str, str] = {}
        for item in fresh:
            item["preferred_query"] = None
            tvdb_id = item.get("tvdb_id")
            tmdb_id = item.get("tmdb_id")
            normalized_title = self._normalize_title(item.get("title", ""))

            # Prefer Sonarr path when available to keep container paths canonical (e.g. /tv/...).
            if tvdb_id and sonarr_by_tvdb.get(str(tvdb_id)):
                item["folder_path"] = sonarr_by_tvdb[str(tvdb_id)]
                item["preferred_query"] = query_by_tvdb.get(str(tvdb_id))
                continue
            if tmdb_id and sonarr_by_tmdb.get(str(tmdb_id)):
                item["folder_path"] = sonarr_by_tmdb[str(tmdb_id)]
                item["preferred_query"] = query_by_tmdb.get(str(tmdb_id))
                continue
            if normalized_title and sonarr_by_title.get(normalized_title):
                item["folder_path"] = sonarr_by_title[normalized_title]
                item["preferred_query"] = query_by_title.get(normalized_title)
                continue

            # No Sonarr match; preserve Plex path and still set preferred query if available.
            if tvdb_id and query_by_tvdb.get(str(tvdb_id)):
                item["preferred_query"] = query_by_tvdb[str(tvdb_id)]
            elif tmdb_id and query_by_tmdb.get(str(tmdb_id)):
                item["preferred_query"] = query_by_tmdb[str(tmdb_id)]

        fresh = [item for item in fresh if item.get("folder_path")]
        fresh_map = {item["rating_key"]: item for item in fresh}
        for item in fresh:
            if item.get("preferred_query"):
                preferred_queries[item["rating_key"]] = item["preferred_query"]

        with get_conn(self.database_path) as conn:
            old = conn.execute(
                "SELECT rating_key FROM plex_shows_cache WHERE library_section_id = ?",
                (section_key,),
            ).fetchall()
            old_keys = {r["rating_key"] for r in old}
            new_keys = set(fresh_map.keys())

            for removed in old_keys - new_keys:
                conn.execute(
                    "UPDATE plex_shows_cache SET is_missing = 1, last_seen_at = ? WHERE rating_key = ?",
                    (now, removed),
                )

            for item in fresh:
                conn.execute(
                    "INSERT INTO plex_shows_cache(rating_key, title, year, folder_path, library_section_id, cached_at, last_seen_at, is_missing) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(rating_key) DO UPDATE SET title=excluded.title, year=excluded.year, folder_path=excluded.folder_path, "
                    "library_section_id=excluded.library_section_id, cached_at=excluded.cached_at, last_seen_at=excluded.last_seen_at, is_missing=0",
                    (
                        item["rating_key"],
                        item["title"],
                        item["year"],
                        item["folder_path"],
                        section_key,
                        now,
                        now,
                    ),
                )
            conn.commit()
        return len(fresh), preferred_queries
