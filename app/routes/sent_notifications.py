import logging

from flask import Blueprint, jsonify, render_template, request

from app.auth import require_api_key
from app.routes._args import parse_bool_arg
from app.utils.db import get_db

logger = logging.getLogger(__name__)

# Outbound notifications: the log of what we POSTed to HAL / Software Heritage
# (see notification_handler._record_sent_notification). Kept in its own
# blueprint, separate from the inbox one that handles *received* notifications.
sent_notifications_bp = Blueprint("sent_notifications", __name__)

# Providers we send to, as stored in sent_notifications.target.
SENT_TARGETS = ("hal", "swh")

# Status filter values accepted by the HTML page, mapped to the success flag.
PAGE_STATUSES: dict[str, bool] = {"succeeded": True, "failed": False}


@sent_notifications_bp.route("/api/notifications/sent", methods=["GET"])
@require_api_key
def api_list_sent_notifications():
    """
    Return recently sent COAR notifications (every attempt, successful or not)
    as JSON, newest first.

    Query params:
        limit: maximum number of records to return (1-1000, default 100).
        target: optional provider filter, one of "hal" or "swh".
        document_id: optional HAL document id filter (exact match).
        software_name: optional normalized software name filter (exact match).
        success: optional true/false filter on the transport outcome (HTTP 2xx).
    """
    limit = request.args.get("limit", default=100, type=int) or 100
    limit = max(1, min(limit, 1000))

    target = request.args.get("target")
    if target is not None:
        target = target.lower()
        if target not in SENT_TARGETS:
            return jsonify(
                {
                    "error": f"Invalid target '{target}'. Expected one of: {', '.join(SENT_TARGETS)}.",
                }
            ), 400

    document_id = request.args.get("document_id") or None
    software_name = request.args.get("software_name") or None
    success = parse_bool_arg("success")

    try:
        records = get_db().list_sent_notifications(
            limit=limit,
            target=target,
            document_id=document_id,
            success=success,
            software_name=software_name,
        )
        return jsonify(
            {
                "count": len(records),
                "limit": limit,
                "filters": {
                    "target": target,
                    "document_id": document_id,
                    "software_name": software_name,
                    "success": success,
                },
                "notifications": records,
            }
        )
    except Exception as e:
        logger.error(f"Failed to list sent notifications: {e}")
        return jsonify({"error": "Failed to retrieve sent notifications"}), 500


@sent_notifications_bp.route("/api/notifications/sent/<key>", methods=["GET"])
@require_api_key
def api_get_sent_notification(key):
    """
    Return a single sent notification by its ArangoDB `_key`.
    """
    try:
        record = get_db().get_document_by_key("sent_notifications", key)
        if record:
            return jsonify(record)
        return jsonify({"error": "Sent notification not found"}), 404
    except Exception as e:
        logger.error(f"Failed to get sent notification {key}: {e}")
        return jsonify({"error": "Failed to retrieve sent notification"}), 500


@sent_notifications_bp.route("/notifications/sent", methods=["GET"])
def show_sent_notifications():
    """
    Display the sent-notification log, with optional server-side filtering by
    target (?target=hal|swh) and outcome (?status=succeeded|failed). Invalid
    filter values are ignored rather than erroring, so a stale bookmark just
    shows everything.
    """
    db = get_db()

    target = (request.args.get("target") or "").lower() or None
    if target not in SENT_TARGETS:
        target = None

    status = (request.args.get("status") or "").lower() or None
    if status not in PAGE_STATUSES:
        status = None
    success = PAGE_STATUSES[status] if status else None

    records = db.list_sent_notifications(limit=200, target=target, success=success)
    total = db.count_sent_notifications()
    return render_template(
        "sent_notifications.html",
        records=records,
        total=total,
        targets=SENT_TARGETS,
        statuses=tuple(PAGE_STATUSES),
        selected_target=target,
        selected_status=status,
    )
