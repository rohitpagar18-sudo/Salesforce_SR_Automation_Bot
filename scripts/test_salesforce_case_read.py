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


def get_access_token() -> tuple[str, str]:
    login_url = required_env("SALESFORCE_LOGIN_URL").rstrip("/")
    client_id = required_env("SALESFORCE_CLIENT_ID")
    client_secret = required_env("SALESFORCE_CLIENT_SECRET")

    response = httpx.post(
        f"{login_url}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30.0,
    )

    response.raise_for_status()
    token_data = response.json()

    return (
        token_data["access_token"],
        token_data["instance_url"].rstrip("/"),
    )


def main() -> None:
    api_version = os.getenv("SALESFORCE_API_VERSION", "v65.0")
    case_number = required_env("SALESFORCE_TEST_CASE_NUMBER")

    try:
        access_token, instance_url = get_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        # Read-only SOQL query
        soql = (
            "SELECT Id, CaseNumber, Subject, Status, "
            "RecordTypeId, CreatedDate, LastModifiedDate "
            f"FROM Case WHERE CaseNumber = '{case_number}' LIMIT 1"
        )

        query_url = (
            f"{instance_url}/services/data/{api_version}/query"
            f"?q={quote(soql)}"
        )

        response = httpx.get(
            query_url,
            headers=headers,
            timeout=30.0,
        )

        response.raise_for_status()
        result = response.json()

        if result.get("totalSize", 0) == 0:
            print("Salesforce Case read: SUCCESS")
            print(f"Case {case_number}: NOT FOUND")
            sys.exit(0)

        case = result["records"][0]

        print("Salesforce Case read: SUCCESS")
        print(f"Case Number: {case.get('CaseNumber')}")
        print(f"Salesforce ID: {case.get('Id')}")
        print(f"Subject: {case.get('Subject')}")
        print(f"Status: {case.get('Status')}")
        print(f"Record Type ID: {case.get('RecordTypeId')}")
        print(f"Created Date: {case.get('CreatedDate')}")
        print(f"Last Modified Date: {case.get('LastModifiedDate')}")
        print()
        print("READ ONLY: No Salesforce record was changed.")

    except httpx.HTTPStatusError as exc:
        print("Salesforce Case read: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            error_data = exc.response.json()

            for error in error_data if isinstance(error_data, list) else [error_data]:
                print(f"Error code: {error.get('errorCode', 'Unknown')}")
                print(f"Message: {error.get('message', 'Unknown error')}")

        except ValueError:
            print("Salesforce returned a non-JSON error.")

        sys.exit(1)

    except (httpx.RequestError, KeyError, ValueError) as exc:
        print("Salesforce Case read: FAILED")
        print(f"Error type: {type(exc).__name__}")
        print(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()