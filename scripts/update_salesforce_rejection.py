import os
import sys

import httpx
from dotenv import load_dotenv


load_dotenv()


EXPECTED_CASE = "23422772"
EXPECTED_TICKET = "559923"


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"Missing environment variable: {name}")
        sys.exit(1)
    return value


def authenticate() -> tuple[str, str]:
    login_url = required_env("SALESFORCE_LOGIN_URL").rstrip("/")

    response = httpx.post(
        f"{login_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": required_env("SALESFORCE_CLIENT_ID"),
            "client_secret": required_env("SALESFORCE_CLIENT_SECRET"),
        },
        timeout=30.0,
    )
    response.raise_for_status()

    data = response.json()
    return data["access_token"], data["instance_url"].rstrip("/")


def main() -> None:
    case_number = required_env("SALESFORCE_TEST_CASE_NUMBER")
    ticket_number = required_env("MANAGEENGINE_TEST_TICKET_ID")
    api_version = os.getenv("SALESFORCE_API_VERSION", "v65.0")

    writes_enabled = (
        os.getenv("ENABLE_SALESFORCE_WRITES", "false").lower() == "true"
    )

    if not writes_enabled:
        print("BLOCKED: ENABLE_SALESFORCE_WRITES is not true.")
        sys.exit(1)

    # Temporary protection for this one controlled test.
    if case_number != EXPECTED_CASE or ticket_number != EXPECTED_TICKET:
        print("BLOCKED: Case or ticket does not match the approved UAT test.")
        sys.exit(1)

    token, instance_url = authenticate()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Step 1: Read the Case immediately before update.
    case_soql = (
        "SELECT Id, CaseNumber, Status, SR_Type__c, "
        "Internal_Comments__c, LastModifiedDate "
        f"FROM Case WHERE CaseNumber = '{case_number}' LIMIT 1"
    )

    case_response = httpx.get(
        f"{instance_url}/services/data/{api_version}/query",
        headers=headers,
        params={"q": case_soql},
        timeout=30.0,
    )
    case_response.raise_for_status()

    records = case_response.json().get("records", [])

    if not records:
        print("BLOCKED: Salesforce Case not found.")
        sys.exit(1)

    case = records[0]
    case_id = case["Id"]

    print("BEFORE UPDATE")
    print(f"Case Number : {case.get('CaseNumber')}")
    print(f"Status      : {case.get('Status')}")
    print(f"SR Type     : {case.get('SR_Type__c')}")
    print(f"Comment     : {case.get('Internal_Comments__c')}")

    # Step 2: Validate Case eligibility.
    if case.get("Status") == "Rejected":
        print("NO ACTION: Case is already Rejected.")
        sys.exit(0)

    if case.get("Status") != "Submitted":
        print("BLOCKED: Current status is not Submitted.")
        sys.exit(1)

    if case.get("SR_Type__c") != "Proof of Payment SR":
        print("BLOCKED: Case is not Proof of Payment SR.")
        sys.exit(1)

    # Step 3: Validate a completed rejected activity exists.
    task_soql = (
        "SELECT Id, Subject, Status, IsClosed "
        f"FROM Task WHERE WhatId = '{case_id}' "
        "AND Status = 'Rejected' "
        "AND IsClosed = true "
        "ORDER BY LastModifiedDate DESC LIMIT 1"
    )

    task_response = httpx.get(
        f"{instance_url}/services/data/{api_version}/query",
        headers=headers,
        params={"q": task_soql},
        timeout=30.0,
    )
    task_response.raise_for_status()

    tasks = task_response.json().get("records", [])

    if not tasks:
        print("BLOCKED: No completed Rejected activity was found.")
        sys.exit(1)

    task = tasks[0]

    print()
    print("VALIDATION")
    print(f"Activity    : {task.get('Subject')}")
    print(f"Task Status : {task.get('Status')}")
    print(f"Task Closed : {task.get('IsClosed')}")
    print("Eligibility : PASSED")

    new_comment = f"Rejecting as per IT ticket #{ticket_number}."

    # Step 4: Update exactly two fields.
    update_response = httpx.patch(
        (
            f"{instance_url}/services/data/{api_version}"
            f"/sobjects/Case/{case_id}"
        ),
        headers=headers,
        json={
            "Status": "Rejected",
            "Internal_Comments__c": new_comment,
        },
        timeout=30.0,
    )

    if update_response.status_code != 204:
        print("Salesforce update: FAILED")
        print(f"HTTP status: {update_response.status_code}")

        try:
            print(update_response.json())
        except ValueError:
            print(update_response.text[:500])

        sys.exit(1)

    # Step 5: Re-read and verify.
    verify_response = httpx.get(
        (
            f"{instance_url}/services/data/{api_version}"
            f"/sobjects/Case/{case_id}"
        ),
        headers=headers,
        params={
            "fields": (
                "CaseNumber,Status,SR_Type__c,"
                "Internal_Comments__c,LastModifiedDate"
            )
        },
        timeout=30.0,
    )
    verify_response.raise_for_status()

    updated = verify_response.json()

    status_correct = updated.get("Status") == "Rejected"
    comment_correct = updated.get("Internal_Comments__c") == new_comment

    print()
    print("AFTER UPDATE")
    print(f"Case Number : {updated.get('CaseNumber')}")
    print(f"Status      : {updated.get('Status')}")
    print(f"Comment     : {updated.get('Internal_Comments__c')}")
    print(f"Verified    : {status_correct and comment_correct}")

    if not status_correct or not comment_correct:
        print("WARNING: Update response succeeded, but verification failed.")
        sys.exit(1)

    print()
    print("Salesforce rejection update: SUCCESS")
    print("ManageEngine has not been changed yet.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        print("Salesforce operation: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            print(exc.response.json())
        except ValueError:
            print(exc.response.text[:500])

        sys.exit(1)

    except httpx.RequestError as exc:
        print("Salesforce operation: FAILED")
        print(f"Network error: {exc}")
        sys.exit(1)