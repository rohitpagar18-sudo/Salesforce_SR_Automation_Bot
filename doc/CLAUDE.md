# CLAUDE.md

## Salesforce SR Cancellation, Rejection, and Closure Automation

### 1. Mission
Build a safe, auditable Python automation service that reads eligible ManageEngine ServiceDesk tickets, extracts Salesforce SR/case numbers, determines the requested action per SR, validates the current Salesforce state, performs only approved updates, and writes an execution summary back to ManageEngine.

The system must support bulk requests, inconsistent ticket formats, attachments, ambiguous language, partial success, retries, and human confirmation. It must never guess an action when evidence is insufficient.

### 2. Primary Design Principle
Use deterministic parsing and business rules first. Do not introduce an LLM in version 1.

Action evidence must be evaluated in this order:
1. Per-row or per-case value in a structured attachment/table, such as `Next Action`.
2. Per-case text in the ticket description/table.
3. Unambiguous instruction applying to all extracted SRs.
4. Subject line only as supporting evidence, never as the sole authority.
5. If evidence conflicts or remains ambiguous, request human confirmation.

A ticket subject such as `SR for Cancellation/Rejection` is ambiguous. A row value such as `Next Action: To be cancelled` is stronger evidence for that row, but the column is optional and cannot be assumed to exist.

### 3. Confirmed Business Context

#### Source system: ManageEngine ServiceDesk
Business users create tickets containing one or many Salesforce SR/case numbers. The data can appear in:
- Subject
- HTML or plain-text description
- Embedded tables
- Excel/CSV attachments
- Free-form remarks

Known ticket metadata useful for filtering:
- Group: `ITMS - SF CRM` OR UAT: `SalesForce - CRM`
- Template: `SalesForce CRM Issues`
- Ticket/work-order number
- Subject
- Description
- Attachments
- Requester and technician, where permitted

Do not hardcode these values. Keep them in environment-specific configuration.

#### Target system: Salesforce Full Copy for development
Salesforce contains the actual case/SR records and related activities/history. The screenshots show an example:
- Case Number: `23439267`
- SR Type / Record Type: `Proof of Payment SR` / `POP`
- Subject: `To be cancelled - Amount allocated`
- Status: `Submitted`
- Open Activities: one activity
- Activity History: a finance-related activity with status `Reject`
- Case History: prior subject and status changes

The item labels and exact API field/object names must be discovered through Salesforce metadata and confirmed with the Salesforce owner. UI labels are not reliable API names.

### 4. Supported Actions
Use the following internal enum only:

```text
CANCEL
REJECT
CLOSE
UNKNOWN
CONFLICT
```

#### CANCEL
Candidate phrases include `cancel`, `cancelled`, `cancellation`, and `to be cancelled`.

Before update:
- Case exists.
- Case is not already cancelled or terminal in an incompatible status.
- Current status is in the configured cancellation-eligible status set.
- Any record-type-specific conditions pass.
- No conflicting action is assigned to the same case.

Update only fields approved in the field mapping, expected initially to include:
- Status = configured cancelled value
- Configured cancellation/reason/audit field = `Cancelled as per IT Ticket #<ticket_number>`

#### REJECT
Candidate phrases include `reject`, `rejected`, `rejection`, and `to be rejected`.

Before update:
- Case exists.
- Current status is eligible for rejection.
- Required finance rejection evidence exists in the configured related activity object/fields.
- Evidence matching must use normalized, configurable values, not only a brittle substring.
- No conflicting action is assigned to the same case.

Expected update:
- Status = configured rejected value
- Configured rejection reason/audit field = `Rejected as per IT Ticket #<ticket_number>`

Do not assume `Activity History` can be queried as a literal object with that name. Identify whether the source is Task, Event, a custom object, process history, or another relation.

#### CLOSE
Candidate phrases include `close`, `closed`, `closure`, and `to be closed`.

Before update:
- Case exists.
- Current status is eligible for closure.
- Every activity in the defined activity scope is closed/completed.
- The business owner has confirmed what counts as an activity and which statuses are terminal.

