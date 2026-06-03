import logging
import re

from flask import Blueprint, jsonify, render_template, request

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


def _origin_id(notification: dict) -> str:
    origin = notification.get("origin") or {}
    return (origin.get("id") or "").rstrip("/")


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

    # Ignore notifications that originate from Software Heritage to avoid loops.
    if _origin_id(notification) == _SWH_ORIGIN:
        logger.info("Notification originated from Software Heritage is ignored.")
        return jsonify({
            "status": "ignored",
            "reason": "Notification from Software Heritage",
            "actor": _SWH_ORIGIN,
        }), 202

    get_db().store_received_notification(notification)

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
    return jsonify({
        "status": "ok",
        "type": notification_type,
        "actor": actor.get("id"),
    }), 202


@coar_inbox_bp.route("/inbox", methods=["GET"])
def inbox_description():
    """
    COAR Notify inbox description.
    """
    return jsonify({
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
    })


@coar_inbox_bp.route("/notifications", methods=["GET"])
def show_notifications():
    """
    Display all received notifications from the ArangoDB-backed store.
    """
    records = get_db().list_received_notifications(limit=200)
    return render_template("notifications.html", records=records)
