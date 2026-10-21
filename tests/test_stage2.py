import os
import json
import datetime
import sqlite3
import pytest
import httpx
from types import SimpleNamespace
from pathlib import Path

from sr_automation import main as main_mod
from sr_automation.manageengine.note_writer import ManageEngineNoteWriter
from sr_automation.persistence.audit_repository import AuditRepository

# Helpers
class FakeME:
    def __init__(self, s):
        self.s = s
    def list_open_tickets(self):
        return [{"id": "559961"}]
    def get_ticket(self, ticket_id):
        now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
        return {
            "id": ticket_id,
            "subject": "Cancel SRs",
            "description": "Please cancel Case Number 23422946 23423532",
            "group": {"name": list(self.s.allowed_groups)[0]},
            "template": {"name": list(self.s.allowed_templates)[0]},
            "status": {"name": list(self.s.ready_statuses)[0]},
            "created_time": now_ms,
            "tags": [self.s.required_tag]
        }

class FakeSF:
    def __init__(self, s, behavior=None):
        self.s = s
        self.behavior = behavior or {}
        self.update_calls = []
        self._refresh_counts = {}
    def authenticate(self):
        return None
    def cases(self, numbers):
        out = {}
        for i, n in enumerate(numbers):
            out[n] = {"Id": f"ID_{n}", "CaseNumber": n, "Status": "New", "RecordType": {"Name": "Default"}, "LastModifiedDate": "LMD0"}
        return out
    def tasks(self, case_ids):
        return {cid: [] for cid in case_ids}
    def refresh_case(self, case_id):
        # case_id may be like ID_23422946
        num = case_id.split("ID_")[-1]
        cnt = self._refresh_counts.get(num, 0) + 1
        self._refresh_counts[num] = cnt
        if self.update_calls:
            status = self.behavior.get("after_update_status", {}).get(num, self.behavior.get("post_status", {}).get(num, "New"))
        elif cnt == 1:
            status = self.behavior.get("initial_status", {}).get(num, "New")
        else:
            status = self.behavior.get("post_status", {}).get(num, "New")
        return {"Id": case_id, "CaseNumber": num, "Status": status, "LastModifiedDate": self.behavior.get("lmd", {}).get(num, "LMD0")}
    def refresh_tasks_for_case(self, case_id):
        return []
    def update_case(self, case_id, fields, timeout=30.0):
        self.update_calls.append((case_id, fields))
        # Decide whether to raise
        if self.behavior.get("raise_on_update"):
            raise httpx.RequestError("simulated timeout")
        class R:
            def raise_for_status(self):
                return None
        return R()


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    # Use a temp audit DB
    dbfile = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_URL", str(dbfile))
    # Ensure DRY_RUN default in tests unless overridden
    monkeypatch.setenv("DRY_RUN", "false")
    # Enable salesforce writes in tests so mocked SF client is used
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    # Patch clients in main
    def make_me(s):
        return FakeME(s)
    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: FakeME(s))
    return tmp_path


def run_cycle_with_behavior(tmp_path, monkeypatch, behavior=None):
    # behavior passed to FakeSF
    def make_sf(s):
        return FakeSF(s, behavior=behavior)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    # Run
    main_mod.run_cycle("config/uat.yaml")
    # Return audit DB path
    return Path(os.environ["AUDIT_DB_URL"])


def test_manageengine_writes_disabled(monkeypatch, tmp_path):
    # Ensure when ENABLE_MANAGEENGINE_WRITES=false no ME note is created
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "false")
    def make_sf(s):
        return FakeSF(s, behavior={})
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    # Patch ME client to capture posted notes
    class SpyME(FakeME):
        def __init__(self, s):
            super().__init__(s)
            self.posted = []
        def list_open_tickets(self):
            return [{"id": "559962"}]
        def get_ticket(self, ticket_id):
            return super().get_ticket(ticket_id)
    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: SpyME(s))
    main_mod.run_cycle("config/uat.yaml")
    # Check audit DB for ME_NOTE_SKIPPED outcome
    db = Path(os.environ["AUDIT_DB_URL"])
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT outcome FROM audit WHERE outcome LIKE 'MANAGEENGINE_%' OR processing_state LIKE 'ME_%'")
    rows = cur.fetchall()
    conn.close()
    assert any(r[0] == 'MANAGEENGINE_WRITES_DISABLED' or r[0] == 'ME_NOTE_SKIPPED' for r in rows)


