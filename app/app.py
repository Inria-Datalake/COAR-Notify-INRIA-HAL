import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from app import __version__
from app.routes.api_documents import api_documents_bp
from app.routes.api_software import api_software_bp
from app.routes.api_status import api_status_bp
from app.routes.coar_inbox import coar_inbox_bp
from app.routes.sent_notifications import sent_notifications_bp
from app.utils.db import env_with_legacy, get_db, init_db
from app.utils.notification_handler import is_quality_filter_enabled

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

# Mention Quality Filter (scores software-mention names at ingestion). Opt-in;
# threshold is the minimum P(valid) for a name to count as valid. The deprecated
# MODEL_FILTER_* env names are still honored as a fallback (see env_with_legacy).
app.config["MENTION_QUALITY_FILTER_ENABLED"] = env_with_legacy(
    "MENTION_QUALITY_FILTER_ENABLED", "MODEL_FILTER_ENABLED", "false"
)
app.config["MENTION_QUALITY_FILTER_THRESHOLD"] = env_with_legacy(
    "MENTION_QUALITY_FILTER_THRESHOLD", "MODEL_FILTER_THRESHOLD", "0.4"
)

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)


@app.context_processor
def inject_app_version():
    """Make the application version available to every template as ``app_version``."""
    return {"app_version": __version__}


app.register_blueprint(api_documents_bp)
app.register_blueprint(api_software_bp)
app.register_blueprint(api_status_bp)
app.register_blueprint(coar_inbox_bp)
app.register_blueprint(sent_notifications_bp)

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

# One-time migration of the seed CSV into the persistent ArangoDB blacklist
# collection. No-op once the collection holds any terms.
try:
    seeded = db_manager.seed_blacklist_from_csv()
    if seeded:
        print(f"Blacklist: seeded {seeded} terms into ArangoDB from CSV")
except Exception as e:
    print(f"Blacklist: failed to seed from CSV: {e}")

# Ensure the collections the dashboard queries and their persistent indexes
# exist (blacklist / quality flags, created_at and normalizedForm on `software`
# and `documents`; the timestamp / target / unique notification_id indexes on
# the two notification logs). Creation is idempotent; doing it at startup means
# simply booting the app provisions them, rather than waiting for the next
# ingestion or send, and keeps the dashboard's counts from hitting a missing
# collection on a cold database.
for _coll in ("software", "documents", "received_notifications", "sent_notifications"):
    try:
        db_manager.check_or_create_collection(_coll)
    except Exception as e:
        print(f"{_coll} indexes: failed to ensure: {e}")


# Activity-chart periods selectable on the dashboard (?days=...): the value used
# in the URL, its button label, and the window length (None = all history).
PERIOD_OPTIONS: list[tuple[str, str]] = [
    ("30", "30 days"),
    ("90", "90 days"),
    ("180", "180 days"),
    ("365", "1 year"),
    ("all", "All"),
]
PERIOD_DAYS: dict[str, int | None] = {"30": 30, "90": 90, "180": 180, "365": 365, "all": None}
DEFAULT_PERIOD = "30"
_BUCKET_WORD = {"day": "daily", "week": "weekly", "month": "monthly"}

# Number of software names shown in the dashboard's distribution chart.
DISTRIBUTION_TOP_N = 20


def _parse_period(raw: str | None) -> str:
    """Whitelist the ``?days=`` value; anything unknown falls back to the default."""
    return raw if raw in PERIOD_DAYS else DEFAULT_PERIOD


@app.get("/")
def home():
    try:
        db_manager = get_db()
        connection_info = db_manager.get_connection_info()
        stats = db_manager.get_dashboard_stats()
        quality_enabled = is_quality_filter_enabled()

        period = _parse_period(request.args.get("days"))
        timeseries = db_manager.get_activity_timeseries(days=PERIOD_DAYS[period])
        bucket_word = _BUCKET_WORD.get(timeseries["bucket"], timeseries["bucket"])
        if period == "all":
            period_heading = f"All time since {timeseries['since']} ({bucket_word})"
        else:
            period_heading = f"Last {period} days ({bucket_word})"

        # Mirror the send path's exclusions: blacklisted names never go out, and
        # quality-invalid ones only when the Mention Quality Filter is off.
        distribution = db_manager.get_software_distribution(
            limit=DISTRIBUTION_TOP_N,
            exclude_blacklisted=True,
            exclude_quality_invalid=quality_enabled,
        )

        return render_template(
            "home.html",
            status=connection_info["status"],
            error=connection_info.get("error"),
            stats=stats,
            timeseries=timeseries,
            period_options=PERIOD_OPTIONS,
            selected_period=period,
            period_heading=period_heading,
            distribution=distribution,
        )
    except Exception as e:
        return render_template("error.html", error=str(e))


@app.get("/database")
def database_info():
    try:
        connection_info = get_db().get_connection_info()

        return render_template(
            "database.html",
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


@app.get("/blacklist")
def blacklist_page():
    """Render the blacklist management UI. Data is loaded client-side from /api/blacklist."""
    try:
        return render_template("blacklist.html")
    except Exception as e:
        return render_template("error.html", error=str(e))


@app.get("/mention-quality")
def mention_quality_page():
    """Render the Mention Quality Filter UI. Data is loaded client-side from /api/mention-quality/stats."""
    try:
        return render_template("mention_quality.html")
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
