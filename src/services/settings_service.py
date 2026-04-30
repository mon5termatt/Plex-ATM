import json
from typing import Any

from src.db.models import get_conn


class SettingsService:
    def __init__(self, database_path: str):
        self.database_path = database_path

    def get(self, key: str, default: Any = None) -> Any:
        with get_conn(self.database_path) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set(self, key: str, value: Any) -> None:
        serialized = json.dumps(value)
        with get_conn(self.database_path) as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, serialized),
            )
            conn.commit()
