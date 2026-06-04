# COAR-Notify-INRIA-HAL

This project implements the COAR notify specification for the INRIA HAL repository for extraction of software mentions
from research papers.

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Environment Variables](#environment-variables)
- [Database Schema](#database-schema)
- [API Documentation](#api-documentation)
    - [Authentication](#authentication)
    - [Health Endpoints](#health-endpoints)
    - [Document Management](#document-management)
    - [Software Endpoints](#software-endpoints)
    - [Blacklist Management](#blacklist-management)
    - [COAR Notify Inbox](#coar-notify-inbox)
- [Notification System](#notification-system)
- [Production Deployment](#production-deployment)
    - [Nginx Reverse Proxy](#nginx-reverse-proxy)
- [Development](#development)

## Overview

The COAR Notify INRIA HAL system is a comprehensive platform for extracting and managing software mentions from research
papers stored in the HAL repository. The system implements the COAR (Coalition of Open Access Repositories) notification
specification to enable bidirectional communication between research repositories and external services.

### Key Features

- **Automated Software Mention Extraction**: Processes research papers to identify software mentions with confidence
  scoring
- **Multi-Provider Support**: Handles HAL, Software Heritage, Zenodo, and GitHub repositories
- **COAR-Compliant Notifications**: Sends and receives standardized notifications for verification workflows
- **Graph-Based Data Model**: Uses ArangoDB to store complex relationships between documents and software
- **Blacklist Management**: Filters out generic or non-software terms with a managed blacklist system
- **RESTful API**: Complete API for document management, software queries, and system administration
- **Provider-Aware Processing**: Different notification types and processing logic per data provider

### Architecture

The system consists of three main collections in ArangoDB:

- **Documents**: Stores HAL paper metadata
- **Software**: Contains extracted software mentions with context and confidence scores
- **Edges**: Links documents to their software mentions for graph traversal

For detailed database schema information, see the [Database Schema Documentation](docs/database.md).

## Quick Start

1. Copy `.env.example` to `.env` and adjust your secrets (recommended):
   ```sh
   cp .env.example .env
   ```
2. Build and start the stack:
   ```sh
   docker compose up --build
   ```
3. Access the services:
    - Flask app: http://localhost:5000
    - ArangoDB UI: http://localhost:8529 (login as `root` with the password from `.env`, default: `changeme`)

## Configuration

- The app reads all configuration from environment variables, loaded from a `.env` file via `python-dotenv` (see `.env.example`). In containers (Compose), env variables provided by the orchestrator take precedence.
- Inside Docker Compose networks, containers must use service names to talk to each other (e.g., `ARANGO_HOST=arangodb`), not `localhost`.
- Defaults now match Compose so it works out-of-the-box in containers:
    - `ARANGO_HOST=arangodb`
    - `ARANGO_PORT=8529`
    - `ARANGO_DB=COAR_NOTIFY_DB`
    - `ARANGO_USERNAME=root`
    - `ARANGO_PASSWORD=changeme`
    - `FLASK_PORT=5000`

### Running without Docker Compose (local dev)

If you run the app locally (e.g., `python run.py` or via a local virtualenv), set `ARANGO_HOST=localhost` in a `.env`
file or environment variables, because `arangodb` is only resolvable inside Compose.

Example `.env` for local runs:

```
ARANGO_HOST=localhost
ARANGO_PORT=8529
ARANGO_USERNAME=root
ARANGO_ROOT_PASSWORD=changeme
ARANGO_DB=COAR_NOTIFY_DB
FLASK_PORT=5000
```

## Environment Variables

- `ARANGO_HOST`: Hostname for ArangoDB (Compose default: `arangodb`; local: `localhost`)
- `ARANGO_PORT`: Port for ArangoDB (default: `8529`)
- `ARANGO_ROOT_PASSWORD`: Root password for ArangoDB (default: `changeme`)
- `ARANGO_USERNAME`: Username for ArangoDB (default: `root`)
- `ARANGO_DB`: Database name (default: `COAR_NOTIFY_DB`)
- `FLASK_PORT`: Port for Flask app (default: `5000`)

## Database Schema

The system uses ArangoDB with three main collections:

- **Documents Collection** (`documents`): Stores HAL document metadata with unique HAL identifiers
- **Software Collection** (`software`): Contains extracted software mentions with confidence scores and context
- **Edge Collection** (`edge_doc_to_software`): Creates relationships between documents and software mentions

### Key Database Features

- **Graph Capabilities**: Enables complex traversal queries between documents and software
- **Automatic Indexing**: Optimized for performance with unique constraints
- **Blacklist Filtering**: 255+ terms filtered automatically during ingestion
- **Verification Tracking**: COAR notifications update `verification_by_author` status

For comprehensive database documentation including schemas, queries, and performance considerations,
see [Database Schema Documentation](docs/database.md).

## API Documentation

### Base URL and reverse-proxy prefix

- Default base URL (local): `http://localhost:5000`
- If served behind NGINX under a prefix (e.g., `/coar`), prepend that prefix to all paths (e.g.,
  `/coar/api/software/status`, `/coar/health`).

### API Entry Points Summary

| Method                   | Endpoint                               | Auth Required | Description                              |
|--------------------------|----------------------------------------|---------------|------------------------------------------|
| **Health & Status**      |
| GET                      | `/`                                    | No            | Home page with database status           |
| GET                      | `/health`                              | No            | Service health check                     |
| GET                      | `/status`                              | Yes           | Upload capability check                  |
| **Document Management**  |
| GET                      | `/api/documents`                       | No            | Documents collection status              |
| GET                      | `/api/document/<id>`                   | No            | Get document by ID                       |
| DELETE                   | `/api/document/<id>`                   | Yes           | Delete document and all software mentions|
| GET                      | `/api/document/<id>/software`          | No            | All software for document                |
| GET                      | `/api/document/<id>/software/<id_sw>`  | No            | Specific software for document           |
| POST                     | `/api/document`                        | Yes           | Insert document (triggers notifications) |
| **Software Endpoints**   |
| GET                      | `/api/software`                        | No            | Software collection status               |
| GET                      | `/api/software/name/<name>`            | No            | Software by normalized name              |
| GET                      | `/api/software/<id_mention>`           | No            | Software mention by ID                   |
| **Blacklist Management** |
| GET                      | `/api/blacklist`                       | No            | View/search blacklist                    |
| GET                      | `/api/blacklist/stats`                 | No            | Blacklist statistics                     |
| POST                     | `/api/blacklist`                       | Yes           | Add term to blacklist                    |
| DELETE                   | `/api/blacklist/<term>`                | Yes           | Remove term from blacklist               |
| POST                     | `/api/blacklist/reload`                | Yes           | Reload blacklist from file               |
| GET                      | `/api/blacklist/export`                | No            | Export blacklist as CSV                  |
| POST                     | `/api/blacklist/import`                | Yes           | Import blacklist from CSV                |
| **COAR Notify Inbox**    |
| GET                      | `/inbox`                               | No            | Get inbox API documentation              |
| POST                     | `/inbox`                               | No            | Receive COAR notification                |
| GET                      | `/notifications`                       | No            | View received notifications (HTML)       |
| GET                      | `/api/notifications`                   | Yes           | List received notifications (JSON)       |
| GET                      | `/api/notifications/<key>`             | Yes           | Get a received notification by key (JSON)|

### Authentication

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

### Health Endpoints

#### Service Health

- **GET `/health`**
    - Returns service status and basic ArangoDB info
    - Returns 200 when up; 503 when down

Example:

```sh
curl -s http://localhost:5000/health | jq
```

#### General Status (with auth)

- **GET `/status`**
    - Headers: `x-api-key`
    - Checks API access and database reachability; lists existence of key collections

Example:

```sh
curl -s -H "x-api-key: $API_KEY" http://localhost:5000/status | jq
```

### Document Management

#### Documents Collection Status

- **GET `/api/documents`**
    - Returns count and status of documents collection

#### Get Document by ID

- **GET `/api/document/<id>`**
    - Returns document metadata by HAL identifier
    - Returns 404 if not found

#### Delete Document

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

#### Get Document Software (All)

- **GET `/api/document/<id_document>/software`**
    - Returns all software mentions for a specific document

#### Get Document Software (Specific)

- **GET `/api/document/<id_document>/software/<id_software>`**
    - Returns a specific software mention for a document

#### Insert Document

- **POST `/api/document`**
    - Headers: `x-api-key`
    - Content-Type: `multipart/form-data` with fields:
        - `file`: JSON file containing software metadata (required)
        - `document_id`: HAL identifier for the document (required)
    - Returns 201 on new insert, 409 if already exists
    - Triggers notification send attempt to HAL and Software Heritage

Examples:

```sh
# Get documents status
curl -s http://localhost:5000/api/documents | jq

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
```

### Software Endpoints

#### Software Status

- **GET `/api/software`**
    - Returns count and status of software collection

#### Get Software by Normalized Name

- **GET `/api/software/name/<name>`**
    - Returns all software mentions with the same normalized name

#### Get Software Mention by ID

- **GET `/api/software/<id_mention>`**
    - Returns a single software mention document by `_key`
    - Returns 404 if not found

Examples:

```sh
# Get software collection status
curl -s http://localhost:5000/api/software | jq

# Get software by normalized name
curl -s http://localhost:5000/api/software/name/python | jq

# Get specific software mention
curl -s http://localhost:5000/api/software/mention456 | jq
```

### Blacklist Management

The blacklist system filters out generic or non-software terms during document processing.

#### View Blacklist

- **GET `/api/blacklist`**
    - Query Parameters:
        - `search`: Search terms (optional)
        - `limit`: Maximum number of results (default: 50)
    - Returns blacklist terms with statistics

#### Get Blacklist Statistics

- **GET `/api/blacklist/stats`**
    - Returns statistics about the blacklist (total terms, file info)

#### Add Term to Blacklist

- **POST `/api/blacklist`**
    - Headers: `x-api-key`
    - JSON Body: `{"term": "term_to_add"}`
    - Returns 201 on success, 409 if term already exists

#### Remove Term from Blacklist

- **DELETE `/api/blacklist/<term>`**
    - Headers: `x-api-key`
    - Returns 200 on success, 404 if term not found

#### Reload Blacklist from File

- **POST `/api/blacklist/reload`**
    - Headers: `x-api-key`
    - Reloads blacklist from CSV file
    - Returns total number of terms loaded

#### Export Blacklist

- **GET `/api/blacklist/export`**
    - Downloads the blacklist as a CSV file

#### Import Blacklist

- **POST `/api/blacklist/import`**
    - Headers: `x-api-key`
    - Form Data:
        - `file`: CSV file to import (required)
        - `overwrite`: Whether to overwrite existing blacklist (default: false)
    - Returns import results with statistics

Examples:

```sh
# View blacklist
curl -s http://localhost:5000/api/blacklist | jq

# Search blacklist terms
curl -s "http://localhost:5000/api/blacklist?search=python&limit=10" | jq

# Get statistics
curl -s http://localhost:5000/api/blacklist/stats | jq

# Add term (requires API key)
curl -s -X POST \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"term": "example"}' \
  http://localhost:5000/api/blacklist | jq

# Remove term (requires API key)
curl -s -X DELETE \
  -H "x-api-key: $API_KEY" \
  http://localhost:5000/api/blacklist/example | jq

# Reload blacklist (requires API key)
curl -s -X POST \
  -H "x-api-key: $API_KEY" \
  http://localhost:5000/api/blacklist/reload | jq

# Export blacklist
curl -s http://localhost:5000/api/blacklist/export -o blacklist.csv
```

### COAR Notify Inbox

The COAR Notify inbox handles bidirectional communication for software mention verification workflows.

#### Get Inbox Documentation

- **GET `/inbox`**
    - Returns comprehensive API documentation for the COAR Notify inbox
    - Includes request/response examples, supported notification types, and usage instructions

#### Receive Notification

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

#### View Received Notifications

- **GET `/notifications`**
    - Renders an HTML page displaying all received notifications
    - Useful for debugging and inspection during development

#### List Received Notifications (JSON)

- **GET `/api/notifications`** — requires `x-api-key`
    - Returns recent received notifications as JSON, newest first
    - Query params:
        - `limit` — max records to return (1–1000, default 100)
        - `origin` — optional filter, `swh` or `hal` (any other value → 400)
    - Response: `{ "count": <n>, "origin": <filter|null>, "notifications": [ ... ] }`
    - Each record: `{ "_key", "received_at", "origin", "payload": { ...verbatim notification... } }`

#### Get a Received Notification by Key (JSON)

- **GET `/api/notifications/<key>`** — requires `x-api-key`
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
```

**Supported Notification Types:**

- **Accept**: Verifies a software mention as correct by the author
- **Reject**: Marks a software mention as incorrect by the author

## Notification System

The notification system implements the COAR specification for bidirectional communication between research repositories
and external services.

### Supported Notification Types

- **ActionReview**: Used for peer review and citation notifications (HAL, Zenodo)
- **RelationshipAnnounce**: Used for linking and repository announcements (Software Heritage, GitHub)

### Provider-Specific Processing

The system automatically detects the provider and selects the appropriate notification type:

- **HAL**: ActionReview notifications for peer review workflows
- **Software Heritage**: RelationshipAnnounce notifications for software linking

### Verification Workflow

1. **Software Mention Extraction**: Papers are processed to identify software mentions
2. **Notification Sending**: Automated notifications are sent to relevant providers
3. **Author Response**: External systems send accept/reject notifications
4. **Status Updates**: `verification_by_author` field is updated in the database
5. **Feedback Loop**: Verification status influences future processing

### Configuration

Notification endpoints are configured via environment variables:

```bash
# HAL Configuration
HAL_BASE_URL=https://inria.hal.science
HAL_INBOX_URL=https://inbox-preprod.archives-ouvertes.fr/

# Software Heritage Configuration
SWH_BASE_URL=https://archive.softwareheritage.org
SWH_INBOX_URL=https://inbox.softwareheritage.org
```

## Receiving notifications

The inbox is able to receive the accept/reject notification directly in the inbox.

the handle of software verification notifications from HAL:

accept_notification(notification)
Marks a software as verified by its author. It extracts the HAL document ID and software name from the notification, finds the corresponding software in the database via the document-to-software edge, and sets verification_by_author to True.

reject_notification(notification)
Marks a software as not verified by the author. It follows the same process as accept_notification, but sets verification_by_author to False.

Both functions run the query on the ArangoDB database.


## Production Deployment

### Nginx Reverse Proxy

For production deployments, it's recommended to run the COAR Notify service behind an Nginx reverse proxy. This provides
SSL termination, proper header forwarding, and security headers.

For complete Nginx configuration examples, see the [Nginx Reverse Proxy Documentation](docs/nginx.md).

#### Quick Nginx Setup

1. **Install Nginx**:
   ```bash
   # Ubuntu/Debian
   sudo apt install nginx
   # CentOS/RHEL
   sudo yum install nginx
   ```

2. **Create Nginx configuration** (`/etc/nginx/sites-available/coar-notify`):
   ```nginx
   server {
       listen 443 ssl http2;
       server_name your-domain.com;

       ssl_certificate /path/to/cert.pem;
       ssl_certificate_key /path/to/key.pem;

       location /coar {
           proxy_pass http://127.0.0.1:5000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header X-Forwarded-Prefix /coar;
           rewrite ^/coar(.*)$ $1 break;
       }
   }
   ```

3. **Enable the site**:
   ```bash
   sudo ln -s /etc/nginx/sites-available/coar-notify /etc/nginx/sites-enabled/
   sudo nginx -t
   sudo systemctl reload nginx
   ```

#### Docker Compose with Nginx

Add Nginx to your `docker-compose.yml`:

```yaml
services:
  app:
  # Remove direct port mapping
  # ports:
  #   - "5000:5000"

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - app
```

## Development

### System Requirements

- Docker and Docker Compose
- Python 3.11+ (for local development)
- ArangoDB instance

### Local Development Setup

1. **Clone the repository**:
   ```sh
   git clone <repository-url>
   cd COAR-Notify-INRIA-HAL
   ```

2. **Set up environment**:
   ```sh
   cp .env.example .env
   # Edit .env with your configuration
   ```

3. **Start services**:
   ```sh
   docker compose up --build
   ```

4. **Access services**:
    - Flask app: http://localhost:5000
    - ArangoDB UI: http://localhost:8529

### Code Quality

Development tools are pinned in `requirements-dev.txt` (`ruff`, `mypy`,
`bump-my-version`); install them with:

```sh
pip install -r requirements-dev.txt
```

Linting and formatting use [Ruff](https://docs.astral.sh/ruff/). The CI build
**fails** if either check reports a problem, so run them before pushing:

```sh
# Check lint and formatting (these are the exact steps CI runs)
ruff check app run.py
ruff format --check app run.py

# Auto-fix lint findings and reformat in place
ruff check --fix app run.py
ruff format app run.py
```

Install the pinned Ruff version from `requirements-dev.txt` — a newer Ruff may
report different issues than CI and make your local results disagree with the
pipeline.

Type checking is relaxed for now and runs **non-blocking** in CI:

```sh
mypy app
```

### Releasing

Releases are cut manually with `bump-my-version`; CI handles the rest on tag push.

1. Ensure `main` is green and your working tree is clean.
2. Bump the version — this updates `pyproject.toml`, commits, and creates a
   `vX.Y.Z` tag:
   ```sh
   bump-my-version bump [patch|minor|major]
   ```
3. Push the commit and the tag:
   ```sh
   git push && git push --tags
   ```

On the tag push, CI builds and publishes the Docker image
`lfoppiano/coar-notify-inria-hal:vX.Y.Z` and creates a GitHub Release with
auto-generated notes from the commits since the previous tag.
