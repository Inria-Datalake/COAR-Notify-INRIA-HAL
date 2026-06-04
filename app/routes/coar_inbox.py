import logging
import re

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
    origin = notification.get("origin") or {}
    return (origin.get("id") or "").rstrip("/")


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


@coar_inbox_bp.route("/notifications", methods=["GET"])
def show_notifications():
    """
    Display all received notifications from the ArangoDB-backed store.
    """
    records = get_db().list_received_notifications(limit=200)
    return render_template("notifications.html", records=records)


@coar_inbox_bp.route("/api/notifications", methods=["GET"])
@require_api_key
def api_list_notifications():
    """
    Return recently received COAR notifications as JSON, newest first.

    Query params:
        limit: maximum number of records to return (1-1000, default 100).
        origin: optional filter, one of "swh" or "hal".
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

    try:
        records = get_db().list_received_notifications(limit=limit, origin=origin)
        return jsonify({"count": len(records), "origin": origin, "notifications": records})
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
