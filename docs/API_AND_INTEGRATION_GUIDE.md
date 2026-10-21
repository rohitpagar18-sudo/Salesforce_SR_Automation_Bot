# API AND INTEGRATION GUIDE

This document lists every integration found in the source code. No assumptions are made beyond what is visible in the repository.

## 1) Integration map

```mermaid
flowchart LR
    A[ManageEngine Ticket] --> B[ManageEngine API]
    B --> C[parse_ticket()]
    C --> D[Salesforce API]
    D --> E[Salesforce update]
    E --> F[ManageEngine note write]
    F --> G[ManageEngine ticket close]
```

### Source of each integration

- ManageEngine API client: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L6-L108)
- Salesforce API client: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L111-L149)
- ManageEngine note writer: [src/sr_automation/manageengine/note_writer.py](../src/sr_automation/manageengine/note_writer.py#L5-L58)
- Main runtime orchestration: [src/sr_automation/main.py](../src/sr_automation/main.py#L963-L1840)

---

## 2) ManageEngine APIs

### 2.1 List open tickets

- Class / Function: `ManageEngineClient.list_open_tickets()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L15-L77)
- Endpoint: `GET {MANAGEENGINE_BASE_URL}/api/v3/requests`
- Method: `GET`
- Request payload: `input_data` JSON containing `list_info` with `row_count`, `start_index`, `sort_field`, `sort_order`, `get_total_count`, and filter criteria for group + status.
- Authentication: `headers = {me_header: me_token, Accept: application/vnd.manageengine.sdp.v3+json}`
- Response shape: JSON object with `requests` and `list_info`
- Retry logic: none explicit; it simply calls `httpx.get(..., timeout=45.0)` and raises on error.
- Error handling: `response.raise_for_status()`

Example request logic:

```python
response = httpx.get(
    f"{self.s.me_url}/api/v3/requests",
    headers=self.headers,
    params={"input_data": json.dumps(input_data)},
    timeout=45.0,
)
response.raise_for_status()
```

### 2.2 Get a single ticket

- Class / Function: `ManageEngineClient.get_ticket()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L79-L80)
- Endpoint: `GET {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}`
- Method: `GET`
- Auth: same as above
- Response: JSON `request`
- Error handling: `r.raise_for_status()`

### 2.3 Get mandatory fields for a status transition

- Class / Function: `ManageEngineClient.get_mandatory_fields()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L82-L90)
- Endpoint: `GET {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}/_get_mandatory_fields`
- Method: `GET`
- Request payload: `{"input_data": json.dumps({"state_id": str(target_status_id)})}`
- Response: JSON object with `mandatory_fields`
- Error handling: `r.raise_for_status()`

### 2.4 Ticket assignment

- Class / Function: `ManageEngineClient.assign_technician()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L91-L95)
- Endpoint: `PUT {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}/assign`
- Method: `PUT`
- Payload: `{"input_data": json.dumps({"request": {"technician": technician}})}`
- Used by Stage 4 or technician-assignment logic in the runtime flow.

### 2.5 Add resolution

- Class / Function: `ManageEngineClient.add_resolution()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L96-L99)
- Endpoint: `POST {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}/resolutions`
- Method: `POST`
- Payload: `{"input_data": json.dumps({"resolution": resolution})}`

### 2.6 Close a ticket

- Class / Function: `ManageEngineClient.close_ticket()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L101-L109)
- Endpoint: `PUT {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}/close`
- Method: `PUT`
- Payload: `{"input_data": json.dumps(closure_info)}`
- Error handling: response returned to caller; no custom retry wrapper is implemented.

### 2.7 Update request fields

- Class / Function: `ManageEngineClient.update_request()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L106-L109)
- Endpoint: `PUT {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}`
- Method: `PUT`
- Payload: generic `input_data` dict

### 2.8 Note reads and writes

- Class / Function: `ManageEngineNoteWriter.post_note()`
- File: [src/sr_automation/manageengine/note_writer.py](../src/sr_automation/manageengine/note_writer.py#L14-L58)
- Endpoint: `POST {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}/notes`
- Method: `POST`
- Payload: `{"note": {"description": ..., "show_to_requester": False, ...}}`
- Response: JSON with note data; if `response.is_error`, it prints details and raises.

- Class / Function: `ManageEngineNoteWriter.list_notes()`
- File: [src/sr_automation/manageengine/note_writer.py](../src/sr_automation/manageengine/note_writer.py#L44-L48)
- Endpoint: `GET {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}/notes`
- Method: `GET`
- Response: `data.get("notes") or data.get("note") or []`

- Class / Function: `ManageEngineNoteWriter.get_note()`
- File: [src/sr_automation/manageengine/note_writer.py](../src/sr_automation/manageengine/note_writer.py#L49-L58)
- Endpoint: `GET {MANAGEENGINE_BASE_URL}/api/v3/requests/{ticket_id}/notes/{note_id}`
- Method: `GET`
- Response: note object or `None`

### 2.9 ManageEngine retry and error handling

The code does not contain a general retry loop or backoff strategy. The main behavior is:

- raise on HTTP status via `raise_for_status()`
- catch some HTTP exceptions in the runtime to reconcile or verify final state
- use re-read verification after timeouts on Salesforce update attempts

This is visible in [src/sr_automation/main.py](../src/sr_automation/main.py#L1336-L1392) and [src/sr_automation/manageengine/note_writer.py](../src/sr_automation/manageengine/note_writer.py#L16-L58).

---

## 3) Salesforce APIs

### 3.1 OAuth token

- Class / Function: `SalesforceClient.authenticate()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L111-L120)
- Endpoint: `POST {SALESFORCE_LOGIN_URL}/services/oauth2/token`
- Method: `POST`
- Request payload: `grant_type=client_credentials`, `client_id`, `client_secret`
- Response: JSON with `access_token` and `instance_url`
- Auth: OAuth 2.0 client credentials
- Error handling: `r.raise_for_status()`

