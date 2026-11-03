import csv
import datetime
import json
import logging
import os
from typing import Any, Union

from pyArango.collection import Collection
from pyArango.connection import Connection
from pyArango.database import Database
from pyArango.theExceptions import CreationError
from werkzeug.datastructures import FileStorage

from app.classes.filters import BlacklistFilter, ClassifierFilter

logger = logging.getLogger(__name__)

# Global database manager instance
db_manager: Union["DatabaseManager", None] = None

# Legacy env names already warned about, so the deprecation notice is logged once
# per process rather than on every ingestion.
_warned_legacy_env: set[str] = set()


def env_with_legacy(new_key: str, legacy_key: str, default: str) -> str:
    """Read ``new_key`` from the environment, falling back to the deprecated
    ``legacy_key`` (with a one-time warning) and finally ``default``.

    Lets the Mention Quality Filter env vars be renamed without silently breaking
    deployments that still set the old ``MODEL_FILTER_*`` names.
    """
    value = os.environ.get(new_key)
    if value is not None:
        return value
    legacy = os.environ.get(legacy_key)
    if legacy is not None:
        if legacy_key not in _warned_legacy_env:
            _warned_legacy_env.add(legacy_key)
            logger.warning(f"Env var {legacy_key} is deprecated; use {new_key} instead.")
        return legacy
    return default


def _bucket_for_span(span_days: int) -> str:
    """Bucket size for an activity window: daily up to 90 days, weekly up to a
    year, monthly beyond, keeping charts at roughly 90 points or fewer."""
    if span_days <= 90:
        return "day"
    if span_days <= 366:
        return "week"
    return "month"


def _bucket_start(d: datetime.date, bucket: str) -> datetime.date:
    """First day of the bucket containing ``d`` (ISO Monday for weeks)."""
    if bucket == "week":
        return d - datetime.timedelta(days=d.weekday())
    if bucket == "month":
        return d.replace(day=1)
    return d


