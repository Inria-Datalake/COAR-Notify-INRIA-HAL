"""Small helpers shared by the API blueprints for parsing query parameters."""

from flask import request


def parse_bool_arg(name: str) -> bool | None:
    """Parse an optional tri-state boolean query param.

    Returns None when the param is absent or unrecognized (so the filter is
    skipped), True for true/1/yes/on, False for false/0/no/off.
    """
    raw = request.args.get(name)
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return None
