import os


class Config:
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    DATABASE_PATH = os.environ.get("APP_DATABASE_PATH", "data/app.db")
    UPLOAD_DIR = os.environ.get("APP_UPLOAD_DIR", "data/uploads")
    LOG_DIR = os.environ.get("APP_LOG_DIR", "data/logs")

    ANIMETHEMES_BASE_URL = "https://api.animethemes.moe"
    ANIMETHEMES_APP_MAX_RPM = int(os.environ.get("ANIMETHEMES_APP_MAX_RPM", "40"))
    ANIMETHEMES_HTTP_TIMEOUT = int(os.environ.get("ANIMETHEMES_HTTP_TIMEOUT", "20"))
