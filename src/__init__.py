from pathlib import Path

from flask import Flask

from src.db.models import init_db
from src.web.routes_settings import settings_bp
from src.web.routes_shows import shows_bp


def create_app() -> Flask:
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(
        __name__,
        template_folder=str(project_root / "templates"),
        static_folder=str(project_root / "static"),
    )
    app.config.from_object("src.config.Config")
    app.secret_key = app.config["SECRET_KEY"]

    init_db(app.config["DATABASE_PATH"])

    app.register_blueprint(settings_bp)
    app.register_blueprint(shows_bp)

    return app
