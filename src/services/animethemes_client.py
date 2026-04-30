import json
import random
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from src.db.models import get_conn


class AnimeThemesClient:
    ANIME_INCLUDE = (
        "animethemes.animethemeentries.videos.audio,"
        "animethemes.song,"
        "animethemes.song.artists"
    )

    def __init__(self, database_path: str, base_url: str, app_max_rpm: int = 40, timeout: int = 20):
        self.database_path = database_path
        self.base_url = base_url.rstrip("/")
        self.app_max_rpm = app_max_rpm
        self.timeout = timeout

    def _now(self) -> float:
        return time.time()

    def _load_rate_state(self) -> dict[str, Any]:
        with get_conn(self.database_path) as conn:
            row = conn.execute("SELECT value FROM api_rate_state WHERE key = 'animethemes'").fetchone()
        if not row:
            return {"window_start": self._now(), "count": 0, "next_allowed_at": 0}
        return json.loads(row["value"])

    def _save_rate_state(self, state: dict[str, Any]) -> None:
        with get_conn(self.database_path) as conn:
            conn.execute(
                "INSERT INTO api_rate_state(key, value) VALUES ('animethemes', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(state),),
            )
            conn.commit()

    def _gate(self) -> None:
        state = self._load_rate_state()
        now = self._now()
        if now < state.get("next_allowed_at", 0):
            time.sleep(max(0, state["next_allowed_at"] - now))
            state = self._load_rate_state()
            now = self._now()

        if now - state.get("window_start", now) >= 60:
            state["window_start"] = now
            state["count"] = 0

        if state.get("count", 0) >= self.app_max_rpm:
            sleep_for = 60 - (now - state["window_start"])
            if sleep_for > 0:
                time.sleep(sleep_for)
            state = {"window_start": self._now(), "count": 0, "next_allowed_at": 0}

        state["count"] = state.get("count", 0) + 1
        self._save_rate_state(state)

    @staticmethod
    def to_romaji_query(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
        ascii_only = ascii_only.replace("-", " ")
        ascii_only = re.sub(r"[^A-Za-z0-9\s\.\']", " ", ascii_only)
        return re.sub(r"\s+", " ", ascii_only).strip()

    @staticmethod
    def _build_query_variants(query: str) -> list[str]:
        variants: list[str] = []
        base = AnimeThemesClient.to_romaji_query(query)
        if base:
            variants.append(base)

        # Relax punctuation-heavy titles (e.g. "2.5 Dimensional Seduction")
        relaxed = re.sub(r"[^\w\s]", " ", base)
        relaxed = re.sub(r"\s+", " ", relaxed).strip()
        if relaxed and relaxed not in variants:
            variants.append(relaxed)

        # Add title without leading numeric token when present.
        without_leading_num = re.sub(r"^\d+(?:\.\d+)?\s+", "", relaxed).strip()
        if without_leading_num and without_leading_num not in variants:
            variants.append(without_leading_num)

        return variants[:3]

    def _request_candidates(self, query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        params = {
            "filter[has]": "animethemes",
            "filter[name]": query,
            "include": self.ANIME_INCLUDE,
        }
        return self._request_candidates_with_params(query, params, mode="name")

    def _request_candidates_with_params(
        self,
        query: str,
        params: dict[str, str],
        mode: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self._gate()
        url = f"{self.base_url}/anime"
        requested_url = f"{url}?{urlencode(params)}"
        response = requests.get(url, params=params, timeout=self.timeout)
        debug: dict[str, Any] = {
            "query": query,
            "mode": mode,
            "requested_url": requested_url,
            "url": response.url,
            "status_code": response.status_code,
            "rate_limit_limit": response.headers.get("X-RateLimit-Limit"),
            "rate_limit_remaining": response.headers.get("X-RateLimit-Remaining"),
            "response_preview": response.text[:500],
        }

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "60"))
            jitter = random.uniform(0.1, 1.5)
            next_allowed = self._now() + retry_after + jitter
            state = self._load_rate_state()
            state["next_allowed_at"] = next_allowed
            self._save_rate_state(state)
            debug["retry_after"] = retry_after
            raise RuntimeError(f"AnimeThemes rate-limited; retry after {retry_after}s")

        response.raise_for_status()
        payload = response.json()
        debug["response_json"] = payload
        data = self._extract_anime_rows(payload)
        debug["anime_count"] = len(data)
        candidates = self._extract_candidates_from_anime_rows(data)
        debug["candidate_count"] = len(candidates)
        return candidates, debug

    def _request_candidates_by_direct_slug(self, slug: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self._gate()
        params = {"include": self.ANIME_INCLUDE}
        url = f"{self.base_url}/anime/{slug}"
        requested_url = f"{url}?{urlencode(params)}"
        response = requests.get(url, params=params, timeout=self.timeout)
        debug: dict[str, Any] = {
            "query": slug,
            "mode": "direct_slug",
            "requested_url": requested_url,
            "url": response.url,
            "status_code": response.status_code,
            "rate_limit_limit": response.headers.get("X-RateLimit-Limit"),
            "rate_limit_remaining": response.headers.get("X-RateLimit-Remaining"),
            "response_preview": response.text[:500],
        }

        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "60"))
            jitter = random.uniform(0.1, 1.5)
            next_allowed = self._now() + retry_after + jitter
            state = self._load_rate_state()
            state["next_allowed_at"] = next_allowed
            self._save_rate_state(state)
            debug["retry_after"] = retry_after
            raise RuntimeError(f"AnimeThemes rate-limited; retry after {retry_after}s")

        if response.status_code == 404:
            debug["anime_count"] = 0
            debug["candidate_count"] = 0
            return [], debug

        response.raise_for_status()
        payload = response.json()
        debug["response_json"] = payload
        data = self._extract_anime_rows(payload)
        debug["anime_count"] = len(data)
        candidates = self._extract_candidates_from_anime_rows(data)
        debug["candidate_count"] = len(candidates)
        return candidates, debug

    @staticmethod
    def _extract_anime_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        anime_value = payload.get("anime")
        if isinstance(anime_value, list):
            return anime_value
        if isinstance(anime_value, dict):
            return [anime_value]
        data_value = payload.get("data", [])
        if isinstance(data_value, list):
            return data_value
        if isinstance(data_value, dict):
            return [data_value]
        return []

    @staticmethod
    def _extract_candidates_from_anime_rows(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        for anime in data:
            name = anime.get("name", "Unknown")
            for theme in anime.get("animethemes", []):
                label = f"{name} - {theme.get('slug', 'theme')}"
                entries = theme.get("animethemeentries", [])
                for entry in entries:
                    for video in entry.get("videos", []):
                        audio_link = ""
                        audio_field = video.get("audio")
                        if isinstance(audio_field, dict):
                            audio_link = str(audio_field.get("link", "")).strip()
                        elif isinstance(audio_field, str):
                            audio_link = audio_field.strip()
                        if not audio_link:
                            continue
                        dedupe_key = (label.casefold(), audio_link.casefold())
                        if dedupe_key in seen_keys:
                            continue
                        seen_keys.add(dedupe_key)
                        candidates.append(
                            {
                                "source": "animethemes",
                                "label": label,
                                "audio_url": audio_link,
                                "meta_json": json.dumps(video, default=str),
                                "cached_at": datetime.now(timezone.utc).isoformat(),
                            }
                        )
        return candidates

    def _search_themes_internal(self, query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for variant in self._build_query_variants(query):
            candidates, debug = self._request_candidates(variant)
            debug["attempt_query"] = variant
            attempts.append(debug)
            if candidates:
                return candidates, {"attempts": attempts, "final": debug}

        # Slug fallback: UI pages use slug forms like 25_jigen_no_ririsa.
        for variant in self._build_slug_variants(query):
            params = {
                "filter[has]": "animethemes",
                "filter[slug]": variant,
                "include": self.ANIME_INCLUDE,
            }
            candidates, debug = self._request_candidates_with_params(variant, params, mode="slug")
            debug["attempt_query"] = variant
            attempts.append(debug)
            if candidates:
                return candidates, {"attempts": attempts, "final": debug}

        # Final fallback to direct resource route by slug.
        for variant in self._build_slug_variants(query):
            candidates, debug = self._request_candidates_by_direct_slug(variant)
            debug["attempt_query"] = variant
            attempts.append(debug)
            if candidates:
                return candidates, {"attempts": attempts, "final": debug}
        final = attempts[-1] if attempts else {"attempt_query": query}
        return [], {"attempts": attempts, "final": final}

    @staticmethod
    def _build_slug_variants(query: str) -> list[str]:
        base = AnimeThemesClient.to_romaji_query(query).casefold()
        if not base:
            return []

        def slugify(value: str) -> str:
            return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value)).strip("_")

        variants: list[str] = []
        raw_slug = slugify(base)
        if raw_slug:
            variants.append(raw_slug)

        # AnimeThemes slugs frequently collapse decimal punctuation (2.5 -> 25).
        decimal_collapsed = re.sub(r"(?<=\d)\.(?=\d)", "", base)
        collapsed_slug = slugify(decimal_collapsed)
        if collapsed_slug and collapsed_slug not in variants:
            variants.append(collapsed_slug)

        return variants[:3]

    def search_themes(self, query: str) -> list[dict[str, Any]]:
        candidates, _ = self._search_themes_internal(query)
        return candidates

    def search_themes_with_debug(self, query: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return self._search_themes_internal(query)
