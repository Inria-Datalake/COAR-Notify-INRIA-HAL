import datetime
import logging
import re
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from app.auth import require_api_key
from app.utils.db import get_db
from app.utils.notification_handler import (
    accept_notification,
    reject_notification,
    send_validation_to_viz,
)

logger = logging.getLogger(__name__)

coar_inbox_bp = Blueprint("coar_inbox", __name__)

_SWH_ORIGIN = "https://www.softwareheritage.org"
_OAI_HAL_PREFIX_RE = re.compile(r"^oai:hal:", re.IGNORECASE)

# Recognised origin classifications for stored notifications.
ORIGIN_SWH = "swh"
ORIGIN_HAL = "hal"
ORIGIN_UNKNOWN = "unknown"
KNOWN_ORIGINS = (ORIGIN_SWH, ORIGIN_HAL)


def _origin_id(notification: dict) -> str:
    """Extract the origin's `id`, tolerating both COAR shapes.

    Per the COAR Notify spec `origin` is an object (``{"id": ...}``), but some
    senders put a bare URL string there instead. Handle both so a malformed
    payload classifies as "unknown" rather than crashing the inbox.
    """
    origin = notification.get("origin") or {}
    if isinstance(origin, str):
        return origin.rstrip("/")
    if isinstance(origin, dict):
        return (origin.get("id") or "").rstrip("/")
    return ""


def _classify_origin(notification: dict) -> str:
    """Classify the sender of an incoming notification as "swh", "hal" or "unknown".

    Matches on substrings of the notification's `origin.id` so it tolerates the
    various HAL/SWH hostnames in play (e.g. inria.hal.science,
    inbox-preprod.archives-ouvertes.fr, archive.softwareheritage.org).
    """
    origin = _origin_id(notification).lower()
    if not origin:
        return ORIGIN_UNKNOWN
    if "softwareheritage" in origin:
        return ORIGIN_SWH
    if "hal" in origin or "archives-ouvertes" in origin:
        return ORIGIN_HAL
    return ORIGIN_UNKNOWN


_HAL_BASE_URL = "https://hal.science"


def _format_received_at(value: Any) -> str:
    """Render the stored ISO timestamp as ``YYYY-MM-DD HH:MM UTC``; fall back to the raw value."""
    if not isinstance(value, str) or not value:
        return "N/A"
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.UTC)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def summarize_received_notification(record: dict) -> dict[str, Any]:
    """
    Flatten a stored received notification into the fields shown on `/notifications`.

    HAL echoes our original Offer back inside ``object`` of its Accept/Reject, so the
    document id lives at ``object.object.id`` and the software name at
    ``object.object["sorg:citation"].name``; the author's comment is the top-level
    ``summary``. Software Heritage notifications use a different shape
    (``object["as:subject"]`` / ``context.id``), so those are used as fallbacks.
    Never raises on a malformed payload: unknown fields come back as ``None``.
    """
    payload = _as_dict(record.get("payload"))
    outer = _as_dict(payload.get("object"))
    inner = _as_dict(outer.get("object"))
    citation = _as_dict(inner.get("sorg:citation"))
    context = _as_dict(payload.get("context"))
    actor = _as_dict(payload.get("actor"))

    raw_types = payload.get("type")
    if isinstance(raw_types, list):
        types = [str(t) for t in raw_types if t]
    elif raw_types:
        types = [str(raw_types)]
    else:
        types = []

    primary = types[0] if types else None
    if primary == "Accept":
        decision, decision_label = "accepted", "Accepted"
    elif primary == "Reject":
        decision, decision_label = "rejected", "Rejected"
    else:
        decision, decision_label = "other", ", ".join(types) or "N/A"

    software = citation.get("name") or outer.get("as:subject") or None

    document_id = inner.get("id") or context.get("id") or None
    document_url = None
    if isinstance(document_id, str) and document_id:
        document_id = _OAI_HAL_PREFIX_RE.sub("", document_id)
        document_url = (
            document_id if document_id.startswith("http") else f"{_HAL_BASE_URL}/{document_id}"
        )
    else:
        document_id = None

    comment = payload.get("summary")
    if not isinstance(comment, str) or not comment.strip():
        comment = None

    return {
        "key": record.get("_key"),
        "origin": record.get("origin") or "N/A",
        "received_at": _format_received_at(record.get("received_at")),
        "types": types,
        "decision": decision,
        "decision_label": decision_label,
        "software": software,
        "document_id": document_id,
        "document_url": document_url,
        "comment": comment,
        "actor": actor.get("id"),
        "payload": payload,
    }


