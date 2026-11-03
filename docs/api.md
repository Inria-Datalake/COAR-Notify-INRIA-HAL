# API Documentation

[← Back to README](../README.md)

## Base URL and reverse-proxy prefix

- Default base URL (local): `http://localhost:5000`
- If served behind NGINX under a prefix (e.g., `/coar`), prepend that prefix to all paths (e.g.,
  `/coar/api/software/status`, `/coar/health`).

## API Entry Points Summary

| Method                   | Endpoint                               | Auth Required | Description                              |
|--------------------------|----------------------------------------|---------------|------------------------------------------|
| **Health & Status**      |
| GET                      | `/`                                    | No            | Dashboard; `?days=30|90|180|365|all` selects the activity window |
| GET                      | `/health`                              | No            | Service health check                     |
| GET                      | `/status`                              | Yes           | Upload capability check                  |
| GET                      | `/blacklist`                           | No            | Blacklist management web UI               |
| **Document Management**  |
| GET                      | `/api/documents`                       | No            | Documents collection status              |
| GET                      | `/api/documents/latest`                | No            | Latest ingested documents (newest first) |
| GET                      | `/api/document/<id>`                   | No            | Get document by ID                       |
| DELETE                   | `/api/document/<id>`                   | Yes           | Delete document and all software mentions|
| GET                      | `/api/document/<id>/software`          | No            | All software for document                |
| GET                      | `/api/document/<id>/software/<id_sw>`  | No            | Specific software for document           |
| POST                     | `/api/document`                        | Yes           | Insert document (notifications optional, see `notify`) |
| **Software Endpoints**   |
| GET                      | `/api/software`                        | No            | Software collection status               |
| GET                      | `/api/software/latest`                 | No            | Latest ingested mentions (newest first); `?blacklisted=`/`?quality_invalid=` filters |
| GET                      | `/api/software/distribution`           | No            | Mentions per software name, top N (global software distribution) |
| GET                      | `/api/software/name/<name>`            | No            | Software by normalized name              |
| GET                      | `/api/software/<id_mention>`           | No            | Software mention by ID                   |
| **Mention Quality Filter** |
| GET                      | `/api/mention-quality/stats`           | No            | Quality-filter counts (scored / flagged) |
| POST                     | `/api/mention-quality/reapply`         | No            | Re-score all stored mentions             |
| **Blacklist Management** |
| GET                      | `/api/blacklist`                       | No            | View/search blacklist                    |
| GET                      | `/api/blacklist/stats`                 | No            | Blacklist statistics                     |
| GET                      | `/api/blacklist/match-count`           | No            | Dry run: stored mentions the list flags  |
| POST                     | `/api/blacklist`                       | No            | Add term to blacklist                    |
| DELETE                   | `/api/blacklist/<term>`                | No            | Remove term from blacklist               |
| POST                     | `/api/blacklist/reapply`               | No            | Recompute flag on all stored mentions    |
| POST                     | `/api/blacklist/reload`                | No            | Merge seed CSV into the collection        |
| GET                      | `/api/blacklist/export`                | No            | Export blacklist as CSV                  |
| POST                     | `/api/blacklist/import`                | No            | Import blacklist from CSV                |
| **COAR Notify Inbox**    |
| GET                      | `/inbox`                               | No            | Get inbox API documentation              |
| POST                     | `/inbox`                               | No            | Receive COAR notification                |
| GET                      | `/notifications`                       | No            | View received notifications (HTML)       |
| GET                      | `/api/notifications`                   | Yes           | List received notifications (JSON)       |
| GET                      | `/api/notifications/<key>`             | Yes           | Get a received notification by key (JSON)|
| **Sent Notifications**   |
| GET                      | `/notifications/sent`                  | No            | View sent notifications log (HTML)       |
| GET                      | `/api/notifications/sent`              | Yes           | List sent notifications (JSON)           |
| GET                      | `/api/notifications/sent/<key>`        | Yes           | Get a sent notification by key (JSON)    |

## Authentication

Some endpoints require an API token sent via the `x-api-key` header. The token is configured through the
`API_TOKEN` environment variable (loaded from `.env`, like all other configuration — see `.env.example`).

Set it before running the app:

```sh
# in your .env
API_TOKEN=<your-strong-token>
```

Generate a strong value with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Clients then send that value in the `x-api-key` header. Requests with a missing or incorrect token receive
`401 Unauthorized`.

Example header:

```
x-api-key: <your-token>
```

## Health Endpoints

### Service Health

