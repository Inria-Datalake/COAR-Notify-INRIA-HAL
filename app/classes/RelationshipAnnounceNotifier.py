import requests
import uuid
import logging

logger = logging.getLogger(__name__)


class RelationshipAnnounceSoftware:
    def __init__(self, payload: dict):
        self._payload = payload

    def to_jsonld(self) -> dict:
        return self._payload


class RelationshipAnnounceNotifier:
    # Attribute annotations for static analyzers
    notification: RelationshipAnnounceSoftware
    target_inbox: str

    def __init__(
            self,
            document_id,
            actor_id,
            actor_name,
            origin_inbox,
            software_name,
            target_id,
            target_inbox,
            token=None,
    ):
        self.target_inbox = target_inbox
        self.token = token

        # Generate a random UUID (version 4) and convert to URN
        notification_id = uuid.uuid4().urn

        payload = {
            "@context": [
                "https://www.w3.org/ns/activitystreams",
                "https://purl.org/coar/notify"
            ],
            "actor": {
                "id": actor_id,
                "type": "Organization",
                "name": actor_name,
            },
            "context": {
                "id": f"https://hal.science/{document_id}",
                "sorg:name": None,
                "sorg:author": {
                    "@type": "Person",
                    "givenName": None,
                    "email": None,
                },
                "ietf:cite-as": "https://doi.org/XXX/YYY",
                "ietf:item": {
                    "id": f"https://hal.science/{document_id}/document",
                    "mediaType": "application/pdf",
                    "type": [
                        "Object",
                        "sorg:ScholarlyArticle",
                    ],
                },
                "type": [
                    "Page",
                    "sorg:AboutPage",
                ],
            },
            "id": notification_id,
            "object": {
                "as:object": f"https://hal.science/{document_id}",
                "as:relationship": "https://w3id.org/codemeta/3.0#citation",
                "as:subject": software_name,
                "id": uuid.uuid4().urn,
                "type": "Relationship",
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
            "type": [
                "Announce",
                "coar-notify:RelationshipAction",
            ],
        }

        self.notification = RelationshipAnnounceSoftware(payload)

    def send(self):
        url = self.target_inbox
        headers = {"Content-Type": "application/ld+json"}

        # Add Authorization header if token is provided
        if self.token:
            headers["Authorization"] = f"Token {self.token}"

        payload = self.notification.to_jsonld()

        logger.debug(f"Sending notification to: {url}")
        logger.debug(f"Payload: {payload}")

        # Add timeout to prevent hanging
        resp = None
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            logger.error(f"Timeout while sending notification to {url}")
            return None
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error while sending to {url}: {e}")
            return None
        except requests.HTTPError:
            logger.error(f"HTTP error: {resp.status_code} - {resp.text}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error while sending notification to {url}: {e}")
            return None