@coar_inbox_bp.route("/inbox", methods=["POST"])
def receive_notification():
    """
    COAR Notify inbox.
    Receives a JSON-LD notification, persists it, and dispatches Accept/Reject.
    """
    notification = request.get_json(force=True)
    notification_types = notification.get("type", [])
    logger.info(f"Received COAR notification: {notification_types}")

    if isinstance(notification_types, list):
        notification_type = notification_types[0] if notification_types else None
    else:
        notification_type = notification_types

    # Persist every received notification (Software Heritage ones included)
    # before any dispatch decision, so the inbox history is complete. Tag it
    # with the classified origin (swh/hal/unknown) for later filtering.
    origin = _classify_origin(notification)
    get_db().store_received_notification(notification, origin=origin)

    # Ignore notifications that originate from Software Heritage to avoid loops.
    # Use the same classification we stored, so dispatch and tagging never disagree.
    if origin == ORIGIN_SWH:
        logger.info("Notification originated from Software Heritage is ignored.")
        return jsonify(
            {
                "status": "ignored",
                "reason": "Notification from Software Heritage",
                "actor": _origin_id(notification) or _SWH_ORIGIN,
            }
        ), 202

    if notification_type in ("Accept", "Reject"):
        hal_id_full = notification["object"]["object"]["id"]
        hal_id = _OAI_HAL_PREFIX_RE.sub("", hal_id_full)
        software_name = notification["object"]["object"]["sorg:citation"]["name"]
        accepted = notification_type == "Accept"

        if accepted:
            accept_notification(notification)
        else:
            reject_notification(notification)

        send_validation_to_viz(hal_id, software_name, accepted)

    actor = notification.get("actor") or {}
    return jsonify(
        {
            "status": "ok",
            "type": notification_type,
            "actor": actor.get("id"),
        }
    ), 202


@coar_inbox_bp.route("/inbox", methods=["GET"])
def inbox_description():
    """
    COAR Notify inbox description.
    """
    return jsonify(
        {
            "title": "COAR Notify Inbox",
            "description": "Receives COAR-compliant notifications for software mention verification",
            "version": "1.0",
            "endpoints": {
                "POST": {
                    "url": "/inbox",
                    "method": "POST",
                    "content_type": "application/json",
                    "description": "Send a COAR notification to verify or reject software mentions",
                },
                "GET": {
                    "url": "/inbox",
                    "method": "GET",
                    "description": "Get this API documentation",
                },
            },
            "supported_notification_types": [
                {
                    "type": "Accept",
                    "description": "Accepts a software mention as verified by the author",
                },
                {
                    "type": "Reject",
                    "description": "Rejects a software mention as not verified by the author",
                },
            ],
            "request_example": {
                "type": "Accept",
                "actor": {
                    "type": "Person",
                    "id": "https://orcid.org/0000-0000-0000-0000",
                },
                "object": {
                    "type": "Offer",
                    "id": "urn:uuid:12345678-1234-1234-1234-123456789012",
                    "object": {
                        "type": "Document",
                        "id": "oai:HAL:hal-01478788",
                        "sorg:citation": {
                            "name": "SoftwareName",
                            "type": "Software",
                        },
                    },
                },
            },
            "view_notifications": {
                "url": "/notifications",
                "method": "GET",
                "description": "View all received notifications in a web interface",
            },
            "api_notifications": {
                "list": {
                    "url": "/api/notifications?limit=100&origin=swh",
                    "method": "GET",
                    "auth": "x-api-key header required",
                    "description": "List recent received notifications as JSON, newest first. "
                    "Optional origin filter: swh or hal.",
                },
                "get": {
                    "url": "/api/notifications/<key>",
                    "method": "GET",
                    "auth": "x-api-key header required",
                    "description": "Fetch a single received notification by its storage key",
                },
            },
        }
    )