- **GET `/health`**
    - Returns service status and basic ArangoDB info
    - Returns 200 when up; 503 when down

Example:

```sh
curl -s http://localhost:5000/health | jq
```

### General Status (with auth)

- **GET `/status`**
    - Headers: `x-api-key`
    - Checks API access and database reachability; lists existence of key collections

Example:

```sh
curl -s -H "x-api-key: $API_KEY" http://localhost:5000/status | jq
```

## Document Management

### Documents Collection Status

- **GET `/api/documents`**
    - Returns count and status of documents collection

### Latest Documents

- **GET `/api/documents/latest`**
    - Returns the most recently ingested documents, newest first
    - Query Parameters:
        - `limit`: Number of documents to return (1-100, default: 10)
    - Sorted by ingestion time (`created_at`); documents ingested before this
      field was introduced are not included
    - Response: `{ "count", "limit", "documents": [{ "file_hal_id", "created_at" }] }`

### Get Document by ID

- **GET `/api/document/<id>`**
    - Returns document metadata by HAL identifier
    - Returns 404 if not found

### Delete Document

- **DELETE `/api/document/<id>`**
    - Headers: `x-api-key`
    - Deletes a document and ALL its associated software mentions
    - Performs atomic deletion: edges → software → document
    - Returns JSON response with deletion statistics
    - Returns 404 if document not found
    - Returns 500 if deletion fails

Response examples:

```json
# Success (200)
{
  "status": "deleted",
  "document_id": "hal-01478788",
  "software_deleted": 5
}

# Document not found (404)
{
  "error": "Document not found"
}

# Deletion failed (500)
{
  "error": "Failed to delete document"
}
```

### Get Document Software (All)

- **GET `/api/document/<id_document>/software`**
    - Returns all software mentions for a specific document

### Get Document Software (Specific)

- **GET `/api/document/<id_document>/software/<id_software>`**
    - Returns a specific software mention for a document

### Insert Document

- **POST `/api/document`**
    - Headers: `x-api-key`
    - Content-Type: `multipart/form-data` with fields:
        - `file`: JSON file containing software metadata (required)
        - `document_id`: HAL identifier for the document (required)
        - `notify`: whether to send notifications after ingestion (optional, default `true`). Set to `false` (also accepts `0`, `no`, `off`) to load the data without sending any notification — useful for bulk imports.
    - Returns 201 on new insert, 409 if already exists
    - When `notify` is enabled (default), triggers a notification send attempt to HAL and Software Heritage; when disabled the response contains `"notifications": {"skipped": true}`

Examples:

```sh
# Get documents status
curl -s http://localhost:5000/api/documents | jq

# Get the 10 latest ingested documents (default limit)
curl -s http://localhost:5000/api/documents/latest | jq

# Get the 5 latest ingested documents
curl -s "http://localhost:5000/api/documents/latest?limit=5" | jq

# Get specific document
curl -s http://localhost:5000/api/document/hal-01478788 | jq

# Delete document and all software mentions (requires API key)
curl -s -X DELETE \
  -H "x-api-key: $API_KEY" \
  http://localhost:5000/api/document/hal-01478788 | jq

# Get all software for a document
curl -s http://localhost:5000/api/document/hal-01478788/software | jq

# Insert new document
curl -s -X POST \
  -H "x-api-key: $API_KEY" \
  -F "file=@/path/to/your.json" \
  -F "document_id=hal-01478788" \
  http://localhost:5000/api/document | jq

# Load a document WITHOUT sending any notification (e.g. bulk import)
curl -s -X POST \
  -H "x-api-key: $API_KEY" \
  -F "file=@/path/to/your.json" \
  -F "document_id=hal-01478788" \
  -F "notify=false" \
  http://localhost:5000/api/document | jq
```

## Software Endpoints

### Software Status

- **GET `/api/software`**
    - Returns count and status of software collection

### Latest Mentions

- **GET `/api/software/latest`**
    - Returns the most recently ingested software mentions, newest first
    - Query Parameters:
        - `limit`: Number of mentions to return (1-100, default: 10)
        - `blacklisted`: `true`/`false` — keep only blacklisted / non-blacklisted mentions (omit for no filter)
        - `quality_invalid`: `true`/`false` — keep only quality-invalid / quality-valid mentions (omit for no filter)
    - Sorted by ingestion time (`created_at`); mentions ingested before this
      field was introduced are not included
    - Each mention carries its flags and score: `blacklisted`, `quality_invalid`, and
      `quality_score` (P(valid) in [0,1], or `null` when never scored — run
      `POST /api/mention-quality/reapply` to backfill)
    - Response: `{ "count", "limit", "filters": { "blacklisted", "quality_invalid" },
      "mentions": [{ "name", "created_at", "blacklisted", "quality_invalid", "quality_score", "context" }] }`