Expected update:
- Status = configured closed value
- Do not write an additional field unless the business owner confirms one.

If any applicable activity is open, skip the case and return `OPEN_ACTIVITIES`.

### 5. Human-in-the-Loop Requirement
When intent is uncertain, automation must stop before Salesforce mutation and create a confirmation request.

Preferred confirmation flow:
1. Add a private/public ManageEngine note with extracted SRs and the ambiguity.
2. If email interaction is available, send the requester three explicit choices: Cancel, Reject, Close.
3. Store a correlation token and ticket ID.
4. Accept only a controlled response, not unrestricted text when avoidable.
5. Re-fetch the ticket and Salesforce records before execution because state may have changed.
6. Record confirmer identity, timestamp, selected action, and evidence source.

### UAT Handoff Note

- The developer running this repository does not have access to the `helpdeskuat` environment. The code will be zipped and shared with an on-site UAT teammate in Dubai who has network access. That teammate will run Stage 2 (guarded writes) under supervision.
- After the UAT teammate runs Stage 2 and shares the outputs, the developer will review results and iterate until Stage 2 passes all checks. Only then will the team proceed to further stages.
- Dry-run has been tested successfully on a teammate's laptop. For UAT Stage 2, set the following environment and YAML values (do not enable these automatically in code):
    - `DRY_RUN=false`
    - `ENABLE_SALESFORCE_WRITES=true`
    - `ENABLE_MANAGEENGINE_WRITES=false`
    - `MANAGEENGINE_AUTO_CLOSE=false`
    - In `config/uat.yaml` set `rule_version: "0.2.0"` and `stage2.max_salesforce_updates_per_run: 1`.

These instructions will be used to coordinate the UAT handoff and the controlled Stage 2 run.

If a single ticket contains different actions per SR, confirmation and execution must operate per SR, not only per ticket.

### 6. Non-Negotiable Safety Rules
- Default to dry-run mode.
- Never update Salesforce from subject-only inference.
- Never update an ambiguous or conflicting SR.
- Never silently ignore an SR found in the ticket.
- Never store credentials, access tokens, URLs with secrets, or real customer data in Git.
- Never log attachment contents or personal/customer fields unless required and approved.
- Never retry a non-idempotent mutation blindly.
- Never mark a ManageEngine ticket completed when any line is pending manual review unless the configured process explicitly allows partial completion.
- Always re-query the Salesforce case immediately before update.
- Always use an idempotency key based on ticket, SR, action, and business-rule version.
- Bulk processing must allow partial success and produce one result per SR.
- Production writes require an explicit environment flag and separate production credentials.

### 7. Target Architecture

```text
ManageEngine poller/webhook
        |
        v
Ticket ingestion and normalization
        |
        +--> attachment downloader/parser
        |
        v
SR extraction and evidence collection
        |
        v
Per-SR intent resolver
        |
        +--> UNKNOWN/CONFLICT --> human confirmation queue
        |
        v
Salesforce case/activity loader
        |
        v
Business rule validator
        |
        +--> ineligible --> skip/manual review
        |
        v
Execution planner (dry-run result)
        |
        v
Idempotency check and Salesforce updater
        |
        v
Audit store + ManageEngine summary note
```

Separate pure decision logic from API clients. Parsing and validation must be testable without network access.

### 8. VS Code Repository Structure

