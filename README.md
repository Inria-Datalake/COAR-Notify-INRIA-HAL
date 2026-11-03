# COAR-Notify-INRIA-HAL

This project implements the COAR notify specification for the INRIA HAL repository for extraction of software mentions
from research papers.

## Documentation

Detailed documentation lives under [`docs/`](docs/):

| Topic | Description |
|-------|-------------|
| [Configuration & Setup](docs/configuration.md) | Quick start, Docker/local configuration, environment variables |
| [API Documentation](docs/api.md) | All HTTP endpoints — documents, software, mention quality, blacklist, inbox |
| [Notification System](docs/notifications.md) | How notifications are built, filtered per provider, and received |
| [Mention Quality Filter](docs/mention-quality-filter.md) | Optional junk-name classifier: runtime behaviour, tuning, backfill, and the model's training/validation/metrics |
| [Database Schema](docs/database.md) | Collections, fields, indexes, sample AQL, performance considerations |
| [Production Deployment](docs/deployment.md) | Running behind an Nginx reverse proxy |
| [Nginx Reverse Proxy](docs/nginx.md) | Complete Nginx configuration reference |
| [Development](docs/development.md) | Local setup, code quality (Ruff/mypy), releasing |

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
- **Blacklist Management**: Flags generic or non-software terms via a persistent, ArangoDB-backed blacklist
  (with a web UI); flagged mentions are still stored but excluded from outbound notifications
- **Mention Quality Filter** (optional): A trained char-ngram model scores each mention name as
  good/junk at ingestion; when enabled, junk mentions are flagged and excluded from notifications
  (see [Mention Quality Filter](docs/mention-quality-filter.md))
- **RESTful API**: Complete API for document management, software queries, and system administration
- **Provider-Aware Processing**: Different notification types and processing logic per data provider

### Architecture

The system consists of three main collections in ArangoDB:

- **Documents**: Stores HAL paper metadata
- **Software**: Contains extracted software mentions with context and confidence scores
- **Edges**: Links documents to their software mentions for graph traversal

Additional collections back the blacklist (`blacklist`), inbound COAR notifications
(`received_notifications`) and the outbound notification log (`sent_notifications`). For the
full schema, indexes, and sample queries, see the
[Database Schema Documentation](docs/database.md).

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

See [Configuration & Setup](docs/configuration.md) for the full configuration and environment-variable reference,
and [Development](docs/development.md) for the local (non-Docker) workflow.
