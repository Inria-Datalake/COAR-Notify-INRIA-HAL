# Notification System

[← Back to README](../README.md)

The notification system implements the COAR specification for bidirectional communication between research repositories
and external services.

## Supported Notification Types

- **ActionReview**: Used for peer review and citation notifications (HAL, Zenodo)
- **RelationshipAnnounce**: Used for linking and repository announcements (Software Heritage, GitHub)

## Provider-Specific Processing

The system automatically detects the provider and selects the appropriate notification type:

- **HAL**: ActionReview notifications for peer review workflows
- **Software Heritage**: RelationshipAnnounce notifications for software linking

## Verification Workflow

1. **Software Mention Extraction**: Papers are processed to identify software mentions
2. **Notification Sending**: Automated notifications are sent to relevant providers
3. **Author Response**: External systems send accept/reject notifications
4. **Status Updates**: `verification_by_author` field is updated in the database
5. **Feedback Loop**: Verification status influences future processing

## Configuration

Notification endpoints are configured via environment variables:

```bash
# HAL Configuration
HAL_BASE_URL=https://inria.hal.science
HAL_INBOX_URL=https://inbox-preprod.archives-ouvertes.fr/

# Software Heritage Configuration
SWH_BASE_URL=https://archive.softwareheritage.org
SWH_INBOX_URL=https://inbox.softwareheritage.org
```

## Software relationship in the payload (`mentionContextAttributes`)

Every outbound notification reports how the software relates to the publication — whether it was
`created` (developed in this work), `used`, and/or `shared` by the paper's authors. These flags are
derived from the software-mention recognizer and aggregated per software in
`db.get_software_notifications`.

The flags are carried in a `mentionContextAttributes` object inside the notification `object`, for
**both** provider payloads:

- **HAL** (`ActionReview`): inside `object`, alongside `mentionType` / `mentionContext`.
- **Software Heritage** (`RelationshipAnnounce`): inside the `Relationship` `object`, alongside `as:subject` / `as:relationship`.

```json
"mentionContextAttributes": {
  "created": true,
  "used":    true,
  "shared":  false
}
```

The key is always present; when no attributes are available it defaults to all `false`. Because these
are the same flags that drive [Notification Filtering](#notification-filtering), a notification's
attributes always agree with the filter mode it was sent under.

The **source** of these flags depends on how many times the software (by normalized name) is mentioned
in the document: a name mentioned exactly once uses that mention's `mentionContextAttributes` (the
passage-level verdict), while a name mentioned two or more times uses `documentContextAttributes`
(softice's document-level verdict, OR-aggregated across the mentions). See
`get_software_notifications` in `app/utils/db.py` and the
[Database Schema Documentation](database.md#context-attributes).

Complete request examples for both providers are in
[`notif_test/`](notif_test/) (`hal-offer-with-attributes.json`,
`swh-announce-with-attributes.json`).

## Notification Filtering

By default, every software mention extracted from a document generates a notification to its provider.
You can restrict which mentions are sent on a **per-provider** basis, using the **mention context** of each
software — whether it was `created` (developed in this work), `used`, or `shared` by the paper's authors.

This is useful when a provider should only hear about a subset of mentions — for example, sending HAL only
the software the authors *created*, or sending Software Heritage only software that was *shared*.

Filtering is controlled by two environment variables (or the `NOTIFICATION_FILTER` block in `config.json`),
each set to one of the following modes:

| Mode                 | Sends a notification when the software is…                          |
|----------------------|---------------------------------------------------------------------|
| `none`               | never — disables sending to that provider entirely                  |
| `all` (default)      | always — no filtering                                               |
| `created`            | marked as *created*                                                 |
| `used`               | marked as *used*                                                    |
| `shared`             | marked as *shared*                                                  |
| `reused`             | *used* but **not** *created* (i.e. a third-party dependency)        |
| `reused_and_shared`  | *used* **and** *shared* but **not** *created*                       |
| `created_not_shared` | *created* but **not** *shared*                                      |

```bash
# Send HAL only software the authors created; disable Software Heritage entirely
HAL_NOTIFICATION_FILTER=created
SWH_NOTIFICATION_FILTER=none
```

Equivalent defaults in `config.json`:

```json
"NOTIFICATION_FILTER": {
  "HAL": "all",
  "SOFTWARE_HERITAGE": "all"
}
```

An unknown or misspelled mode is **not** treated as an error: a warning is logged and all notifications are
passed through unchanged, so a typo never silently drops notifications. The number of mentions skipped by a
filter is logged per document.

Independently of the mode filter, mentions flagged `blacklisted` (see [Blacklist Management](api.md#blacklist-management))
are always excluded from outbound notifications, and — when `MENTION_QUALITY_FILTER_ENABLED=true` — mentions flagged
`quality_invalid` (see [Mention Quality Filter](mention-quality-filter.md)) are excluded
too. The order is: blacklist filter → quality filter → per-provider mode filter; each logs how many mentions it
skips per document.

## Outbound notification log

Every send attempt made by the ingestion path (`POST /api/document`, unless `notify=false`) is
recorded in the `sent_notifications` collection, whether it succeeded or not — one record per
software name, document and provider (HAL / Software Heritage). Each record keeps:

- the JSON-LD payload as sent, with its `id` URN and COAR `type` lifted to top-level fields,
- the target and inbox URL, the HAL document id and the software name,
- `success` (the inbox answered HTTP 2xx), `http_status` and an `error` text for failures
  (timeout, connection error, HTTP error with the response body, or an exception),
- the `created` / `used` / `shared` flags and the per-provider filter mode in force.

`success` is **transport** success only: HAL replies 202 (accepted for processing), and the
author's Accept/Reject decision arrives later in the inbox and is stored as a *received*
notification. Recording never interferes with sending — a database problem is logged and the
send keeps its real outcome.

The log feeds the dashboard ("Notifications sent" card = successful sends, the
"Notifications sent by target" table, and the sent / failed timeseries) and is browsable at
`GET /notifications/sent` or through `GET /api/notifications/sent` (see the
[API documentation](api.md#list-sent-notifications-json)). It only exists from the version
that introduced it: earlier sends were never persisted and cannot be backfilled, so the counts
start at zero on deployment.

Received Accept/Reject notifications carry `inReplyTo` equal to the original Offer's `id`, so
they can be joined to the originating record through the unique `notification_id` index;
that join is not implemented yet.

## Receiving notifications

The inbox is able to receive the accept/reject notification directly in the inbox.

the handle of software verification notifications from HAL:

accept_notification(notification)
Marks a software as verified by its author. It extracts the HAL document ID and software name from the notification, finds the corresponding software in the database via the document-to-software edge, and sets verification_by_author to True.

reject_notification(notification)
Marks a software as not verified by the author. It follows the same process as accept_notification, but sets verification_by_author to False.

Both functions run the query on the ArangoDB database.

See the [COAR Notify Inbox API](api.md#coar-notify-inbox) for the inbox endpoints and payload examples.