```text
salesforce-sr-automation/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .gitignore
├── .env.example
├── .pre-commit-config.yaml
├── Makefile
├── config/
│   ├── base.yaml
│   ├── fullcopy.yaml
│   ├── uat.yaml
│   └── production.yaml.example
├── docs/
│   ├── business-rules.md
│   ├── field-mapping.md
│   ├── api-notes.md
│   ├── runbook.md
│   ├── security.md
│   ├── test-scenarios.md
│   └── decision-records/
├── samples/
│   ├── sanitized-tickets/
│   └── expected-results/
├── scripts/
│   ├── discover_salesforce_metadata.py
│   ├── inspect_manageengine_ticket.py
│   ├── run_dry_run.py
│   └── replay_fixture.py
├── src/sr_automation/
│   ├── __init__.py
│   ├── main.py
│   ├── cli.py
│   ├── settings.py
│   ├── logging_config.py
│   ├── domain/
│   │   ├── enums.py
│   │   ├── models.py
│   │   ├── errors.py
│   │   └── result_codes.py
│   ├── manageengine/
│   │   ├── client.py
│   │   ├── mapper.py
│   │   ├── ticket_reader.py
│   │   ├── attachment_reader.py
│   │   └── note_writer.py
│   ├── parsing/
│   │   ├── html_normalizer.py
│   │   ├── table_parser.py
│   │   ├── spreadsheet_parser.py
│   │   ├── sr_extractor.py
│   │   ├── evidence_extractor.py
│   │   └── intent_resolver.py
│   ├── salesforce/
│   │   ├── client.py
│   │   ├── metadata.py
│   │   ├── repositories.py
│   │   └── mapper.py
│   ├── rules/
│   │   ├── engine.py
│   │   ├── common.py
│   │   ├── cancellation.py
│   │   ├── rejection.py
│   │   └── closure.py
│   ├── orchestration/
│   │   ├── ticket_processor.py
│   │   ├── case_processor.py
│   │   ├── execution_planner.py
│   │   └── confirmation_service.py
│   ├── persistence/
│   │   ├── audit_repository.py
│   │   ├── idempotency_repository.py
│   │   └── models.py
│   └── observability/
│       ├── metrics.py
│       └── tracing.py
└── tests/
    ├── unit/
    ├── integration/
    ├── contract/
    ├── fixtures/
    └── conftest.py
```

### 9. Core Domain Models
Use typed models, preferably dataclasses or Pydantic models.

```python
class Ticket:
    ticket_id: str
    ticket_number: str
    subject: str
    description_html: str | None
    description_text: str
    group: str | None
    template: str | None
    attachments: list[Attachment]
    requester_id: str | None
    modified_at: datetime | None

class CaseRequest:
    sr_number: str
    requested_action: Action
    confidence: str
    evidence: list[Evidence]
    source_ticket_number: str
    row_reason: str | None
    requires_confirmation: bool

class SalesforceCaseSnapshot:
    sf_id: str
    case_number: str
    status: str
    record_type: str | None
    subject: str | None
    activities: list[ActivitySnapshot]
    last_modified_at: datetime

class ProcessingResult:
    ticket_number: str
    sr_number: str
    action: Action
    outcome: str
    reason_code: str
    message: str
    before_state: dict | None
    intended_changes: dict | None
    after_state: dict | None
    correlation_id: str
    rule_version: str
```

Evidence must contain source type, source location, normalized text/value, and precedence. Suggested source types:
- `ATTACHMENT_ROW_ACTION`
- `DESCRIPTION_TABLE_ACTION`
- `DESCRIPTION_CASE_TEXT`
- `DESCRIPTION_GLOBAL_TEXT`
- `SUBJECT_TEXT`
- `HUMAN_CONFIRMATION`

### 10. Intent Resolution Logic
Resolve intent independently for every SR.

```text
1. Parse all sources and extract unique SR numbers.
2. Associate nearby/row-level evidence with each SR.
3. Normalize explicit action phrases to CANCEL, REJECT, or CLOSE.
4. Rank evidence by source precedence.
5. If strongest evidence contains exactly one action, select it.
6. If equally authoritative evidence conflicts, return CONFLICT.
7. If only a mixed subject such as cancellation/rejection exists, return UNKNOWN.
8. If ticket-level instruction has one action and no row conflicts exist, apply it to all extracted SRs.
9. If no action can be resolved, request confirmation.
```

Do not initially use a numeric ML confidence score. Use explainable categories:
- `HIGH`: explicit row/per-SR action or human confirmation
- `MEDIUM`: single unambiguous description-level instruction applying to all SRs
- `LOW`: subject-only, weak language, contradictory context, or inferred action

