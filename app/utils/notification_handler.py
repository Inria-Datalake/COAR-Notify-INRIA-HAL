import logging
import os
import re
from enum import Enum
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from flask import current_app

from app.classes.ActionReviewNotifier import ActionReviewNotifier
from app.classes.RelationshipAnnounceNotifier import RelationshipAnnounceNotifier
from app.utils.db import get_db

logger = logging.getLogger(__name__)

_OAI_HAL_PREFIX_RE = re.compile(r"^oai:hal:", re.IGNORECASE)


def _origin_base_url() -> str:
    """Public-facing base URL advertised as the origin of outbound COAR notifications."""
    return os.getenv("COAR_ORIGIN_BASE_URL", "https://prod-datadcis-api.inria.fr/coar").rstrip("/")


def _origin_inbox_url() -> str:
    return f"{_origin_base_url()}/inbox"


class ProviderType(Enum):
    """Enumeration of supported data providers."""

    SW_VIZ = "software_viz"
    HAL = "hal"
    SOFTWARE_HERITAGE = "software_heritage"
    UNKNOWN = "unknown"


class NotificationFilterMode(Enum):
    """Filter modes restricting which software notifications get sent to a provider."""
    ALL = "all"
    CREATED = "created"
    USED = "used"
    SHARED = "shared"
    REUSED = "reused"
    REUSED_AND_SHARED = "reused_and_shared"
    CREATED_NOT_SHARED = "created_not_shared"


# Each predicate receives the three aggregated booleans from get_software_notifications
# and returns True if the software should be sent for this mode.
_FILTER_PREDICATES = {
    NotificationFilterMode.ALL:                lambda created, used, shared: True,
    NotificationFilterMode.CREATED:            lambda created, used, shared: created,
    NotificationFilterMode.USED:               lambda created, used, shared: used,
    NotificationFilterMode.SHARED:             lambda created, used, shared: shared,
    NotificationFilterMode.REUSED:             lambda created, used, shared: used and not created,
    NotificationFilterMode.REUSED_AND_SHARED:  lambda created, used, shared: used and shared and not created,
    NotificationFilterMode.CREATED_NOT_SHARED: lambda created, used, shared: created and not shared,
}


