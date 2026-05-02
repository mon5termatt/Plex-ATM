from datetime import datetime, timezone
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlparse

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)

from src.db.models import get_conn
from src.services.animethemes_client import AnimeThemesClient
from src.services.plex_cache_service import PlexCacheService
from src.services.plex_client import PlexClient
from src.services.settings_service import SettingsService
from src.services.sonarr_client import SonarrClient
from src.services.theme_apply_service import ThemeApplyService


shows_bp = Blueprint("shows", __name__)
_bulk_scan_lock = threading.Lock()
_bulk_scan_state: dict = {
    "running": False,
    "total": 0,
    "scanned": 0,
    "matched": 0,
    "saved_candidates": 0,
    "failed": 0,
    "current_title": "",
    "started_at": "",
    "finished_at": "",
    "message": "",
}


def _bulk_state_snapshot() -> dict:
    with _bulk_scan_lock:
        return dict(_bulk_scan_state)


def _bulk_state_update(**kwargs) -> None:
    with _bulk_scan_lock:
        _bulk_scan_state.update(kwargs)


def _services():
    db = current_app.config["DATABASE_PATH"]
    return {
        "settings": SettingsService(db),
        "cache": PlexCacheService(db),
        "apply": ThemeApplyService(db),
        "anime": AnimeThemesClient(
            db,
            current_app.config["ANIMETHEMES_BASE_URL"],
            app_max_rpm=current_app.config["ANIMETHEMES_APP_MAX_RPM"],
            timeout=current_app.config["ANIMETHEMES_HTTP_TIMEOUT"],
        ),
    }


def _is_within_trusted_paths(folder_path: str, trusted_paths: list[str]) -> bool:
    if not trusted_paths:
        return False
    normalized = os.path.normcase(os.path.abspath(folder_path))
    for trusted in trusted_paths:
        trusted_norm = os.path.normcase(os.path.abspath(trusted))
        if normalized.startswith(trusted_norm):
            return True
    return False


def _runtime_trusted_paths(svc: dict, include_sonarr: bool = True) -> list[str]:
    paths = list(svc["settings"].get("trusted_library_paths", []) or [])
    library_root_override = str(svc["settings"].get("library_root_override", "") or "").strip()
    if library_root_override:
        paths.append(library_root_override)
    sonarr_url = svc["settings"].get("sonarr_url", "")
    sonarr_api_key = svc["settings"].get("sonarr_api_key", "")
    if include_sonarr and sonarr_url and sonarr_api_key:
        try:
            sonarr_series = SonarrClient(sonarr_url, sonarr_api_key).list_series()
            for row in sonarr_series:
                path = str(row.get("path", "")).strip()
                if path:
                    paths.append(path)
        except Exception:
            pass
    # preserve order while deduplicating
    unique: list[str] = []
    seen: set[str] = set()
    for p in paths:
        key = p.casefold()
        if key and key not in seen:
            unique.append(p)
            seen.add(key)
    return unique


def _resolve_write_folder(svc: dict, folder_path: str) -> str:
    """
    If library_root_override is configured, write under that root using the show's leaf folder.
    Example: folder_path=/tv/Show Name and override=/media/anime -> /media/anime/Show Name
    """
    override_root = str(svc["settings"].get("library_root_override", "") or "").strip()
    if not override_root:
        return folder_path
    leaf = os.path.basename(os.path.normpath(folder_path))
    if not leaf:
        return folder_path
    return os.path.join(override_root, leaf)


def _resolve_existing_show_folder_path(svc: dict, folder_path: str, trusted_roots: list[str] | None = None) -> str:
    """
    Prefer an existing folder path at runtime.
    - If provided path exists, use it.
    - Otherwise try common/container roots (e.g. /tv) with same show folder basename.
    - Otherwise keep original.
    """
    raw = str(folder_path or "").strip()
    if not raw:
        return raw
    leaf = os.path.basename(os.path.normpath(raw))
    mapped = _apply_runtime_path_mappings(raw)
    if not leaf:
        return mapped or raw

    # Prefer container-native mounted roots first.
    candidate_roots = ["/tv", "/anime", "/media/tv", "/media/anime"]
    candidate_roots.extend(trusted_roots if trusted_roots is not None else _runtime_trusted_paths(svc))

    seen = set()
    for root in candidate_roots:
        root = str(root or "").strip()
        if not root:
            continue
        key = root.casefold()
        if key in seen:
            continue
        seen.add(key)
        candidate = os.path.join(root, leaf)
        if os.path.isdir(candidate):
            return candidate

    # Fallback to direct paths if no preferred-root candidate exists.
    if os.path.isdir(raw):
        return raw
    if mapped and os.path.isdir(mapped):
        return mapped

    return mapped or raw


def _apply_runtime_path_mappings(path: str) -> str:
    raw = (path or "").strip()
    mapping_spec = os.environ.get("APP_PATH_MAPPINGS", "").strip()
    if not raw or not mapping_spec:
        return raw
    mapped = raw
    for rule in mapping_spec.split(";"):
        rule = rule.strip()
        if not rule or "=" not in rule:
            continue
        src, dst = rule.split("=", 1)
        src = src.strip().rstrip("/")
        dst = dst.strip().rstrip("/")
        if not src or not dst:
            continue
        if mapped == src or mapped.startswith(src + "/"):
            mapped = dst + mapped[len(src):]
            break
    return mapped


