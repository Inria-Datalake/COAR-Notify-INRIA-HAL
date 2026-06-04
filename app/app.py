import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

from app import __version__
from app.routes.api_documents import api_documents_bp
from app.routes.api_software import api_software_bp
from app.routes.api_status import api_status_bp
from app.routes.coar_inbox import coar_inbox_bp
from app.utils.db import get_db, init_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

app = Flask(__name__, template_folder="templates", static_folder="static")

app.config["ARANGO_HOST"] = os.environ.get("ARANGO_HOST", "localhost")
app.config["ARANGO_PORT"] = int(os.environ.get("ARANGO_PORT", 8529))
app.config["ARANGO_USERNAME"] = os.environ.get("ARANGO_USERNAME", "root")
app.config["ARANGO_PASSWORD"] = os.environ.get("ARANGO_ROOT_PASSWORD", "examplepassword")
app.config["ARANGO_DB"] = os.environ.get("ARANGO_DB", "COAR_NOTIFY_DB")
app.config["SW_VIZ_URL"] = os.environ.get("SW_VIZ_URL", "")
app.config["SW_VIZ_TOKEN"] = os.environ.get("SW_VIZ_TOKEN", "")

# Per-provider notification filter mode (e.g., "all", "created", "reused_and_shared")
app.config["HAL_NOTIFICATION_FILTER"] = os.environ.get("HAL_NOTIFICATION_FILTER", "all")
app.config["SWH_NOTIFICATION_FILTER"] = os.environ.get("SWH_NOTIFICATION_FILTER", "all")

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


@app.context_processor
def inject_app_version():
    """Make the application version available to every template as ``app_version``."""
    return {"app_version": __version__}


app.register_blueprint(api_documents_bp)
app.register_blueprint(api_software_bp)
app.register_blueprint(api_status_bp)
app.register_blueprint(coar_inbox_bp)

db_manager = init_db(app)

try:
    connection_info = db_manager.get_connection_info()
    print(
        f"ArangoDB connection: host={connection_info['host']} port={connection_info['port']} "
        f"db='{connection_info['db']}' user='{connection_info['user']}' "
        f"version={connection_info['version']} collections={connection_info['collections']}"
    )
except Exception as e:
    print(f"ArangoDB info: failed to fetch info: {e}")


@app.get("/")
def home():
    try:
        db_manager = get_db()
        connection_info = db_manager.get_connection_info()

        return render_template(
            "home.html",
            status=connection_info["status"],
            host=connection_info["host"],
            port=connection_info["port"],
            db_name=connection_info["db"],
            user=connection_info["user"],
            version=connection_info["version"],
            num_collections=connection_info["collections"],
            error=connection_info.get("error"),
        )
    except Exception as e:
        return render_template("error.html", error=str(e))


@app.get("/health")
def health():
    try:
        db_manager = get_db()
        connection_info = db_manager.get_connection_info()

        if connection_info["status"] == "up":
            return jsonify(
                {
                    "status": "up",
                    "version": __version__,
                    "arango": {
                        "host": connection_info["host"],
                        "port": connection_info["port"],
                        "db": connection_info["db"],
                        "user": connection_info["user"],
                        "version": connection_info["version"],
                        "collections": connection_info["collections"],
                    },
                }
            ), 200
        return jsonify(
            {
                "status": "down",
                "error": connection_info.get("error", "Unknown error"),
                "arango": {
                    "host": connection_info["host"],
                    "port": connection_info["port"],
                    "db": connection_info["db"],
                    "user": connection_info["user"],
                },
            }
        ), 503
    except Exception as e:
        return jsonify(
            {
                "status": "down",
                "error": str(e),
                "arango": {
                    "host": app.config.get("ARANGO_HOST", "unknown"),
                    "port": app.config.get("ARANGO_PORT", "unknown"),
                    "db": app.config.get("ARANGO_DB", "unknown"),
                    "user": app.config.get("ARANGO_USERNAME", "unknown"),
                },
            }
        ), 503