### Software Distribution

- **GET `/api/software/distribution`**
    - Returns the number of mentions per normalized software name, most mentioned
      first — the global software distribution also charted on the dashboard
    - Query Parameters:
        - `limit`: Number of names to return (1-100, default: 20)
        - `exclude_blacklisted`: `true`/`false` (default `true`) — leave blacklisted mentions out of the counts
        - `exclude_quality_invalid`: `true`/`false` — leave quality-filtered mentions out; defaults to
          the Mention Quality Filter's enabled state (`MENTION_QUALITY_FILTER_ENABLED`), so the
          default distribution matches what is actually notifiable
    - A name whose mentions are all excluded does not appear and is not counted in the totals
    - Response: `{ "limit", "exclude_blacklisted", "exclude_quality_invalid",
      "total_names", "total_mentions", "items": [{ "name", "count" }] }`

### Get Software by Normalized Name

- **GET `/api/software/name/<name>`**
    - Returns all software mentions with the same normalized name

### Get Software Mention by ID

- **GET `/api/software/<id_mention>`**
    - Returns a single software mention document by `_key`
    - Returns 404 if not found

Examples:

```sh
# Get software collection status
curl -s http://localhost:5000/api/software | jq

# Get the 10 latest ingested mentions (default limit)
curl -s http://localhost:5000/api/software/latest | jq

# Get the 5 latest ingested mentions
curl -s "http://localhost:5000/api/software/latest?limit=5" | jq

# Inspect the latest blacklisted mentions
curl -s "http://localhost:5000/api/software/latest?limit=10&blacklisted=true" | jq

# Inspect the latest mentions the model flagged invalid (with their scores)
curl -s "http://localhost:5000/api/software/latest?limit=10&quality_invalid=true" | jq

# Global software distribution (top 20, blacklisted excluded)
curl "http://localhost:5000/api/software/distribution"

# Top 50 including blacklisted names
curl "http://localhost:5000/api/software/distribution?limit=50&exclude_blacklisted=false"

# Get software by normalized name
curl -s http://localhost:5000/api/software/name/python | jq

# Get specific software mention
curl -s http://localhost:5000/api/software/mention456 | jq
```

## Mention Quality Filter API

The Mention Quality Filter scores every software-mention name and flags low-quality / junk ones (see
[Mention Quality Filter](mention-quality-filter.md)). Like the blacklist it **flags, doesn't drop**, and is
**enforced only at notification time** (and only while `MENTION_QUALITY_FILTER_ENABLED` is on). The `/mention-quality`
web UI (linked from the dashboard) shows the current counts and a button to re-run the filter; it is backed
by two endpoints:

- **GET `/api/mention-quality/stats`**
    - Read-only counts: `total_mentions`, `scored`, `quality_invalid`, `distinct_names`, plus the active
      `threshold` and whether enforcement is `enabled`. Does not modify data.
- **POST `/api/mention-quality/reapply`**
    - Re-scores every stored mention with the current model and rewrites `quality_score` / `quality_invalid`
      at `MENTION_QUALITY_FILTER_THRESHOLD` (default 0.4). Use it to backfill mentions ingested before the filter
      existed, or to propagate a retrained model. Idempotent for a fixed model+threshold; a logged no-op
      if the model file is unavailable. Returns `{ "status", "updated", "quality_invalid", "distinct_scored" }`.

To inspect *which* mentions are flagged, use the `/api/software/latest` filters above
(`?quality_invalid=true`, `?blacklisted=true`).

```sh
# Current Mention Quality Filter counts
curl -s http://localhost:5000/api/mention-quality/stats | jq

# Re-score all stored mentions (backfill / after retraining)
curl -s -X POST http://localhost:5000/api/mention-quality/reapply | jq
```

## Blacklist Management

The blacklist holds generic or non-software terms (e.g. `Python`, `Windows`, `Section`) that should not
generate notifications. Its behaviour:

- **Flag, don't drop.** At ingestion every mention is stored; mentions whose normalized name is on the
  blacklist are marked `blacklisted: true` on the `software` document. Nothing is silently discarded.
- **Enforced only at notification time.** When notifications are built, blacklisted mentions are excluded
  from what is sent to HAL and Software Heritage (the rest of the data, and the APIs, still expose them).