def _find_theme_file_in_show_folder(folder_path: str) -> str:
    """
    Check only inside the show's own folder for current theme files.
    """
    base = _apply_runtime_path_mappings(str(folder_path or "").strip())
    if not base:
        return ""
    for filename in ("theme.mp3", "theme.ogg", "theme.m4a", "theme.flac"):
        candidate = os.path.join(base, filename)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _append_debug_log(settings_service: SettingsService, message: str) -> None:
    logs = settings_service.get("debug_logs", [])
    logs.append(f"{datetime.now(timezone.utc).isoformat()} | {message}")
    settings_service.set("debug_logs", logs[-80:])


def _set_show_api_debug(settings_service: SettingsService, rating_key: str, key: str, value: dict) -> None:
    payloads = settings_service.get("show_api_debug", {})
    show_payloads = payloads.get(rating_key, {})
    show_payloads[key] = value
    payloads[rating_key] = show_payloads
    settings_service.set("show_api_debug", payloads)


def _build_plex_poster_url(svc: dict, rating_key: str) -> str:
    plex_url = str(svc["settings"].get("plex_url", "") or "").strip().rstrip("/")
    plex_token = str(svc["settings"].get("plex_token", "") or "").strip()
    if not plex_url or not plex_token or not rating_key:
        return ""
    return f"{plex_url}/library/metadata/{rating_key}/thumb?X-Plex-Token={plex_token}"


def _normalized_list_state(raw: dict) -> dict:
    state = {
        "q": str(raw.get("q", "") or "").strip(),
        "theme_filter": str(raw.get("theme_filter", "all") or "").strip().lower() or "all",
        "sort_by": str(raw.get("sort_by", "title") or "").strip().lower() or "title",
        "sort_dir": str(raw.get("sort_dir", "asc") or "").strip().lower() or "asc",
        "page": str(raw.get("page", "1") or "").strip() or "1",
        "page_size": str(raw.get("page_size", "50") or "").strip() or "50",
        "view": str(raw.get("view", "table") or "").strip().lower() or "table",
    }
    if state["theme_filter"] not in {"all", "has", "none"}:
        state["theme_filter"] = "all"
    if state["sort_by"] not in {"title", "year", "folder", "has_theme"}:
        state["sort_by"] = "title"
    if state["sort_dir"] not in {"asc", "desc"}:
        state["sort_dir"] = "asc"
    if state["view"] not in {"table", "grid"}:
        state["view"] = "table"
    return state


def _query_from_animethemes_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return ""

    path_parts = [p for p in parsed.path.split("/") if p]
    if "anime" in path_parts:
        idx = path_parts.index("anime")
        if idx + 1 < len(path_parts):
            slug = path_parts[idx + 1].strip()
            if slug:
                return slug.replace("_", " ")

    query_map = parse_qs(parsed.query)
    name_value = (query_map.get("filter[name]") or [""])[0].strip()
    if name_value:
        return name_value
    slug_value = (query_map.get("filter[slug]") or [""])[0].strip()
    if slug_value:
        return slug_value.replace("_", " ")
    return ""


@shows_bp.route("/")
def home():
    return redirect(url_for("shows.list_shows"))


_SHOWS_LIST_QS_SESSION_KEY = "shows_list_qs"
_SHOWS_LIST_QS_MAX_LEN = 2000