Only HIGH and approved MEDIUM cases may proceed. LOW always requires confirmation.

### 11. SR Extraction
- Start with configurable regex for the observed SR format, initially likely an 8-digit number.
- Avoid treating ticket number, dates, amounts, phone numbers, or Salesforce IDs as SRs.
- Prefer values under headers such as `SR`, `SR Number`, or `Case Number`.
- Deduplicate while preserving original order.
- Record every extraction source and location.
- Validate candidates by querying Salesforce in batches.
- If business confirms that case numbers vary in length, update configuration and tests rather than broadening the regex silently.

Initial candidate regex:

```regex
(?<!\d)\d{8}(?!\d)
```

This is a candidate only. Validate against at least 30 to 50 historical tickets before adoption.

### 12. Salesforce Discovery Before Coding Mutations
Create a read-only discovery script that:
1. Authenticates to Full Copy using the approved OAuth flow.
2. Describes the Case object.
3. Finds API names for case number, status, record type, cancellation reason, rejection reason, audit comments, and relevant custom fields.
4. Identifies the underlying objects/relations shown as Open Activities, Activity History, Approval History, and Case History.
5. Retrieves valid status picklist values and record-type-specific values.
6. Tests read queries for one sanitized known case.
7. Produces `docs/field-mapping.md` without exposing sensitive records.

Do not build write logic until this mapping is reviewed.

Example mapping format:

```yaml
salesforce:
  case_object: Case
  fields:
    case_number: CaseNumber
    status: Status
    record_type_id: RecordTypeId
    cancellation_reason: TO_BE_CONFIRMED
    rejection_reason: TO_BE_CONFIRMED
  status_values:
    cancelled: TO_BE_CONFIRMED
    rejected: TO_BE_CONFIRMED
    closed: TO_BE_CONFIRMED
  eligible_statuses:
    cancel: []
    reject: []
    close: []
```

### 13. ManageEngine Discovery Before Polling
Create a read-only script that retrieves one UAT ticket and confirms:
- Correct API base URL and version for the deployed ManageEngine product
- Authentication method
- Ticket/work-order endpoint
- Pagination and filtering
- Description representation, including HTML
- Attachment metadata and download flow
- Notes/replies endpoint
- Available status/group/template identifiers
- Rate-limit and retry behavior

Save sanitized response shapes as contract-test fixtures. Do not commit real tokens, requester details, account names, or customer data.

### 14. Configuration and Secrets
Use environment variables for secrets and YAML/TOML for non-secret rules.

Suggested environment variables:

```dotenv
APP_ENV=fullcopy
DRY_RUN=true
LOG_LEVEL=INFO

MANAGEENGINE_BASE_URL=
MANAGEENGINE_API_TOKEN=
MANAGEENGINE_GROUP_ID=
MANAGEENGINE_TEMPLATE_ID=

SALESFORCE_LOGIN_URL=
SALESFORCE_CLIENT_ID=
SALESFORCE_CLIENT_SECRET=
SALESFORCE_USERNAME=
SALESFORCE_PRIVATE_KEY_PATH=

AUDIT_DB_URL=sqlite:///local_audit.db
ENABLE_SALESFORCE_WRITES=false
ENABLE_MANAGEENGINE_NOTES=false
```

Prefer an approved OAuth flow and secret manager. The exact Salesforce authentication method must follow organizational security requirements. Do not assume username/password authentication.

### 15. Processing Algorithm