### 3.2 Query records

- Class / Function: `SalesforceClient.query()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L118-L120)
- Endpoint: `GET {instance}/services/data/{SALESFORCE_API_VERSION}/query`
- Method: `GET`
- Payload: SOQL via querystring parameter `q`
- Response: `r.json().get("records", [])`
- Auth: Bearer token header

### 3.3 Case lookup

- Class / Function: `SalesforceClient.cases()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L121-L130)
- SOQL:

```sql
SELECT Id,CaseNumber,Status,RecordType.Name,SR_Type__c,LastModifiedDate
FROM Case WHERE CaseNumber IN (...)
```

- Response: dict keyed by `CaseNumber`
- Next function: `decide()` and `main.run_cycle()`

### 3.4 Task lookup

- Class / Function: `SalesforceClient.tasks()` and `.refresh_tasks_for_case()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L131-L149)
- SOQL:

```sql
SELECT Id,WhatId,Subject,Status,IsClosed,Owner.Name,LastModifiedDate
FROM Task WHERE WhatId IN (...) ORDER BY LastModifiedDate DESC
```

- Response: task records grouped by `WhatId`
- This is used to detect open tasks and rejection evidence.

### 3.5 Update case status

- Class / Function: `SalesforceClient.update_case()`
- File: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L139-L149)
- Endpoint: `PATCH {instance}/services/data/{version}/sobjects/Case/{case_id}`
- Method: `PATCH`
- Request payload: JSON body like `{"Status": "Cancelled"}` and optionally comment field
- Response: raw HTTP response object
- Verification: `run_cycle()` re-reads the case and checks the post-update status before recording `SUCCESS`

### 3.6 Salesforce error handling

Source: [src/sr_automation/main.py](../src/sr_automation/main.py#L290-L359)

The code extracts Salesforce error details with `_extract_salesforce_error_details()`, sanitizes payloads, and logs error fields. No centralized retry policy is present here.

---

## 4) Other APIs

No other external API clients are found in source. The code uses:

- ManageEngine HTTP API
- Salesforce HTTP API
- Local SQLite/Audit DB access via the audit repository

The phrase “NOT FOUND IN SOURCE CODE” applies to any other third-party API beyond these two external systems.

---

## 5) Environment variables used by integrations

These are read in [src/sr_automation/settings.py](../src/sr_automation/settings.py#L59-L171):

- `MANAGEENGINE_BASE_URL`
- `MANAGEENGINE_API_TOKEN`
- `MANAGEENGINE_AUTH_HEADER`
- `SALESFORCE_LOGIN_URL`
- `SALESFORCE_CLIENT_ID`
- `SALESFORCE_CLIENT_SECRET`
- `SALESFORCE_API_VERSION`
- `DRY_RUN`
- `ENABLE_SALESFORCE_WRITES`
- `ENABLE_MANAGEENGINE_WRITES`
- `MANAGEENGINE_AUTO_CLOSE`
- `AUDIT_DB_URL`

The security-sensitive values are redacted in some logging logic. Source: [src/sr_automation/main.py](../src/sr_automation/main.py#L161-L236).

---

## 6) Dependency map

```mermaid
flowchart TD
    A[run_cycle()] -->|fetch open tickets| B[ManageEngineClient.list_open_tickets()]
    B -->|JSON requests| C[GET /api/v3/requests]
    C -->|ticket list| A
    A -->|fetch one ticket| D[ManageEngineClient.get_ticket()]
    D -->|JSON request| E[GET /api/v3/requests/{id}]
    E -->|ticket data| A
    A -->|parse intent| F[parse_ticket()]
    A -->|lookup cases| G[SalesforceClient.cases()]
    G -->|SOQL query| H[GET /query]
    H -->|Case records| A
    A -->|check tasks| I[SalesforceClient.tasks()]
    I -->|SOQL query| J[GET /query]
    J -->|Task records| A
    A -->|patch status| K[SalesforceClient.update_case()]
    K -->|PATCH /sobjects/Case/{id}| L[Salesforce result]
    L -->|verify rerun| A
    A -->|write summary note| M[ManageEngineNoteWriter.post_note()]
    M -->|POST /notes| N[ManageEngine note result]
    N -->|optional closure| O[ManageEngineClient.close_ticket()]
    O -->|PUT /close| P[ManageEngine close result]
```

---

## 7) API call trace in runtime flow

The runtime calls appear in the main loop here:

- Ticket list: [src/sr_automation/main.py](../src/sr_automation/main.py#L976-L998)
- Ticket fetch: [src/sr_automation/main.py](../src/sr_automation/main.py#L1004-L1031)
- Salesforce lookup: [src/sr_automation/main.py](../src/sr_automation/main.py#L1094-L1103)
- Salesforce patch and verification: [src/sr_automation/main.py](../src/sr_automation/main.py#L1320-L1475)
- ManageEngine summary note: [src/sr_automation/main.py](../src/sr_automation/main.py#L1480-L1540)
- ManageEngine close logic: [src/sr_automation/main.py](../src/sr_automation/main.py#L1540-L1840)

---

## 8) Practical conclusion

This project uses two live external APIs only:

1. ManageEngine HTTP API for ticket discovery, ticket fetch, summary notes, and closure.
2. Salesforce HTTP API for OAuth, SOQL lookup, and case status updates.

There is no evidence of any broader integration layer or service bus in the source. The rest of the logic is local Python rule evaluation plus a SQLite-like audit repository.