@shows_bp.route("/shows")
def list_shows():
    svc = _services()
    library_key = svc["settings"].get("library_key", "")
    if library_key and not request.args:
        saved_qs = session.get(_SHOWS_LIST_QS_SESSION_KEY)
        if isinstance(saved_qs, str) and saved_qs.strip():
            trimmed = saved_qs.strip()[:_SHOWS_LIST_QS_MAX_LEN]
            return redirect(f"{url_for('shows.list_shows')}?{trimmed}")

    query = request.args.get("q", "").strip()
    theme_filter = request.args.get("theme_filter", "all").strip().lower()
    if theme_filter not in {"all", "has", "none"}:
        theme_filter = "all"
    sort_by = request.args.get("sort_by", "title").strip().lower()
    sort_dir = request.args.get("sort_dir", "asc").strip().lower()
    if sort_by not in {"title", "year", "folder", "has_theme"}:
        sort_by = "title"
    if sort_dir not in {"asc", "desc"}:
        sort_dir = "asc"
    view_mode = request.args.get("view", "table").strip().lower()
    if view_mode not in {"table", "grid"}:
        view_mode = "table"
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("page_size", "50"))
    except ValueError:
        page_size = 50
    page = max(page, 1)
    page_size = max(10, min(page_size, 200))

    shows: list[dict] = []
    total = 0
    # Fast path for large libraries: don't fetch Sonarr series list during shows index render.
    trusted_roots = _runtime_trusted_paths(svc, include_sonarr=False)

    use_db_page = (
        library_key
        and theme_filter == "all"
        and sort_by in {"title", "year", "folder"}
    )

    if library_key and use_db_page:
        shows, total = svc["cache"].get_cached_shows_sorted_page(
            library_key,
            query=query,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
            shows, total = svc["cache"].get_cached_shows_sorted_page(
                library_key,
                query=query,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_dir=sort_dir,
            )
            total_pages = max(1, (total + page_size - 1) // page_size)
        enriched = []
        for row in shows:
            show = dict(row)
            show["folder_path"] = _resolve_existing_show_folder_path(svc, show["folder_path"], trusted_roots=trusted_roots)
            current_theme_path = _find_theme_file_in_show_folder(show["folder_path"])
            show["has_current_theme"] = bool(current_theme_path)
            show["poster_url"] = _build_plex_poster_url(svc, show["rating_key"])
            enriched.append(show)
        shows = enriched
    elif library_key:
        shows = svc["cache"].get_cached_shows_filtered(library_key, query=query)
        enriched = []
        for row in shows:
            show = dict(row)
            show["folder_path"] = _resolve_existing_show_folder_path(svc, show["folder_path"], trusted_roots=trusted_roots)
            current_theme_path = _find_theme_file_in_show_folder(show["folder_path"])
            show["has_current_theme"] = bool(current_theme_path)
            show["poster_url"] = _build_plex_poster_url(svc, show["rating_key"])
            if theme_filter == "has" and not show["has_current_theme"]:
                continue
            if theme_filter == "none" and show["has_current_theme"]:
                continue
            enriched.append(show)

        reverse = sort_dir == "desc"
        if sort_by == "title":
            enriched.sort(key=lambda s: (s.get("title") or "").casefold(), reverse=reverse)
        elif sort_by == "year":
            enriched.sort(key=lambda s: (s.get("year") or 0, (s.get("title") or "").casefold()), reverse=reverse)
        elif sort_by == "folder":
            enriched.sort(key=lambda s: (s.get("folder_path") or "").casefold(), reverse=reverse)
        elif sort_by == "has_theme":
            enriched.sort(key=lambda s: (1 if s.get("has_current_theme") else 0, (s.get("title") or "").casefold()), reverse=reverse)

        total = len(enriched)
        total_pages = max(1, (total + page_size - 1) // page_size) if library_key else 1
        if page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        end = start + page_size
        shows = enriched[start:end]
    else:
        total_pages = 1
    candidate_counts: dict[str, int] = {}
    rating_keys = [str(s.get("rating_key", "") or "") for s in shows if s.get("rating_key")]
    if rating_keys:
        placeholders = ",".join("?" for _ in rating_keys)
        with get_conn(current_app.config["DATABASE_PATH"]) as conn:
            rows = conn.execute(
                f"SELECT show_rating_key, COUNT(*) AS c FROM theme_candidates "
                f"WHERE show_rating_key IN ({placeholders}) GROUP BY show_rating_key",
                tuple(rating_keys),
            ).fetchall()
        candidate_counts = {str(r["show_rating_key"]): int(r["c"] or 0) for r in rows}
    for show in shows:
        show["candidate_count"] = candidate_counts.get(str(show.get("rating_key", "")), 0)

    if request.query_string:
        session[_SHOWS_LIST_QS_SESSION_KEY] = request.query_string.decode()[:_SHOWS_LIST_QS_MAX_LEN]

    bulk_scan_results = svc["settings"].get("last_bulk_scan_results", {}) or {}
    return render_template(
        "shows.html",
        shows=shows,
        library_key=library_key,
        q=query,
        theme_filter=theme_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
        view_mode=view_mode,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
        bulk_scan_results=bulk_scan_results,
    )


@shows_bp.route("/shows/rescan", methods=["POST"])
def rescan_shows():
    svc = _services()
    plex_url = svc["settings"].get("plex_url", "")
    plex_token = svc["settings"].get("plex_token", "")
    library_key = svc["settings"].get("library_key", "")
    if not (plex_url and plex_token and library_key):
        flash("Configure Plex URL/token/library in settings first.", "error")
        return redirect(url_for("settings.settings"))

    try:
        _append_debug_log(
            svc["settings"],
            f"About to request Plex section scan: {plex_url.rstrip('/')}/library/sections/{library_key}/all?type=2",
        )
        sonarr_index = None
        sonarr_url = svc["settings"].get("sonarr_url", "")
        sonarr_api_key = svc["settings"].get("sonarr_api_key", "")
        if sonarr_url and sonarr_api_key:
            try:
                _append_debug_log(
                    svc["settings"],
                    f"About to request Sonarr series list: {sonarr_url.rstrip('/')}/api/v3/series",
                )
                sonarr_index = SonarrClient(sonarr_url, sonarr_api_key).build_path_index()
                _append_debug_log(
                    svc["settings"],
                    f"Sonarr index loaded: tvdb={len(sonarr_index.get('by_tvdb', {}))}, title={len(sonarr_index.get('by_title', {}))}",
                )
            except Exception:
                sonarr_index = None
                _append_debug_log(svc["settings"], "Sonarr index load failed; continuing without Sonarr paths.")

        count, preferred_queries = svc["cache"].refresh_cache(
            PlexClient(plex_url, plex_token),
            library_key,
            sonarr_index=sonarr_index,
        )
        svc["settings"].set("last_rescan_at", datetime.now(timezone.utc).isoformat())
        existing_overrides = svc["settings"].get("show_search_overrides", {})
        existing_overrides.update(preferred_queries)
        svc["settings"].set("show_search_overrides", existing_overrides)
        _append_debug_log(svc["settings"], f"Plex rescan complete for section={library_key}. cached={count}")
        _append_debug_log(svc["settings"], f"Preferred original-title queries mapped={len(preferred_queries)}")
        flash(f"Library rescan completed. {count} shows cached.", "success")
    except Exception as exc:
        _append_debug_log(svc["settings"], f"Plex rescan failed: {exc}")
        flash(f"Rescan failed: {exc}", "error")
    return redirect(url_for("shows.list_shows"))


@shows_bp.route("/shows/bulk-find-candidates", methods=["POST"])
def bulk_find_candidates():
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": False, "message": "Use /shows/bulk-find-candidates/start for live progress."}), 400

    svc = _services()
    library_key = svc["settings"].get("library_key", "")
    if not library_key:
        flash("Configure and select a Plex library in Settings first.", "error")
        return redirect(url_for("settings.settings"))

    shows = svc["cache"].get_cached_shows_filtered(library_key, query="")
    search_overrides = svc["settings"].get("show_search_overrides", {}) or {}
    scanned = 0
    with_candidates = 0
    saved_candidates = 0
    failed = 0
    no_candidates_titles: list[str] = []
    failed_titles: list[str] = []

    for row in shows:
        rating_key = str(row["rating_key"])
        title = str(row["title"] or "").strip()
        raw_query = str(search_overrides.get(rating_key, title) or title).strip()
        search_query = AnimeThemesClient.to_romaji_query(raw_query) or raw_query
        if not search_query:
            continue
        scanned += 1
        try:
            candidates = svc["anime"].search_themes(search_query)
            with get_conn(current_app.config["DATABASE_PATH"]) as conn:
                conn.execute(
                    "DELETE FROM theme_candidates WHERE show_rating_key = ? AND source = 'animethemes'",
                    (rating_key,),
                )
                for candidate in candidates:
                    conn.execute(
                        "INSERT INTO theme_candidates(show_rating_key, source, label, audio_url, meta_json, cached_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            rating_key,
                            candidate["source"],
                            candidate["label"],
                            candidate["audio_url"],
                            candidate["meta_json"],
                            candidate["cached_at"],
                        ),
                    )
                conn.commit()
            if candidates:
                with_candidates += 1
                saved_candidates += len(candidates)
            else:
                no_candidates_titles.append(title)
                _append_debug_log(
                    svc["settings"],
                    f"Bulk candidate scan found none for '{title}' query='{search_query}'",
                )
        except Exception as exc:
            failed += 1
            failed_titles.append(title)
            _append_debug_log(
                svc["settings"],
                f"Bulk candidate scan failed for '{title}' query='{search_query}': {exc}",
            )

    svc["settings"].set(
        "last_bulk_scan_results",
        {
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "scanned": scanned,
            "matched": with_candidates,
            "saved_candidates": saved_candidates,
            "failed": failed,
            "no_candidates_titles": no_candidates_titles[:200],
            "failed_titles": failed_titles[:200],
        },
    )

    flash(
        f"Bulk scan complete. Scanned {scanned} shows, matched {with_candidates}, "
        f"saved {saved_candidates} candidates, failed {failed}.",
        "success" if failed == 0 else "error",
    )
    return redirect(url_for("shows.list_shows"))


def _run_bulk_scan_job(app, selected_rating_keys: list[str] | None = None) -> None:
    with app.app_context():
        svc = _services()
        library_key = svc["settings"].get("library_key", "")
        shows = svc["cache"].get_cached_shows_filtered(library_key, query="") if library_key else []
        if selected_rating_keys:
            selected_set = {str(v) for v in selected_rating_keys}
            shows = [row for row in shows if str(row["rating_key"]) in selected_set]
        search_overrides = svc["settings"].get("show_search_overrides", {}) or {}

        total = len(shows)
        scanned = 0
        with_candidates = 0
        saved_candidates = 0
        failed = 0
        no_candidates_titles: list[str] = []
        failed_titles: list[str] = []

        _bulk_state_update(
            running=True,
            total=total,
            scanned=0,
            matched=0,
            saved_candidates=0,
            failed=0,
            current_title="",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at="",
            message="Bulk scan started.",
        )

        for row in shows:
            rating_key = str(row["rating_key"])
            title = str(row["title"] or "").strip()
            raw_query = str(search_overrides.get(rating_key, title) or title).strip()
            search_query = AnimeThemesClient.to_romaji_query(raw_query) or raw_query
            if not search_query:
                continue

            _bulk_state_update(current_title=title)
            scanned += 1
            try:
                candidates, debug = svc["anime"].search_themes_with_debug(search_query)
                attempts = debug.get("attempts", [])
                with get_conn(current_app.config["DATABASE_PATH"]) as conn:
                    conn.execute(
                        "DELETE FROM theme_candidates WHERE show_rating_key = ? AND source = 'animethemes'",
                        (rating_key,),
                    )
                    for candidate in candidates:
                        conn.execute(
                            "INSERT INTO theme_candidates(show_rating_key, source, label, audio_url, meta_json, cached_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                rating_key,
                                candidate["source"],
                                candidate["label"],
                                candidate["audio_url"],
                                candidate["meta_json"],
                                candidate["cached_at"],
                            ),
                        )
                    conn.commit()
                if candidates:
                    with_candidates += 1
                    saved_candidates += len(candidates)
                else:
                    no_candidates_titles.append(title)
                    _append_debug_log(
                        svc["settings"],
                        f"Bulk candidate scan found none for '{title}' query='{search_query}'",
                    )

                # Be extra conservative near server limits.
                remaining_values = []
                for attempt in attempts:
                    remaining = attempt.get("rate_limit_remaining")
                    if remaining is None:
                        continue
                    try:
                        remaining_values.append(int(str(remaining)))
                    except ValueError:
                        continue
                if remaining_values and min(remaining_values) <= 2:
                    time.sleep(2.0)
                else:
                    time.sleep(0.15)
            except Exception as exc:
                failed += 1
                failed_titles.append(title)
                _append_debug_log(
                    svc["settings"],
                    f"Bulk candidate scan failed for '{title}' query='{search_query}': {exc}",
                )

            _bulk_state_update(
                scanned=scanned,
                matched=with_candidates,
                saved_candidates=saved_candidates,
                failed=failed,
                message=f"Scanning {scanned}/{total}: {title}",
            )

        final_message = (
            f"Bulk scan complete. Scanned {scanned} shows, matched {with_candidates}, "
            f"saved {saved_candidates} candidates, failed {failed}."
        )
        svc["settings"].set(
            "last_bulk_scan_results",
            {
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "scanned": scanned,
                "matched": with_candidates,
                "saved_candidates": saved_candidates,
                "failed": failed,
                "no_candidates_titles": no_candidates_titles[:200],
                "failed_titles": failed_titles[:200],
            },
        )
        _bulk_state_update(
            running=False,
            current_title="",
            finished_at=datetime.now(timezone.utc).isoformat(),
            message=final_message,
        )


@shows_bp.route("/shows/bulk-find-candidates/start", methods=["POST"])
def start_bulk_find_candidates():
    svc = _services()
    library_key = svc["settings"].get("library_key", "")
    if not library_key:
        return jsonify({"ok": False, "message": "Configure and select a Plex library in Settings first."}), 400

    state = _bulk_state_snapshot()
    if state.get("running"):
        return jsonify({"ok": False, "message": "Bulk scan already running.", "state": state}), 409

    selected_rating_keys = request.form.getlist("selected_rating_keys")
    if not selected_rating_keys and request.is_json:
        payload = request.get_json(silent=True) or {}
        selected_rating_keys = [str(v) for v in payload.get("selected_rating_keys", []) if str(v).strip()]

    app = current_app._get_current_object()
    worker = threading.Thread(target=_run_bulk_scan_job, args=(app, selected_rating_keys), daemon=True)
    worker.start()
    return jsonify({"ok": True, "message": "Bulk scan started.", "state": _bulk_state_snapshot()})


@shows_bp.route("/shows/bulk-find-candidates/status")
def bulk_find_candidates_status():
    return jsonify({"ok": True, "state": _bulk_state_snapshot()})


@shows_bp.route("/shows/bulk-scan")
def bulk_scan_page():
    svc = _services()
    library_key = svc["settings"].get("library_key", "")
    if not library_key:
        flash("Configure Plex and select a TV library in Settings first.", "error")
        return redirect(url_for("settings.settings"))

    shows = svc["cache"].get_cached_shows_filtered(library_key, query="")
    rating_keys = [str(s["rating_key"]) for s in shows]
    candidate_counts: dict[str, int] = {}
    animethemes_counts: dict[str, int] = {}
    if rating_keys:
        placeholders = ",".join("?" for _ in rating_keys)
        with get_conn(current_app.config["DATABASE_PATH"]) as conn:
            rows = conn.execute(
                f"SELECT show_rating_key, COUNT(*) AS c FROM theme_candidates "
                f"WHERE show_rating_key IN ({placeholders}) GROUP BY show_rating_key",
                tuple(rating_keys),
            ).fetchall()
            animethemes_rows = conn.execute(
                f"SELECT show_rating_key, COUNT(*) AS c FROM theme_candidates "
                f"WHERE source = 'animethemes' AND show_rating_key IN ({placeholders}) GROUP BY show_rating_key",
                tuple(rating_keys),
            ).fetchall()
        candidate_counts = {str(r["show_rating_key"]): int(r["c"] or 0) for r in rows}
        animethemes_counts = {str(r["show_rating_key"]): int(r["c"] or 0) for r in animethemes_rows}

    enriched = []
    trusted_roots = _runtime_trusted_paths(svc, include_sonarr=False)
    for row in shows:
        show = dict(row)
        show["folder_path"] = _resolve_existing_show_folder_path(svc, show["folder_path"], trusted_roots=trusted_roots)
        show["candidate_count"] = candidate_counts.get(str(show["rating_key"]), 0)
        show["animethemes_count"] = animethemes_counts.get(str(show["rating_key"]), 0)
        enriched.append(show)

    return render_template(
        "bulk_scan.html",
        shows=enriched,
        bulk_scan_results=svc["settings"].get("last_bulk_scan_results", {}) or {},
    )


@shows_bp.route("/shows/<rating_key>")
def show_detail(rating_key: str):
    svc = _services()
    svc["settings"].set("debug_logs", [])
    trusted_roots = _runtime_trusted_paths(svc, include_sonarr=False)
    with get_conn(current_app.config["DATABASE_PATH"]) as conn:
        show = conn.execute(
            "SELECT rating_key, title, year, folder_path FROM plex_shows_cache WHERE rating_key = ?",
            (rating_key,),
        ).fetchone()
        candidates = conn.execute(
            "SELECT id, source, label, audio_url FROM theme_candidates WHERE show_rating_key = ? ORDER BY id DESC",
            (rating_key,),
        ).fetchall()
        installs = conn.execute(
            "SELECT installed_from, installed_file, installed_at, status, notes "
            "FROM theme_installs WHERE show_rating_key = ? ORDER BY id DESC LIMIT 20",
            (rating_key,),
        ).fetchall()
    if not show:
        flash("Show not found in cache.", "error")
        return redirect(url_for("shows.list_shows"))
    show_data = dict(show)
    show_data["folder_path"] = _resolve_existing_show_folder_path(
        svc,
        show_data["folder_path"],
        trusted_roots=trusted_roots,
    )
    show_data["poster_url"] = _build_plex_poster_url(svc, show_data["rating_key"])

    live_plex = None
    plex_url = svc["settings"].get("plex_url", "")
    plex_token = svc["settings"].get("plex_token", "")
    if plex_url and plex_token:
        try:
            live_plex = PlexClient(plex_url, plex_token).get_show_metadata_raw(rating_key)
        except Exception as exc:
            live_plex = {"error": str(exc)}

    live_sonarr = None
    sonarr_url = svc["settings"].get("sonarr_url", "")
    sonarr_api_key = svc["settings"].get("sonarr_api_key", "")
    if sonarr_url and sonarr_api_key:
        try:
            tvdb_id = None
            tmdb_id = None
            if live_plex and isinstance(live_plex, dict):
                metadata = live_plex.get("MediaContainer", {}).get("Metadata", [])
                if metadata:
                    guid_items = metadata[0].get("Guid", [])
                    for item in guid_items:
                        guid_value = str(item.get("id", ""))
                        if guid_value.startswith("tvdb://"):
                            raw = guid_value.split("tvdb://", 1)[1]
                            if raw.isdigit():
                                tvdb_id = int(raw)
                        if guid_value.startswith("tmdb://"):
                            raw = guid_value.split("tmdb://", 1)[1]
                            if raw.isdigit():
                                tmdb_id = int(raw)
            live_sonarr = SonarrClient(sonarr_url, sonarr_api_key).find_series_for_show(
                show_data["title"],
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
            )
        except Exception as exc:
            live_sonarr = {"error": str(exc)}

    search_overrides = svc["settings"].get("show_search_overrides", {})
    animethemes_url_overrides = svc["settings"].get("show_animethemes_url_overrides", {}) or {}
    initial_query = search_overrides.get(rating_key, show_data["title"])
    initial_query = AnimeThemesClient.to_romaji_query(initial_query) or initial_query
    initial_animethemes_url_override = str(animethemes_url_overrides.get(rating_key, "") or "").strip()
    sonarr_alternate_queries: list[str] = []
    if isinstance(live_sonarr, dict):
        seen = set()
        for item in live_sonarr.get("alternateTitles", []) or []:
            alt_title = AnimeThemesClient.to_romaji_query(str(item.get("title", "")).strip())
            if alt_title and alt_title.casefold() not in seen:
                sonarr_alternate_queries.append(alt_title)
                seen.add(alt_title.casefold())
            if len(sonarr_alternate_queries) >= 8:
                break
    debug_logs = []
    api_debug = svc["settings"].get("show_api_debug", {}).get(rating_key, {})
    local_theme_path = _find_theme_file_in_show_folder(show_data["folder_path"])
    local_theme_available = bool(local_theme_path)
    back_to_list_params = _normalized_list_state(request.args)
    return render_template(
        "show_detail.html",
        show=show_data,
        candidates=[dict(c) for c in candidates],
        local_theme_available=local_theme_available,
        installs=[dict(i) for i in installs],
        live_plex=live_plex,
        live_sonarr=live_sonarr,
        sonarr_alternate_queries=sonarr_alternate_queries,
        api_debug=api_debug,
        initial_search_query=initial_query,
        initial_animethemes_url_override=initial_animethemes_url_override,
        back_to_list_params=back_to_list_params,
        back_to_list_url=url_for("shows.list_shows", **back_to_list_params),
        animethemes_base_url=current_app.config["ANIMETHEMES_BASE_URL"].rstrip("/"),
        debug_logs=debug_logs,
    )


@shows_bp.route("/shows/<rating_key>/current-theme")
def play_current_theme(rating_key: str):
    svc = _services()
    with get_conn(current_app.config["DATABASE_PATH"]) as conn:
        show = conn.execute(
            "SELECT rating_key, folder_path FROM plex_shows_cache WHERE rating_key = ?",
            (rating_key,),
        ).fetchone()
    if not show:
        flash("Show not found.", "error")
        return redirect(url_for("shows.list_shows"))

    target_folder = _resolve_existing_show_folder_path(svc, show["folder_path"])
    trusted_paths = _runtime_trusted_paths(svc)
    if not _is_within_trusted_paths(target_folder, trusted_paths):
        flash("Current theme path is outside trusted library paths.", "error")
        return redirect(url_for("shows.list_shows"))

    theme_path = _find_theme_file_in_show_folder(target_folder)
    if theme_path:
        return send_file(theme_path)

    flash("No current theme file found for this show.", "error")
    return redirect(url_for("shows.list_shows"))


@shows_bp.route("/shows/<rating_key>/find", methods=["POST"])
def find_candidates(rating_key: str):
    svc = _services()
    with get_conn(current_app.config["DATABASE_PATH"]) as conn:
        show = conn.execute(
            "SELECT rating_key, title FROM plex_shows_cache WHERE rating_key = ?",
            (rating_key,),
        ).fetchone()
    if not show:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "message": "Show missing from cache."}), 404
        flash("Show missing from cache.", "error")
        return redirect(url_for("shows.list_shows"))
    raw_query = request.form.get("search_query", "").strip() or show["title"]
    manual_url_override = request.form.get("animethemes_url_override", "").strip()
    derived_query_from_url = _query_from_animethemes_url(manual_url_override)
    search_query_seed = derived_query_from_url or raw_query
    search_query = AnimeThemesClient.to_romaji_query(search_query_seed) or search_query_seed
    url_overrides = svc["settings"].get("show_animethemes_url_overrides", {}) or {}
    if manual_url_override:
        url_overrides[rating_key] = manual_url_override
    else:
        url_overrides.pop(rating_key, None)
    svc["settings"].set("show_animethemes_url_overrides", url_overrides)
    try:
        _append_debug_log(
            svc["settings"],
            "About to request AnimeThemes: "
            f"{current_app.config['ANIMETHEMES_BASE_URL'].rstrip('/')}/anime "
            f"for title='{show['title']}' query='{search_query}'",
        )
        candidates, debug = svc["anime"].search_themes_with_debug(search_query)
        attempts = debug.get("attempts", [])

        # If primary query misses, fall back to Sonarr alternate titles for this show.
        if not candidates:
            sonarr_url = svc["settings"].get("sonarr_url", "")
            sonarr_api_key = svc["settings"].get("sonarr_api_key", "")
            if sonarr_url and sonarr_api_key:
                try:
                    sonarr_series = SonarrClient(sonarr_url, sonarr_api_key).find_series_for_show(show["title"])
                    if sonarr_series:
                        seen_queries = {search_query.casefold()}
                        for alias in sonarr_series.get("alternateTitles", []) or []:
                            alias_query = AnimeThemesClient.to_romaji_query(str(alias.get("title", "")).strip())
                            if not alias_query or alias_query.casefold() in seen_queries:
                                continue
                            seen_queries.add(alias_query.casefold())
                            _append_debug_log(
                                svc["settings"],
                                f"Fallback AnimeThemes alias query for '{show['title']}': '{alias_query}'",
                            )
                            alias_candidates, alias_debug = svc["anime"].search_themes_with_debug(alias_query)
                            attempts.extend(alias_debug.get("attempts", []))
                            if alias_candidates:
                                candidates = alias_candidates
                                break
                except Exception as exc:
                    _append_debug_log(svc["settings"], f"Sonarr alias fallback lookup failed: {exc}")
        with get_conn(current_app.config["DATABASE_PATH"]) as conn:
            conn.execute(
                "DELETE FROM theme_candidates WHERE show_rating_key = ? AND source = 'animethemes'",
                (rating_key,),
            )
            for candidate in candidates:
                conn.execute(
                    "INSERT INTO theme_candidates(show_rating_key, source, label, audio_url, meta_json, cached_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        rating_key,
                        candidate["source"],
                        candidate["label"],
                        candidate["audio_url"],
                        candidate["meta_json"],
                        candidate["cached_at"],
                    ),
                )
            conn.commit()
        _set_show_api_debug(
            svc["settings"],
            rating_key,
            "animethemes_lookup",
            {
                "show_title": show["title"],
                "effective_query": search_query,
                "attempts": attempts,
                "saved_candidates_count": len(candidates),
            },
        )
        for idx, attempt in enumerate(attempts, start=1):
            _append_debug_log(
                svc["settings"],
                "AnimeThemes attempt "
                f"{idx} title='{show['title']}' query='{attempt.get('attempt_query')}' status={attempt.get('status_code')} "
                f"anime={attempt.get('anime_count', 0)} candidates={attempt.get('candidate_count', 0)} "
                f"remaining={attempt.get('rate_limit_remaining')} url={attempt.get('url')} requested={attempt.get('requested_url')}",
            )
            _append_debug_log(
                svc["settings"],
                "AnimeThemes response preview "
                f"attempt={idx} title='{show['title']}' body={attempt.get('response_preview', '').replace(chr(10), ' ')}",
            )
        message = f"Saved {len(candidates)} AnimeThemes candidates."
        if request.headers.get("X-Requested-With") == "fetch":
            with get_conn(current_app.config["DATABASE_PATH"]) as conn:
                rows = conn.execute(
                    "SELECT id, source, label, audio_url FROM theme_candidates WHERE show_rating_key = ? ORDER BY id DESC",
                    (rating_key,),
                ).fetchall()
                show_row = conn.execute(
                    "SELECT folder_path FROM plex_shows_cache WHERE rating_key = ?",
                    (rating_key,),
                ).fetchone()
            local_theme_url = ""
            if show_row:
                resolved_folder = _resolve_existing_show_folder_path(svc, show_row["folder_path"])
                if _find_theme_file_in_show_folder(resolved_folder):
                    local_theme_url = url_for("shows.play_current_theme", rating_key=rating_key)
            return jsonify(
                {
                    "ok": True,
                    "message": message,
                    "candidates": [dict(r) for r in rows],
                    "local_theme_url": local_theme_url,
                    "show_title": show["title"],
                }
            )
        flash(message, "success")
    except Exception as exc:
        _append_debug_log(svc["settings"], f"AnimeThemes lookup failed for '{show['title']}' query='{search_query}': {exc}")
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "message": f"Theme lookup failed: {exc}"}), 500
        flash(f"Theme lookup failed: {exc}", "error")
    list_state = _normalized_list_state(request.form)
    return redirect(url_for("shows.show_detail", rating_key=rating_key, **list_state))


