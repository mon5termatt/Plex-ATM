import os

from src import create_app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "1") in {"1", "true", "True"}
    app.run(host=host, port=port, debug=debug)