```text
process_ticket(ticket_id):
  create correlation_id
  acquire ticket-level lock
  fetch current ManageEngine ticket
  verify group/template/status filters
  normalize description and download allowed attachments
  parse tables/spreadsheets and extract SR candidates

  if no SR candidates:
      write NO_SR_FOUND result
      route to manual review
      return

  resolve CaseRequest for each SR
  batch-query Salesforce snapshots

  for each CaseRequest:
      if UNKNOWN or CONFLICT:
          create confirmation item
          continue

      if Salesforce record missing:
          add SR_NOT_FOUND result
          continue

      validate action using pure rules
      if validation fails:
          add skipped/manual-review result
          continue

      create intended update plan
      if dry-run:
          add WOULD_UPDATE result
          continue

      check idempotency record
      re-fetch Salesforce snapshot
      re-run validation
      perform minimal field update
      re-fetch and verify expected state
      persist audit result

  write one ManageEngine summary note
  set final automation disposition
  release lock
```

### 16. Idempotency and Concurrency
Use an idempotency key such as:

```text
sha256(ticket_number + sr_number + action + rule_version)
```

Persist:
- Key
- First-seen and last-attempt timestamps
- Outcome
- Salesforce record ID
- Before and after status
- Correlation ID

Use optimistic concurrency when Salesforce supports the needed conditional behavior, or compare `LastModifiedDate` between validation and update. If state changes, stop with `STALE_RECORD` and re-evaluate. Only one worker may process a ticket at a time.

### 17. Result and Exception Codes
At minimum:

```text
SUCCESS
WOULD_UPDATE
ALREADY_IN_TARGET_STATE
SR_NOT_FOUND
NO_SR_FOUND
UNKNOWN_ACTION
CONFLICTING_ACTION
INVALID_CURRENT_STATUS
REJECTION_EVIDENCE_MISSING
OPEN_ACTIVITIES
FIELD_MAPPING_MISSING
STALE_RECORD
SALESFORCE_READ_FAILED
SALESFORCE_UPDATE_FAILED
MANAGEENGINE_READ_FAILED
ATTACHMENT_PARSE_FAILED
CONFIRMATION_REQUIRED
DUPLICATE_EXECUTION
PARTIAL_SUCCESS
```

Exceptions must not erase successful results for other SRs in the same ticket.

### 18. ManageEngine Summary Note
Use a concise, auditable format:

```text
Automation result for ticket #591200
Mode: DRY RUN
Rule version: 0.1.0
Correlation ID: <id>

SR 23439267 | CANCEL | WOULD_UPDATE | Submitted -> Cancelled
SR 23437647 | CANCEL | SKIPPED | INVALID_CURRENT_STATUS
SR 23412928 | UNKNOWN | MANUAL REVIEW | Confirmation required

Processed: 3 | Success/Would update: 1 | Skipped: 1 | Manual review: 1
No Salesforce records were changed in dry-run mode.
```

Do not put raw stack traces or secrets in ticket notes.

### 19. Logging, Audit, and Monitoring
Use structured JSON logs with:
- timestamp
- environment
- correlation_id
- ticket_number
- SR number
- action
- stage
- outcome
- duration
- retry count
- sanitized error code

Maintain a durable audit record for each SR. Track:
- Tickets detected and processed
- SRs extracted
- Intent resolution rate
- Confirmation rate
- Success, skipped, and failed counts
- API latency and error rate
- Duplicate prevention events
- Processing duration per ticket and per SR

Never log OAuth tokens, authorization headers, full descriptions, attachment bodies, customer names, or unrestricted Salesforce payloads.

### 20. Testing Strategy

#### Unit tests
- HTML normalization
- Table extraction
- Spreadsheet row parsing
- SR regex, deduplication, and false positives
- Evidence precedence
- Mixed-action tickets
- Conflict detection
- All validation rules
- Idempotency key creation
- Summary generation

#### Contract tests
Use sanitized API response fixtures to lock down expected shapes for both APIs.

#### Integration tests
- ManageEngine UAT read
- ManageEngine attachment download
- Salesforce Full Copy query
- Salesforce activity query
- Salesforce update against dedicated test cases only
- ManageEngine note creation on dedicated test tickets only

#### Historical replay/UAT
Create an Excel/CSV dataset with:
- ticket number or synthetic identifier
- subject
- description
- sanitized attachment representation
- SR number
- support engineer expected action per SR
- expected eligibility result
- expected final outcome
- ambiguity/confirmation flag

