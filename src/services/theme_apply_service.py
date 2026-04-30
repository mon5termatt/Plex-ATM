import os
import tempfile
from datetime import datetime, timezone

import requests

from src.db.models import get_conn


class ThemeApplyService:
    def __init__(self, database_path: str):
        self.database_path = database_path

    @staticmethod
    def _apply_path_mappings(path: str) -> str:
        """
        Apply optional prefix remaps from APP_PATH_MAPPINGS env var.
        Format: "/from1=/to1;/from2=/to2"
        Example: "/plex/ANIME=/media/anime;/tv=/media/tv"
        """
        mapping_spec = os.environ.get("APP_PATH_MAPPINGS", "").strip()
        if not mapping_spec:
            return path

        mapped = path
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

    @staticmethod
    def _resolve_target_folder(folder_path: str) -> str:
        raw = (folder_path or "").strip()
        if not raw:
            raise ValueError("Target folder path is empty.")

        raw = ThemeApplyService._apply_path_mappings(raw)

        # Common mismatch: Sonarr returns Linux/container path (/tv/...) while app runs on Windows.
        if os.name == "nt" and raw.startswith("/"):
            raise ValueError(
                f"Path '{raw}' looks like a Linux/container path. Configure path mapping to a local Windows folder."
            )

        return os.path.abspath(os.path.normpath(raw))

    def _log_install(self, show_rating_key: str, installed_from: str, installed_file: str, status: str, notes: str = "") -> None:
        with get_conn(self.database_path) as conn:
            conn.execute(
                "INSERT INTO theme_installs(show_rating_key, installed_from, installed_file, installed_at, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    show_rating_key,
                    installed_from,
                    installed_file,
                    datetime.now(timezone.utc).isoformat(),
                    status,
                    notes,
                ),
            )
            conn.commit()

    def _safe_write(self, folder_path: str, filename: str, content: bytes) -> str:
        target_dir = self._resolve_target_folder(folder_path)
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, filename)
        fd, tmp_path = tempfile.mkstemp(prefix="theme_", suffix=".tmp", dir=target_dir)
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
        os.replace(tmp_path, target)
        return target

    @staticmethod
    def _verify_written_file(path: str) -> tuple[bool, str]:
        if not os.path.exists(path):
            return (False, f"File missing after write: {path}")
        if not os.path.isfile(path):
            return (False, f"Path is not a file: {path}")
        size = os.path.getsize(path)
        if size <= 0:
            return (False, f"File is empty after write: {path}")
        if not os.access(path, os.R_OK):
            return (False, f"File is not readable: {path}")
        return (True, f"Verified file write: {path} ({size} bytes)")

    def install_from_url(self, show_rating_key: str, folder_path: str, audio_url: str, filename: str = "theme.mp3") -> tuple[bool, str]:
        try:
            response = requests.get(audio_url, timeout=45)
            response.raise_for_status()
            path = self._safe_write(folder_path, filename, response.content)
            ok, verify_msg = self._verify_written_file(path)
            status = "success" if ok else "failed"
            self._log_install(show_rating_key, "animethemes", path, status, verify_msg)
            return (ok, verify_msg)
        except Exception as exc:
            self._log_install(show_rating_key, "animethemes", folder_path, "failed", str(exc))
            return (False, str(exc))

    def install_from_upload(self, show_rating_key: str, folder_path: str, uploaded_bytes: bytes, filename: str = "theme.mp3") -> tuple[bool, str]:
        try:
            path = self._safe_write(folder_path, filename, uploaded_bytes)
            ok, verify_msg = self._verify_written_file(path)
            status = "success" if ok else "failed"
            self._log_install(show_rating_key, "custom_upload", path, status, verify_msg)
            return (ok, verify_msg)
        except Exception as exc:
            self._log_install(show_rating_key, "custom_upload", folder_path, "failed", str(exc))
            return (False, str(exc))
