from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from src.services.plex_client import PlexClient
from src.services.settings_service import SettingsService
from src.services.sonarr_client import SonarrClient


settings_bp = Blueprint("settings", __name__)


@settings_bp.route("/settings", methods=["GET", "POST"])
def settings():
    service = SettingsService(current_app.config["DATABASE_PATH"])

    if request.method == "POST":
        plex_url = request.form.get("plex_url", "").strip()
        plex_token = request.form.get("plex_token", "").strip()
        library_key = request.form.get("library_key", "").strip()
        sonarr_url = request.form.get("sonarr_url", "").strip()
        sonarr_api_key = request.form.get("sonarr_api_key", "").strip()
        library_root_override = request.form.get("library_root_override", "").strip()

        service.set("plex_url", plex_url)
        service.set("plex_token", plex_token)
        service.set("library_key", library_key)
        service.set("sonarr_url", sonarr_url)
        service.set("sonarr_api_key", sonarr_api_key)
        service.set("library_root_override", library_root_override)
        if plex_url and plex_token and library_key:
            try:
                sections = PlexClient(plex_url, plex_token).list_sections()
                matched = next((s for s in sections if str(s.get("key")) == library_key), None)
                service.set("trusted_library_paths", matched.get("locations", []) if matched else [])
            except Exception:
                service.set("trusted_library_paths", [])
        flash("Settings saved.", "success")
        return redirect(url_for("settings.settings"))

    plex_url = service.get("plex_url", "")
    plex_token = service.get("plex_token", "")
    sonarr_url = service.get("sonarr_url", "")
    sonarr_api_key = service.get("sonarr_api_key", "")
    library_root_override = service.get("library_root_override", "")
    selected_library = service.get("library_key", "")

    sections = []
    plex_ok = False
    if plex_url and plex_token:
        plex = PlexClient(plex_url, plex_token)
        plex_ok, _ = plex.validate()
        if plex_ok:
            try:
                sections = [s for s in plex.list_sections() if s.get("type") == "show"]
            except Exception as exc:
                flash(f"Could not load libraries: {exc}", "error")

    sonarr_status = None
    if sonarr_url and sonarr_api_key:
        sonarr_ok, sonarr_msg = SonarrClient(sonarr_url, sonarr_api_key).validate()
        sonarr_status = {"ok": sonarr_ok, "message": sonarr_msg}

    return render_template(
        "settings.html",
        plex_url=plex_url,
        plex_token=plex_token,
        sections=sections,
        selected_library=selected_library,
        plex_ok=plex_ok,
        sonarr_url=sonarr_url,
        sonarr_api_key=sonarr_api_key,
        library_root_override=library_root_override,
        sonarr_status=sonarr_status,
    )