# Origins the /notifications page can filter on. Broader than the API's
# KNOWN_ORIGINS because the page also lets you inspect "unknown"-tagged records.
PAGE_ORIGINS = (ORIGIN_SWH, ORIGIN_HAL, ORIGIN_UNKNOWN)

# Fixed "show" filters for the /notifications page: query value -> (label, COAR type,
# require a comment). Kept deliberately small so the pills stay meaningful to reviewers.
PAGE_FILTERS: dict[str, tuple[str, str | None, bool]] = {
    "accepted": ("Accepted", "Accept", False),
    "rejected": ("Rejected", "Reject", False),
    "commented": ("With comment", None, True),
}


@coar_inbox_bp.route("/notifications", methods=["GET"])
def show_notifications():
    """
    Display received notifications from the ArangoDB-backed store, with optional
    server-side filtering by origin (?origin=swh|hal|unknown) and decision
    (?show=accepted|rejected|commented). Invalid filter values are ignored rather
    than erroring, so a stale bookmark just shows everything.
    """
    db = get_db()

    origin = (request.args.get("origin") or "").lower() or None
    if origin not in PAGE_ORIGINS:
        origin = None

    show = (request.args.get("show") or "").lower() or None
    if show not in PAGE_FILTERS:
        show = None
    _, notification_type, has_comment = PAGE_FILTERS.get(show or "", ("", None, False))

    records = db.list_received_notifications(
        limit=200,
        origin=origin,
        notification_type=notification_type,
        has_comment=has_comment,
    )
    rows = [summarize_received_notification(r) for r in records]
    return render_template(
        "notifications.html",
        rows=rows,
        origins=PAGE_ORIGINS,
        filters={k: v[0] for k, v in PAGE_FILTERS.items()},
        selected_origin=origin,
        selected_show=show,
    )


@coar_inbox_bp.route("/api/notifications", methods=["GET"])
@require_api_key
def api_list_notifications():
    """
    Return recently received COAR notifications as JSON, newest first.

    Query params:
        limit: maximum number of records to return (1-1000, default 100).
        origin: optional filter, one of "swh" or "hal".
        type: optional COAR type filter, e.g. "Accept" or "Reject".
    """
    limit = request.args.get("limit", default=100, type=int) or 100
    limit = max(1, min(limit, 1000))

    origin = request.args.get("origin")
    if origin is not None:
        origin = origin.lower()
        if origin not in KNOWN_ORIGINS:
            return jsonify(
                {
                    "error": f"Invalid origin '{origin}'. Expected one of: {', '.join(KNOWN_ORIGINS)}.",
                }
            ), 400

    notification_type = request.args.get("type") or None

    try:
        records = get_db().list_received_notifications(
            limit=limit, origin=origin, notification_type=notification_type
        )
        return jsonify(
            {
                "count": len(records),
                "origin": origin,
                "type": notification_type,
                "notifications": records,
            }
        )
    except Exception as e:
        logger.error(f"Failed to list notifications: {e}")
        return jsonify({"error": "Failed to retrieve notifications"}), 500


@coar_inbox_bp.route("/api/notifications/<key>", methods=["GET"])
@require_api_key
def api_get_notification(key):
    """
    Return a single received notification by its ArangoDB `_key`.
    """
    try:
        record = get_db().get_document_by_key("received_notifications", key)
        if record:
            return jsonify(record)
        return jsonify({"error": "Notification not found"}), 404
    except Exception as e:
        logger.error(f"Failed to get notification {key}: {e}")
        return jsonify({"error": "Failed to retrieve notification"}), 500
