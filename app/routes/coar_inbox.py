import logging

from flask import Blueprint, request, jsonify, render_template

from app.utils.notification_handler import accept_notification, reject_notification, \
    send_validation_to_viz

logger = logging.getLogger(__name__)

coar_inbox_bp = Blueprint("coar_inbox", __name__)

received_notifications = []

@coar_inbox_bp.route("/inbox", methods=["POST"])
def receive_notification():
    """
    COAR Notify inbox.
    Receives a JSON-LD notification and validates it.
    """
    notification = request.get_json(force=True)
    notification_types = notification.get('type', [])
    logger.info(f"Received COAR notification: {notification_types}")

    # Store the notification for display
    received_notifications.append(notification)

    if type(notification_types) is list:
        notification_type = notification_types[0]
    else:
        notification_type = notification_types

    notification_origin = notification.get("origin", None)
    if notification_origin and notification_origin["id"] == "https://www.softwareheritage.org/":
        logger.info("Notification originated from Software Heritage is ignored.")
        return jsonify({
            "status": "ok",
            "type": getattr(notification, "type", None),
            "actor": notification_origin["id"]
        }), 202

    if notification_type in ("Accept", "Reject"):
        hal_id_full = notification['object']['object']['id']
        hal_id = hal_id_full.replace('oai:HAL:', '')
        software_name = notification['object']['object']['sorg:citation']['name']

        notification_accepted = notification_type == "Accept"

        if notification_accepted:
            accept_notification(notification)
        else:
            reject_notification(notification)

        send_validation_to_viz(hal_id, software_name, notification_accepted)

    # Respond with the type and actor info
    return jsonify({
        "status": "ok",
        "type": getattr(notification, "type", None),
        "actor": getattr(notification['actor'], "id", None)
    }), 202

@coar_inbox_bp.route("/inbox", methods=["GET"])
def inbox_description():
    """
    COAR Notify inbox description.
    Returns information about how to send notifications to this inbox.
    """
    return jsonify({
        "title": "COAR Notify Inbox",
        "description": "This inbox receives COAR-compliant notifications for software mention verification",
        "version": "1.0",
        "endpoints": {
            "POST": {
                "url": "/inbox",
                "method": "POST",
                "content_type": "application/json",
                "description": "Send a COAR notification to verify or reject software mentions"
            },
            "GET": {
                "url": "/inbox",
                "method": "GET",
                "description": "Get this API documentation"
            }
        },
        "supported_notification_types": [
            {
                "type": "Accept",
                "description": "Accepts a software mention as verified by the author",
                "purpose": "Marks software as verified in the database"
            },
            {
                "type": "Reject",
                "description": "Rejects a software mention",
                "purpose": "Marks software as not verified by the author"
            }
        ],
        "request_format": {
            "content_type": "application/ld+json",
            "required_fields": [
                "type",
                "actor",
                "object"
            ],
            "example_accept": {
                "type": "Accept",
                "actor": {
                    "type": "Person",
                    "id": "https://orcid.org/0000-0000-0000-0000"
                },
                "object": {
                    "type": "Offer",
                    "id": "urn:uuid:12345678-1234-1234-1234-123456789012",
                    "object": {
                        "type": "Document",
                        "id": "oai:HAL:hal-01478788",
                        "sorg:citation": {
                            "name": "SoftwareName",
                            "type": "Software"
                        }
                    }
                }
            },
            "example_reject": {
                "type": "Reject",
                "actor": {
                    "type": "Person",
                    "id": "https://orcid.org/0000-0000-0000-0000"
                },
                "object": {
                    "type": "Offer",
                    "id": "urn:uuid:12345678-1234-1234-1234-123456789012",
                    "object": {
                        "type": "Document",
                        "id": "oai:HAL:hal-01478788",
                        "sorg:citation": {
                            "name": "SoftwareName",
                            "type": "Software"
                        }
                    }
                }
            }
        },
        "usage_examples": {
            "curl_accept": "curl -X POST http://localhost:5000/inbox \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\n    \"type\": \"Accept\",\n    \"actor\": {\n      \"type\": \"Person\",\n      \"id\": \"https://orcid.org/0000-0000-0000-0000\"\n    },\n    \"object\": {\n      \"type\": \"Offer\",\n      \"id\": \"urn:uuid:12345678-1234-1234-1234-123456789012\",\n      \"object\": {\n        \"type\": \"Document\",\n        \"id\": \"oai:HAL:hal-01478788\",\n        \"sorg:citation\": {\n          \"name\": \"SoftwareName\",\n          \"type\": \"Software\"\n        }\n      }\n    }\n  }'",
            "curl_reject": "curl -X POST http://localhost:5000/inbox \\\n  -H \"Content-Type: application/json\" \\\n  -d '{\n    \"type\": \"Reject\",\n    \"actor\": {\n      \"type\": \"Person\",\n      \"id\": \"https://orcid.org/0000-0000-0000-0000\"\n    },\n    \"object\": {\n      \"type\": \"Offer\",\n      \"id\": \"urn:uuid:12345678-1234-1234-1234-123456789012\",\n      \"object\": {\n        \"type\": \"Document\",\n        \"id\": \"oai:HAL:hal-01478788\",\n        \"sorg:citation\": {\n          \"name\": \"SoftwareName\",\n          \"type\": \"Software\"\n        }\n      }\n    }\n  }'",
            "python": "import requests\nimport json\n\n# Send Accept notification\naccept_payload = {\n    \"type\": \"Accept\",\n    \"actor\": {\n        \"type\": \"Person\",\n        \"id\": \"https://orcid.org/0000-0000-0000-0000\"\n    },\n    \"object\": {\n        \"type\": \"Offer\",\n        \"id\": \"urn:uuid:12345678-1234-1234-1234-123456789012\",\n        \"object\": {\n            \"type\": \"Document\",\n            \"id\": \"oai:HAL:hal-01478788\",\n            \"sorg:citation\": {\n                \"name\": \"SoftwareName\",\n                \"type\": \"Software\"\n            }\n        }\n    }\n}\n\nresponse = requests.post(\n    \"http://localhost:5000/inbox\",\n    headers={\"Content-Type\": \"application/json\"},\n    json=accept_payload\n)\n\nprint(f\"Status: {response.status_code}\")\nprint(f\"Response: {response.json()}\")"
        },
        "responses": {
            "202": {
                "description": "Notification accepted and processed",
                "example": {
                    "status": "ok",
                    "type": "Accept",
                    "actor": "https://orcid.org/0000-0000-0000-0000"
                },
                "note": "The response contains the notification type and actor ID from the processed notification"
            },
            "400": {
                "description": "Invalid request - no JSON data provided or malformed request",
                "note": "Occurs when the request doesn't contain valid JSON data"
            }
        },
        "view_notifications": {
            "url": "/notifications",
            "method": "GET",
            "description": "View all received notifications in a web interface"
        }
    })


@coar_inbox_bp.route("/notifications", methods=["GET"])
def show_notifications():
    """
    Display all received notifications on a web page.
    """
    return render_template("notifications.html", notifications=received_notifications)
