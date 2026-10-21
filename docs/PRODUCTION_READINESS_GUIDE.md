# PRODUCTION READINESS GUIDE

This guide reflects only what is visible in the source code and config files. Where a production concern is not defined in code, it is called out as “NOT FOUND IN SOURCE CODE”.

---

## 1) Current UAT setup

The active config used by default is [config/uat.yaml](../config/uat.yaml#L1-L110).

Important UAT-specific values visible in source:

- Allowed group: `SalesForce - CRM`
- Allowed template: `SalesForce CRM Issues`
- Ready status: `Open`
- Safety filter: enabled and required tag `SR-AUTOMATION-UAT`
- Target statuses:
  - Cancel -> `Cancelled`
  - Reject -> `Rejected`
  - Close -> `Closed`
- Rule version: `0.2.0`

Evidence:

- config: [config/uat.yaml](../config/uat.yaml#L1-L110)
- settings loader: [src/sr_automation/settings.py](../src/sr_automation/settings.py#L87-L171)

### UAT-only settings

The code clearly shows UAT-specific configuration values and comments:

- `closure_rules.environment: "uat"` in [config/uat.yaml](../config/uat.yaml#L17-L41)
- a comment specifically says “Add only when running against production: - ITMS - SF CRM” in [config/uat.yaml](../config/uat.yaml#L2-L8)
- `required_tag` is set to `SR-AUTOMATION-UAT` in [config/uat.yaml](../config/uat.yaml#L41-L51)
- `DRY_RUN` defaults to `true` in [src/sr_automation/settings.py](../src/sr_automation/settings.py#L103-L109)
- `ENABLE_SALESFORCE_WRITES` defaults to `false` and `ENABLE_MANAGEENGINE_WRITES` defaults to `false` in [src/sr_automation/settings.py](../src/sr_automation/settings.py#L109-L145)

### Hardcoded values and test accounts

These values are hardcoded or config-based and not abstracted into a true production service model:

- Technician name and ID: `Suruthi Vijayasingh`, `77787` in [config/uat.yaml](../config/uat.yaml#L9-L18)
- closure rule `env.uat` values in [config/uat.yaml](../config/uat.yaml#L19-L41)
- required tag `SR-AUTOMATION-UAT` in [config/uat.yaml](../config/uat.yaml#L41-L51)
- team/group names in [config/uat.yaml](../config/uat.yaml#L1-L12)

No dedicated production automation service account is referenced in the source. This is “NOT FOUND IN SOURCE CODE”.

---

## 2) Current service users and technician configuration

The config includes a technician record:

- `manageengine.technician.name: "Suruthi Vijayasingh"`
- `manageengine.technician.id: "77787"`

Source: [config/uat.yaml](../config/uat.yaml#L9-L18)

The code also reads these values into the `Settings` object: [src/sr_automation/settings.py](../src/sr_automation/settings.py#L150-L171).

This means the bot currently assumes an individual technician assignment model in UAT, not a dedicated service-account model.

### Important gap

There is no evidence in source for a dedicated automation service account with:

- a username or alias
- an email or personal account
- a service principal
- a dedicated permission group

This is “NOT FOUND IN SOURCE CODE”.

---

## 3) What would change for a dedicated automation service account

If the organization creates a dedicated automation service account, the current code would require changes in at least three areas:

1. Credential model
   - Replace direct token/secret usage with the production service-account credentials.
   - Source: [src/sr_automation/settings.py](../src/sr_automation/settings.py#L59-L72)

2. Authorization model
   - Update `allowed_groups`, `allowed_templates`, and readiness/closure rules to match the production service account’s actual access model.
   - Source: [config/uat.yaml](../config/uat.yaml#L1-L12) and [src/sr_automation/settings.py](../src/sr_automation/settings.py#L87-L145)

3. Technician / closure behavior
   - Current UAT config includes a technician name and ID. A production service account may no longer require or allow a personal technician assignment.
   - Source: [config/uat.yaml](../config/uat.yaml#L9-L18)

### Operational differences the code would need to reflect

- `MANAGEENGINE_BASE_URL` and `MANAGEENGINE_API_TOKEN` must point to production endpoints.
- `SALESFORCE_LOGIN_URL`, `SALESFORCE_CLIENT_ID`, and `SALESFORCE_CLIENT_SECRET` must be production secrets.
- `MANAGEENGINE_AUTO_CLOSE` must be explicitly evaluated in live mode, not just in UAT defaults.
- `DRY_RUN` and write flags must be set to production-safe values.

This is visible in [src/sr_automation/settings.py](../src/sr_automation/settings.py#L94-L145).

---

## 4) Production deployment checklist

### Security and configuration

- Ensure all required environment variables are populated in production.
- Confirm `MANAGEENGINE_BASE_URL`, `MANAGEENGINE_API_TOKEN`, `SALESFORCE_LOGIN_URL`, `SALESFORCE_CLIENT_ID`, `SALESFORCE_CLIENT_SECRET` are correct.
- Confirm `AUDIT_DB_URL` points to a production-safe audit repository.
- Validate `DRY_RUN`, `ENABLE_SALESFORCE_WRITES`, `ENABLE_MANAGEENGINE_WRITES`, and `MANAGEENGINE_AUTO_CLOSE` are intentionally set.
- Evidence: [src/sr_automation/settings.py](../src/sr_automation/settings.py#L59-L145)

### Permission model

- Validate the service account can read open ManageEngine tickets in the expected group and status.
- Validate the service account can query Salesforce Cases and update the target status values.
- Validate the user can write notes and close tickets if `MANAGEENGINE_AUTO_CLOSE` is enabled.
- Evidence: [src/sr_automation/clients.py](../src/sr_automation/clients.py#L15-L149)

### Business validation

- Validate the allowed groups and templates match production support operations.
- Validate record types and status eligibility match production case data.
- Validate `require_high_confidence_intent` for rejection is acceptable in production workflow.
- Evidence: [config/uat.yaml](../config/uat.yaml#L1-L89), [src/sr_automation/rules.py](../src/sr_automation/rules.py#L8-L38)

---

## 5) Pre-deployment validation checklist

Before production rollout, the source code suggests checking the following:

- Parse edge cases for `CANCEL`, `REJECT`, and `CLOSE` keywords.
- Validate that no accidental numeric false positives occur from dates or unrelated numbers.
- Confirm `CASE_PATTERN` is not too permissive for production data: [src/sr_automation/parser.py](../src/sr_automation/parser.py#L1-L5)
- Run the bot in dry run and confirm `WOULD_UPDATE` and `MANUAL_REVIEW` behavior is correct.
- Confirm `SalesforceClient.authenticate()` works with the production OAuth credentials.
- Confirm `resolve_stage2_patch_limit()` is appropriate for production safety caps.
- Evidence: [src/sr_automation/main.py](../src/sr_automation/main.py#L260-L279), [src/sr_automation/clients.py](../src/sr_automation/clients.py#L111-L149)

---

## 6) Post-deployment validation checklist

After deployment, validate the following in the actual environment:

- Ticket fetch works for the configured group, template, and status.
- Case lookup finds target records correctly.
- Parallel or overlapping updates do not create duplicate processing.
- Summary notes are created and correlate to the ticket.
- Auto-close behavior only triggers when the outcome is successful.
- Evidence: [src/sr_automation/main.py](../src/sr_automation/main.py#L1091-L1840)

---

## 7) Rollback process

The code does not define a formal rollback mechanism or deployment rollback playbook. This is “NOT FOUND IN SOURCE CODE”.

What is visible in code is the safety model:

- dry-run mode
- write flags
- max update cap
- stale record checks
- idempotency around audit entries

Source:

- [src/sr_automation/main.py](../src/sr_automation/main.py#L1194-L1392)
- [src/sr_automation/settings.py](../src/sr_automation/settings.py#L87-L145)

In practice, the code supports operational rollback by disabling write flags and running within dry-run mode, but it does not define an actual production rollback procedure.

---

## 8) Monitoring requirements

The project logs several important signals directly in the runtime:

- `Ticket {id} | {action} | {confidence} | {reason_code}`
- `ACTION_RULE_SELECTED case=...`
- `ELIGIBILITY_RESULT ...`
- `PATCH_ATTEMPTS_USED=...`
- `ME_NOTE_ADDED` or `ME_NOTE_FAILED`

Evidence: [src/sr_automation/main.py](../src/sr_automation/main.py#L1088-L1475) and [src/sr_automation/main.py](../src/sr_automation/main.py#L1480-L1840).

### Minimum monitoring items

- Number of tickets processed
- Number of `MANUAL_REVIEW` outcomes
- Number of `CASE_NOT_FOUND` and `INVALID_CURRENT_STATUS` outcomes
- Salesforce patch failure counts
- ManageEngine note creation failure counts
- ManageEngine ticket closure counts

---

## 9) Logging requirements

The project already prints structured status lines and error details.

Examples:

- `print_http_error_details()` in [src/sr_automation/main.py](../src/sr_automation/main.py#L260-L289)
- comment and status logging in [src/sr_automation/main.py](../src/sr_automation/main.py#L1091-L1475)
- ManageEngine note logging in [src/sr_automation/manageengine/note_writer.py](../src/sr_automation/manageengine/note_writer.py#L16-L58)

### Logging strength

The bot does log enough to understand:

- which ticket was processed,
- which action was resolved,
- what confidence was assigned,
- what outcome was returned,
- whether a Salesforce patch succeeded or failed,
- and whether a note or closure was verified.

### Logging gap

There is no explicit centralized application logging framework or structured log sink beyond console prints. This is “NOT FOUND IN SOURCE CODE”.

---

## 10) Support model

The project code does not define a formal production support model, support roster, or on-call process. That is “NOT FOUND IN SOURCE CODE”.

What does exist in source:

- a technician record in config: [config/uat.yaml](../config/uat.yaml#L9-L18)
- several audit and status records in the code: [src/sr_automation/persistence/audit_repository.py](../src/sr_automation/persistence/audit_repository.py)
- console-based operational diagnostics in [src/sr_automation/main.py](../src/sr_automation/main.py#L963-L1840)

This indicates a support model based on operational logs and manual review of cases rather than a formal service desk process definition.

---

## 11) Known risks

The main risks visible from code only are:

1. Case extraction is too permissive
   - Regex accepts any 4–8 digit sequence and can incorrectly capture dates like `2026`.
   - Source: [src/sr_automation/parser.py](../src/sr_automation/parser.py#L1-L5)

2. Reject requires high confidence by config and rule gate
   - If intent confidence is not high, the bot can block or require manual review.
   - Source: [src/sr_automation/rules.py](../src/sr_automation/rules.py#L12-L18)

3. Open-task logic can block updates for `CANCEL` and `CLOSE`
   - This may be intended but must be validated against production ticket patterns.
   - Source: [src/sr_automation/rules.py](../src/sr_automation/rules.py#L31-L36)

4. Partial processing is configured permissively
   - `allow_partial_processing: true` in [config/uat.yaml](../config/uat.yaml#L79-L89)
   - This may let some cases be processed while others remain manual when production volume is high.

5. No explicit production rollback plan is defined in source
   - “NOT FOUND IN SOURCE CODE”

---

## 12) Final assessment

The codebase is operationally ready as a controlled UAT automation pattern, but it is not yet a complete production-hardened service definition. The main gaps visible from source are:

- no dedicated production service-account model
- no formal rollback runbook
- no production support process in code
- no strict case-number validation beyond raw digit matching
- a UAT-specific config still embedded in the active YAML

The source is enough to understand the business logic and automation sequence, but not enough to claim full production readiness without a separate environment, credential, and support model review.
