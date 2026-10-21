import os
import sys

import httpx
from dotenv import load_dotenv


load_dotenv()


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"Missing environment variable: {name}")
        sys.exit(1)
    return value


def main() -> None:
    login_url = required_env("SALESFORCE_LOGIN_URL").rstrip("/")
    client_id = required_env("SALESFORCE_CLIENT_ID")
    client_secret = required_env("SALESFORCE_CLIENT_SECRET")
    api_version = os.getenv("SALESFORCE_API_VERSION", "v65.0")

    token_response = httpx.post(
        f"{login_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30.0,
    )
    token_response.raise_for_status()

    token_data = token_response.json()
    access_token = token_data["access_token"]
    instance_url = token_data["instance_url"].rstrip("/")

    response = httpx.get(
        f"{instance_url}/services/data/{api_version}/sobjects/Task/describe",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=30.0,
    )
    response.raise_for_status()

    keywords = (
        "finance",
        "assigned",
        "assignment",
        "team",
        "group",
        "department",
        "queue",
        "process",
        "owner",
        "status",
        "closed",
        "reject",
    )

    fields = response.json().get("fields", [])

    print(f"Total Task fields: {len(fields)}")
    print("=" * 100)

    for field in fields:
        label = field.get("label", "")
        api_name = field.get("name", "")
        combined = f"{label} {api_name}".lower()

        if any(keyword in combined for keyword in keywords):
            print(f"Label      : {label}")
            print(f"API Name   : {api_name}")
            print(f"Type       : {field.get('type')}")
            print(f"Updateable : {field.get('updateable')}")
            print(f"Custom     : {field.get('custom')}")

            values = [
                item.get("value")
                for item in field.get("picklistValues", [])
                if item.get("active")
            ]

            if values:
                print(f"Values     : {values}")

            print("-" * 100)

    print("READ ONLY: No Salesforce record was changed.")


if __name__ == "__main__":
    main()