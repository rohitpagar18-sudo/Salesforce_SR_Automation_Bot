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
    client_id = required_env("SALESFORCE_CLIENT_ID")
    client_secret = required_env("SALESFORCE_CLIENT_SECRET")
    api_version = os.getenv("SALESFORCE_API_VERSION", "v65.0")

    try:
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

        soql = """
            SELECT Id, CaseNumber, Subject, Status, CreatedDate
            FROM Case
            ORDER BY CreatedDate DESC
            LIMIT 10
        """

        query_url = (
            f"{instance_url}/services/data/{api_version}/query"
            f"?q={quote(soql)}"
        )

        response = httpx.get(
            query_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        response.raise_for_status()

        result = response.json()
        records = result.get("records", [])

        print(f"Accessible recent Cases: {len(records)}")
        print("-" * 80)

        for case in records:
            print(f"Case Number : {case.get('CaseNumber')}")
            print(f"Subject     : {case.get('Subject')}")
            print(f"Status      : {case.get('Status')}")
            print(f"Created Date: {case.get('CreatedDate')}")
            print("-" * 80)

        print("READ ONLY: No Salesforce records were changed.")

    except httpx.HTTPStatusError as exc:
        print("Salesforce Case listing: FAILED")
        print(f"HTTP status: {exc.response.status_code}")

        try:
            print(f"Response: {exc.response.json()}")
        except ValueError:
            print("Salesforce returned a non-JSON error.")

        sys.exit(1)

    except (httpx.RequestError, KeyError, ValueError) as exc:
        print("Salesforce Case listing: FAILED")
        print(f"Error type: {type(exc).__name__}")
        print(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()