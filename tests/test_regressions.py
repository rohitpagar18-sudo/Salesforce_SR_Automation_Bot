import datetime
import os
import sqlite3
from pathlib import Path

import httpx

from sr_automation import main as main_mod
from sr_automation.persistence.audit_repository import AuditRepository


class FakeMEBulk:
    def __init__(self, s):
        self.s = s
        self.notes = []
        self.closed = []
        self.assigned = []
        now = int(datetime.datetime.utcnow().timestamp() * 1000)
        self.ticket = {
            "id": "559970",
            "subject": "Cancel SRs",
            "description": "Please cancel Case Number 1001 1002 1003",
            "group": {"name": list(self.s.allowed_groups)[0]},
            "template": {"name": list(self.s.allowed_templates)[0]},
            "status": {"name": list(self.s.ready_statuses)[0]},
            "created_time": now,
            "tags": [self.s.required_tag],
        }

    def list_open_tickets(self):
        return [{"id": "559970"}]

    def get_ticket(self, ticket_id):
        return self.ticket

    def list_notes(self, ticket_id, timeout=30.0):
        return self.notes

    def post_note(self, ticket_id, payload, timeout=30.0):
        class Response:
            def __init__(self, data):
                self._data = data
            def json(self):
                return self._data
            def raise_for_status(self):
                return None
        self.notes.append({"description": payload["note"]["description"], "id": "N-1"})
        return Response({"note": {"id": "N-1"}})

    def assign_technician(self, ticket_id, tech):
        self.assigned.append((ticket_id, tech))
        class Response:
            def raise_for_status(self):
                return None
        return Response()

    def update_request(self, ticket_id, payload):
        class Response:
            def raise_for_status(self):
                return None
        return Response()

    def close_ticket(self, ticket_id, closure_info):
        self.closed.append((ticket_id, closure_info))
        self.ticket["status"] = {"name": "Closed"}
        class Response:
            def raise_for_status(self):
                return None
        return Response()


class FakeSFBulk:
    def __init__(self, s, status_map=None):
        self.s = s
        self.status_map = status_map or {}
        self.patch_calls = []
        self._calls = 0

    def authenticate(self):
        return None

    def cases(self, numbers):
        out = {}
        for n in numbers:
            status = self.status_map.get(str(n), "New")
            out[n] = {"Id": f"ID_{n}", "CaseNumber": n, "Status": status, "RecordType": {"Name": "Default"}, "LastModifiedDate": "LMD0"}
        return out

    def tasks(self, case_ids):
        return {cid: [] for cid in case_ids}

    def refresh_case(self, case_id):
        num = case_id.replace("ID_", "")
        return {"Id": case_id, "CaseNumber": num, "Status": self.status_map.get(num, "New"), "RecordType": {"Name": "Default"}, "LastModifiedDate": "LMD0"}

    def refresh_tasks_for_case(self, case_id):
        return []

    def update_case(self, case_id, fields):
        self._calls += 1
        self.patch_calls.append((case_id, fields))
        num = case_id.replace("ID_", "")
        self.status_map[num] = fields["Status"]
        class Response:
            def raise_for_status(self):
                return None
        return Response()