def _next_bucket(d: datetime.date, bucket: str) -> datetime.date:
    """First day of the bucket after the one starting at ``d``."""
    if bucket == "week":
        return d + datetime.timedelta(days=7)
    if bucket == "month":
        return (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return d + datetime.timedelta(days=1)


class DatabaseManager:
    """
    Centralized database management class for ArangoDB operations.

    This class handles all database connections, collections, and queries in one place,
    providing a clean interface for database operations throughout the application.
    """

    # Persistent indexes ensured (idempotently) the first time each collection is
    # accessed. Keyed by collection name; each spec is the field list plus options.
    COLLECTION_INDEXES: dict[str, list[dict[str, Any]]] = {
        # Enforce one record per HAL document at the DB level (app code also checks
        # via document_exists, this is the hard guarantee) and speed up lookups.
        "documents": [
            {"fields": ["file_hal_id"], "unique": True},
            # Newest-first listing (get_latest_documents) and the per-day documents
            # histogram both filter/sort on created_at.
            {"fields": ["created_at"]},
        ],
        # Notifications are filtered by origin and sorted newest-first by received_at.
        "received_notifications": [
            {"fields": ["origin"]},
            {"fields": ["received_at"]},
        ],
        # One record per outbound COAR notification attempt (HAL / SWH). Listed
        # newest-first by sent_at and filtered by target / document / software name.
        # notification_id is the payload's uuid4 URN: unique so an attempt is never
        # recorded twice, and so received Accept/Reject replies (whose inReplyTo
        # carries that URN) can be joined to the originating send later. The
        # index is sparse (pyArango default), so a record whose notifier failed
        # to build (notification_id null) is still accepted.
        "sent_notifications": [
            {"fields": ["sent_at"]},
            # Covering index for the dashboard's sent/failed series: the per-day
            # aggregate reads only sent_at and success. Non-sparse so a record
            # with a null flag could never fall out of the index.
            {"fields": ["sent_at", "success"], "sparse": False},
            {"fields": ["target"]},
            {"fields": ["document_id"]},
            {"fields": ["software_name"]},
            {"fields": ["notification_id"], "unique": True},
        ],
        # One record per blacklisted term; the unique index makes add idempotent
        # at the DB level and speeds up membership lookups.
        "blacklist": [
            {"fields": ["name"], "unique": True},
        ],
        # Software mentions are filtered by the blacklist / quality flags and sorted
        # or bucketed by created_at (dashboard stats, per-day histograms, latest
        # filtered listings). Single-field indexes speed each flag/sort on its own;
        # the compound one serves the combined filter-then-sort queries.
        # normalizedForm powers the distinct-name counts (dashboard + mention-quality),
        # blacklist match-count, and by-name lookups; quality_score the "scored" count.
        "software": [
            {"fields": ["blacklisted"]},
            {"fields": ["quality_invalid"]},
            {"fields": ["created_at"]},
            {"fields": ["blacklisted", "quality_invalid", "created_at"]},
            # Covering index for the dashboard's per-day software series
            # (get_activity_timeseries): a created_at range scan whose aggregate
            # reads only the two flags, so no document is fetched. The compound
            # index above cannot serve it (created_at is its last field). Must be
            # non-sparse: a sparse compound index drops every entry where any of
            # its fields is null, and mentions ingested before the flags existed
            # have null blacklisted / quality_invalid.
            {"fields": ["created_at", "blacklisted", "quality_invalid"], "sparse": False},
            {"fields": ["software_name.normalizedForm"]},
            {"fields": ["quality_score"]},
            # Covering index for get_software_distribution: its per-name AGGREGATE
            # reads the two flags, so they must be in the index or every entry
            # costs a full-document fetch (~2M point lookups, 12-23s in prod).
            # Non-sparse, because the query only guarantees normalizedForm != null
            # and the optimizer rejects a sparse index unless all fields are non-null.
            {
                "fields": ["software_name.normalizedForm", "blacklisted", "quality_invalid"],
                "sparse": False,
            },
        ],
    }

    def __init__(self, host: str, port: int, username: str, password: str, db_name: str):
        """
        Initialize the DatabaseManager.

        Args:
            host: ArangoDB host
            port: ArangoDB port
            username: Database username
            password: Database password
            db_name: Database name
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.db_name = db_name
        self._connection: Connection | None = None
        self._database: Database | None = None
        # Collections whose indexes have already been ensured this process, so we
        # don't re-issue ensure* HTTP calls on every store/list operation.
        self._indexed_collections: set[str] = set()

    def connect(self) -> Connection:
        """
        Establish connection to ArangoDB.

        Returns:
            Connection: The ArangoDB connection object

        Raises:
            ConnectionError: If connection fails
        """
        if self._connection is None:
            try:
                self._connection = Connection(
                    arangoURL=f"http://{self.host}:{self.port}",
                    username=self.username,
                    password=self.password,
                )
                logger.info(f"Connected to ArangoDB at {self.host}:{self.port}")
            except Exception as e:
                logger.error(f"Failed to connect to ArangoDB: {e}")
                raise ConnectionError(f"ArangoDB connection failed: {e}") from e

        return self._connection

    def get_database(self) -> Database:
        """
        Get the database instance, creating it if necessary.

        Returns:
            Database: The ArangoDB database object

        Raises:
            Exception: If database cannot be accessed
        """
        if self._database is None:
            conn = self.connect()

            # Handle race conditions across multiple workers
            try:
                if not conn.hasDatabase(self.db_name):
                    try:
                        conn.createDatabase(name=self.db_name)
                        logger.info(f"Created database: {self.db_name}")
                    except CreationError as e:
                        # If another worker just created it, it will exist now
                        if not conn.hasDatabase(self.db_name):
                            raise Exception(f"Failed to create database: {self.db_name}") from e
            except Exception as e:
                logger.warning(f"Database access issue: {e}")
                # Try to proceed if we can access the DB
                if not conn.hasDatabase(self.db_name):
                    raise Exception(f"Cannot access database: {self.db_name}") from e

            self._database = conn[self.db_name]
            logger.info(f"Using database: {self.db_name}")

        return self._database

    def get_connection_info(self) -> dict[str, Any]:
        """
        Get connection and database information.

        Returns:
            Dict containing connection status and details
        """
        info = {
            "host": self.host,
            "port": self.port,
            "db": self.db_name,
            "user": self.username,
            "status": "down",
            "version": None,
            "collections": "unknown",
        }

        try:
            conn = self.connect()
            info["status"] = "up"

            # Get version info
            try:
                version_info = conn.getVersion() or {}
                info["version"] = version_info.get("version") or version_info.get("server")
            except Exception:
                pass

            # Get collection count
            try:
                db = self.get_database()
                coll_info = db.fetchCollections()
                if isinstance(coll_info, dict) and "result" in coll_info:
                    info["collections"] = len(coll_info["result"])
            except Exception:
                pass

        except Exception as e:
            info["error"] = str(e)
            logger.error(f"Failed to get connection info: {e}")

        return info

    def check_or_create_collection(
        self, collection_name: str, collection_type: str = "Collection"
    ) -> Collection:
        """
        Get collection if exists, else create it safely under concurrency.

        Args:
            collection_name: Name of the collection
            collection_type: Type of collection ('Collection' or 'Edges')

        Returns:
            Collection: The collection object
        """
        db = self.get_database()

        if db.hasCollection(collection_name):
            collection = db[collection_name]
            self._ensure_indexes(collection)
            return collection

        try:
            db.createCollection(collection_type, name=collection_name)
            logger.info(f"Created collection: {collection_name}")
        except CreationError:
            # Likely created concurrently by another worker
            logger.info(f"Collection {collection_name} already exists (created by another worker)")

        collection = db[collection_name]
        self._ensure_indexes(collection)
        return collection

    def _ensure_indexes(self, collection: Collection) -> None:
        """
        Create the persistent indexes declared for this collection, if any.

        Idempotent and cheap: pyArango's ensurePersistentIndex is a no-op when the
        index already exists, and we additionally skip the call entirely once a
        collection has been handled in this process. Index failures are logged but
        never propagated, so they cannot break the surrounding request.
        """
        name = collection.name
        if name in self._indexed_collections:
            return

        for spec in self.COLLECTION_INDEXES.get(name, []):
            try:
                # sparse defaults to True to match pyArango's default, under which
                # every pre-existing index was built — a different default would make
                # ensurePersistentIndex create non-sparse duplicates of them all.
                collection.ensurePersistentIndex(
                    spec["fields"],
                    unique=spec.get("unique", False),
                    sparse=spec.get("sparse", True),
                )
                logger.debug(
                    f"Ensured index on {name}.{'.'.join(spec['fields'])} "
                    f"(unique={spec.get('unique', False)}, sparse={spec.get('sparse', True)})"
                )
            except Exception as e:
                logger.error(f"Failed to ensure index on {name}.{spec['fields']}: {e}")

        self._indexed_collections.add(name)

    def get_collection(self, collection_name: str) -> Collection | None:
        """
        Get a collection by name.

        Args:
            collection_name: Name of the collection

        Returns:
            Collection object or None if not found
        """
        try:
            db = self.get_database()
            if db.hasCollection(collection_name):
                return db[collection_name]
        except Exception as e:
            logger.error(f"Failed to get collection {collection_name}: {e}")
        return None

    def execute_aql_query(
        self, query: str, bind_vars: dict[str, Any] | None = None, raw_results: bool = False
    ) -> Any:
        """
        Execute an AQL query.

        Args:
            query: AQL query string
            bind_vars: Bind variables for the query
            raw_results: Whether to return raw results

        Returns:
            Query results

        Raises:
            Exception: If query execution fails
        """
        try:
            db = self.get_database()
            result = db.AQLQuery(query, bindVars=bind_vars or {}, rawResults=raw_results)
            logger.debug(f"Executed AQL query: {query[:100]}...")
            return result
        except Exception as e:
            logger.error(f"AQL query failed: {query[:100]}... Error: {e}")
            raise

    def load_blacklist(self, csv_path: str = "./app/static/data/blacklist.csv") -> set:
        """
        Load blacklist terms from CSV file.

        Args:
            csv_path: Path to the blacklist CSV file

        Returns:
            Set of blacklisted terms
        """
        blacklist = set()
        try:
            with open(csv_path, newline="", encoding="utf-8") as csvfile:
                reader = csv.reader(csvfile)
                next(reader, None)  # skip header if exists
                for row in reader:
                    if row and row[0]:
                        blacklist.add(row[0].strip())
            logger.debug(f"Loaded {len(blacklist)} terms from blacklist")
        except Exception as e:
            logger.warning(f"Failed to load blacklist from {csv_path}: {e}")

        return blacklist

    def get_blacklist_terms(self) -> set[str]:
        """Return the set of blacklisted terms stored in the ``blacklist`` collection."""
        try:
            self.check_or_create_collection("blacklist")
            result = self.execute_aql_query("FOR b IN blacklist RETURN b.name", raw_results=True)
            return {term for term in result if term}
        except Exception as e:
            logger.error(f"Failed to read blacklist terms: {e}")
            return set()

    def count_blacklist_terms(self) -> int:
        """Return how many terms are in the ``blacklist`` collection."""
        try:
            self.check_or_create_collection("blacklist")
            result = self.execute_aql_query("RETURN LENGTH(blacklist)", raw_results=True)
            return list(result)[0]
        except Exception as e:
            logger.error(f"Failed to count blacklist terms: {e}")
            return 0

    def add_blacklist_term(self, term: str) -> bool:
        """
        Add a term to the blacklist.

        Returns True if inserted, False if blank or already present.
        """
        term = (term or "").strip()
        if not term:
            return False
        try:
            collection = self.check_or_create_collection("blacklist")
            exists = list(
                self.execute_aql_query(
                    "FOR b IN blacklist FILTER b.name == @name LIMIT 1 RETURN 1",
                    bind_vars={"name": term},
                    raw_results=True,
                )
            )
            if exists:
                return False
            doc = collection.createDocument({"name": term})
            doc.save()
            logger.info(f"Added term to blacklist: {term}")
            return True
        except Exception as e:
            logger.error(f"Failed to add blacklist term '{term}': {e}")
            return False

    def remove_blacklist_term(self, term: str) -> bool:
        """Remove a term from the blacklist. Returns True if a term was removed."""
        try:
            self.check_or_create_collection("blacklist")
            removed = list(
                self.execute_aql_query(
                    "FOR b IN blacklist FILTER b.name == @name REMOVE b IN blacklist RETURN 1",
                    bind_vars={"name": term},
                    raw_results=True,
                )
            )
            if removed:
                logger.info(f"Removed term from blacklist: {term}")
            return len(removed) > 0
        except Exception as e:
            logger.error(f"Failed to remove blacklist term '{term}': {e}")
            return False

    def clear_blacklist(self) -> int:
        """Remove all terms from the blacklist. Returns how many were removed."""
        try:
            self.check_or_create_collection("blacklist")
            removed = list(
                self.execute_aql_query(
                    "FOR b IN blacklist REMOVE b IN blacklist RETURN 1", raw_results=True
                )
            )
            return len(removed)
        except Exception as e:
            logger.error(f"Failed to clear blacklist: {e}")
            return 0

    def seed_blacklist_from_csv(self, csv_path: str = "./app/static/data/blacklist.csv") -> int:
        """
        Populate the blacklist collection from the seed CSV when it is empty.

        This is the one-time migration from the in-image CSV to persistent
        storage; it is a no-op once the collection holds any terms. Returns the
        number of terms inserted.
        """
        try:
            self.check_or_create_collection("blacklist")
            if self.count_blacklist_terms() > 0:
                return 0
            terms = self.load_blacklist(csv_path)
            inserted = sum(1 for term in terms if self.add_blacklist_term(term))
            logger.info(f"Seeded blacklist collection with {inserted} terms from {csv_path}")
            return inserted
        except Exception as e:
            logger.error(f"Failed to seed blacklist from {csv_path}: {e}")
            return 0

    def count_software_matching_blacklist(
        self, blacklist: set[str] | None = None
    ) -> dict[str, int]:
        """
        Count stored software mentions whose normalized name matches the blacklist.

        Read-only dry run: reports the impact of the current (or a supplied)
        blacklist without changing any ``blacklisted`` flags. Useful to size up a
        list before running reapply_blacklist.
        """
        try:
            terms = list(self.get_blacklist_terms() if blacklist is None else blacklist)
            query = """
                LET matching = (
                    FOR s IN software
                        FILTER s.software_name.normalizedForm IN @bl
                        RETURN s.software_name.normalizedForm
                )
                RETURN {
                    total_mentions: LENGTH(software),
                    matching_mentions: LENGTH(matching),
                    matching_names: LENGTH(UNIQUE(matching)),
                    blacklist_terms: LENGTH(@bl)
                }
            """
            return list(self.execute_aql_query(query, bind_vars={"bl": terms}, raw_results=True))[0]
        except Exception as e:
            logger.error(f"Failed to count blacklist matches: {e}")
            return {
                "total_mentions": 0,
                "matching_mentions": 0,
                "matching_names": 0,
                "blacklist_terms": 0,
            }

    def remove_duplicates(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Remove duplicate JSON objects by hashing.

        Args:
            items: List of dictionaries to deduplicate

        Returns:
            List with duplicates removed
        """
        seen = set()
        unique = []
        for item in items:
            key = json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(item)

        logger.debug(f"Removed {len(items) - len(unique)} duplicates")
        return unique

    def document_exists(self, collection_name: str, key_field: str, key_value: str) -> bool:
        """
        Check if a document exists in a collection.

        Args:
            collection_name: Name of the collection
            key_field: Field name to check
            key_value: Value to match

        Returns:
            True if document exists, False otherwise
        """
        try:
            query = f"FOR d IN {collection_name} FILTER d.{key_field} == @value RETURN 1"
            result = self.execute_aql_query(query, bind_vars={"value": key_value}, raw_results=True)
            return len(list(result)) > 0
        except Exception as e:
            logger.error(f"Failed to check document existence: {e}")
            return False

    def insert_document_as_json(
        self,
        document_id: str,
        file_json: FileStorage | dict[str, Any],
    ) -> bool:
        """
        Insert a JSON file into ArangoDB with document, software, and edge collections.

        Args:
            document_id: Unique identifier for the document
            file_json: File object or dictionary containing the data

        Returns:
            True if inserted, False if already exists or failed
        """
        try:
            # Get collections
            documents_collection = self.check_or_create_collection("documents")
            software_collection = self.check_or_create_collection("software")
            doc_soft_edge = self.check_or_create_collection("edge_doc_to_software", "Edges")

            # Load blacklist from its persistent ArangoDB collection
            blacklist = self.get_blacklist_terms()

            # Process input
            if hasattr(file_json, "read"):
                data_json = json.load(file_json)
            else:
                data_json = file_json

            # Check if document already exists
            if self.document_exists("documents", "file_hal_id", document_id):
                logger.warning(f"Document with ID '{document_id}' already exists in DB. Skipping.")
                return False

            # Timestamp ingestion so documents/software can be charted over time
            # (see get_activity_timeseries). Stored as an ISO-8601 UTC string,
            # matching received_notifications.received_at.
            now_iso = datetime.datetime.now(datetime.UTC).isoformat()

            # Insert main document
            document_document = documents_collection.createDocument(
                {"file_hal_id": document_id, "created_at": now_iso}
            )
            document_document.save()
            logger.debug(f"Created document with ID: {document_id}")

            # Process mentions. We store *every* mention and only flag the ones
            # whose normalized name is blacklisted; the blacklist is enforced
            # later, when notifications are built (see get_software_notifications
            # and filter_blacklisted_notifications).
            mentions = self.remove_duplicates(data_json.get("mentions", []))
            blacklist_filter = BlacklistFilter(blacklist)
            # Mention Quality Filter model (see sandbox/train_classifier.py). Opt-in
            # and tunable via env: MENTION_QUALITY_FILTER_ENABLED (default off) and
            # MENTION_QUALITY_FILTER_THRESHOLD (default 0.4). Loaded once and reused
            # for every mention; degrades gracefully if the model file is absent.
            model_enabled = self._quality_enabled()
            model_threshold = self._quality_threshold()
            classifier_filter = (
                ClassifierFilter(threshold=model_threshold) if model_enabled else None
            )
            inserted_count = 0
            blacklisted_count = 0
            quality_invalid_count = 0

            for mention in mentions:
                # Check before renaming keys (filters tolerate either key).
                is_blacklisted = blacklist_filter.filter_mention(mention)
                # Quality score is an independent signal from the blacklist; stored
                # alongside it (never overwriting ``blacklisted``). None when the
                # quality filter is disabled or the name can't be scored.
                quality_score = classifier_filter.score(mention) if classifier_filter else None
                quality_invalid = quality_score is not None and quality_score < model_threshold

                # Rename fields for consistency
                mention["software_name"] = mention.pop("software-name")
                mention["software_type"] = mention.pop("software-type")

                # Insert software document
                mention["blacklisted"] = is_blacklisted
                mention["quality_score"] = quality_score
                mention["quality_invalid"] = quality_invalid
                mention["created_at"] = now_iso
                software_document = software_collection.createDocument(mention)
                software_document.save()

                # Create edge from document to software
                edge_doc_soft = doc_soft_edge.createEdge()
                edge_doc_soft["_from"] = document_document._id
                edge_doc_soft["_to"] = software_document._id
                edge_doc_soft.save()

                inserted_count += 1
                if is_blacklisted:
                    blacklisted_count += 1
                if quality_invalid:
                    quality_invalid_count += 1

            logger.info(
                f"Inserted {inserted_count} software mentions "
                f"({blacklisted_count} blacklisted, {quality_invalid_count} quality-invalid) "
                f"for document with ID: {document_id}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to insert JSON file: {e}")
            return False

    def get_software_notifications(self, document_id: str) -> list[dict[str, Any]]:
        """
        Get software notifications for a HAL document.

        Args:
            document_id: HAL document identifier

        Returns:
            List of notification data
        """
        try:
            # created/used/shared source depends on how many times the software
            # (by normalizedForm) is mentioned in the document:
            #   * exactly one mention  -> that mention's mentionContextAttributes
            #     (the passage-level verdict is the direct evidence when there is
            #     nothing to aggregate across).
            #   * two or more mentions -> documentContextAttributes, OR-aggregated
            #     across the group (softice's document-level verdict, meaningful
            #     precisely because there are several mentions to reconcile).
            # Contexts are always aggregated across the whole group. Missing
            # attributes (legacy mentions) evaluate to false either way.
            query = """
                FOR doc IN documents
                    FILTER doc.file_hal_id == @document_id
                    FOR edge IN edge_doc_to_software
                        FILTER edge._from == doc._id
                        LET mention = DOCUMENT(edge._to)
                        COLLECT softwareName = mention.software_name.normalizedForm INTO mentionsGroup
                        LET onlyMention = FIRST(mentionsGroup).mention
                        RETURN {
                            softwareName: softwareName,
                            contexts: mentionsGroup[*].mention.context,
                            created: LENGTH(mentionsGroup) == 1
                                ? onlyMention.mentionContextAttributes.created.value == true
                                : LENGTH(mentionsGroup[* FILTER CURRENT.mention.documentContextAttributes.created.value == true]) > 0,
                            used: LENGTH(mentionsGroup) == 1
                                ? onlyMention.mentionContextAttributes.used.value == true
                                : LENGTH(mentionsGroup[* FILTER CURRENT.mention.documentContextAttributes.used.value == true]) > 0,
                            shared: LENGTH(mentionsGroup) == 1
                                ? onlyMention.mentionContextAttributes.shared.value == true
                                : LENGTH(mentionsGroup[* FILTER CURRENT.mention.documentContextAttributes.shared.value == true]) > 0,
                            blacklisted: LENGTH(mentionsGroup[* FILTER CURRENT.mention.blacklisted == true]) > 0,
                            quality_invalid: LENGTH(mentionsGroup[* FILTER CURRENT.mention.quality_invalid == true]) > 0
                        }
            """

            result = self.execute_aql_query(
                query, bind_vars={"document_id": document_id}, raw_results=True
            )
            return list(result)

        except Exception as e:
            logger.error(f"Failed to get software notifications for {document_id}: {e}")
            return []

    def reapply_blacklist(self, blacklist: set[str]) -> dict[str, int]:
        """
        Recompute the ``blacklisted`` flag on every stored software mention.

        Sets ``blacklisted: true`` for mentions whose normalized name is in
        ``blacklist`` and ``false`` for all others, in a single server-side
        pass. This also backfills the field on mentions ingested before the
        flag existed (they become ``false``). The ``IN @blacklist`` check
        mirrors BlacklistFilter.filter_mention.

        Args:
            blacklist: Current set of blacklisted normalized software names.

        Returns:
            Dict with ``updated`` (total mentions touched) and ``blacklisted``
            (how many ended up flagged).
        """
        try:
            query = """
                FOR s IN software
                    UPDATE s WITH { blacklisted: s.software_name.normalizedForm IN @blacklist } IN software
                    RETURN NEW.blacklisted
            """
            flags = list(
                self.execute_aql_query(
                    query, bind_vars={"blacklist": list(blacklist)}, raw_results=True
                )
            )
            result = {"updated": len(flags), "blacklisted": sum(1 for f in flags if f)}
            logger.info(
                f"Reapplied blacklist: {result['blacklisted']} of {result['updated']} "
                f"software mentions flagged"
            )
            return result
        except Exception as e:
            logger.error(f"Failed to reapply blacklist: {e}")
            return {"updated": 0, "blacklisted": 0}

    @staticmethod
    def _quality_threshold() -> float:
        """Active quality threshold, read from env like the ingestion path (db default 0.4)."""
        return float(
            env_with_legacy("MENTION_QUALITY_FILTER_THRESHOLD", "MODEL_FILTER_THRESHOLD", "0.4")
        )

    @staticmethod
    def _quality_enabled() -> bool:
        """Whether the quality flag is enforced at send time (MENTION_QUALITY_FILTER_ENABLED)."""
        return env_with_legacy(
            "MENTION_QUALITY_FILTER_ENABLED", "MODEL_FILTER_ENABLED", "false"
        ).lower() in ("1", "true", "yes")

    def get_mention_quality_stats(self) -> dict[str, Any]:
        """
        Read-only stats on the Mention Quality Filter flags across all mentions.

        Reports how many stored software mentions have been scored
        (``quality_score`` present) and how many are currently flagged invalid,
        plus the distinct-name count. The active ``threshold`` and ``enabled``
        state are surfaced too, so the management page can show whether the flag
        is actually enforced (it is only acted on at send time when
        MENTION_QUALITY_FILTER_ENABLED is on). Mirrors count_software_matching_blacklist:
        a failure (or cold, empty DB) yields zeros rather than raising.
        """
        stats: dict[str, Any] = {
            "total_mentions": 0,
            "scored": 0,
            "quality_invalid": 0,
            "distinct_names": 0,
            "threshold": self._quality_threshold(),
            "enabled": self._quality_enabled(),
        }
        try:
            query = """
                RETURN {
                    total_mentions: LENGTH(software),
                    scored: LENGTH(FOR s IN software FILTER s.quality_score != null RETURN 1),
                    quality_invalid: LENGTH(FOR s IN software FILTER s.quality_invalid == true RETURN 1),
                    distinct_names: LENGTH(
                        FOR s IN software
                            FILTER s.software_name.normalizedForm != null
                            RETURN DISTINCT s.software_name.normalizedForm
                    )
                }
            """
            counts = list(self.execute_aql_query(query, raw_results=True))[0]
            stats.update(counts)
        except Exception as e:
            logger.error(f"Failed to get mention quality stats: {e}")
        return stats

    def reapply_mention_quality(self, threshold: float | None = None) -> dict[str, int]:
        """
        Recompute ``quality_score`` and ``quality_invalid`` on every stored mention.

        Backfills the quality flags on mentions ingested before the model existed
        (or while it was disabled), and re-scores everything when the model is
        retrained. Because the classifier keys only off the normalized name,
        distinct names are scored once (batched) and the result is fanned back out
        to every mention in a single server-side AQL UPDATE.

        The ``quality_invalid`` formula matches the ingestion path exactly
        (``score is not None and score < threshold``, see insert_document_as_json).
        Flags are written unconditionally; enforcement stays gated by
        MENTION_QUALITY_FILTER_ENABLED at send time. If the model file is unavailable the
        call is a logged no-op rather than an error.

        Args:
            threshold: Override for the invalid cutoff; defaults to
                MENTION_QUALITY_FILTER_THRESHOLD (0.4).

        Returns:
            Dict with ``updated`` (mentions touched), ``quality_invalid`` (how many
            ended up flagged), and ``distinct_scored`` (distinct names scored).
        """
        if threshold is None:
            threshold = self._quality_threshold()

        no_op = {"updated": 0, "quality_invalid": 0, "distinct_scored": 0}
        try:
            # Reuse ClassifierFilter's cached, gracefully-degrading model loader.
            classifier = ClassifierFilter(threshold=threshold)
            pipe = classifier._pipeline()
            if pipe is None:
                logger.warning("reapply_mention_quality: model unavailable; no mentions updated.")
                return no_op

            names = list(
                self.execute_aql_query(
                    """
                    FOR s IN software
                        FILTER s.software_name.normalizedForm != null
                        RETURN DISTINCT s.software_name.normalizedForm
                    """,
                    raw_results=True,
                )
            )
            if not names:
                return no_op

            # Batch scoring is the production path (see sandbox/score_mentions.py):
            # vectorize + predict in one shot. Column 1 is P(valid).
            proba = pipe.predict_proba(names)[:, 1]
            scores = {name: float(p) for name, p in zip(names, proba, strict=True)}

            query = """
                FOR s IN software
                    LET nm = s.software_name.normalizedForm
                    LET score = nm == null ? null : @scores[nm]
                    UPDATE s WITH {
                        quality_score: score,
                        quality_invalid: score != null && score < @threshold
                    } IN software
                    RETURN NEW.quality_invalid
            """
            flags = list(
                self.execute_aql_query(
                    query,
                    bind_vars={"scores": scores, "threshold": threshold},
                    raw_results=True,
                )
            )
            result = {
                "updated": len(flags),
                "quality_invalid": sum(1 for f in flags if f),
                "distinct_scored": len(names),
            }
            logger.info(
                f"Reapplied mention quality filter (threshold {threshold}): "
                f"{result['quality_invalid']} of {result['updated']} mentions flagged invalid "
                f"({result['distinct_scored']} distinct names scored)"
            )
            return result
        except Exception as e:
            logger.error(f"Failed to reapply mention quality filter: {e}")
            return no_op

    def update_software_with_author_validation(
        self, document_id: str, software_name: str, accepted: bool
    ) -> bool:
        """
        Update software verification status.

        Args:
            document_id: HAL identifier
            software_name: Software name
            accepted: Verification status

        Returns:
            True if update successful, False otherwise
        """
        try:
            query = """
                FOR doc IN documents
                    FILTER doc.file_hal_id == @hal_id
                    FOR edge_soft IN edge_doc_to_software
                        FILTER edge_soft._from == doc._id
                        LET software = DOCUMENT(edge_soft._to)
                        FILTER software.software_name.normalizedForm == @software_name
                        UPDATE software WITH { verification_by_author: @verification } IN software
                        RETURN NEW
            """

            bind_vars = {
                "hal_id": document_id,
                "software_name": software_name,
                "verification": accepted,
            }

            result = self.execute_aql_query(query, bind_vars=bind_vars, raw_results=True)
            updated_count = len(list(result))

            if updated_count > 0:
                logger.info(
                    f"Updated verification status for {updated_count} software entries "
                    f"(HAL: {document_id}, Software: {software_name}, Status: {accepted})"
                )
            else:
                logger.warning(
                    f"No software entries found for HAL: {document_id}, Software: {software_name}"
                )

            return updated_count > 0

        except Exception as e:
            logger.error(f"Failed to update software verification: {e}")
            return False

    def get_collection_count(self, collection_name: str) -> int:
        """
        Get the count of documents in a collection.

        Args:
            collection_name: Name of the collection

        Returns:
            Document count
        """
        try:
            collection = self.get_collection(collection_name)
            if collection:
                return collection.count()
            return 0
        except Exception as e:
            logger.error(f"Failed to get collection count for {collection_name}: {e}")
            return 0

    def get_dashboard_stats(self) -> dict[str, Any]:
        """
        Aggregate counts for the overview dashboard (see ``/dashboard``).

        Returns a single dict with the simple collection totals plus the
        distinct-software count and the notification breakdowns by origin and by
        COAR type. Every piece is computed defensively: a failure (or a cold,
        empty DB) yields zeros / empty breakdowns rather than raising, so the
        dashboard always renders.
        """
        stats: dict[str, Any] = {
            "documents_count": 0,
            "software_count": 0,
            "distinct_software_count": 0,
            "notifications_count": 0,
            # origin -> {"total": n, "accepted": a, "rejected": r}
            "notifications_by_origin": {},
            # Outbound notifications recorded in sent_notifications (since the
            # version that introduced the collection; earlier sends are unknown).
            "notifications_sent_count": 0,  # successful (HTTP 2xx) sends
            "notifications_sent_failed_count": 0,
            # target -> {"total": n, "succeeded": s, "failed": f}
            "notifications_sent_by_target": {},
        }

        # Simple totals via server-side AQL LENGTH(). We deliberately avoid
        # get_collection_count() here: it relies on pyArango's cached collection
        # list (hasCollection), which can be stale when another connection or
        # gunicorn worker created the collection after this Database object was
        # built — yielding a phantom 0. AQL always sees the live collections.
        # The collection names are fixed literals, so interpolation is safe; a
        # missing collection raises and is caught, leaving the total at 0.
        def _count(collection_name: str) -> int:
            try:
                rows = list(
                    self.execute_aql_query(f"RETURN LENGTH({collection_name})", raw_results=True)
                )
                return rows[0] if rows else 0
            except Exception as e:
                logger.error(f"Failed to count {collection_name}: {e}")
                return 0

        stats["documents_count"] = _count("documents")
        stats["software_count"] = _count("software")
        stats["notifications_count"] = _count("received_notifications")

        # Distinct software names (normalized form), mirroring the distinct
        # logic used elsewhere for software lookups. The `!= null` filter lets the
        # sparse persistent index on software_name.normalizedForm serve the distinct
        # scan instead of a full collection enumeration.
        try:
            result = self.execute_aql_query(
                "RETURN COUNT(FOR s IN software "
                "FILTER s.software_name.normalizedForm != null "
                "RETURN DISTINCT s.software_name.normalizedForm)",
                raw_results=True,
            )
            rows = list(result)
            stats["distinct_software_count"] = rows[0] if rows else 0
        except Exception as e:
            logger.error(f"Failed to count distinct software: {e}")

        # Notifications grouped by our derived origin (swh / hal / unknown),
        # with the accepted/rejected split per origin. `payload.type` is
        # polymorphic (string or array), handled via IS_ARRAY; COLLECT ... INTO
        # group = ts gathers each notification's type list so we can count the
        # ones containing Accept / Reject within each origin.
        try:
            result = self.execute_aql_query(
                """
                FOR n IN received_notifications
                    LET ts = IS_ARRAY(n.payload.type) ? n.payload.type : [n.payload.type]
                    COLLECT o = n.origin INTO group = ts
                    RETURN {
                        origin: o,
                        total: LENGTH(group),
                        accepted: LENGTH(group[* FILTER "Accept" IN CURRENT]),
                        rejected: LENGTH(group[* FILTER "Reject" IN CURRENT])
                    }
                """,
                raw_results=True,
            )
            stats["notifications_by_origin"] = {
                (row["origin"] or "unknown"): {
                    "total": row["total"],
                    "accepted": row["accepted"],
                    "rejected": row["rejected"],
                }
                for row in result
            }
        except Exception as e:
            logger.error(f"Failed to count notifications by origin: {e}")

        # Outbound notifications by target (hal / swh) with the succeeded/failed
        # split; the overall totals are the sums over the targets.
        try:
            result = self.execute_aql_query(
                """
                FOR n IN sent_notifications
                    COLLECT t = n.target
                        AGGREGATE total = COUNT(1),
                                  succeeded = SUM(n.success == true ? 1 : 0)
                    RETURN {
                        target: t,
                        total: total,
                        succeeded: succeeded,
                        failed: total - succeeded
                    }
                """,
                raw_results=True,
            )
            by_target: dict[str, dict[str, int]] = {}
            for row in result:
                by_target[row["target"] or "unknown"] = {
                    "total": row["total"],
                    "succeeded": row["succeeded"],
                    "failed": row["failed"],
                }
            stats["notifications_sent_by_target"] = by_target
            stats["notifications_sent_count"] = sum(v["succeeded"] for v in by_target.values())
            stats["notifications_sent_failed_count"] = sum(v["failed"] for v in by_target.values())
        except Exception as e:
            logger.error(f"Failed to count sent notifications by target: {e}")

        return stats

    def _earliest_activity_date(self) -> datetime.date | None:
        """
        The earliest day with any recorded activity (document ingestion, received
        or sent notification), used as the start of the "all time" window.

        Each sub-query walks the collection's sparse timestamp index in order and
        stops at the first entry (``> null`` keeps the sparse index eligible, see
        get_software_distribution). Returns None on an empty DB or on failure.
        """
        try:
            rows = list(
                self.execute_aql_query(
                    """
                    RETURN MIN([
                        FIRST(FOR d IN documents
                                  FILTER d.created_at > null
                                  SORT d.created_at ASC LIMIT 1
                                  RETURN d.created_at),
                        FIRST(FOR n IN received_notifications
                                  FILTER n.received_at > null
                                  SORT n.received_at ASC LIMIT 1
                                  RETURN n.received_at),
                        FIRST(FOR n IN sent_notifications
                                  FILTER n.sent_at > null
                                  SORT n.sent_at ASC LIMIT 1
                                  RETURN n.sent_at)
                    ])
                    """,
                    raw_results=True,
                )
            )
            if rows and rows[0]:
                return datetime.date.fromisoformat(str(rows[0])[:10])
        except Exception as e:
            logger.error(f"Failed to find the earliest activity date: {e}")
        return None

    def get_activity_timeseries(self, days: int | None = 30) -> dict[str, Any]:
        """
        Activity counts over the last ``days`` days (default 30), or over the
        whole recorded history when ``days`` is None.

        The window is split into buckets whose size adapts to its length (see
        ``_bucket_for_span``: daily up to 90 days, weekly up to a year, monthly
        beyond), so a long window still yields a readable chart. Returns a dict
        with ``labels`` (the ISO date of each bucket's first day, oldest first),
        ``bucket`` (``"day"`` / ``"week"`` / ``"month"``), ``since`` / ``until``
        (ISO dates of the window) and ``spans_years`` (whether the window crosses
        a year boundary, for label formatting), plus nine equal-length series:
        ``documents`` and ``software`` (by ingestion ``created_at``),
        ``blacklisted`` and ``quality_invalid`` (software mentions flagged by the
        blacklist / Mention Quality Filter, also by ``created_at``),
        ``notifications`` / ``accepted`` / ``rejected`` (by ``received_at``), and
        ``sent`` / ``sent_failed`` (outbound notifications recorded in
        ``sent_notifications`` by ``sent_at``, split on their ``success`` flag).
        Buckets with no activity are zero-filled so the axis is continuous.

        Each AQL query filters on the raw ISO timestamp (``>= @since`` with a
        ``YYYY-MM-DD`` bound, which sorts before any ``YYYY-MM-DDT...`` value of
        that day) so the collection's timestamp index serves the range scan, and
        groups by day; the daily rows are then folded into buckets here.
        Documents/software only gained a ``created_at`` from the version that
        introduced it, so rows ingested earlier are simply absent. Each query is
        guarded independently; a failure yields a zero series rather than raising.
        """
        today = datetime.datetime.now(datetime.UTC).date()
        if days is None:
            earliest = self._earliest_activity_date()
            start = earliest or today - datetime.timedelta(days=364)
        else:
            start = today - datetime.timedelta(days=max(days, 1) - 1)
        bucket = _bucket_for_span((today - start).days + 1)
        # Snap to the bucket boundary so the first bucket is a complete one.
        start = _bucket_start(start, bucket)

        axis: list[datetime.date] = []
        current = start
        while current <= today:
            axis.append(current)
            current = _next_bucket(current, bucket)
        labels = [d.isoformat() for d in axis]
        index = {label: i for i, label in enumerate(labels)}
        since = start.isoformat()  # ISO dates compare lexicographically.
        n = len(axis)

        def slot(day: str) -> int | None:
            """Bucket position of a ``YYYY-MM-DD`` day, or None if outside the axis."""
            try:
                return index.get(
                    _bucket_start(datetime.date.fromisoformat(day), bucket).isoformat()
                )
            except ValueError:
                return None

        result: dict[str, Any] = {
            "labels": labels,
            "bucket": bucket,
            "since": since,
            "until": today.isoformat(),
            "spans_years": start.year != today.year,
            "documents": [0] * n,
            "software": [0] * n,
            "blacklisted": [0] * n,
            "quality_invalid": [0] * n,
            "notifications": [0] * n,
            "accepted": [0] * n,
            "rejected": [0] * n,
            "sent": [0] * n,
            "sent_failed": [0] * n,
        }

        # Documents by ingestion date (created_at). No `!= null` guard is
        # needed: null sorts below every string in AQL, so the range condition
        # alone excludes untimestamped rows and lets the sparse created_at index
        # serve the scan.
        try:
            rows = self.execute_aql_query(
                """
                FOR d IN documents
                    FILTER d.created_at >= @since
                    COLLECT day = SUBSTRING(d.created_at, 0, 10) WITH COUNT INTO c
                    RETURN {day: day, count: c}
                """,
                bind_vars={"since": since},
                raw_results=True,
            )
            for row in rows:
                i = slot(row["day"])
                if i is not None:
                    result["documents"][i] += row["count"]
        except Exception as e:
            logger.error(f"Failed to build documents timeseries: {e}")

        # Software mentions by ingestion date, with the blacklist / Mention
        # Quality Filter "filter usage" counts, all from one pass. Reads only
        # created_at and the two flags, so the non-sparse
        # [created_at, blacklisted, quality_invalid] index covers it and no
        # document is fetched (the window can span the whole collection).
        try:
            rows = self.execute_aql_query(
                """
                FOR s IN software
                    FILTER s.created_at >= @since
                    COLLECT day = SUBSTRING(s.created_at, 0, 10)
                        AGGREGATE total = COUNT(1),
                                  blacklisted = SUM(s.blacklisted == true ? 1 : 0),
                                  quality_invalid = SUM(s.quality_invalid == true ? 1 : 0)
                    RETURN {
                        day: day,
                        total: total,
                        blacklisted: blacklisted,
                        quality_invalid: quality_invalid
                    }
                """,
                bind_vars={"since": since},
                raw_results=True,
            )
            for row in rows:
                i = slot(row["day"])
                if i is not None:
                    result["software"][i] += row["total"]
                    result["blacklisted"][i] += row["blacklisted"]
                    result["quality_invalid"][i] += row["quality_invalid"]
        except Exception as e:
            logger.error(f"Failed to build software timeseries: {e}")

        # Notifications received, with the accepted/rejected split, by received_at.
        try:
            rows = self.execute_aql_query(
                """
                FOR n IN received_notifications
                    FILTER n.received_at >= @since
                    LET ts = IS_ARRAY(n.payload.type) ? n.payload.type : [n.payload.type]
                    COLLECT day = SUBSTRING(n.received_at, 0, 10) INTO group = ts
                    RETURN {
                        day: day,
                        total: LENGTH(group),
                        accepted: LENGTH(group[* FILTER "Accept" IN CURRENT]),
                        rejected: LENGTH(group[* FILTER "Reject" IN CURRENT])
                    }
                """,
                bind_vars={"since": since},
                raw_results=True,
            )
            for row in rows:
                i = slot(row["day"])
                if i is not None:
                    result["notifications"][i] += row["total"]
                    result["accepted"][i] += row["accepted"]
                    result["rejected"][i] += row["rejected"]
        except Exception as e:
            logger.error(f"Failed to build notifications timeseries: {e}")

        # Notifications sent (recorded attempts), split on success, by sent_at.
        try:
            rows = self.execute_aql_query(
                """
                FOR n IN sent_notifications
                    FILTER n.sent_at >= @since
                    COLLECT day = SUBSTRING(n.sent_at, 0, 10)
                        AGGREGATE total = COUNT(1),
                                  succeeded = SUM(n.success == true ? 1 : 0)
                    RETURN {day: day, succeeded: succeeded, failed: total - succeeded}
                """,
                bind_vars={"since": since},
                raw_results=True,
            )
            for row in rows:
                i = slot(row["day"])
                if i is not None:
                    result["sent"][i] += row["succeeded"]
                    result["sent_failed"][i] += row["failed"]
        except Exception as e:
            logger.error(f"Failed to build sent-notifications timeseries: {e}")

        return result

    def get_software_distribution(
        self,
        limit: int = 20,
        exclude_blacklisted: bool = True,
        exclude_quality_invalid: bool = False,
    ) -> dict[str, Any]:
        """
        Mentions per software name (``software_name.normalizedForm``), most
        mentioned first, limited to the top ``limit`` names, plus the totals the
        UI needs for "top N of M": ``total_names`` (distinct names with at least
        one counted mention) and ``total_mentions``.

        ``exclude_blacklisted`` / ``exclude_quality_invalid`` leave the
        corresponding flagged mentions out of the counts (a name whose mentions
        are all excluded disappears). The exclusions are folded into the
        AGGREGATE rather than expressed as FILTERs, and the name condition uses
        ``> null`` (equivalent to ``!= null``: null sorts below every value), so
        the whole thing is one pass over the covering
        ``[normalizedForm, blacklisted, quality_invalid]`` index with no
        per-document fetch (~2M mentions in prod). Returns zeros / an empty
        ``items`` list on failure.
        """
        result: dict[str, Any] = {
            "limit": limit,
            "exclude_blacklisted": exclude_blacklisted,
            "exclude_quality_invalid": exclude_quality_invalid,
            "total_names": 0,
            "total_mentions": 0,
            "items": [],
        }
        try:
            rows = list(
                self.execute_aql_query(
                    """
                    LET grouped = (
                        FOR s IN software
                            FILTER s.software_name.normalizedForm > null
                            COLLECT name = s.software_name.normalizedForm
                                AGGREGATE c = SUM(
                                    (@exclude_blacklisted == false OR s.blacklisted != true)
                                    AND (@exclude_quality_invalid == false
                                         OR s.quality_invalid != true) ? 1 : 0
                                )
                            FILTER c > 0
                            RETURN {name: name, count: c}
                    )
                    RETURN {
                        total_names: LENGTH(grouped),
                        total_mentions: SUM(grouped[*].count),
                        items: (FOR g IN grouped SORT g.count DESC, g.name ASC LIMIT @limit RETURN g)
                    }
                    """,
                    bind_vars={
                        "limit": limit,
                        "exclude_blacklisted": exclude_blacklisted,
                        "exclude_quality_invalid": exclude_quality_invalid,
                    },
                    raw_results=True,
                )
            )
            if rows:
                result["total_names"] = rows[0].get("total_names") or 0
                result["total_mentions"] = rows[0].get("total_mentions") or 0
                result["items"] = rows[0].get("items") or []
        except Exception as e:
            logger.error(f"Failed to compute software distribution: {e}")
        return result

    def get_latest_documents(self, limit: int = 10) -> list[dict[str, Any]]:
        """
        The ``limit`` most recently ingested documents, newest first.

        Sorted by ``created_at`` (stamped at ingestion from this version on), so
        documents whose rows predate that field are not returned. Failures yield
        an empty list rather than raising.

        Each row also carries ``mentions_count`` (software mentions linked to the
        document) and ``mentions_with_context_count`` (those with at least one
        context characterization — created/used/shared. Mentions whose softice
        output lacks ``mentionContextAttributes`` count as uncharacterized.
        """
        try:
            return list(
                self.execute_aql_query(
                    """
                    FOR d IN documents
                        FILTER d.created_at != null
                        SORT d.created_at DESC
                        LIMIT @limit
                        LET mentions = (
                            FOR edge IN edge_doc_to_software
                                FILTER edge._from == d._id
                                RETURN DOCUMENT(edge._to)
                        )
                        RETURN {
                            file_hal_id: d.file_hal_id,
                            created_at: d.created_at,
                            mentions_count: LENGTH(mentions),
                            mentions_with_context_count: LENGTH(mentions[*
                                FILTER CURRENT.mentionContextAttributes.created.value == true
                                    OR CURRENT.mentionContextAttributes.used.value == true
                                    OR CURRENT.mentionContextAttributes.shared.value == true
                            ])
                        }
                    """,
                    bind_vars={"limit": limit},
                    raw_results=True,
                )
            )
        except Exception as e:
            logger.error(f"Failed to get latest documents: {e}")
            return []

    def get_latest_mentions(
        self,
        limit: int = 10,
        blacklisted: bool | None = None,
        quality_invalid: bool | None = None,
    ) -> list[dict[str, Any]]:
        """
        The ``limit`` most recently ingested software mentions, newest first.

        Sorted by ``created_at`` like get_latest_documents. Returns the
        normalized software name and ingestion timestamp, the ``blacklisted`` /
        ``quality_invalid`` flags and the raw ``quality_score``, plus a ``context``
        object with the created/used/shared characterization booleans. A mention
        whose softice output lacks ``mentionContextAttributes`` reports all three
        as ``false``.

        Optional ``blacklisted`` / ``quality_invalid`` filters narrow the result to
        mentions with that flag set (``True``) or not set (``False``); ``None``
        (the default) applies no filter. The not-set case uses ``!= true`` so
        legacy mentions that predate a flag (stored as null) count as not-flagged,
        matching how these flags are interpreted everywhere else.
        """
        try:
            # Filter clauses are fixed literals selected by the boolean args (no
            # user text is interpolated), like list_received_notifications.
            filters = ["FILTER s.created_at != null"]
            if blacklisted is not None:
                filters.append(
                    "FILTER s.blacklisted == true"
                    if blacklisted
                    else "FILTER s.blacklisted != true"
                )
            if quality_invalid is not None:
                filters.append(
                    "FILTER s.quality_invalid == true"
                    if quality_invalid
                    else "FILTER s.quality_invalid != true"
                )
            filter_clause = "\n                        ".join(filters)
            query = f"""
                    FOR s IN software
                        {filter_clause}
                        SORT s.created_at DESC
                        LIMIT @limit
                        RETURN {{
                            name: s.software_name.normalizedForm,
                            created_at: s.created_at,
                            blacklisted: s.blacklisted == true,
                            quality_invalid: s.quality_invalid == true,
                            quality_score: s.quality_score,
                            context: {{
                                created: s.mentionContextAttributes.created.value == true,
                                used: s.mentionContextAttributes.used.value == true,
                                shared: s.mentionContextAttributes.shared.value == true
                            }}
                        }}
                    """
            return list(self.execute_aql_query(query, bind_vars={"limit": limit}, raw_results=True))
        except Exception as e:
            logger.error(f"Failed to get latest mentions: {e}")
            return []

    def get_document_by_key(self, collection_name: str, key: str) -> dict[str, Any] | None:
        """
        Fetch a single document by `_key` from a named collection.
        """
        try:
            query = f"FOR d IN {collection_name} FILTER d._key == @key RETURN d"
            result = self.execute_aql_query(query, bind_vars={"key": key}, raw_results=True)
            docs = list(result)
            if docs:
                return docs[0]
        except Exception as e:
            logger.error(f"Failed to get document by key {collection_name}/{key}: {e}")
        return None

    def store_received_notification(
        self, notification: dict[str, Any], origin: str | None = None
    ) -> None:
        """
        Persist an incoming COAR notification for later inspection via `/notifications`.

        `origin` is our derived classification of the sender (e.g. "swh", "hal",
        "unknown"), stored alongside the verbatim payload so it can be filtered on.
        """
        try:
            collection = self.check_or_create_collection("received_notifications")
            doc = collection.createDocument(
                {
                    "received_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "origin": origin,
                    "payload": notification,
                }
            )
            doc.save()
        except Exception as e:
            logger.error(f"Failed to persist received notification: {e}")

    def list_received_notifications(
        self,
        limit: int = 100,
        origin: str | None = None,
        notification_type: str | None = None,
        has_comment: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return the most recent received notifications, newest first.

        If `origin` is given, only notifications classified with that origin
        (e.g. "swh" or "hal") are returned. If `notification_type` is given,
        only notifications whose COAR `type` matches are returned; the payload's
        `type` may be a single string or an array, so both shapes are handled.
        If `has_comment` is true, only notifications carrying a non-empty
        `summary` (the author's free-text comment) are returned.
        """
        try:
            # Ensure collection exists so the query doesn't fail on a cold DB.
            self.check_or_create_collection("received_notifications")
            bind_vars: dict[str, Any] = {"limit": limit}
            filters = []
            if origin:
                filters.append("FILTER n.origin == @origin")
                bind_vars["origin"] = origin
            if notification_type:
                # `payload.type` is polymorphic in JSON-LD: a bare string or an
                # array of strings. Branch on IS_ARRAY so neither shape is missed.
                filters.append(
                    "FILTER (IS_ARRAY(n.payload.type) "
                    "? @ntype IN n.payload.type "
                    ": n.payload.type == @ntype)"
                )
                bind_vars["ntype"] = notification_type
            if has_comment:
                filters.append(
                    "FILTER IS_STRING(n.payload.summary) AND LENGTH(TRIM(n.payload.summary)) > 0"
                )
            filter_clause = "\n                    ".join(filters)
            query = f"""
                FOR n IN received_notifications
                    {filter_clause}
                    SORT n.received_at DESC
                    LIMIT @limit
                    RETURN n
            """
            result = self.execute_aql_query(query, bind_vars=bind_vars, raw_results=True)
            return list(result)
        except Exception as e:
            logger.error(f"Failed to list received notifications: {e}")
            return []

    def distinct_received_notification_types(self) -> list[str]:
        """
        Return the distinct COAR notification `type` values seen so far, sorted.

        Used to build the type filter UI. Flattens array-shaped `type` values so
        each individual type appears once regardless of how it was stored.
        """
        try:
            self.check_or_create_collection("received_notifications")
            query = """
                FOR n IN received_notifications
                    LET types = IS_ARRAY(n.payload.type) ? n.payload.type : [n.payload.type]
                    FOR t IN types
                        FILTER t != null
                        SORT t
                        RETURN DISTINCT t
            """
            return list(self.execute_aql_query(query, raw_results=True))
        except Exception as e:
            logger.error(f"Failed to list distinct notification types: {e}")
            return []

    def store_sent_notification(
        self,
        *,
        target: str,
        document_id: str,
        software_name: str | None,
        payload: dict[str, Any],
        target_inbox: str | None,
        success: bool,
        http_status: int | None,
        error: str | None,
        mention_context_attributes: dict[str, bool] | None,
        filter_mode: str | None,
    ) -> str | None:
        """
        Persist one outbound COAR notification attempt (successful or not) in
        ``sent_notifications`` for the dashboard and ``/notifications/sent``.

        ``target`` is the provider ("hal" / "swh"), ``payload`` the JSON-LD that
        was (or would have been) POSTed; its ``id`` URN and ``type`` are lifted
        to top-level fields for indexing and filtering. ``success`` is transport
        success (HTTP 2xx); the author's later Accept/Reject lives in
        ``received_notifications``. Never raises: storage must not fail the send.

        Returns:
            the new record's ``_key``, or None on failure.
        """
        try:
            collection = self.check_or_create_collection("sent_notifications")
            doc = collection.createDocument(
                {
                    "sent_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "target": target,
                    "document_id": document_id,
                    "software_name": software_name,
                    "notification_id": payload.get("id"),
                    "notification_type": payload.get("type"),
                    "target_inbox": target_inbox,
                    "payload": payload,
                    "success": success,
                    "http_status": http_status,
                    "error": error,
                    "mention_context_attributes": mention_context_attributes,
                    "filter_mode": filter_mode,
                }
            )
            doc.save()
            return str(doc._key)
        except Exception as e:
            logger.error(f"Failed to persist sent notification: {e}")
            return None

    def list_sent_notifications(
        self,
        limit: int = 100,
        target: str | None = None,
        document_id: str | None = None,
        success: bool | None = None,
        software_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return the most recent sent-notification records, newest first.

        Each optional argument adds an exact-match filter (``target`` "hal" /
        "swh", the HAL ``document_id``, the ``success`` flag, the normalized
        ``software_name``).
        """
        try:
            self.check_or_create_collection("sent_notifications")
            bind_vars: dict[str, Any] = {"limit": limit}
            filters = []
            if target:
                filters.append("FILTER n.target == @target")
                bind_vars["target"] = target
            if document_id:
                filters.append("FILTER n.document_id == @document_id")
                bind_vars["document_id"] = document_id
            if success is not None:
                filters.append("FILTER n.success == @success")
                bind_vars["success"] = success
            if software_name:
                filters.append("FILTER n.software_name == @software_name")
                bind_vars["software_name"] = software_name
            filter_clause = "\n                    ".join(filters)
            query = f"""
                FOR n IN sent_notifications
                    {filter_clause}
                    SORT n.sent_at DESC
                    LIMIT @limit
                    RETURN n
            """
            result = self.execute_aql_query(query, bind_vars=bind_vars, raw_results=True)
            return list(result)
        except Exception as e:
            logger.error(f"Failed to list sent notifications: {e}")
            return []

    def count_sent_notifications(self, success: bool | None = None) -> int:
        """Number of sent-notification records, optionally only the (un)successful ones."""
        try:
            self.check_or_create_collection("sent_notifications")
            if success is None:
                rows = list(
                    self.execute_aql_query("RETURN LENGTH(sent_notifications)", raw_results=True)
                )
            else:
                rows = list(
                    self.execute_aql_query(
                        "RETURN COUNT(FOR n IN sent_notifications "
                        "FILTER n.success == @success RETURN 1)",
                        bind_vars={"success": success},
                        raw_results=True,
                    )
                )
            return rows[0] if rows else 0
        except Exception as e:
            logger.error(f"Failed to count sent notifications: {e}")
            return 0

    def get_document_by_id(self, id: str) -> dict[str, Any] | None:
        """
        Get a document by id with related softwares

        Args:
            id: Document id

        Returns:
            Document data with related softwares or None if not found
        """
        try:
            query = """
                FOR doc IN documents
                    FILTER doc.file_hal_id == @id
                    LET mentions = (
                        FOR edge IN edge_doc_to_software
                            FILTER edge._from == doc._id
                            LET software = DOCUMENT(edge._to)
                            RETURN software
                    )
                    RETURN {
                        document: doc,
                        mentions: mentions
                    }
            """
            result = self.execute_aql_query(query, bind_vars={"id": id}, raw_results=True)
            docs = list(result)
            if docs:
                return docs[0]

        except Exception:
            logger.debug(f"Document not found: documents/{id}")
        return None

    def delete_document_by_id(self, document_id: str) -> dict[str, Any] | None:
        """
        Delete a document and all its associated software mentions by file_hal_id.

        Args:
            document_id: HAL document identifier (file_hal_id)

        Returns:
            Dict with deletion results or None if failed
        """
        try:
            query = """
                LET matching_docs = (
                    FOR d IN documents
                        FILTER d.file_hal_id == @document_id
                        RETURN d
                )

                LET software_to_delete = (
                    FOR d IN matching_docs
                        FOR edge IN edge_doc_to_software
                            FILTER edge._from == d._id
                            RETURN DOCUMENT(edge._to)
                )

                LET delete_edges = (
                    FOR d IN matching_docs
                        FOR edge IN edge_doc_to_software
                            FILTER edge._from == d._id
                            REMOVE edge IN edge_doc_to_software
                )

                LET delete_software = (
                    FOR software IN software_to_delete
                        REMOVE software IN software
                )

                LET delete_doc = (
                    FOR d IN matching_docs
                        REMOVE d IN documents
                )

                RETURN {
                    deleted: true,
                    document_id: @document_id,
                    software_deleted: COUNT(software_to_delete)
                }
            """

            result = self.execute_aql_query(
                query, bind_vars={"document_id": document_id}, raw_results=True
            )
            deletion_result = list(result)

            if deletion_result:
                software_count = deletion_result[0].get("software_deleted", 0)
                logger.info(
                    f"Successfully deleted document {document_id} and {software_count} software entries"
                )
                return deletion_result[0]
            else:
                logger.warning(f"No document found to delete with ID: {document_id}")
                return None

        except Exception as e:
            logger.error(f"Failed to delete document {document_id}: {e}")
            return None

    def get_software_by_normalized_name(self, name: str) -> list[dict[str, Any]]:
        """
        Get software documents by normalized name.

        Args:
            name: Software name

        Returns:
            List of matching software documents
        """
        try:
            query = """
                FOR soft IN software
                    FILTER soft.software_name.normalizedForm == @name
                    RETURN soft
            """

            result = self.execute_aql_query(query, bind_vars={"name": name}, raw_results=True)
            return list(result)

        except Exception as e:
            logger.error(f"Failed to get software by normalized name {name}: {e}")
            return []

    def get_document_software(
        self, id_document: str, id_software: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Get software linked to a document.

        Args:
            id_document: Document ID
            id_software: Optional software ID for filtering

        Returns:
            List of software documents
        """
        try:
            if id_software:
                query = """
                    FOR doc IN documents
                        FILTER doc.file_hal_id == @id_document
                        FOR edge_soft IN edge_doc_to_software
                            FILTER edge_soft._from == doc._id
                            LET software = DOCUMENT(edge_soft._to)
                            FILTER software._key == @software_id
                            RETURN software
                """
                result = self.execute_aql_query(
                    query,
                    bind_vars={"id_document": id_document, "software_id": id_software},
                    raw_results=True,
                )
            else:
                query = """
                    FOR doc IN documents
                        FILTER doc.file_hal_id == @id_document
                        FOR edge_soft IN edge_doc_to_software
                            FILTER edge_soft._from == doc._id
                            LET software = DOCUMENT(edge_soft._to)
                            RETURN software
                """
                result = self.execute_aql_query(
                    query, bind_vars={"id_document": id_document}, raw_results=True
                )

            return list(result)

        except Exception as e:
            logger.error(f"Failed to get document software: {e}")
            return []


def init_db(app):
    """
    Initialize the database manager.

    Args:
        app: Flask application instance

    Returns:
        DatabaseManager: The initialized database manager
    """
    global db_manager

    db_manager = DatabaseManager(
        host=app.config["ARANGO_HOST"],
        port=app.config["ARANGO_PORT"],
        username=app.config["ARANGO_USERNAME"],
        password=app.config["ARANGO_PASSWORD"],
        db_name=app.config["ARANGO_DB"],
    )

    # Initialize the database (creates if needed)
    db_manager.get_database()

    logger.info(f"Database manager initialized for {app.config['ARANGO_DB']}")
    return db_manager


def get_db() -> DatabaseManager:
    """
    Get the global database manager instance.

    Returns:
        DatabaseManager: The database manager instance

    Raises:
        RuntimeError: If database manager is not initialized
    """
    if db_manager is None:
        raise RuntimeError("Database manager not initialized. Call init_db() first.")
    return db_manager
