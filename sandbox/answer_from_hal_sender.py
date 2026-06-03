import requests
import json

# Load JSON from a file
#with open("docs/notif_test/reject.json", "r", encoding="utf-8") as f:
with open("docs/notif_test/accept.json", "r", encoding="utf-8") as f:
    payload = json.load(f)

url = "https://inbox-preprod.archives-ouvertes.fr"
headers = {"Content-Type": "application/ld+json"}

response = requests.post(url, headers=headers, json=payload)
print(response.status_code)