- **Persistent storage.** The blacklist lives in the ArangoDB `blacklist` collection, so edits made through
  the API or UI survive restarts. On first startup it is seeded once from `app/static/data/blacklist.csv`.
- **Web UI.** A management page is available at `/blacklist` (add/remove/filter terms, a read-only
  *Count matches* dry run, *Reapply*, and CSV export).

> **Note on auth:** the blacklist write endpoints below (`POST`/`DELETE`/`reapply`/`reload`/`import`) are
> **not** behind `x-api-key` — they back the unauthenticated `/blacklist` UI. Gate them at the reverse proxy
> if the UI must be restricted.

Because enforcement uses the stored flag, changing the blacklist does **not** retroactively change already
ingested documents — run **Reapply** (`POST /api/blacklist/reapply`) to recompute flags across stored mentions.

### View Blacklist

- **GET `/api/blacklist`**
    - Query Parameters:
        - `search`: Search terms (optional)
        - `limit`: Maximum number of results (default: 50)
    - Returns blacklist terms with statistics

### Get Blacklist Statistics

- **GET `/api/blacklist/stats`**
    - Returns statistics about the blacklist (total terms, storage backend, seed path)

### Count Matches (dry run)

- **GET `/api/blacklist/match-count`**
    - Read-only: counts how many **stored** mentions the current blacklist would flag, without changing
      anything. Returns `total_mentions`, `matching_mentions`, `matching_names`, and `blacklist_terms`.
    - Useful to size up the impact before running Reapply.

### Add Term to Blacklist

- **POST `/api/blacklist`**
    - JSON Body: `{"term": "term_to_add"}`
    - Returns 201 on success, 409 if term already exists

### Remove Term from Blacklist

- **DELETE `/api/blacklist/<term>`**
    - Returns 200 on success, 404 if term not found
    - The term is taken as a URL path segment (a `path` converter, so names containing `/` are supported);
      percent-encode it in the client.

### Reapply Blacklist to Existing Documents

- **POST `/api/blacklist/reapply`**
    - Recomputes the `blacklisted` flag on **every** stored mention against the current blacklist (sets
      `true` for listed names, `false` otherwise — also backfilling mentions ingested before the flag existed).
    - Returns `{"status": "reapplied", "updated": N, "blacklisted": M}`.
    - This is a single AQL `UPDATE` over the whole `software` collection; on large datasets it can take a while.

### Reload Blacklist from the Seed CSV

- **POST `/api/blacklist/reload`**
    - Merges the seed CSV (`app/static/data/blacklist.csv`) into the collection, adding any missing terms
      (existing terms are left untouched). Returns the total term count.

### Export Blacklist

- **GET `/api/blacklist/export`**
    - Downloads the blacklist as a CSV file

### Import Blacklist

- **POST `/api/blacklist/import`**
    - Form Data:
        - `file`: CSV file to import (required)
        - `overwrite`: Whether to clear the collection before importing (default: false)
    - Returns import results with statistics

Examples:

```sh
# View blacklist
curl -s http://localhost:5000/api/blacklist | jq

# Search blacklist terms
curl -s "http://localhost:5000/api/blacklist?search=python&limit=10" | jq

# Get statistics
curl -s http://localhost:5000/api/blacklist/stats | jq

# Count how many stored mentions the current blacklist would flag (read-only)
curl -s http://localhost:5000/api/blacklist/match-count | jq

# Add term
curl -s -X POST \
  -H "Content-Type: application/json" \
  -d '{"term": "example"}' \
  http://localhost:5000/api/blacklist | jq

# Remove term (percent-encode names containing "/")
curl -s -X DELETE http://localhost:5000/api/blacklist/example | jq

# Recompute the blacklisted flag on all stored mentions
curl -s -X POST http://localhost:5000/api/blacklist/reapply | jq

# Merge the seed CSV into the collection
curl -s -X POST http://localhost:5000/api/blacklist/reload | jq

# Export blacklist
curl -s http://localhost:5000/api/blacklist/export -o blacklist.csv
```

## COAR Notify Inbox

The COAR Notify inbox handles bidirectional communication for software mention verification workflows.

### Get Inbox Documentation

- **GET `/inbox`**
    - Returns comprehensive API documentation for the COAR Notify inbox
    - Includes request/response examples, supported notification types, and usage instructions

### Receive Notification

