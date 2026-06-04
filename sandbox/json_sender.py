import argparse
import os
import requests

API_URL = "http://127.0.0.1:5500/insert"
JSON_DIR = "./app/static/data/json_files"


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
    parser = argparse.ArgumentParser(
        description="POST JSON files to the COAR Notify insert endpoint."
    )
    parser.add_argument(
        "--api-key", help="API token; falls back to the API_TOKEN environment variable."
    )
    args = parser.parse_args()
    headers = {"x-api-key": resolve_api_key(args.api_key)}

    json_files = [f for f in os.listdir(JSON_DIR) if f.endswith(".json")]
    for json_file in json_files:
        file_path = os.path.join(JSON_DIR, json_file)
        with open(file_path, "rb") as f:
            files = {"file": (json_file, f, "application/json")}
            try:
                response = requests.post(API_URL, files=files, headers=headers)
                print(f"Response: {response.status_code} - {response.json()}")
            except Exception as e:
                print(f"Error sending {json_file}: {e}")


if __name__ == "__main__":
    main()
