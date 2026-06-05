import csv
import datetime
import json
import logging
from typing import Any, Union

from pyArango.collection import Collection
from pyArango.connection import Connection
from pyArango.database import Database
from pyArango.theExceptions import CreationError
from werkzeug.datastructures import FileStorage

logger = logging.getLogger(__name__)

# Global database manager instance
db_manager: Union["DatabaseManager", None] = None


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
        ],
        # Notifications are filtered by origin and sorted newest-first by received_at.
        "received_notifications": [
            {"fields": ["origin"]},
            {"fields": ["received_at"]},
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
                collection.ensurePersistentIndex(spec["fields"], unique=spec.get("unique", False))
                logger.debug(
                    f"Ensured index on {name}.{'.'.join(spec['fields'])} "
                    f"(unique={spec.get('unique', False)})"
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
        blacklist_csv: str = "./app/static/data/blacklist.csv",
    ) -> bool:
        """
        Insert a JSON file into ArangoDB with document, software, and edge collections.

        Args:
            document_id: Unique identifier for the document
            file_json: File object or dictionary containing the data
            blacklist_csv: Path to blacklist CSV file

        Returns:
            True if inserted, False if already exists or failed
        """
        try:
            # Get collections
            documents_collection = self.check_or_create_collection("documents")
            software_collection = self.check_or_create_collection("software")
            doc_soft_edge = self.check_or_create_collection("edge_doc_to_software", "Edges")

            # Load blacklist
            blacklist = self.load_blacklist(blacklist_csv)

            # Process input
            if hasattr(file_json, "read"):
                data_json = json.load(file_json)
            else:
                data_json = file_json

            # Check if document already exists
            if self.document_exists("documents", "file_hal_id", document_id):
                logger.warning(f"Document with ID '{document_id}' already exists in DB. Skipping.")
                return False

            # Insert main document
            document_document = documents_collection.createDocument({"file_hal_id": document_id})
            document_document.save()
            logger.debug(f"Created document with ID: {document_id}")

            # Process mentions
            mentions = self.remove_duplicates(data_json.get("mentions", []))
            inserted_count = 0

            for mention in mentions:
                norm_name = mention["software-name"]["normalizedForm"]
                if norm_name not in blacklist:
                    # Rename fields for consistency
                    mention["software_name"] = mention.pop("software-name")
                    mention["software_type"] = mention.pop("software-type")

                    # Insert software document
                    software_document = software_collection.createDocument(mention)
                    software_document.save()

                    # Create edge from document to software
                    edge_doc_soft = doc_soft_edge.createEdge()
                    edge_doc_soft["_from"] = document_document._id
                    edge_doc_soft["_to"] = software_document._id
                    edge_doc_soft.save()

                    inserted_count += 1

            logger.info(
                f"Inserted {inserted_count} software mentions for document with ID: {document_id}"
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
            query = """
                FOR doc IN documents
                    FILTER doc.file_hal_id == @document_id
                    FOR edge IN edge_doc_to_software
                        FILTER edge._from == doc._id
                        LET mention = DOCUMENT(edge._to)
                        COLLECT softwareName = mention.software_name.normalizedForm INTO mentionsGroup
                        RETURN {
                            softwareName: softwareName,
                            contexts: mentionsGroup[*].mention.context,
                            created: LENGTH(mentionsGroup[* FILTER CURRENT.mention.mentionContextAttributes.created.value == true]) > 0,
                            used:    LENGTH(mentionsGroup[* FILTER CURRENT.mention.mentionContextAttributes.used.value    == true]) > 0,
                            shared:  LENGTH(mentionsGroup[* FILTER CURRENT.mention.mentionContextAttributes.shared.value  == true]) > 0
                        }
            """

            result = self.execute_aql_query(
                query, bind_vars={"document_id": document_id}, raw_results=True
            )
            return list(result)

        except Exception as e:
            logger.error(f"Failed to get software notifications for {document_id}: {e}")
            return []

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
            "notifications_by_origin": {},
            "notifications_by_type": {},
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
        # logic used elsewhere for software lookups.
        try:
            result = self.execute_aql_query(
                "RETURN COUNT(FOR s IN software RETURN DISTINCT s.software_name.normalizedForm)",
                raw_results=True,
            )
            rows = list(result)
            stats["distinct_software_count"] = rows[0] if rows else 0
        except Exception as e:
            logger.error(f"Failed to count distinct software: {e}")

        # Notifications grouped by our derived origin (swh / hal / unknown).
        try:
            result = self.execute_aql_query(
                """
                FOR n IN received_notifications
                    COLLECT o = n.origin WITH COUNT INTO c
                    RETURN {origin: o, count: c}
                """,
                raw_results=True,
            )
            stats["notifications_by_origin"] = {
                (row["origin"] or "unknown"): row["count"] for row in result
            }
        except Exception as e:
            logger.error(f"Failed to count notifications by origin: {e}")

        # Notifications grouped by COAR type. `payload.type` is polymorphic
        # (string or array), so flatten via IS_ARRAY just like
        # distinct_received_notification_types does.
        try:
            result = self.execute_aql_query(
                """
                FOR n IN received_notifications
                    LET ts = IS_ARRAY(n.payload.type) ? n.payload.type : [n.payload.type]
                    FOR t IN ts
                        FILTER t != null
                        COLLECT type = t WITH COUNT INTO c
                        SORT type
                        RETURN {type: type, count: c}
                """,
                raw_results=True,
            )
            stats["notifications_by_type"] = {row["type"]: row["count"] for row in result}
        except Exception as e:
            logger.error(f"Failed to count notifications by type: {e}")

        return stats

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
    ) -> list[dict[str, Any]]:
        """
        Return the most recent received notifications, newest first.

        If `origin` is given, only notifications classified with that origin
        (e.g. "swh" or "hal") are returned. If `notification_type` is given,
        only notifications whose COAR `type` matches are returned; the payload's
        `type` may be a single string or an array, so both shapes are handled.
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
