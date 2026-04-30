from datetime import datetime, timezone
import json
import os

from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for

from src.db.models import get_conn
from src.services.animethemes_client import AnimeThemesClient
from src.services.plex_cache_service import PlexCacheService
from src.services.plex_client import PlexClient
from src.services.settings_service import SettingsService
from src.services.sonarr_client import SonarrClient
from src.services.theme_apply_service import ThemeApplyService


shows_bp = Blueprint("shows", __name__)


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


def _runtime_trusted_paths(svc: dict) -> list[str]:
    paths = list(svc["settings"].get("trusted_library_paths", []) or [])
    library_root_override = str(svc["settings"].get("library_root_override", "") or "").strip()
    if library_root_override:
        paths.append(library_root_override)
    sonarr_url = svc["settings"].get("sonarr_url", "")
    sonarr_api_key = svc["settings"].get("sonarr_api_key", "")
    if sonarr_url and sonarr_api_key:
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


@shows_bp.route("/")
def home():
    return redirect(url_for("shows.list_shows"))


@shows_bp.route("/shows")
def list_shows():
    svc = _services()
    library_key = svc["settings"].get("library_key", "")
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
    if library_key:
        shows = svc["cache"].get_cached_shows_filtered(library_key, query=query)

    enriched = []
    for row in shows:
        show = dict(row)
        show["folder_path"] = _apply_runtime_path_mappings(show["folder_path"]) or show["folder_path"]
        current_theme_path = _find_theme_file_in_show_folder(show["folder_path"])
        show["has_current_theme"] = bool(current_theme_path)
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
    return render_template(
        "shows.html",
        shows=shows,
        library_key=library_key,
        q=query,
        theme_filter=theme_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        has_prev=page > 1,
        has_next=page < total_pages,
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


@shows_bp.route("/shows/<rating_key>")
def show_detail(rating_key: str):
    svc = _services()
    svc["settings"].set("debug_logs", [])
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
                show["title"],
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
            )
        except Exception as exc:
            live_sonarr = {"error": str(exc)}

    search_overrides = svc["settings"].get("show_search_overrides", {})
    initial_query = search_overrides.get(rating_key, show["title"])
    initial_query = AnimeThemesClient.to_romaji_query(initial_query) or initial_query
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
    local_theme_path = _find_theme_file_in_show_folder(show["folder_path"])
    local_theme_available = bool(local_theme_path)
    return render_template(
        "show_detail.html",
        show=dict(show),
        candidates=[dict(c) for c in candidates],
        local_theme_available=local_theme_available,
        installs=[dict(i) for i in installs],
        live_plex=live_plex,
        live_sonarr=live_sonarr,
        sonarr_alternate_queries=sonarr_alternate_queries,
        api_debug=api_debug,
        initial_search_query=initial_query,
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

    target_folder = _apply_runtime_path_mappings(show["folder_path"]) or show["folder_path"]
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
        flash("Show missing from cache.", "error")
        return redirect(url_for("shows.list_shows"))
    raw_query = request.form.get("search_query", "").strip() or show["title"]
    search_query = AnimeThemesClient.to_romaji_query(raw_query) or raw_query
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
        flash(f"Saved {len(candidates)} AnimeThemes candidates.", "success")
    except Exception as exc:
        _append_debug_log(svc["settings"], f"AnimeThemes lookup failed for '{show['title']}' query='{search_query}': {exc}")
        flash(f"Theme lookup failed: {exc}", "error")
    return redirect(url_for("shows.show_detail", rating_key=rating_key))


@shows_bp.route("/shows/<rating_key>/apply", methods=["POST"])
def apply_candidate(rating_key: str):
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
        flash("Candidate or show not found.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key))
    trusted_paths = _runtime_trusted_paths(svc)
    target_folder = _resolve_write_folder(svc, show["folder_path"])
    if not _is_within_trusted_paths(target_folder, trusted_paths):
        flash("Target folder is outside trusted Plex library paths.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key))
    ok, message = svc["apply"].install_from_url(show["rating_key"], target_folder, candidate["audio_url"])
    flash(f"Theme applied: {message}" if ok else f"Apply failed: {message}", "success" if ok else "error")
    return redirect(url_for("shows.show_detail", rating_key=rating_key))


@shows_bp.route("/shows/<rating_key>/upload", methods=["POST"])
def upload_theme(rating_key: str):
    svc = _services()
    file = request.files.get("theme_file")
    if not file or not file.filename:
        flash("Choose an audio file first.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key))
    if not file.filename.lower().endswith((".mp3", ".m4a", ".flac", ".ogg")):
        flash("Only audio file uploads are supported.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key))

    with get_conn(current_app.config["DATABASE_PATH"]) as conn:
        show = conn.execute(
            "SELECT rating_key, folder_path FROM plex_shows_cache WHERE rating_key = ?",
            (rating_key,),
        ).fetchone()
        if not show:
            flash("Show not found.", "error")
            return redirect(url_for("shows.list_shows"))

    trusted_paths = _runtime_trusted_paths(svc)
    target_folder = _resolve_write_folder(svc, show["folder_path"])
    if not _is_within_trusted_paths(target_folder, trusted_paths):
        flash("Target folder is outside trusted Plex library paths.", "error")
        return redirect(url_for("shows.show_detail", rating_key=rating_key))

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
    return redirect(url_for("shows.show_detail", rating_key=rating_key))
