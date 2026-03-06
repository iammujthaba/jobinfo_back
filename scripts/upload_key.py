"""
Upload the public key to Meta via the Graph API.

Usage:
    python scripts/upload_key.py

Prerequisite: Run  python scripts/generate_keys.py  first.
"""
import os
import sys

import httpx

# ── Configuration ─────────────────────────────────────────────────────────────
# Reads from environment variables, or falls back to the defaults below.
ACCESS_TOKEN = os.environ.get(
    "WHATSAPP_TOKEN",
    "EAAUHbaiApjUBQ3FJUOvKhck6kXu7tV735zMgIGdqw4JPZCxq4zj8wcEJF0wOvGMNv76bSKpxm527zaDDNENWQqxzPpvCss49HVGpKBwrxV60FXdKdeZBcemnFOt2Aaz8dsonZAK191hwikfI1ttF182TYZCZCGi4EsmZCO0qdzJWybSdQLxEj32JpJbRVvFjVBlQZDZD",
)
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_ID", "588420227696339")
PUBLIC_KEY_PATH = os.path.join("keys", "flow_public.pem")


def main():
    if not os.path.exists(PUBLIC_KEY_PATH):
        print(f"❌ Public key not found at {PUBLIC_KEY_PATH}")
        print("   Run  python scripts/generate_keys.py  first.")
        sys.exit(1)

    print("Reading public key...")
    with open(PUBLIC_KEY_PATH, "r") as f:
        public_key = f.read()

    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/whatsapp_business_encryption"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    data = {"business_public_key": public_key}

    print(f"Uploading to Meta (Phone ID: {PHONE_NUMBER_ID})...")
    response = httpx.post(url, headers=headers, data=data)

    if response.status_code == 200:
        print("✅ Success! Public key uploaded to Meta.")
        print(response.json())
    else:
        print(f"❌ Error (HTTP {response.status_code}):")
        print(response.json())
        sys.exit(1)


if __name__ == "__main__":
    main()