def read_outcomes(db_path):
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("SELECT case_number, outcome FROM audit ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    return rows


def _single_case_me(case_number):
    class SingleCaseME(FakeME):
        def get_ticket(self, ticket_id):
            now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
            return {
                "id": ticket_id,
                "subject": "Cancel SR",
                "description": f"Please cancel Case Number {case_number}",
                "group": {"name": list(self.s.allowed_groups)[0]},
                "template": {"name": list(self.s.allowed_templates)[0]},
                "status": {"name": list(self.s.ready_statuses)[0]},
                "created_time": now_ms,
                "tags": [self.s.required_tag],
            }
    return SingleCaseME


def test_max_updates_counts_attempts(monkeypatch, tmp_path):
    # stage2.max_salesforce_updates_per_run is 1 in config. Use behavior where update succeeds.
    db = run_cycle_with_behavior(tmp_path, monkeypatch, behavior={})
    # the FakeSF instance is created inside run_cycle; we can't access it here to count calls.
    # Instead, inspect audit DB to ensure only one SUCCESS was recorded and the second case not updated.
    rows = read_outcomes(db)
    # there should be at most 1 SUCCESS entry
    success_count = sum(1 for _, o in rows if o == "SUCCESS")
    assert success_count <= 1


def test_timeout_blocks_and_reconciles_success(monkeypatch, tmp_path):
    # Simulate timeout but post-status shows target -> reconciliation success
    # For simplicity, set post_status mapping to have status 'Cancelled' matching target
    # Ensure post_status mapping covers whichever case is patched
    behavior = {"raise_on_update": True, "post_status": {"23422946": "Cancelled", "23423532": "Cancelled"}}
    db = run_cycle_with_behavior(tmp_path, monkeypatch, behavior=behavior)
    rows = read_outcomes(db)
    # find outcomes
    outcomes = {case: outcome for case, outcome in rows}
    # At least one case should show reconciliation success or already-in-target, and the other should be blocked/limited
    assert any(o in {"SUCCESS_AFTER_TIMEOUT_RECONCILIATION", "ALREADY_IN_TARGET_STATE"} for o in outcomes.values())
    assert any(o in {"WRITE_BLOCKED_AFTER_UNCERTAIN_RESULT", "UPDATE_LIMIT_REACHED", ""} for o in outcomes.values())


def test_timeout_blocks_and_unknown(monkeypatch, tmp_path):
    # Simulate timeout and post-status unchanged -> UPDATE_RESULT_UNKNOWN and circuit breaker active
    behavior = {"raise_on_update": True, "post_status": {}}
    db = run_cycle_with_behavior(tmp_path, monkeypatch, behavior=behavior)
    rows = read_outcomes(db)
    outcomes = {case: outcome for case, outcome in rows}
    # One case should be unknown after timeout and another should be blocked
    assert any(o == "UPDATE_RESULT_UNKNOWN" for o in outcomes.values())
    assert any(o == "WRITE_BLOCKED_AFTER_UNCERTAIN_RESULT" for o in outcomes.values())


def test_already_in_target_state_skips_patch(monkeypatch, tmp_path):
    # Simulate that refresh_case already shows target status -> ALREADY_IN_TARGET_STATE
    behavior = {"initial_status": {"23422946": "Cancelled", "23423532": "Cancelled"}, "post_status": {"23422946": "Cancelled", "23423532": "Cancelled"}}
    db = run_cycle_with_behavior(tmp_path, monkeypatch, behavior=behavior)
    rows = read_outcomes(db)
    outcomes = {case: outcome for case, outcome in rows}
    # At least one case should be recognized as already in target state
    assert any(o == "ALREADY_IN_TARGET_STATE" for o in outcomes.values())


def test_failed_audit_allows_retry(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key = audit.make_idempotency_key("559961", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(
        idempotency_key=key,
        ticket_id="559961",
        case_number="23422946",
        requested_action="CANCEL",
        rule_version="0.2.0",
        before_status="New",
        intended_status="Cancelled",
        after_status="",
        salesforce_id="ID_23422946",
        last_modified_date="LMD0",
        processing_state="FAILED",
        outcome="VERIFICATION_FAILED",
        correlation_id="retry-1",
    )

    sf_obj = {}
    def make_sf(s):
        sf = FakeSF(s, behavior={"initial_status": {"23422946": "New"}, "post_status": {"23422946": "New"}, "after_update_status": {"23422946": "Cancelled"}})
        sf_obj["sf"] = sf
        return sf

    monkeypatch.setattr(main_mod, "ManageEngineClient", _single_case_me("23422946"))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    main_mod.run_cycle("config/uat.yaml")

    rows = read_outcomes(db)
    assert any(case == "23422946" and outcome == "SUCCESS" for case, outcome in rows)
    assert sf_obj["sf"].update_calls


def test_verified_audit_skips_patch(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key = audit.make_idempotency_key("559961", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(
        idempotency_key=key,
        ticket_id="559961",
        case_number="23422946",
        requested_action="CANCEL",
        rule_version="0.2.0",
        before_status="New",
        intended_status="Cancelled",
        after_status="Cancelled",
        salesforce_id="ID_23422946",
        last_modified_date="LMD0",
        processing_state="SALESFORCE_VERIFIED",
        outcome="SUCCESS",
        correlation_id="skip-1",
    )

    sf_obj = {}
    def make_sf(s):
        sf = FakeSF(s, behavior={"initial_status": {"23422946": "Cancelled"}, "post_status": {"23422946": "Cancelled"}})
        sf_obj["sf"] = sf
        return sf

    monkeypatch.setattr(main_mod, "ManageEngineClient", _single_case_me("23422946"))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    main_mod.run_cycle("config/uat.yaml")

    rows = read_outcomes(db)
    assert any(case == "23422946" and outcome == "SUCCESS" for case, outcome in rows)
    assert sf_obj["sf"].update_calls == []


def test_verified_audit_not_in_target_requires_reconciliation(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key = audit.make_idempotency_key("559961", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(
        idempotency_key=key,
        ticket_id="559961",
        case_number="23422946",
        requested_action="CANCEL",
        rule_version="0.2.0",
        before_status="New",
        intended_status="Cancelled",
        after_status="Cancelled",
        salesforce_id="ID_23422946",
        last_modified_date="LMD0",
        processing_state="SALESFORCE_VERIFIED",
        outcome="SUCCESS",
        correlation_id="manual-review-1",
    )

    sf_obj = {}
    def make_sf(s):
        sf = FakeSF(s, behavior={"initial_status": {"23422946": "New"}, "post_status": {"23422946": "New"}})
        sf_obj["sf"] = sf
        return sf

    monkeypatch.setattr(main_mod, "ManageEngineClient", _single_case_me("23422946"))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    main_mod.run_cycle("config/uat.yaml")

    rows = read_outcomes(db)
    assert any(outcome in {"RECONCILIATION_REQUIRED", "MANUAL_REVIEW"} for _, outcome in rows)
    assert sf_obj["sf"].update_calls == []


def test_update_limit_reached_allows_retry(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key = audit.make_idempotency_key("559961", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(
        idempotency_key=key,
        ticket_id="559961",
        case_number="23422946",
        requested_action="CANCEL",
        rule_version="0.2.0",
        before_status="New",
        intended_status="Cancelled",
        after_status="",
        salesforce_id="ID_23422946",
        last_modified_date="LMD0",
        processing_state="FAILED",
        outcome="UPDATE_LIMIT_REACHED",
        correlation_id="limit-1",
    )

    def make_sf(s):
        return FakeSF(s, behavior={"initial_status": {"23422946": "New"}, "post_status": {"23422946": "New"}, "after_update_status": {"23422946": "Cancelled"}})

    monkeypatch.setattr(main_mod, "ManageEngineClient", _single_case_me("23422946"))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    main_mod.run_cycle("config/uat.yaml")

    rows = read_outcomes(db)
    assert any(case == "23422946" and outcome == "SUCCESS" for case, outcome in rows)


def test_next_stage2_run_updates_remaining_new_case(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key = audit.make_idempotency_key("559961", "23423532", "CANCEL", "0.2.0")
    audit.insert_audit(
        idempotency_key=key,
        ticket_id="559961",
        case_number="23423532",
        requested_action="CANCEL",
        rule_version="0.2.0",
        before_status="New",
        intended_status="Cancelled",
        after_status="",
        salesforce_id="ID_23423532",
        last_modified_date="LMD0",
        processing_state="FAILED",
        outcome="UPDATE_LIMIT_REACHED",
        correlation_id="next-run-1",
    )

    def make_sf(s):
        return FakeSF(s, behavior={"initial_status": {"23423532": "New"}, "post_status": {"23423532": "New"}, "after_update_status": {"23423532": "Cancelled"}})

    monkeypatch.setattr(main_mod, "ManageEngineClient", _single_case_me("23423532"))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    main_mod.run_cycle("config/uat.yaml")

    rows = read_outcomes(db)
    assert any(case == "23423532" and outcome == "SUCCESS" for case, outcome in rows)


def test_dry_run_never_patches(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "true")
    db = run_cycle_with_behavior(tmp_path, monkeypatch, behavior={})
    rows = read_outcomes(db)
    # All outcomes should be WOULD_UPDATE or DETECTED
    assert all(o in {"WOULD_UPDATE", "DETECTED"} for _, o in rows)


def test_manageengine_note_url_has_no_trailing_apostrophe():
    me = ManageEngineNoteWriter(SimpleNamespace(me_url="https://helpdeskuat.damacgroup.com/", me_token="token", me_header="authtoken"))
    url = me.build_note_url("559961")
    assert url == "https://helpdeskuat.damacgroup.com/api/v3/requests/559961/notes"
    assert not url.endswith("'")
    assert not url.endswith('"')
    assert not url.endswith("/")


def test_manageengine_note_post_uses_note_form_payload(monkeypatch):
    captured = {}
    def fake_post(url, headers, data, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        captured["timeout"] = timeout
        req = httpx.Request("POST", url)
        return httpx.Response(200, request=req, json={"note": {"id": "42"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    me = ManageEngineNoteWriter(SimpleNamespace(me_url="https://helpdeskuat.damacgroup.com", me_token="token", me_header="authtoken"))

    result = me.post_note("559961", {"note": {"note": "summary text", "show_to_requester": False, "mark_first_response": False, "add_to_linked_requests": False}}, timeout=30.0)

    assert result.status_code == 200
    assert captured["url"] == "https://helpdeskuat.damacgroup.com/api/v3/requests/559961/notes"
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    payload = json.loads(captured["data"]["input_data"])
    assert "note" in payload
    assert payload["note"]["description"] == "summary text"
    assert payload["note"]["show_to_requester"] is False


def test_manageengine_note_http_400_logs_response_details(monkeypatch, capsys):
    def fake_post(url, headers, data, timeout):
        req = httpx.Request("POST", url, headers=headers, content=data["input_data"])
        return httpx.Response(400, request=req, json={"error": {"message": "bad input"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    me = ManageEngineNoteWriter(SimpleNamespace(me_url="https://helpdeskuat.damacgroup.com", me_token="token", me_header="authtoken"))

    with pytest.raises(httpx.HTTPStatusError):
        me.post_note("559961", {"note": {"description": "summary text"}})

    captured = capsys.readouterr().out
    assert "HTTP status: 400" in captured
    assert "https://helpdeskuat.damacgroup.com/api/v3/requests/559961/notes" in captured
    assert "bad input" in captured
    assert "token" not in captured.lower()


def test_note_failure_for_one_ticket_does_not_stop_remaining_tickets(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")

    class TicketME(FakeME):
        def list_open_tickets(self):
            return [{"id": "559961"}, {"id": "559962"}]

    def make_sf(s):
        return FakeSF(s, behavior={})

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: TicketME(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))

    def fake_post_note(self, ticket_id, payload, timeout=30.0):
        if ticket_id == "559961":
            req = httpx.Request("POST", f"https://helpdeskuat.damacgroup.com/api/v3/requests/{ticket_id}/notes")
            response = httpx.Response(400, request=req, json={"error": {"message": "bad input"}})
            raise httpx.HTTPStatusError("bad input", request=response.request, response=response)
        req = httpx.Request("POST", f"https://helpdeskuat.damacgroup.com/api/v3/requests/{ticket_id}/notes")
        return httpx.Response(200, request=req, json={"note": {"id": "ok-2"}})

    monkeypatch.setattr(main_mod.ManageEngineNoteWriter, "post_note", fake_post_note)

    db = Path(os.environ["AUDIT_DB_URL"])
    main_mod.run_cycle("config/uat.yaml")

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT ticket_id, processing_state, outcome FROM audit WHERE processing_state LIKE 'ME_%' OR outcome LIKE 'ME_%' ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    assert any(ticket == "559961" and state == "ME_NOTE_FAILED" for ticket, state, _ in rows)
    assert any(ticket == "559962" and state == "ME_NOTE_VERIFIED" for ticket, state, _ in rows)
