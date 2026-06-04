import os
import secrets
from functools import wraps

from flask import request, jsonify

# The single API token clients must present in the `x-api-key` header.
# Read from the environment (see .env / .env.example). python-dotenv has
# already populated os.environ by the time the route modules import this,
# because app.py calls load_dotenv() before importing the routes.
API_TOKEN = os.environ.get("API_TOKEN")


def _is_authorized(provided_key):
    """Return True only if `provided_key` is a valid API token.

    SECURITY DECISION (please implement — see the README "Authentication" section):

    `API_TOKEN` may be unset (None) if the environment isn't configured, and a
    request with no `x-api-key` header also gives `provided_key is None`. A naive
    `provided_key == API_TOKEN` check would then compare `None == None` and WRONGLY
    grant access — i.e. an unconfigured server would be wide open (fail-open).

    Implement a fail-closed check (≈3-5 lines):
      - reject every request when API_TOKEN is unset/empty, and
      - reject when provided_key is missing or doesn't match API_TOKEN.
    """
    # Fail closed: an unconfigured server (no/empty API_TOKEN) rejects everything,
    # and a request without an x-api-key header (provided_key is None) is rejected
    # before any comparison.
    if not API_TOKEN or not provided_key:
        return False
    # Constant-time comparison to avoid leaking the token via timing differences.
    return secrets.compare_digest(provided_key, API_TOKEN)


def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _is_authorized(request.headers.get("x-api-key")):
            return jsonify({"error": "Unauthorized Token"}), 401
        return f(*args, **kwargs)
    return decorated