def test_stage2_null_limit_processes_all_eligible_cases(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    db.unlink(missing_ok=True)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")
    monkeypatch.setenv("MANAGEENGINE_AUTO_CLOSE", "false")

    def make_me(s):
        return FakeMEBulk(s)

    def make_sf(s):
        return FakeSFBulk(s, {"1001": "New", "1002": "New", "1003": "New"})

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: make_me(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", lambda s: FakeMEBulk(s))

    main_mod.run_cycle("config/uat.yaml")
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT outcome FROM audit WHERE requested_action = 'CANCEL'")
    rows = cur.fetchall()
    conn.close()
    assert sum(1 for (outcome,) in rows if outcome == "SUCCESS") >= 3


def test_sf_patch_http_400_logs_sanitized_error_details(monkeypatch, capsys):
    db = Path(os.environ["AUDIT_DB_URL"])
    db.unlink(missing_ok=True)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")
    monkeypatch.setenv("MANAGEENGINE_AUTO_CLOSE", "false")

    class Patch400SF(FakeSFBulk):
        def update_case(self, case_id, fields):
            payload = {
                "errorCode": "INVALID_FIELD",
                "message": "The Status field is invalid. token=abc123",
                "fields": ["Status", "token"],
            }
            request = httpx.Request("PATCH", "https://example.invalid/services/data/v65.0/sobjects/Case/ID_1001")
            response = httpx.Response(400, json=payload, request=request)
            raise httpx.HTTPStatusError("Patch failed", request=response.request, response=response)

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: FakeMEBulk(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: Patch400SF(s, {"1001": "New"}))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", lambda s: FakeMEBulk(s))

    main_mod.run_cycle("config/uat.yaml")
    captured = capsys.readouterr().out

    assert "PATCH_FAILED" in captured
    assert "HTTP 400" in captured
    assert "case=1001" in captured
    assert "errorCode=INVALID_FIELD" in captured
    assert "message=" in captured
    assert "The Status field is invalid" in captured
    assert "fields=[\'Status\', \'token\']" in captured or "fields=[\"Status\", \"token\"]" in captured
    assert "abc123" not in captured
    assert "Bearer" not in captured
    assert "Authorization" not in captured


def test_sf_patch_http_400_logs_sanitized_error_details_list_payload(monkeypatch, capsys):
    db = Path(os.environ["AUDIT_DB_URL"])
    db.unlink(missing_ok=True)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")
    monkeypatch.setenv("MANAGEENGINE_AUTO_CLOSE", "false")

    class Patch400ListSF(FakeSFBulk):
        def update_case(self, case_id, fields):
            payload = [{
                "errorCode": "INVALID_STATUS",
                "message": "Case cannot be closed. client_secret=supersecret",
                "fields": ["Status"],
            }]
            request = httpx.Request("PATCH", "https://example.invalid/services/data/v65.0/sobjects/Case/ID_1002")
            response = httpx.Response(400, json=payload, request=request)
            raise httpx.HTTPStatusError("Patch failed", request=response.request, response=response)

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: FakeMEBulk(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: Patch400ListSF(s, {"1002": "New"}))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", lambda s: FakeMEBulk(s))

    main_mod.run_cycle("config/uat.yaml")
    captured = capsys.readouterr().out

    assert "PATCH_FAILED" in captured
    assert "HTTP 400" in captured
    assert "case=1002" in captured
    assert "errorCode=INVALID_STATUS" in captured
    assert "Case cannot be closed" in captured
    assert "supersecret" not in captured
    assert "fields=['Status']" in captured or 'fields=["Status"]' in captured


def test_stage2_already_in_target_state_does_not_consume_limit(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")
    monkeypatch.setenv("MANAGEENGINE_AUTO_CLOSE", "false")

    def make_me(s):
        return FakeMEBulk(s)

    def make_sf(s):
        return FakeSFBulk(s, {"1001": "Cancelled", "1002": "New", "1003": "New"})

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: make_me(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: make_sf(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", lambda s: FakeMEBulk(s))

    main_mod.run_cycle("config/uat.yaml")
    db = Path(os.environ["AUDIT_DB_URL"])
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT outcome FROM audit WHERE case_number IN ('1001','1002','1003') AND requested_action='CANCEL'")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    assert "ALREADY_IN_TARGET_STATE" in rows
    assert rows.count("SUCCESS") >= 2


def test_summary_digest_note_is_idempotent(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    audit.insert_audit(
        idempotency_key="me_digest|559970|abc123",
        ticket_id="559970",
        case_number="",
        requested_action="ME_SUMMARY_DIGEST",
        rule_version="0.2.0",
        before_status="abc123",
        intended_status="",
        after_status="",
        salesforce_id="",
        last_modified_date="",
        processing_state="ME_NOTE_VERIFIED",
        outcome="ME_NOTE_VERIFIED",
        correlation_id="c1",
    )
    rows = audit.get_by_ticket("559970")
    assert any(r.get("requested_action") == "ME_SUMMARY_DIGEST" and r.get("before_status") == "abc123" for r in rows)