Use at least 50 historical tickets, including cancellation, rejection, closure, bulk, contradictory, and malformed examples. Measure separately:
- SR extraction precision/recall
- Intent classification accuracy
- Validation agreement
- False-update count

The release gate must prioritize zero incorrect writes, not only 95 percent overall accuracy.

### 21. Required Test Scenarios
1. One cancellation SR with explicit description.
2. Bulk cancellation table with 100 SRs.
3. Ambiguous subject `Cancellation/Rejection`, but each row says `To be cancelled`.
4. Ambiguous subject and no row action.
5. One ticket containing cancel and reject rows.
6. Conflicting description and attachment row.
7. Duplicate SR repeated in body and attachment.
8. Ticket number accidentally matching SR length.
9. SR not found in Salesforce.
10. Already cancelled/rejected/closed case.
11. Invalid current status.
12. Rejection requested with finance evidence.
13. Rejection requested without required finance evidence.
14. Closure requested with all scoped activities closed.
15. Closure requested with at least one open activity.
16. Attachment unreadable or unsupported.
17. Salesforce timeout after validation but before update.
18. Update succeeds but response is lost; retry must not duplicate.
19. Case changes between validation and update.
20. ManageEngine note fails after Salesforce succeeds; reconciliation must repair reporting without repeating Salesforce update.

### 22. Delivery Phases

#### Phase 0: Business and metadata discovery
- Obtain 30 to 50 sanitized historical tickets and support engineer assessments.
- Confirm per-action eligibility rules.
- Confirm Salesforce object/field/status mappings.
- Confirm ManageEngine API contract.
- Deliver field mapping and decision table.

#### Phase 1: Read-only POC
- Read one or more UAT tickets.
- Parse HTML/tables/attachments.
- Extract SRs and classify intent.
- Query Salesforce read-only.
- Produce console/JSON dry-run report.
- No external updates.

#### Phase 2: Validation engine
- Implement deterministic validators.
- Add per-SR outcomes and historical replay tests.
- Business team reviews dry-run results.

#### Phase 3: Controlled Full Copy writes
- Enable writes only for dedicated test cases.
- Add idempotency, re-validation, audit, and post-update verification.
- Keep ManageEngine note writing optional.

#### Phase 4: Bulk and human confirmation
- Support 100+ SRs with batching and rate controls.
- Add confirmation queue/email or controlled ManageEngine response.
- Resume safely after confirmation.

#### Phase 5: UAT and production readiness
- Run historical and live-shadow UAT.
- Complete security, operational, rollback, and access reviews.
- Deploy with writes disabled initially.
- Enable production writes gradually by action type or small allowlist.

### 23. Definition of Done for POC
- A VS Code project runs locally with one documented command.
- ManageEngine UAT ticket can be fetched securely.
- HTML description and supported attachment rows are normalized.
- SRs are extracted with evidence locations.
- CANCEL, REJECT, CLOSE, UNKNOWN, and CONFLICT are returned per SR.
- Salesforce Full Copy cases and required related records are queried read-only.
- Dry-run results explain every decision.
- Unit tests cover core parsing and decision logic.
- No credentials or real sensitive data are committed.
- No Salesforce or ManageEngine mutation occurs.

### 24. Coding Standards
- Python 3.12 or organization-approved version.
- Use type hints throughout.
- Use small, composable functions and dependency injection for API clients.
- Keep network I/O out of domain rules.
- Use `httpx` or an approved HTTP client with explicit timeouts.
- Use a maintained Salesforce client library only if approved; otherwise isolate REST calls behind `SalesforceClient`.
- Use `pytest`, coverage, Ruff, and a type checker such as mypy or Pyright.
- Use UTC internally and ISO 8601 timestamps.
- Add docstrings where behavior is not obvious.
- Avoid generic `except Exception` except at the top-level boundary where errors are converted to safe result codes.
- Redact secrets in all exceptions and HTTP logging.

