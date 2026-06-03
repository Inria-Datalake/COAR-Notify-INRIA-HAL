import logging

from flask import Blueprint, jsonify

from app.auth import require_api_key
from app.utils.db import get_db

logger = logging.getLogger(__name__)

api_status_bp = Blueprint("api_status", __name__)


@api_status_bp.route("/status", methods=["GET"])
@require_api_key
def can_upload():
    """
    Check API-key validity and database reachability, and report which
    essential collections exist.
    """
    try:
        db_manager = get_db()

        collections = ["documents", "software", "edge_doc_to_software"]
        existing = {}
        for collection in collections:
            try:
                existing[collection] = db_manager.get_collection(collection) is not None
            except Exception as e:
                logger.warning(f"Failed to check collection {collection}: {e}")
                existing[collection] = False

        can_upload = all(existing.values())
        return jsonify({
            "status": "ok" if can_upload else "error",
            "can_upload": can_upload,
            "collections": existing,
        })
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return jsonify({
            "status": "error",
            "message": str(e),
            "can_upload": False,
        }), 500
