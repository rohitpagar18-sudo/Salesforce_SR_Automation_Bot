import json
import os
import sys

import httpx
from dotenv import load_dotenv


load_dotenv()


APPROVED_TICKET = "559923"
APPROVED_CASE = "23422772"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()

    if not value:
        print(f"Missing environment variable: {name}")
        sys.exit(1)

    return value


def nested_name(value: object) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name is not None else None

    return None


def main() -> None:
    base_url = required_env("MANAGEENGINE_BASE_URL").rstrip("/")
    api_token = required_env("MANAGEENGINE_API_TOKEN")
    auth_header = os.getenv(
        "MANAGEENGINE_AUTH_HEADER",
        "authtoken",
    ).strip()

    ticket_id = required_env("MANAGEENGINE_TEST_TICKET_ID")
    # Fixed values for this single approved UAT test only.
    case_number = "23422772"
    previous_status = "Submitted"
    new_status = "Rejected"

    writes_enabled = (
        os.getenv(
            "ENABLE_MANAGEENGINE_WRITES",
            "false",
        ).lower()
        == "true"
    )

    auto_close = (
        os.getenv(
            "MANAGEENGINE_AUTO_CLOSE",
            "false",
        ).lower()
        == "true"
    )

    if not writes_enabled:
        print(
            "BLOCKED: ENABLE_MANAGEENGINE_WRITES "
            "must be true."
        )
        sys.exit(1)

    if auto_close:
        print(
            "BLOCKED: MANAGEENGINE_AUTO_CLOSE must remain "
            "false during note testing."
        )
        sys.exit(1)

    # Temporary protection for this one UAT test.
    if ticket_id != APPROVED_TICKET:
        print("BLOCKED: Ticket is not the approved UAT ticket.")
        sys.exit(1)

    if case_number != APPROVED_CASE:
        print("BLOCKED: Case is not the approved Full Copy Case.")
        sys.exit(1)

    headers = {
        auth_header: api_token,
        "Accept": "application/vnd.manageengine.sdp.v3+json",
    }

    ticket_url = (
        f"{base_url}/api/v3/requests/{ticket_id}"
    )

    # Step 1: Re-read ticket before writing the note.
    ticket_response = httpx.get(
        ticket_url,
        headers=headers,
        timeout=30.0,
    )
    ticket_response.raise_for_status()

    ticket = ticket_response.json().get("request", {})

    ticket_status = nested_name(ticket.get("status"))
    ticket_group = nested_name(ticket.get("group"))
    ticket_template = nested_name(ticket.get("template"))

    print("MANAGEENGINE TICKET BEFORE NOTE")
    print(f"Ticket ID : {ticket.get('id')}")
    print(f"Subject   : {ticket.get('subject')}")
    print(f"Status    : {ticket_status}")
    print(f"Group     : {ticket_group}")
    print(f"Template  : {ticket_template}")

    if str(ticket.get("id")) != ticket_id:
        print("BLOCKED: Returned ticket ID does not match.")
        sys.exit(1)

    if ticket_status != "Open":
        print("BLOCKED: ManageEngine ticket is not Open.")
        sys.exit(1)

    if ticket_group != "SalesForce - CRM":
        print("BLOCKED: Ticket group is outside bot scope.")
        sys.exit(1)

    if ticket_template != "SalesForce CRM Issues":
        print("BLOCKED: Ticket template is outside bot scope.")
        sys.exit(1)

    note_description = (
        f"Automation result for ticket #{ticket_id}<br>"
        f"Environment: UAT<br>"
        f"Salesforce Case: {case_number}<br>"
        f"Action: REJECT<br>"
        f"Previous Status: {previous_status}<br>"
        f"New Status: {new_status}<br>"
        f"Result: SUCCESS<br>"
        f"Comment: Rejecting as per IT ticket #{ticket_id}.<br>"
        "Salesforce update was verified successfully."
    )

    input_data = {
        "note": {
            "description": note_description,
            "show_to_requester": False,
            "mark_first_response": False,
            "add_to_linked_requests": False,
        }
    }

    note_url = (
        f"{base_url}/api/v3/requests/{ticket_id}/notes"
    )

    note_response = httpx.post(
        note_url,
        headers={
            **headers,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "input_data": json.dumps(input_data),
        },
        timeout=30.0,
    )

    note_response.raise_for_status()
    result = note_response.json()

    response_status = result.get("response_status", {})
    note = result.get("note", {})

    api_status = response_status.get("status")
    status_code = (
        response_status.get("statuscode")
        or response_status.get("status_code")
    )

    print()
    print("MANAGEENGINE NOTE RESULT")
    print(f"API Status : {api_status}")
    print(f"Status Code: {status_code}")
    print(f"Note ID    : {note.get('id')}")
    print(
        "Visible to requester:",
        note.get("show_to_requester"),
    )

    if api_status != "success":
        print("ManageEngine note creation: FAILED")
        print(result)
        sys.exit(1)

    print()
    print("ManageEngine note creation: SUCCESS")
    print("Ticket was NOT closed.")
    print("Verify the private note manually in ManageEngine.")


if __name__ == "__main__":
    try:
        main()

    except httpx.HTTPStatusError as exc:
        print("ManageEngine operation: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            print(f"Response: {exc.response.json()}")
        except ValueError:
            print(exc.response.text[:500])

        sys.exit(1)

    except httpx.RequestError as exc:
        print("ManageEngine operation: FAILED")
        print(f"Network error: {exc}")
        sys.exit(1)

    except ValueError as exc:
        print("ManageEngine operation: FAILED")
        print(f"Invalid JSON response: {exc}")
        sys.exit(1)