def filter_notifications_by_mode(notifications: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """
    Filter a list of software notifications by mode.

    Each notification dict is expected to carry boolean keys 'created', 'used', 'shared'
    (added by get_software_notifications). Unknown modes log a warning and pass-through
    so a typo in config never silently drops notifications.
    """
    try:
        mode_enum = NotificationFilterMode(mode)
    except ValueError:
        logger.warning(f"Unknown notification filter mode '{mode}', passing all notifications through")
        return notifications

    predicate = _FILTER_PREDICATES.get(mode_enum)
    if predicate is None:
        logger.warning(f"No predicate registered for filter mode '{mode}', passing all notifications through")
        return notifications

    return [
        n for n in notifications
        if predicate(bool(n.get('created')), bool(n.get('used')), bool(n.get('shared')))
    ]


class NotificationType(Enum):
    """Enumeration of notification types."""

    ACTION_REVIEW = "action_review"
    RELATIONSHIP_ANNOUNCE = "relationship_announce"
    OFFER_ANNOUNCE = "offer_announce"
    UNDEFINED = "undefined"


def detect_provider_from_document_data(doc_id: str) -> ProviderType:
    """
    Detect the provider type from document identifier.

    Args:
        doc_id: The document identifier to analyze

    Returns:
        ProviderType: The detected provider type
    """
    if not doc_id:
        return ProviderType.UNKNOWN

    doc_id_lower = doc_id.lower()

    if doc_id_lower.startswith("oai:hal:"):
        return ProviderType.HAL
    elif doc_id_lower.startswith("swh:"):
        return ProviderType.SOFTWARE_HERITAGE
    else:
        return ProviderType.UNKNOWN


def get_notification_type_for_provider(
    provider: ProviderType, document_context: str | None = None
) -> NotificationType:
    """
    Determine the appropriate notification type for a given provider.

    Args:
        provider: The provider type
        document_context: Optional context about the document

    Returns:
        NotificationType: The recommended notification type
    """
    if provider == ProviderType.HAL:
        # HAL typically uses ActionReview notifications for peer review
        return NotificationType.ACTION_REVIEW
    elif provider == ProviderType.SOFTWARE_HERITAGE:
        # Software Heritage often uses RelationshipAnnounce for linking
        return NotificationType.RELATIONSHIP_ANNOUNCE
    else:
        # Default to ActionReview for unknown providers
        return NotificationType.ACTION_REVIEW


def extract_notification_data(notification: dict[str, Any]) -> tuple[str, str]:
    """Extract ID and software name from notification payload."""
    try:
        # Support multiple ID formats
        id_full = notification["object"]["object"]["id"]

        # Extract provider and clean ID
        provider = detect_provider_from_document_data(id_full)

        if provider == ProviderType.HAL:
            doc_id = _OAI_HAL_PREFIX_RE.sub("", id_full)
        else:
            # For other providers, use the ID as-is or apply provider-specific cleaning
            doc_id = id_full

        software_name = notification["object"]["object"]["sorg:citation"]["name"]
        return doc_id, software_name
    except KeyError as e:
        logger.error(f"Missing expected key in notification: {e}")
        raise ValueError(f"Invalid notification format: missing {e}") from e


def accept_notification(notification: dict[str, Any]) -> bool:
    """
    Handle notification acceptance by marking software as verified by author.

    Args:
        notification: COAR notification payload

    Returns:
        bool: True if update was successful, False otherwise
    """
    try:
        db_manager = get_db()
        document_id, software_name = extract_notification_data(notification)
        logger.info(f"Accepting notification for HAL: {document_id}, Software: {software_name}")
        return db_manager.update_software_with_author_validation(document_id, software_name, True)
    except (ValueError, KeyError) as e:
        logger.error(f"Failed to accept notification: {e}")
        return False


def reject_notification(notification: dict[str, Any]) -> bool:
    """
    Handle notification rejection by marking software as not verified by author.

    Args:
        notification: COAR notification payload

    Returns:
        bool: True if update was successful, False otherwise
    """
    try:
        db_manager = get_db()
        document_id, software_name = extract_notification_data(notification)
        logger.info(f"Rejecting notification for HAL: {document_id}, Software: {software_name}")
        return db_manager.update_software_with_author_validation(document_id, software_name, False)
    except (ValueError, KeyError) as e:
        logger.error(f"Failed to reject notification: {e}")
        return False


def get_software_notifications(document_id: str) -> list[dict[str, Any]]:
    """
    Retrieve software notifications for a given HAL document.

    Args:
        hal_filename: HAL filename without extension

    Returns:
        List of notification data for software mentions in the document
    """
    try:
        from app.utils.db import get_db

        db_manager = get_db()
        return db_manager.get_software_notifications(document_id)

    except Exception as e:
        logger.error(f"Failed to retrieve software notifications for {document_id}: {e}")
        return []


def get_notification_config_for_provider(provider: ProviderType) -> dict[str, str]:
    """
    Get notification configuration for a specific provider.

    Args:
        provider: The provider type

    Returns:
        Dict containing provider-specific configuration
    """

    load_dotenv(override=True)
    config = {}

    if provider == ProviderType.HAL:
        hal_token = os.getenv("HAL_TOKEN")
        logger.debug(f"HAL_TOKEN from environment: {'set' if hal_token else 'NOT SET'}")
        config.update(
            {
                "base_url": os.getenv("HAL_BASE_URL", "https://inria.hal.science"),
                "inbox_url": os.getenv(
                    "HAL_INBOX_URL", "https://inbox-preprod.archives-ouvertes.fr/"
                ),
                "token": hal_token,
            }
        )
    elif provider == ProviderType.SOFTWARE_HERITAGE:
        swh_token = os.getenv("SWH_TOKEN")
        logger.debug(f"SWH_TOKEN from environment: {'set' if swh_token else 'NOT SET'}")
        config.update(
            {
                "base_url": os.getenv("SWH_BASE_URL", "https://archive.softwareheritage.org"),
                "inbox_url": os.getenv("SWH_INBOX_URL", "https://inbox.staging.swh.network/"),
                "token": swh_token,
            }
        )
    elif provider == ProviderType.SW_VIZ:
        sw_viz_token = os.getenv("SW_VIZ_TOKEN")
        logger.debug(f"SW_VIZ_TOKEN from environment: {'set' if sw_viz_token else 'NOT SET'}")
        config.update(
            {
                "base_url": os.getenv("SW_VIZ_URL", "http://coar-viz:8080"),
                "token": sw_viz_token,
            }
        )

    return config


def send_notifications_to_swh(document_id: str, notifications=None) -> dict[str, Any]:
    """
    Send COAR notifications specifically to Software Heritage for software mentions.

    Args:
        document_id: document identifier
        notifications: List of notification data for software mentions in the document

    Returns:
        Dict: {'success_count': int, 'failure_count': int, 'total_count': int}
    """
    try:
        if not document_id:
            logger.error("Invalid document ID provided")
            return {"success_count": 0, "failure_count": 0, "total_count": 0}

        logger.info(f"Processing Software Heritage notifications for document: {document_id}")

        if not notifications:
            logger.warning(
                f"No software retrieved for {document_id}. No notifications will be sent."
            )
            return {"success_count": 0, "failure_count": 0, "total_count": 0}

        filter_mode = current_app.config.get("SWH_NOTIFICATION_FILTER", "all")
        before_count = len(notifications)
        notifications = filter_notifications_by_mode(notifications, filter_mode)
        skipped = before_count - len(notifications)
        if skipped:
            logger.info(f"SWH filter '{filter_mode}' skipped {skipped} of {before_count} software notifications for {document_id}")
        if not notifications:
            return {'success_count': 0, 'failure_count': 0, 'total_count': 0}

        config = get_notification_config_for_provider(ProviderType.SOFTWARE_HERITAGE)

        success_count = 0
        failure_count = 0

        for notification in notifications:
            software_name = notification.get("softwareName")
            try:
                notifier = RelationshipAnnounceNotifier(
                    document_id,
                    actor_id=_origin_base_url(),
                    actor_name="Inria DataLake",
                    origin_inbox=_origin_inbox_url(),
                    software_name=software_name,
                    target_id="https://www.softwareheritage.org",
                    target_inbox=config["inbox_url"],
                    token=config["token"],
                )
                response = notifier.send()

                if response and 200 <= response.status_code < 300:
                    success_count += 1
                    logger.debug(
                        f"Successfully sent SWH notification for software: {software_name}"
                    )
                else:
                    failure_count += 1
                    status = response.status_code if response else "No response"
                    logger.error(
                        f"Failed to send SWH notification for software {software_name}: HTTP {status}"
                    )

            except Exception as e:
                failure_count += 1
                logger.error(
                    f"Exception processing SWH notification for software {software_name}: {e}"
                )

        total_count = len(notifications)
        logger.info(
            f"SWH notifications for {document_id}: {success_count} successful, {failure_count} failed (total: {total_count})"
        )

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "total_count": total_count,
        }

    except Exception as e:
        logger.error(f"Failed to process Software Heritage notifications for {document_id}: {e}")
        return {
            "success_count": 0,
            "failure_count": len(notifications) if notifications else 0,
            "total_count": len(notifications) if notifications else 0,
        }


