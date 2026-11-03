import logging
import uuid

import requests

logger = logging.getLogger(__name__)


class ActionReviewSoftware:
    def __init__(self, payload: dict):
        self._payload = payload

    def to_jsonld(self) -> dict:
        return self._payload


class ActionReviewNotifier:
    # Attribute annotations for static analyzers
    notification: ActionReviewSoftware
    target_inbox: str
    # Outcome of the last send(): HTTP status (None when no response was
    # received) and a human-readable failure reason (None on success). Lets the
    # caller record the attempt without changing send()'s Response|None contract.
    last_status: int | None
    last_error: str | None

    def __init__(
        self,
        document_id,
        actor_id,
        actor_name,
        origin_inbox,
        software_name,
        software_repo,
        mention_type,
        mention_context,
        target_id,
        target_inbox,
        mention_context_attributes=None,
        token=None,
    ):
        self.target_inbox = target_inbox
        self.token = token
        self.last_status = None
        self.last_error = None

        # Generate a random UUID (version 4) and convert to URN
        notification_id = uuid.uuid4().urn

        payload = {
            "@context": ["https://www.w3.org/ns/activitystreams", "https://purl.org/coar/notify"],
            "id": notification_id,
            "type": ["Offer", "coar-notify:ReviewAction"],
            "actor": {
                "id": actor_id,
                "type": "Service",
                "name": actor_name,
            },
            "origin": {
                "id": actor_id,
                "type": "Service",
                "inbox": origin_inbox,
            },
            "target": {
                "id": target_id,
                "type": "Service",
                "inbox": target_inbox,
            },
            "object": {
                "id": f"https://hal.science/{document_id}",
                "ietf:cite-as": None,
                "sorg:citation": {
                    "@context": "https://doi.org/10.5063/schema/codemeta-2.0",
                    "type": "SoftwareSourceCode",
                    "name": software_name,
                    "codeRepository": software_repo,
                    "referencePublication": None,
                },
                "mentionType": mention_type,
                "mentionContext": mention_context,
                "mentionContextAttributes": mention_context_attributes
                or {
                    "created": False,
                    "used": False,
                    "shared": False,
                },
            },
        }

        self.notification = ActionReviewSoftware(payload)

    def send(self):
        url = self.target_inbox
        headers = {"Content-Type": "application/ld+json"}

        # Add Authorization header if token is provided
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        payload = self.notification.to_jsonld()

        logger.debug(f"Sending notification to: {url}")
        logger.debug(f"Payload: {payload}")

        # Add timeout to prevent hanging
        resp = None
        self.last_status = None
        self.last_error = None
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            self.last_status = resp.status_code
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            self.last_error = f"Timeout after 20s while sending to {url}"
            logger.error(f"Timeout while sending notification to {url}")
            return None
        except requests.exceptions.ConnectionError as e:
            self.last_error = f"Connection error: {e}"
            logger.error(f"Connection error while sending to {url}: {e}")
            return None
        except requests.HTTPError:
            status = resp.status_code if resp is not None else None
            body = resp.text if resp is not None else ""
            self.last_error = f"HTTP {status}: {body[:500]}"
            logger.error(f"HTTP error: {status} - {body}")
            return None
        except Exception as e:
            self.last_error = f"Unexpected error: {e}"
            logger.error(f"Unexpected error while sending notification to {url}: {e}")
            return None