### 25. Commands to Provide
The implementation should eventually support commands similar to:

```bash
python -m sr_automation.cli inspect-ticket --ticket 591200
python -m sr_automation.cli dry-run --ticket 591200
python -m sr_automation.cli discover-salesforce
python -m sr_automation.cli replay --fixture tests/fixtures/ticket_591200.json
pytest -q
ruff check .
```

Do not invent working command implementations before the project files exist.

### 26. Initial Backlog in Recommended Order
1. Create repository skeleton, settings, linting, and tests.
2. Add `.env.example` and secret-redaction rules.
3. Implement ManageEngine read-only client and fixture capture.
4. Normalize ticket HTML to text and tables.
5. Implement Excel/CSV attachment parser with file-size/type limits.
6. Implement SR extraction and evidence model.
7. Implement deterministic per-SR intent resolver.
8. Build Salesforce metadata discovery and field mapping.
9. Implement Salesforce read-only repositories.
10. Implement action-specific pure validation rules.
11. Build dry-run execution plan and JSON/console reports.
12. Replay historical tickets and tune configurable rules.
13. Add audit/idempotency persistence.
14. Add controlled Salesforce updates in Full Copy.
15. Add post-update verification and reconciliation.
16. Add ManageEngine summary note.
17. Add human-confirmation workflow.
18. Add scheduler/webhook worker, monitoring, and deployment packaging.

### 27. Open Questions That Must Be Resolved
- What are the actual Salesforce API names for every required field?
- Which object and fields represent finance rejection evidence?
- Does activity status appear as `Reject`, `Rejected`, or another configured value?
- What exactly counts as an open activity for closure?
- Which current statuses are eligible for each action and record type?
- Is the cancellation reason field different from the rejection reason field?
- Is a ticket audit comment required for closure?
- Is action determined per ticket or per SR when rows differ?
- Which attachment formats are supported: XLSX, XLS, CSV, PDF, image?
- Is OCR in scope? Recommendation: not for POC.
- What ManageEngine event indicates a ticket is ready for automation?
- How is a confirmation response captured and authenticated?
- Can partial success be written back while keeping the ticket open?
- What are API rate limits and batch-size constraints?
- What data retention period applies to audit logs and fixtures?

### 28. Instructions for the Coding Assistant
When asked to implement this project:
1. Read this entire file before changing code.
2. State assumptions and identify unresolved field mappings.
3. Implement the smallest vertical slice that satisfies the current task.
4. Write or update tests before or alongside implementation.
5. Do not add Salesforce mutations until read-only discovery and mappings are complete.
6. Do not use an LLM, fuzzy semantic classifier, OCR, browser automation, or UI scraping unless explicitly approved.
7. Treat ManageEngine and Salesforce clients as replaceable adapters.
8. Preserve deterministic, per-SR decision evidence in every result.
9. Keep dry-run as the default.
10. Never place secrets or unsanitized production data in generated files.
11. If an API detail is unknown, create a typed interface/config placeholder and mark it `TO_BE_CONFIRMED`; do not fabricate endpoints or field names.
12. At the end of every coding task, report changed files, tests run, remaining risks, and the next recommended step.

### 29. First Implementation Prompt
Use the following as the first coding instruction after placing this file at repository root:

