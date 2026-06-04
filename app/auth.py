import os
import secrets
from functools import wraps

from flask import request, jsonify

def _is_authorized(provided_key):
    """Return True only if `provided_key` is a valid API token.

    The expected token is the `API_TOKEN` environment variable, read on every
    call rather than captured at import time. Late binding keeps auth correct
    regardless of whether this module is imported before or after load_dotenv(),
    and lets the token be rotated in the environment without re-importing.

    Fail closed: an unconfigured server (no/empty API_TOKEN) rejects everything,
    and a request without an x-api-key header (provided_key is None) is rejected
    before any comparison.
    """
    expected = os.environ.get("API_TOKEN")
    if not expected or not provided_key:
        return False
    # Constant-time comparison to avoid leaking the token via timing differences.
    return secrets.compare_digest(provided_key, expected)


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authorized(request.headers.get("x-api-key")):
            return jsonify({"error": "Unauthorized Token"}), 401
        return f(*args, **kwargs)
    return decorated
