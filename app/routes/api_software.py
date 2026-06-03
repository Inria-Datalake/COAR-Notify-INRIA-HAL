import logging

from flask import Blueprint, Response, jsonify, request

from app.auth import require_api_key
from app.utils.blacklist_manager import blacklist_manager
from app.utils.db import get_db

logger = logging.getLogger(__name__)

api_software_bp = Blueprint("api_software", __name__)


@api_software_bp.route("/api/software", methods=["GET"])
def software_status():
    try:
        db_manager = get_db()
        total_count = db_manager.get_collection_count("software")
        return jsonify({
            "collection_name": "software",
            "total_documents": total_count,
        })
    except Exception as e:
        logger.error(f"Failed to get software status: {e}")
        return jsonify({"error": "Failed to retrieve software status"}), 500


@api_software_bp.route("/api/software/name/<name>", methods=["GET"])
def software_from_name(name):
    try:
        db_manager = get_db()
        return jsonify(db_manager.get_software_by_normalized_name(name))
    except Exception as e:
        logger.error(f"Failed to get software by {name}: {e}")
        return jsonify({"error": "Failed to retrieve software"}), 500


@api_software_bp.route("/api/software/<id_mention>", methods=["GET"])
def software_mention_from_id(id_mention):
    try:
        db_manager = get_db()
        doc = db_manager.get_document_by_key("software", id_mention)
        if doc:
            return jsonify(doc)
        return jsonify({"error": "Software mention not found"}), 404
    except Exception as e:
        logger.error(f"Failed to get software mention {id_mention}: {e}")
        return jsonify({"error": "Failed to retrieve software mention"}), 500


# Blacklist management endpoints

@api_software_bp.route("/api/blacklist", methods=["GET"])
def get_blacklist():
    try:
        search_query = request.args.get("search", "").strip()
        limit = int(request.args.get("limit", 50))

        stats = blacklist_manager.get_blacklist_stats()

        if search_query:
            terms = blacklist_manager.search_blacklist(search_query, limit)
            return jsonify({
                "stats": stats,
                "terms": terms,
                "search_query": search_query,
                "limit": limit,
                "total_matches": len(terms),
            })

        all_terms = sorted(blacklist_manager.get_blacklist())
        return jsonify({
            "stats": stats,
            "terms": all_terms,
            "total_count": len(all_terms),
        })
    except Exception as e:
        logger.error(f"Failed to get blacklist: {e}")
        return jsonify({"error": "Failed to retrieve blacklist"}), 500


@api_software_bp.route("/api/blacklist/stats", methods=["GET"])
def get_blacklist_stats():
    try:
        return jsonify(blacklist_manager.get_blacklist_stats())
    except Exception as e:
        logger.error(f"Failed to get blacklist stats: {e}")
        return jsonify({"error": "Failed to retrieve blacklist statistics"}), 500


@api_software_bp.route("/api/blacklist", methods=["POST"])
@require_api_key
def add_to_blacklist():
    try:
        data = request.get_json()
        if not data or "term" not in data:
            return jsonify({"error": "term is required in request body"}), 400

        term = data["term"].strip()
        if not term:
            return jsonify({"error": "term cannot be empty"}), 400

        if blacklist_manager.add_to_blacklist(term):
            return jsonify({
                "success": True,
                "message": f"Term '{term}' added to blacklist",
                "term": term,
            }), 201
        return jsonify({
            "success": False,
            "message": f"Term '{term}' already exists in blacklist",
            "term": term,
        }), 409
    except Exception as e:
        logger.error(f"Failed to add term to blacklist: {e}")
        return jsonify({"error": "Failed to add term to blacklist"}), 500


@api_software_bp.route("/api/blacklist/<term>", methods=["DELETE"])
@require_api_key
def remove_from_blacklist(term):
    try:
        if blacklist_manager.remove_from_blacklist(term):
            return jsonify({
                "success": True,
                "message": f"Term '{term}' removed from blacklist",
                "term": term,
            })
        return jsonify({
            "success": False,
            "message": f"Term '{term}' not found in blacklist",
            "term": term,
        }), 404
    except Exception as e:
        logger.error(f"Failed to remove term from blacklist: {e}")
        return jsonify({"error": "Failed to remove term from blacklist"}), 500


@api_software_bp.route("/api/blacklist/reload", methods=["POST"])
@require_api_key
def reload_blacklist():
    try:
        term_count = blacklist_manager.reload_blacklist()
        return jsonify({
            "success": True,
            "message": "Blacklist reloaded successfully",
            "total_terms": term_count,
        })
    except Exception as e:
        logger.error(f"Failed to reload blacklist: {e}")
        return jsonify({"error": "Failed to reload blacklist"}), 500


@api_software_bp.route("/api/blacklist/export", methods=["GET"])
def export_blacklist():
    try:
        csv_content = blacklist_manager.export_blacklist()
        return Response(
            csv_content,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=blacklist.csv"},
        )
    except Exception as e:
        logger.error(f"Failed to export blacklist: {e}")
        return jsonify({"error": "Failed to export blacklist"}), 500


@api_software_bp.route("/api/blacklist/import", methods=["POST"])
@require_api_key
def import_blacklist():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename.endswith(".csv"):
            return jsonify({"error": "File must be a CSV file"}), 400

        overwrite = request.form.get("overwrite", "false").lower() in ["true", "1", "yes"]
        csv_content = file.read().decode("utf-8")
        result = blacklist_manager.import_blacklist_from_csv(csv_content, overwrite)

        if result["success"]:
            return jsonify({
                "success": True,
                "message": f"Successfully imported {result['imported_terms']} terms",
                "total_terms": result["total_terms"],
                "overwrite": result["overwrite"],
            })
        return jsonify({
            "success": False,
            "error": result.get("error", "Import failed"),
        }), 400
    except Exception as e:
        logger.error(f"Failed to import blacklist: {e}")
        return jsonify({"error": "Failed to import blacklist"}), 500
