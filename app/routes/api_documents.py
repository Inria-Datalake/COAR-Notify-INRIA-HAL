import logging

from flask import Blueprint, jsonify, request

from app.auth import require_api_key
from app.utils.db import get_db
from app.utils.notification_handler import (
    get_software_notifications,
    send_notifications_to_hal,
    send_notifications_to_swh,
)

logger = logging.getLogger(__name__)

api_documents_bp = Blueprint("api_documents", __name__)


@api_documents_bp.route("/api/documents", methods=["GET"])
def documents_status():
    try:
        db_manager = get_db()
        total_count = db_manager.get_collection_count("documents")
        return jsonify({
            "collection_name": "documents",
            "total_documents": total_count,
        })
    except Exception as e:
        logger.error(f"Failed to get documents status: {e}")
        return jsonify({"error": "Failed to retrieve documents status"}), 500


@api_documents_bp.route("/api/document/<id>", methods=["GET"])
def document_from_id(id):
    try:
        db_manager = get_db()
        doc = db_manager.get_document_by_id(id)
        if doc:
            return jsonify(doc)
        return jsonify({"error": "Document not found"}), 404
    except Exception as e:
        logger.error(f"Failed to get document {id}: {e}")
        return jsonify({"error": "Failed to retrieve document"}), 500


@api_documents_bp.route("/api/document/<id>", methods=["DELETE"])
@require_api_key
def delete_document(id):
    """
    Delete a document and all its associated software mentions.
    """
    try:
        db_manager = get_db()

        if not db_manager.get_document_by_id(id):
            return jsonify({"error": "Document not found"}), 404

        deletion_result = db_manager.delete_document_by_id(id)
        if deletion_result:
            return jsonify({
                "status": "deleted",
                "document_id": id,
                "software_deleted": deletion_result.get("software_deleted", 0),
            })
        return jsonify({"error": "Failed to delete document"}), 500
    except Exception as e:
        logger.error(f"Failed to delete document {id}: {e}")
        return jsonify({"error": "Failed to delete document"}), 500


@api_documents_bp.route("/api/document/<id_document>/software", methods=["GET"])
def document_software_all_from_id(id_document):
    try:
        db_manager = get_db()
        return jsonify(db_manager.get_document_software(id_document))
    except Exception as e:
        logger.error(f"Failed to get software for document {id_document}: {e}")
        return jsonify({"error": "Failed to retrieve document software"}), 500


@api_documents_bp.route(
    "/api/document/<id_document>/software/<id_software>", methods=["GET"]
)
def document_software_from_id(id_document, id_software):
    try:
        db_manager = get_db()
        return jsonify(db_manager.get_document_software(id_document, id_software))
    except Exception as e:
        logger.error(
            f"Failed to get software {id_software} for document {id_document}: {e}"
        )
        return jsonify({"error": "Failed to retrieve document software"}), 500


@api_documents_bp.route("/api/document", methods=["POST"])
@require_api_key
def insert_new_document():
    """
    Expects a JSON file uploaded as form-data with key 'file' and a mandatory document_id
    field used as the HAL identifier for the document.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    document_id = request.form.get("document_id")
    if not document_id:
        return jsonify({"error": "document_id parameter is required"}), 400

    file = request.files["file"]

    try:
        db_manager = get_db()
        inserted = db_manager.insert_document_as_json(document_id, file)
    except Exception as e:
        logger.error(f"File insertion failed: {e}")
        return jsonify({"error": f"Insertion failed: {str(e)}"}), 500

    if not inserted:
        return jsonify({
            "status": "exists",
            "message": "Document already exists in the database",
            "document_id": document_id,
        }), 409

    notifications = get_software_notifications(document_id)
    notification_results = {}

    for provider, sender in (
        ("hal", send_notifications_to_hal),
        ("swh", send_notifications_to_swh),
    ):
        try:
            result = sender(document_id, notifications)
            notification_results[provider] = {
                "sent": result["success_count"],
                "failed": result["failure_count"],
            }
        except Exception as e:
            logger.error(f"{provider.upper()} notification failed for {document_id}: {e}")
            notification_results[provider] = {
                "sent": 0,
                "failed": len(notifications) if notifications else 0,
                "error": str(e),
            }

    total_sent = sum(r.get("sent", 0) for r in notification_results.values())
    total_failed = sum(r.get("failed", 0) for r in notification_results.values())

    return jsonify({
        "status": "inserted",
        "document_id": document_id,
        "notifications": {
            "summary": {
                "total_sent": total_sent,
                "total_failed": total_failed,
                "total_attempts": total_sent + total_failed,
            },
            "by_provider": notification_results,
        },
    }), 201
