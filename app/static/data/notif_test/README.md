# Example COAR Notify payloads

Sample notification payloads used for manual testing and as a reference for the
JSON-LD this service produces and consumes.

## Outbound (sent by this service)

| File | Type | Target | Notes |
|------|------|--------|-------|
| `hal-offer-with-attributes.json` | `Offer` + `coar-notify:ReviewAction` | HAL | Built by `ActionReviewNotifier`. |
| `swh-announce-with-attributes.json` | `Announce` + `coar-notify:RelationshipAction` | Software Heritage | Built by `RelationshipAnnounceNotifier`. |
| `sofair-hal-03685380-2.json` | `Offer` + `coar-notify:ReviewAction` | HAL | Older example. |

### `mentionContextAttributes`

Both outbound examples carry a `mentionContextAttributes` object inside `object`:

```json
"mentionContextAttributes": {
    "created": true,
    "used":    true,
    "shared":  false
}
```

These flags report how the software relates to the publication, derived from the
software-mention recognizer and aggregated per software in
`db.get_software_notifications`. The same flags also drive the
`HAL_NOTIFICATION_FILTER` / `SWH_NOTIFICATION_FILTER` filter modes
(see `app/utils/notification_handler.py`), so a notification's attributes always
agree with the filter mode it was sent under. The key is always present; when no
attributes are available it defaults to all `false`.

## Inbound (received by this service)

| File | Type | Notes |
|------|------|-------|
| `accept.json` | `Accept` | HAL accepting an offer; marks software verified by author. |
| `reject.json` | `Reject` | HAL rejecting an offer. |
