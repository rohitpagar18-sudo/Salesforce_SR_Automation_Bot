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
    api_version = os.getenv("SALESFORCE_API_VERSION", "v65.0")
    case_number = required_env("SALESFORCE_TEST_CASE_NUMBER")

    try:
        token, instance_url = authenticate()

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

        case_soql = (
            "SELECT Id, CaseNumber, Status, SR_Type__c, "
            "Rejection_Reason__c, Internal_Comments__c, "
            "LastModifiedDate "
            f"FROM Case WHERE CaseNumber = '{case_number}' LIMIT 1"
        )

        case_response = httpx.get(
            f"{instance_url}/services/data/{api_version}/query",
            headers=headers,
            params={"q": case_soql},
            timeout=30.0,
        )
        case_response.raise_for_status()

        cases = case_response.json().get("records", [])

        if not cases:
            print(f"Case {case_number}: NOT FOUND")
            sys.exit(0)

        case = cases[0]
        case_id = case["Id"]

        task_soql = (
            "SELECT Id, Subject, Status, IsClosed, "
            "Assigned_User__c, Process_Name__c, "
            "OwnerId, Owner.Name, CreatedDate, ActivityDate, "
            "LastModifiedDate "
            f"FROM Task WHERE WhatId = '{case_id}' "
            "ORDER BY LastModifiedDate DESC"
        )

        task_response = httpx.get(
            f"{instance_url}/services/data/{api_version}/query",
            headers=headers,
            params={"q": task_soql},
            timeout=30.0,
        )
        task_response.raise_for_status()

        tasks = task_response.json().get("records", [])

        print("Salesforce Case and Activity read: SUCCESS")
        print()
        print("CASE")
        print(f"Case Number      : {case.get('CaseNumber')}")
        print(f"Status           : {case.get('Status')}")
        print(f"SR Type          : {case.get('SR_Type__c')}")
        print(f"Rejection Reason : {case.get('Rejection_Reason__c')}")
        print(f"Internal Comment : {case.get('Internal_Comments__c')}")
        print(f"Activities Found : {len(tasks)}")

        if not tasks:
            print()
            print("No related Task activity was found.")

        for index, task in enumerate(tasks, start=1):
            owner = task.get("Owner") or {}

            print()
            print(f"ACTIVITY {index}")
            print(f"Subject          : {task.get('Subject')}")
            print(f"Status           : {task.get('Status')}")
            print(f"Is Closed        : {task.get('IsClosed')}")
            print(f"Assigned User    : {task.get('Assigned_User__c')}")
            print(f"Process Name     : {task.get('Process_Name__c')}")
            print(f"Record Owner     : {owner.get('Name')}")
            print(f"Activity Date    : {task.get('ActivityDate')}")
            print(f"Modified Date    : {task.get('LastModifiedDate')}")

        print()
        print("READ ONLY: No Salesforce records were changed.")

    except httpx.HTTPStatusError as exc:
        print("Salesforce Activity read: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            error_data = exc.response.json()
            print(f"Response: {error_data}")
        except ValueError:
            print(f"Response: {exc.response.text[:500]}")

        sys.exit(1)

    except (httpx.RequestError, KeyError, ValueError) as exc:
        print("Salesforce Activity read: FAILED")
        print(f"Error: {type(exc).__name__}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()