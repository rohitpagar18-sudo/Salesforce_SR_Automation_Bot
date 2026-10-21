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

    token_url = f"{login_url}/services/oauth2/token"

    try:
        token_response = httpx.post(
            token_url,
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

        print("OAuth authentication: SUCCESS")
        print(f"Instance URL: {instance_url}")
        print(f"Token type: {token_data.get('token_type', 'Unknown')}")

        versions_response = httpx.get(
            f"{instance_url}/services/data/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )

        versions_response.raise_for_status()
        versions = versions_response.json()

        available_versions = {
            item.get("version")
            for item in versions
            if isinstance(item, dict)
        }

        requested_version = api_version.removeprefix("v")

        print("Salesforce REST API connection: SUCCESS")
        print(f"Configured API version: {api_version}")
        print(
            "Configured version available:",
            requested_version in available_versions,
        )

    except httpx.HTTPStatusError as exc:
        print("Salesforce connection: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            print(f"Salesforce response: {exc.response.json()}")
        except ValueError:
            print("Salesforce returned a non-JSON error response.")

        sys.exit(1)

    except (httpx.RequestError, KeyError, ValueError) as exc:
        print("Salesforce connection: FAILED")
        print(f"Error type: {type(exc).__name__}")
        print(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()