def send_notifications_to_hal(document_id: str, notifications=None) -> dict[str, Any]:
    """
    Send COAR notifications to HAL for software mentions in a document.

    Args:
        document_id: document identifier
        notifications: List of notification data for software mentions in the document

    Returns:
        Dict: {'success_count': int, 'failure_count': int, 'total_count': int}
    """
    try:
        if not document_id:
            logger.error("Invalid document ID provided")
            return {"success_count": 0, "failure_count": 0, "total_count": 0}

        logger.info(f"Processing notifications for HAL for document: {document_id}")

        if not notifications:
            logger.warning(
                f"No software retrieved for {document_id}. No notifications will be sent."
            )
            return {"success_count": 0, "failure_count": 0, "total_count": 0}

        filter_mode = current_app.config.get("HAL_NOTIFICATION_FILTER", "all")
        before_count = len(notifications)
        notifications = filter_notifications_by_mode(notifications, filter_mode)
        skipped = before_count - len(notifications)
        if skipped:
            logger.info(f"HAL filter '{filter_mode}' skipped {skipped} of {before_count} software notifications for {document_id}")
        if not notifications:
            return {'success_count': 0, 'failure_count': 0, 'total_count': 0}

        config = get_notification_config_for_provider(ProviderType.HAL)

        success_count = 0
        failure_count = 0

        for notification in notifications:
            software_name = notification.get("softwareName", "Unknown software")
            try:
                notifier = ActionReviewNotifier(
                    document_id,
                    actor_id=_origin_base_url(),
                    actor_name="Inria DataLake",
                    origin_inbox=_origin_inbox_url(),
                    software_name=software_name,
                    software_repo=None,
                    mention_type="software",
                    mention_context=notification.get("contexts", []),
                    target_id=config["base_url"],
                    target_inbox=config["inbox_url"],
                    token=config["token"],
                )
                response = notifier.send()
                if response and 200 <= response.status_code < 300:
                    success_count += 1
                    logger.debug(
                        f"Successfully sent HAL notification for software: {software_name}"
                    )
                else:
                    failure_count += 1
                    status = response.status_code if response else "No response"
                    logger.error(
                        f"Failed to send HAL notification for software {software_name}: HTTP {status}"
                    )

            except Exception as e:
                failure_count += 1
                logger.error(
                    f"Exception processing HAL notification for software {software_name}: {e}"
                )

        total_count = len(notifications)
        logger.info(
            f"HAL notifications for {document_id}: {success_count} successful, {failure_count} failed (total: {total_count})"
        )

        return {
            "success_count": success_count,
            "failure_count": failure_count,
            "total_count": total_count,
        }

    except Exception as e:
        logger.error(f"Failed to process notifications for document_id {document_id}: {e}")
        return {
            "success_count": 0,
            "failure_count": len(notifications) if notifications else 0,
            "total_count": len(notifications) if notifications else 0,
        }


def send_validation_to_viz(document_id: str, software_name: str, accepted: bool = True):
    """
    Send validation information to Software Viz service.

    Args:
        document_id: HAL document identifier
        software_name: Software name
        accepted: Verification status

    Returns:
        bool: True if sent successfully, False otherwise
    """

    config = get_notification_config_for_provider(ProviderType.SW_VIZ)

    endpoint = "accepted_notification" if accepted else "rejected_notification"
    url = f"{config['base_url']}/api/{endpoint}/{document_id}/{software_name}"
    headers = {"Content-Type": "application/json"}

    if config["token"]:
        headers["Authorization"] = f'Bearer {config["token"]}'

    try:
        response = requests.post(url, headers=headers, timeout=5)
        response.raise_for_status()
        logger.info(f"Successfully sent {endpoint} notification to {url}")
        return response
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send notification: {e}")
        return False
    except Exception as e:
        logger.error(f"Error sending validation to Software Viz: {e}")
        return False
