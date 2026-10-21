import os
import sys
from urllib.parse import quote

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
    case_number = required_env("SALESFORCE_TEST_CASE_NUMBER")
    ticket_number = "559923"
    api_version = os.getenv("SALESFORCE_API_VERSION", "v65.0")

    token_response = httpx.post(
        f"{login_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": required_env("SALESFORCE_CLIENT_ID"),
            "client_secret": required_env("SALESFORCE_CLIENT_SECRET"),
        },
        timeout=30.0,
    )
    token_response.raise_for_status()

    token_data = token_response.json()
    instance_url = token_data["instance_url"].rstrip("/")
    headers = {
        "Authorization": f"Bearer {token_data['access_token']}",
        "Accept": "application/json",
    }

    soql = (
        "SELECT Id, CaseNumber, Status, SR_Type__c, "
        "Internal_Comments__c, LastModifiedDate "
        f"FROM Case WHERE CaseNumber = '{case_number}' LIMIT 1"
    )

    response = httpx.get(
        f"{instance_url}/services/data/{api_version}/query"
        f"?q={quote(soql)}",
        headers=headers,
        timeout=30.0,
    )
    response.raise_for_status()

    records = response.json().get("records", [])

    if not records:
        print("Decision: CASE_NOT_FOUND")
        sys.exit(0)

    case = records[0]

    eligible = (
        case.get("Status") == "Submitted"
        and case.get("SR_Type__c") == "Proof of Payment SR"
    )

    print("REJECTION DRY RUN")
    print(f"ManageEngine Ticket : {ticket_number}")
    print(f"Salesforce Case     : {case.get('CaseNumber')}")
    print(f"Current Status      : {case.get('Status')}")
    print(f"SR Type             : {case.get('SR_Type__c')}")
    print(f"Eligible            : {eligible}")

    if eligible:
        print("Result              : WOULD_UPDATE")
        print("Planned Status      : Rejected")
        print(
            "Planned Comment     : "
            f"Rejecting as per IT ticket {ticket_number}"
        )
    else:
        print("Result              : SKIPPED")

    print("DRY RUN: No Salesforce record was changed.")


if __name__ == "__main__":
    main()