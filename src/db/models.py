import os
import sqlite3
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plex_shows_cache (
    rating_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    year INTEGER,
    folder_path TEXT NOT NULL,
    library_section_id TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    is_missing INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS theme_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_rating_key TEXT NOT NULL,
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    audio_url TEXT,
    meta_json TEXT,
    cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_installs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_rating_key TEXT NOT NULL,
    installed_from TEXT NOT NULL,
    installed_file TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS api_rate_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init_db(database_path: str) -> None:
    os.makedirs(os.path.dirname(database_path), exist_ok=True)
    with sqlite3.connect(database_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def get_conn(database_path: str):
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