@shows_bp.route("/shows/<rating_key>/quick-scan", methods=["POST"])
def quick_scan_show(rating_key: str):
    svc = _services()
    with get_conn(current_app.config["DATABASE_PATH"]) as conn:
        show = conn.execute(
            "SELECT rating_key, title FROM plex_shows_cache WHERE rating_key = ?",
            (rating_key,),
        ).fetchone()
    if not show:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "message": "Show missing from cache."}), 404
        flash("Show missing from cache.", "error")
        return redirect(url_for("shows.list_shows", **_normalized_list_state(request.form)))

    search_overrides = svc["settings"].get("show_search_overrides", {}) or {}
    url_overrides = svc["settings"].get("show_animethemes_url_overrides", {}) or {}
    url_override = str(url_overrides.get(rating_key, "") or "").strip()
    derived_query = _query_from_animethemes_url(url_override)
    raw_query = str(search_overrides.get(rating_key, show["title"]) or show["title"]).strip()
    search_query_seed = derived_query or raw_query
    search_query = AnimeThemesClient.to_romaji_query(search_query_seed) or search_query_seed

    try:
        candidates = svc["anime"].search_themes(search_query)
        with get_conn(current_app.config["DATABASE_PATH"]) as conn:
            conn.execute(
                "DELETE FROM theme_candidates WHERE show_rating_key = ? AND source = 'animethemes'",
                (rating_key,),
            )
            for candidate in candidates:
                conn.execute(
                    "INSERT INTO theme_candidates(show_rating_key, source, label, audio_url, meta_json, cached_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        rating_key,
                        candidate["source"],
                        candidate["label"],
                        candidate["audio_url"],
                        candidate["meta_json"],
                        candidate["cached_at"],
                    ),
                )
            conn.commit()
            show_row = conn.execute(
                "SELECT folder_path FROM plex_shows_cache WHERE rating_key = ?",
                (rating_key,),
            ).fetchone()
        has_current_theme = False
        if show_row:
            resolved_folder = _resolve_existing_show_folder_path(svc, show_row["folder_path"])
            has_current_theme = bool(_find_theme_file_in_show_folder(resolved_folder))
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(
                {
                    "ok": True,
                    "message": f"Quick scan complete for '{show['title']}'. Saved {len(candidates)} candidates.",
                    "rating_key": rating_key,
                    "candidate_count": len(candidates),
                    "has_current_theme": has_current_theme,
                }
            )
        flash(f"Quick scan complete for '{show['title']}'. Saved {len(candidates)} candidates.", "success")
    except Exception as exc:
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify({"ok": False, "message": f"Quick scan failed for '{show['title']}': {exc}"}), 500
        flash(f"Quick scan failed for '{show['title']}': {exc}", "error")

    return redirect(url_for("shows.list_shows", **_normalized_list_state(request.form)))


