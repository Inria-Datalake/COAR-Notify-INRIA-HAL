import argparse
import os
import requests

URL = "http://127.0.0.1:5500/status"


def resolve_api_key(cli_value):
    """Return the API key from --api-key or the API_TOKEN env var.

    Exits with a clear error if neither is provided.
    """
    key = cli_value or os.environ.get("API_TOKEN")
    if not key:
        raise SystemExit(
            "No API key provided. Pass --api-key <token> or set the API_TOKEN environment variable."
        )
    return key


def main():
    parser = argparse.ArgumentParser(description="Check the COAR Notify /status endpoint.")
    parser.add_argument(
        "--api-key", help="API token; falls back to the API_TOKEN environment variable."
    )
    args = parser.parse_args()

    response = requests.get(URL, headers={"x-api-key": resolve_api_key(args.api_key)})
    print(response.json())


if __name__ == "__main__":
    main()