```text
Read CLAUDE.md completely. Create only the Phase 1 read-only project skeleton and deterministic parsing layer.

Deliver:
1. Python package and folder structure from CLAUDE.md.
2. pyproject.toml with runtime and development dependencies.
3. settings loader using environment variables and YAML, with dry-run true by default.
4. Typed domain models for Ticket, Attachment, Evidence, CaseRequest, and ProcessingResult.
5. HTML normalizer and table parser.
6. Configurable SR extractor, initially using an 8-digit candidate pattern.
7. Per-SR deterministic intent resolver implementing evidence precedence and returning CANCEL, REJECT, CLOSE, UNKNOWN, or CONFLICT.
8. ManageEngineClient and SalesforceClient interfaces/stubs only. Do not invent API endpoints or Salesforce custom API field names.
9. CLI command that processes a local sanitized JSON fixture and prints a JSON dry-run result.
10. Unit tests for explicit cancellation, ambiguous cancellation/rejection subject, per-row mixed actions, conflicting evidence, duplicate SRs, and no SR found.
11. README with setup and VS Code run/test instructions.
12. .env.example and .gitignore.

Constraints:
- No API writes.
- No LLM.
- No OCR.
- No secrets.
- Network-independent unit tests.
- Use strict typing and structured result codes.
- Mark unknown external mappings as TO_BE_CONFIRMED.

After implementation, run tests and linting. Report all generated files, command results, assumptions, and blockers. Do not proceed to API integration in this task.
```

### 30. Immediate Next Business Artifact
Request a sanitized sample sheet with one row per SR containing:

```text
Ticket Number
Subject
Description
Attachment/Table Content
SR Number
Support Engineer Expected Action
Eligibility Decision
Expected Final Salesforce Status
Reason for Decision
Would Ask User for Confirmation? Yes/No
```

This dataset becomes the ground truth for deterministic-rule development and UAT.


### 31. Environment Discovery Results

The following technical findings were confirmed during the initial read-only discovery:

#### Salesforce Full Copy

- OAuth 2.0 Client Credentials authentication is working.
- Salesforce REST API version v65.0 is available.
- Salesforce CLI and third-party Salesforce libraries are not required for the POC.
- The configured OAuth execution user can list and read Case records.
- Case records visible through the API may not appear in a Salesforce UI list view because list views can apply additional filters.
- Direct Case access through the Salesforce record ID confirmed that the API and UI point to the same Full Copy environment.
- Salesforce Case metadata discovery returned approximately 850 fields.

Confirmed Salesforce Case field mappings:

- Case number: `CaseNumber`
- Main status: `Status`
- Record type: `RecordTypeId`
- SR type: `SR_Type__c`
- Standard internal comments: `Comments`
- Custom internal comments: `Internal_Comments__c`
- Rejection reason: `Rejection_Reason__c`
- Possible cancellation reason: `Reason_For_Cancellation_M__c`
- Cancellation indicator: `IsCancelled__c`

Target status values available on the Case object include:

- `Cancelled`
- `Rejected`
- `Closed`
- `Finance Rejected`
- `Bulk Cancelled`
- `Bulk Closed`

The correct status and supporting fields must be confirmed per SR type and record type before implementing Salesforce updates.

#### ManageEngine UAT

- Confirmed UAT base URL: `https://helpdeskuat.damacgroup.com`
- Confirmed read-only endpoint: `/api/v3/requests`
- API-key authentication using the `authtoken` header is working.
- The API successfully returned a recent ticket with HTTP status 200.
- The API response contains ticket ID, subject, status, group, and template.
- A returned sample ticket belonged to group `ITMS-ServiceDesk` and template `Default Request`.
- This sample ticket is outside the expected Salesforce automation scope.
- Target ManageEngine group, template, status, or automation-ready filter still requires confirmation.

#### Network Constraint

- ManageEngine UAT is accessible from the Dubai office network.
- The India laptop returned `[Errno 11001] getaddrinfo failed`.
- The failure occurred before HTTP authentication and indicates DNS or network reachability failure.
- This is not a Python-code or API-token failure.
- Development from India requires approved VPN, internal DNS access, firewall access, or execution from an approved Dubai-hosted machine or server.

#### Confirmed Automation Boundary

The automation must not scan and update all Salesforce Cases.

Processing begins only from a ManageEngine ticket that matches the approved group, template, and readiness filters. Salesforce is queried only for Case numbers extracted from that eligible ticket.

All operations remain read-only and in dry-run mode until:

1. ManageEngine ticket filtering is confirmed.
2. Salesforce field mappings are approved.
3. Action-specific eligibility rules are approved.
4. Dedicated test Cases are identified.
5. Business users review dry-run results.