import json
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
    base_url = required_env("MANAGEENGINE_BASE_URL").rstrip("/")
    api_token = required_env("MANAGEENGINE_API_TOKEN")
    auth_header = os.getenv(
        "MANAGEENGINE_AUTH_HEADER",
        "authtoken",
    ).strip()

    url = f"{base_url}/api/v3/requests"

    input_data = {
        "list_info": {
            "row_count": 1,
            "start_index": 1,
            "sort_field": "created_time",
            "sort_order": "desc",
        }
    }

    headers = {
        auth_header: api_token,
        "Accept": "application/vnd.manageengine.sdp.v3+json",
    }

    try:
        response = httpx.get(
            url,
            headers=headers,
            params={"input_data": json.dumps(input_data)},
            timeout=30.0,
            follow_redirects=False,
        )

        response.raise_for_status()
        data = response.json()

        requests = data.get("requests", [])

        print("ManageEngine connection: SUCCESS")
        print(f"HTTP status: {response.status_code}")
        print(f"Accessible tickets returned: {len(requests)}")

        if requests:
            ticket = requests[0]

            print(f"Ticket ID: {ticket.get('id')}")
            print(f"Subject: {ticket.get('subject')}")

            status = ticket.get("status") or {}
            group = ticket.get("group") or {}
            template = ticket.get("template") or {}

            print(f"Status: {status.get('name')}")
            print(f"Group: {group.get('name')}")
            print(f"Template: {template.get('name')}")

        print("READ ONLY: No ManageEngine ticket was changed.")

    except httpx.HTTPStatusError as exc:
        print("ManageEngine connection: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            print(f"Response: {exc.response.json()}")
        except ValueError:
            print("Non-JSON response received.")
            print(exc.response.text[:500])

        sys.exit(1)

    except httpx.RequestError as exc:
        print("ManageEngine connection: FAILED")
        print(f"Network error: {exc}")
        sys.exit(1)

    except ValueError:
        print("ManageEngine connection: FAILED")
        print("The server did not return valid JSON.")
        sys.exit(1)


if __name__ == "__main__":
    main()