- **POST `/inbox`**
    - Accepts a JSON-LD COAR notification payload
    - Content-Type: `application/json` or `application/ld+json`
    - Supported types: `Accept`, `Reject`
    - Returns 202 with notification processing summary
    - Automatically updates verification status in database
    - **Every** received notification is persisted to the `received_notifications`
      collection (with a `received_at` timestamp and a classified `origin` of
      `swh`, `hal`, or `unknown`), including those that are ignored.
    - Notifications originating from Software Heritage are stored but **not
      dispatched** (returned with `"status": "ignored"`), to avoid notification
      loops.

### View Received Notifications

- **GET `/notifications`**
    - Renders an HTML page displaying all received notifications
    - Useful for debugging and inspection during development

### List Received Notifications (JSON)

- **GET `/api/notifications`** — requires `x-api-key`
    - Returns recent received notifications as JSON, newest first
    - Query params:
        - `limit` — max records to return (1–1000, default 100)
        - `origin` — optional filter, `swh` or `hal` (any other value → 400)
    - Response: `{ "count": <n>, "origin": <filter|null>, "notifications": [ ... ] }`
    - Each record: `{ "_key", "received_at", "origin", "payload": { ...verbatim notification... } }`

### Get a Received Notification by Key (JSON)

- **GET `/api/notifications/<key>`** — requires `x-api-key`
    - `<key>` is the `_key` returned by the list endpoint
    - Returns the single record, or 404 if not found

### View Sent Notifications

- **GET `/notifications/sent`**
    - Renders an HTML table of the outbound notification log (every attempt the
      ingestion path made to HAL / Software Heritage, newest first, 200 at most)
    - Query params: `target` (`hal` | `swh`), `status` (`succeeded` | `failed`);
      invalid values are ignored

### List Sent Notifications (JSON)

- **GET `/api/notifications/sent`** — requires `x-api-key`
    - Returns recent sent-notification records as JSON, newest first
    - Query params:
        - `limit` — max records to return (1–1000, default 100)
        - `target` — optional filter, `hal` or `swh` (any other value → 400)
        - `document_id` — optional exact-match filter on the HAL document id
        - `software_name` — optional exact-match filter on the normalized software name
        - `success` — optional `true`/`false` filter on the transport outcome (HTTP 2xx)
    - Response: `{ "count", "limit", "filters": { "target", "document_id", "software_name", "success" }, "notifications": [ ... ] }`
    - Each record: `{ "_key", "sent_at", "target", "document_id", "software_name", "notification_id",
      "notification_type", "target_inbox", "payload", "success", "http_status", "error",
      "mention_context_attributes", "filter_mode" }` (see
      [Database Schema](database.md#5-sent-notifications-collection-sent_notifications))

### Get a Sent Notification by Key (JSON)

- **GET `/api/notifications/sent/<key>`** — requires `x-api-key`
    - `<key>` is the `_key` returned by the list endpoint
    - Returns the single record, or 404 if not found

Examples:

```sh
# Get inbox API documentation
curl -s http://localhost:5000/inbox | jq

# Send Accept notification
curl -s -X POST \
  -H "Content-Type: application/json" \
  -d '{
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
  }' \
  http://localhost:5000/inbox | jq

# View notifications in browser
# http://localhost:5000/notifications

# List received notifications as JSON (requires API key)
curl -s -H "x-api-key: $API_TOKEN" \
  "http://localhost:5000/api/notifications?limit=20" | jq

# Only Software Heritage notifications
curl -s -H "x-api-key: $API_TOKEN" \
  "http://localhost:5000/api/notifications?origin=swh" | jq

# Fetch a single notification by its storage key
curl -s -H "x-api-key: $API_TOKEN" \
  http://localhost:5000/api/notifications/106031488 | jq

# View the sent-notification log in browser
# http://localhost:5000/notifications/sent

# List sent notifications as JSON (requires API key)
curl -s -H "x-api-key: $API_TOKEN" \
  "http://localhost:5000/api/notifications/sent?limit=20" | jq

# Failed attempts to Software Heritage only
curl -s -H "x-api-key: $API_TOKEN" \
  "http://localhost:5000/api/notifications/sent?target=swh&success=false" | jq

# Everything sent for one document
curl -s -H "x-api-key: $API_TOKEN" \
  "http://localhost:5000/api/notifications/sent?document_id=hal-03685380v1" | jq
```

**Supported Notification Types:**

- **Accept**: Verifies a software mention as correct by the author
- **Reject**: Marks a software mention as incorrect by the author

---

For how notifications are built, filtered, and received, see
[Notification System](notifications.md).