@shows_bp.route("/shows/<rating_key>/apply", methods=["POST"])
def apply_candidate(rating_key: str):
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    candidate_id = request.form.get("candidate_id", "").strip()
    svc = _services()
    with get_conn(current_app.config["DATABASE_PATH"]) as conn:
        show = conn.execute(
            "SELECT rating_key, folder_path FROM plex_shows_cache WHERE rating_key = ?",
            (rating_key,),
        ).fetchone()
        candidate = conn.execute(
            "SELECT id, source, audio_url FROM theme_candidates WHERE id = ? AND show_rating_key = ?",
            (candidate_id, rating_key),
        ).fetchone()
    if not show or not candidate:
        msg = "Candidate or show not found."
        if is_fetch:
            return jsonify({"ok": False, "message": msg}), 404
        flash(msg, "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))
    audio_url = str(candidate["audio_url"] or "").strip()
    if not audio_url:
        msg = "This candidate has no download URL."
        if is_fetch:
            return jsonify({"ok": False, "message": msg}), 400
        flash(msg, "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))
    trusted_paths = _runtime_trusted_paths(svc)
    show_folder = _resolve_existing_show_folder_path(svc, show["folder_path"])
    target_folder = _resolve_write_folder(svc, show_folder)
    if not _is_within_trusted_paths(target_folder, trusted_paths):
        msg = "Target folder is outside trusted Plex library paths."
        if is_fetch:
            return jsonify({"ok": False, "message": msg}), 403
        flash(msg, "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))
    if is_fetch:

        def ndjson_stream():
            for ev in svc["apply"].install_from_url_with_progress(show["rating_key"], target_folder, audio_url):
                if ev.get("type") == "progress":
                    row = {
                        "type": "progress",
                        "message": ev.get("message", ""),
                    }
                    if "completed_steps" in ev:
                        row["completed_steps"] = ev.get("completed_steps")
                    if "total_steps" in ev:
                        row["total_steps"] = ev.get("total_steps")
                    if ev.get("stage"):
                        row["stage"] = ev.get("stage")
                    yield json.dumps(row) + "\n"
                elif ev.get("type") == "done":
                    ok_ev = bool(ev.get("ok"))
                    raw_msg = str(ev.get("message", "") or "")
                    payload = {
                        "type": "done",
                        "ok": ok_ev,
                        "message": (f"Theme applied: {raw_msg}" if ok_ev else f"Apply failed: {raw_msg}"),
                    }
                    if ok_ev:
                        payload["local_theme_url"] = url_for("shows.play_current_theme", rating_key=rating_key)
                    else:
                        payload["error_detail"] = raw_msg
                    if not ok_ev and isinstance(ev.get("failed_step"), int):
                        payload["failed_step"] = ev["failed_step"]
                    yield json.dumps(payload) + "\n"

        return Response(
            stream_with_context(ndjson_stream()),
            mimetype="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )

    ok, message = svc["apply"].install_from_url(show["rating_key"], target_folder, audio_url)
    flash(f"Theme applied: {message}" if ok else f"Apply failed: {message}", "success" if ok else "error")
    return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))


@shows_bp.route("/shows/<rating_key>/upload", methods=["POST"])
def upload_theme(rating_key: str):
    svc = _services()
    file = request.files.get("theme_file")
    if not file or not file.filename:
        flash("Choose an audio file first.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))
    if not file.filename.lower().endswith((".mp3", ".m4a", ".flac", ".ogg")):
        flash("Only audio file uploads are supported.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))

    with get_conn(current_app.config["DATABASE_PATH"]) as conn:
        show = conn.execute(
            "SELECT rating_key, folder_path FROM plex_shows_cache WHERE rating_key = ?",
            (rating_key,),
        ).fetchone()
        if not show:
            flash("Show not found.", "error")
            return redirect(url_for("shows.list_shows"))

    trusted_paths = _runtime_trusted_paths(svc)
    show_folder = _resolve_existing_show_folder_path(svc, show["folder_path"])
    target_folder = _resolve_write_folder(svc, show_folder)
    if not _is_within_trusted_paths(target_folder, trusted_paths):
        flash("Target folder is outside trusted Plex library paths.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))

    ok, message = svc["apply"].install_from_upload(show["rating_key"], target_folder, file.read())
    if ok:
        with get_conn(current_app.config["DATABASE_PATH"]) as conn:
            conn.execute(
                "INSERT INTO theme_candidates(show_rating_key, source, label, audio_url, meta_json, cached_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    rating_key,
                    "custom_upload",
                    f"Uploaded: {file.filename}",
                    "",
                    json.dumps({"filename": file.filename}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
    flash(f"Upload applied: {message}" if ok else f"Upload failed: {message}", "success" if ok else "error")
    return redirect(url_for("shows.show_detail", rating_key=rating_key, **_normalized_list_state(request.form)))
