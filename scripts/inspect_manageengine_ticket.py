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


def nested_name(value: object) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    return None


def main() -> None:
    base_url = required_env("MANAGEENGINE_BASE_URL").rstrip("/")
    token = required_env("MANAGEENGINE_API_TOKEN")
    ticket_id = required_env("MANAGEENGINE_TEST_TICKET_ID")
    auth_header = os.getenv(
        "MANAGEENGINE_AUTH_HEADER",
        "authtoken",
    ).strip()

    url = f"{base_url}/api/v3/requests/{ticket_id}"

    try:
        response = httpx.get(
            url,
            headers={
                auth_header: token,
                "Accept": "application/vnd.manageengine.sdp.v3+json",
            },
            timeout=30.0,
        )
        response.raise_for_status()

        data = response.json()
        ticket = data.get("request", {})

        print("ManageEngine ticket read: SUCCESS")
        print(f"Ticket ID : {ticket.get('id')}")
        print(f"Subject   : {ticket.get('subject')}")
        print(f"Status    : {nested_name(ticket.get('status'))}")
        print(f"Group     : {nested_name(ticket.get('group'))}")
        print(f"Template  : {nested_name(ticket.get('template'))}")

        description = ticket.get("description") or ""
        print(f"Description available: {bool(description)}")
        print(f"Description length   : {len(description)} characters")

        attachments = ticket.get("attachments") or []
        print(f"Attachments in response: {len(attachments)}")

        print("\nTop-level ticket fields:")
        for field_name in sorted(ticket.keys()):
            print(f"- {field_name}")

        print("\nREAD ONLY: No ManageEngine data was changed.")

    except httpx.HTTPStatusError as exc:
        print("ManageEngine ticket read: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            print(f"Response: {exc.response.json()}")
        except ValueError:
            print(exc.response.text[:300])

        sys.exit(1)

    except httpx.RequestError as exc:
        print("ManageEngine ticket read: FAILED")
        print(f"Network error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()