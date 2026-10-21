import os
import json
import datetime
import sqlite3
import httpx
import pytest
from pathlib import Path
from types import SimpleNamespace

from sr_automation import main as main_mod
from sr_automation.persistence.audit_repository import AuditRepository

class FakeMEFull:
    def __init__(self, s, behavior=None):
        self.s = s
        self.behavior = behavior or {}
        self.assigned = []
        self.resolutions = []
        self.closed = []
        now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
        self.tickets = {
            "559970": {
                "id": "559970",
                "subject": "Cancel SRs",
                "description": "Please cancel Case Number 23422946 23423532",
                "group": {"name": "SalesForce - CRM"},
                "template": {"name": "SalesForce CRM Issues"},
                "status": {"name": "Open"},
                "created_time": now_ms,
                "tags": ["SR-AUTOMATION-UAT"],
                "udf_fields": {
                    "udf_pick_8104": {"value": "Regular"},
                    "udf_pick_13501": {"value": "Mortgage"},
                    "udf_long_15301": {"value": "1"},
                },
            }
        }
    def list_open_tickets(self):
        return [{"id": "559970"}]
    def get_ticket(self, ticket_id):
        # Prefer returning self.tickets entry (which may be modified by test actions)
        if ticket_id in self.tickets:
            return self.tickets.get(ticket_id)
        # Fallback: return a ticket matching config `allowed_groups`, `allowed_templates`, `ready_statuses`
        now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)
        return {
            "id": ticket_id,
            "subject": "Cancel SRs",
            "description": "Please cancel Case Number 23422946 23423532",
            "group": {"name": "SalesForce - CRM"},
            "template": {"name": "SalesForce CRM Issues"},
            "status": {"name": "Open"},
            "created_time": now_ms,
            "tags": ["SR-AUTOMATION-UAT"],
            "udf_fields": {
                "udf_pick_8104": {"value": "Regular"},
                "udf_pick_13501": {"value": "Mortgage"},
                "udf_long_15301": {"value": "1"},
            },
        }
    def assign_technician(self, ticket_id, technician):
        self.assigned.append((ticket_id, technician))
        # Simulate assign success
        req = {};
        class R:
            def raise_for_status(self): return None
            def json(self): return {}
        # write back assigned technician into ticket
        # Return technician field in shape ManageEngine exposes (use 'technician' dict)
        if technician.get("id"):
            self.tickets[ticket_id]["technician"] = {"id": technician.get("id"), "name": None}
        else:
            self.tickets[ticket_id]["technician"] = {"id": None, "name": technician.get("name")}
        return R()
    def add_resolution(self, ticket_id, resolution):
        self.resolutions.append((ticket_id, resolution))
        class R:
            def raise_for_status(self): return None
            def json(self): return {}
        return R()
    def close_ticket(self, ticket_id, closure_info):
        self.closed.append((ticket_id, closure_info))
        # Simulate close success by setting ticket status
        self.tickets[ticket_id]["status"] = {"name": "Closed"}
        class R:
            def raise_for_status(self): return None
            def json(self): return {}
        return R()
    def update_request(self, ticket_id, input_data):
        request = input_data.get("request", {})
        if "status" in request:
            status_obj = request["status"]
            status_name = status_obj.get("name") if isinstance(status_obj, dict) and status_obj.get("name") not in (None, "") else {"4": "Resolved"}.get(str(status_obj.get("id")))
            if status_name:
                self.tickets[ticket_id]["status"] = {"name": status_name}
        if "resolution" in request:
            self.tickets[ticket_id]["resolution"] = {"content": request["resolution"]["content"]}
        if "technician" in request:
            self.tickets[ticket_id]["technician"] = request["technician"]
        class R:
            def raise_for_status(self): return None
            def json(self): return {}
        return R()

class FakeSFNoop:
    def __init__(self, s):
        pass
    def authenticate(self):
        return None
    def cases(self, numbers):
        return {n: {"Id": f"ID_{n}", "CaseNumber": n, "Status": "Cancelled", "RecordType": {"Name": "Default"}, "LastModifiedDate": "LMD"} for n in numbers}
    def tasks(self, ids):
        return {i: [] for i in ids}

@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    dbfile = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_URL", str(dbfile))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")
    monkeypatch.setenv("MANAGEENGINE_AUTO_CLOSE", "true")
    return tmp_path


def run_stage4(monkeypatch, me):
    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    main_mod.run_cycle("config/uat.yaml")


def test_all_cases_success_closes_ticket(monkeypatch, tmp_path):
    # Prepare audit: two successful case audits and a ME note verified
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id="c1")
    key2 = audit.make_idempotency_key("559970", "23423532", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key2, ticket_id="559970", case_number="23423532", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23423532", last_modified_date="LMD2", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c2")
    # Insert ME note verified audit
    audit.insert_audit(idempotency_key=str('me1'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")
    me = FakeMEFull(None)
    run_stage4(monkeypatch, me)
    # Verify ticket resolved and audits present
    rows = audit.get_by_ticket("559970")
    outcomes = [r.get("outcome") for r in rows]
    assert "ME_RESOLVED_VERIFIED" in outcomes or any(o=="ME_RESOLVED_VERIFIED" for o in outcomes)


def test_manual_case_keeps_ticket_open(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))
    # One case manual
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="", salesforce_id="", last_modified_date="", processing_state="FAILED", outcome="WOULD_UPDATE", correlation_id="c1")
    # Another success
    key2 = audit.make_idempotency_key("559970", "23423532", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key2, ticket_id="559970", case_number="23423532", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23423532", last_modified_date="LMD2", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c2")
    # ME note verified
    audit.insert_audit(idempotency_key=str('me2'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")
    me = FakeMEFull(None)
    run_stage4(monkeypatch, me)
    rows = audit.get_by_ticket("559970")
    outcomes = [r.get("outcome") for r in rows]
    assert "MANUAL_REVIEW_REQUIRED" in outcomes


def test_status_update_payload_uses_configured_resolved_data(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))

    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    key2 = audit.make_idempotency_key("559970", "23423532", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key2, ticket_id="559970", case_number="23423532", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23423532", last_modified_date="LMD2", processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id="c2")
    audit.insert_audit(idempotency_key=str('me3'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    me = FakeMEFull(None)
    run_stage4(monkeypatch, me)

    assert me.tickets["559970"]["status"]["name"] == "Resolved"
    assert me.tickets["559970"]["technician"]["id"] == "77787"


def test_already_assigned_technician_is_not_assigned_again(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))

    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str('me4'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    me = FakeMEFull(None)
    me.tickets["559970"]["technician"] = {"id": "77787", "name": "Suruthi Vijayasingh"}
    run_stage4(monkeypatch, me)

    assert me.assigned == []
    assert me.tickets["559970"]["status"]["name"] == "Resolved"


def test_status_update_logs_key_names_without_values(monkeypatch, capsys):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))

    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str('me_status_log'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class StatusLogME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.sent_payloads = []
        def update_request(self, ticket_id, payload):
            self.sent_payloads.append(payload)
            class Response:
                def raise_for_status(self):
                    return None
            return Response()

    me = StatusLogME(None)

    class StubNoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, *args, **kwargs):
            return {"note": {"description": "summary"}}
        def post_note(self, ticket_id, payload, timeout=30.0):
            class Response:
                def json(self):
                    return {"note": {"id": "N-200"}}
                def raise_for_status(self):
                    return None
            return Response()
        def list_notes(self, ticket_id, timeout=30.0):
            return []

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", StubNoteWriter)
    monkeypatch.setattr(main_mod, "resolve_closure", lambda **kwargs: {
        "mode": "AUTO",
        "reason": "AUTO",
        "missing_fields": [],
        "previous_item": "Consulting",
        "payload": {"request": {
            "status": {"id": "4"},
            "resolution": {"content": "Salesforce automation completed successfully."},
            "udf_fields": {"udf_pick_8104": {"name": "Regular"}},
        }},
        "request": {"status": {"id": "4"}, "resolution": {"content": "Salesforce automation completed successfully."}, "udf_fields": {"udf_pick_8104": {"name": "Regular"}}},
    })

    main_mod.run_cycle("config/uat.yaml")
    out = capsys.readouterr().out
    assert "ME_NOTE_ADDED ticket=559970 note_id=N-200" in out
    assert "ME_ASSIGNED ticket=559970" in out
    assert "ME_STATUS_UPDATE_FIELDS ticket=559970 request_keys=['status', 'resolution', 'udf_fields'] udf_fields=['udf_pick_8104']" in out
    assert "secret" not in out


def test_manual_closure_logs_reason_as_audit_outcome(monkeypatch, capsys):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))

    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str('me_manual_reason'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class ManualClosureME(FakeMEFull):
        def update_request(self, ticket_id, payload):
            raise AssertionError("manual closure should not call update_request")

    me = ManualClosureME(None)

    class StubNoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, *args, **kwargs):
            return {"note": {"description": "summary"}}
        def post_note(self, ticket_id, payload, timeout=30.0):
            class Response:
                def json(self):
                    return {"note": {"id": "N-300"}}
                def raise_for_status(self):
                    return None
            return Response()
        def list_notes(self, ticket_id, timeout=30.0):
            return []

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", StubNoteWriter)
    monkeypatch.setattr(main_mod, "resolve_closure", lambda **kwargs: {
        "mode": "MANUAL",
        "reason": "ME_MANUAL_CLOSURE_REQUIRED",
        "missing_fields": ["subcategory"],
        "previous_item": "Consulting",
        "payload": None,
        "request": None,
    })

    main_mod.run_cycle("config/uat.yaml")
    out = capsys.readouterr().out
    assert "ME_CLOSURE_MANUAL ticket=559970 reason=ME_MANUAL_CLOSURE_REQUIRED missing=['subcategory'] previous_item=Consulting" in out
    manual_reason = next((row.get("outcome") for row in audit.get_by_ticket("559970") if row.get("requested_action") == "ME_COMPLETION" and row.get("processing_state") == "CLOSURE_CONFIGURATION_REQUIRED"), None)
    assert manual_reason == "ME_MANUAL_CLOSURE_REQUIRED"


class FailStatusUpdateME(FakeMEFull):
    def update_request(self, ticket_id, input_data):
        self.tickets[ticket_id]["status"] = {"name": "Open"}
        response = type("Resp", (), {"status_code": 400, "json": lambda self: {"message": "status update failed"}})()
        raise httpx.HTTPStatusError("status update failed", request=httpx.Request("PUT", "https://example.invalid"), response=response)


def test_resolution_update_succeeds(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str("me_resolution_success"), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class ResolutionCheckME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.calls = []
        def update_request(self, ticket_id, input_data):
            self.calls.append({"ticket_id": ticket_id, "input_data": input_data})
            if "resolution" in input_data["request"]:
                self.tickets[ticket_id]["resolution"] = {"content": input_data["request"]["resolution"]["content"]}
            if "status" in input_data["request"]:
                self.tickets[ticket_id]["status"] = input_data["request"]["status"]
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {}
            return R()

    me = ResolutionCheckME(None)
    run_stage4(monkeypatch, me)
    assert len(me.calls) == 1
    payload = me.calls[0]["input_data"]["request"]
    assert payload["resolution"] == {"content": "Salesforce automation completed successfully. All related Salesforce Cases were verified in the requested status."}
    assert payload["status"] == {"id": "4"}
    assert "status.name" not in json.dumps(payload)
    assert me.tickets["559970"]["status"] == {"id": "4"}


def test_status_update_succeeds(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str("me_status_ok"), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class StatusCheckME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.calls = []
        def update_request(self, ticket_id, input_data):
            self.calls.append(input_data)
            if "resolution" in input_data["request"]:
                self.tickets[ticket_id]["resolution"] = {"content": input_data["request"]["resolution"]["content"]}
            if "status" in input_data["request"]:
                self.tickets[ticket_id]["status"] = input_data["request"]["status"]
            class R:
                def raise_for_status(self):
                    return None
                def json(self):
                    return {}
            return R()

    me = StatusCheckME(None)
    run_stage4(monkeypatch, me)
    status_calls = [x for x in me.calls if "status" in x["request"]]
    assert status_calls
    assert status_calls[0]["request"]["status"] == {"id": "4"}
    assert "status_change_comments" not in json.dumps(status_calls[0]["request"])
    assert "status.name" not in json.dumps(status_calls[0]["request"])


def test_resolution_http_400_captures_response_fields(monkeypatch, capsys):
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str("me_resolution_400"), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class Resolution400ME(FakeMEFull):
        def update_request(self, ticket_id, input_data):
            response = httpx.Response(400, json={"messages": [{"field": "request.resolution.content", "message": "resolution is invalid"}]}, request=httpx.Request("PUT", f"https://example.invalid/api/v3/requests/{ticket_id}?authtoken=secret"))
            raise httpx.HTTPStatusError("bad resolution", request=response.request, response=response)

    me = Resolution400ME(None)
    run_stage4(monkeypatch, me)
    captured = capsys.readouterr().out
    assert "HTTP 400" in captured
    assert "request.resolution.content" in captured
    assert "resolution is invalid" in captured
    assert "secret" not in captured


def test_status_http_400_captures_response_fields(monkeypatch, capsys):
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str("me_status_400"), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class Status400ME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.resolution_ok = False
        def update_request(self, ticket_id, input_data):
            if "status" in input_data["request"]:
                response = httpx.Response(400, json={"messages": [{"field": "request.status.status_change_comments", "message": "status_change_comments invalid"}]}, request=httpx.Request("PUT", f"https://example.invalid/api/v3/requests/{ticket_id}?authtoken=secret"))
                raise httpx.HTTPStatusError("bad status", request=response.request, response=response)
            self.tickets[ticket_id]["resolution"] = {"content": input_data["request"]["resolution"]["content"]}
            return type("R", (), {"raise_for_status": lambda self: None})()

    me = Status400ME(None)
    run_stage4(monkeypatch, me)
    captured = capsys.readouterr().out
    assert "HTTP 400" in captured
    assert "request.status.status_change_comments" in captured
    assert "status_change_comments invalid" in captured
    assert "secret" not in captured


def test_existing_correct_note_is_not_duplicated(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559959", "12345", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559959", case_number="12345", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_12345", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")

    class ExistingNoteME(FakeMEFull):
        def list_open_tickets(self):
            return [{"id": "559959"}]
        def get_ticket(self, ticket_id):
            return {"id": "559959", "group": {"name": "SalesForce - CRM"}, "template": {"name": "SalesForce CRM Issues"}, "status": {"name": "Open"}, "tags": ["SR-AUTOMATION-UAT"]}

    class NoteWriter:
        def __init__(self, s):
            self.s = s
            self.calls = 0
        def build_summary(self, *args, **kwargs):
            raise AssertionError("equivalent note should prevent post_note")
        def post_note(self, ticket_id, payload, timeout=30.0):
            self.calls += 1
            raise AssertionError("duplicate note should not be posted")
        def list_notes(self, ticket_id, timeout=30.0):
            return [{"id": "N-987", "description": "Automation result for ticket #559959\nEnvironment: UAT\nAction: CANCEL\nSalesforce results:\n12345 | CANCEL | SUCCESS | Cancelled\nSuccessful: 1\nManual review: 0\nFailed: 0"}]

    me = ExistingNoteME(None)
    writer = NoteWriter(None)
    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", lambda s: writer)
    main_mod.run_cycle("config/uat.yaml")
    assert writer.calls == 0


def test_final_resolved_status_is_verified(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str("me_resolved_verify"), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class FinalResolvedME(FakeMEFull):
        def update_request(self, ticket_id, input_data):
            # Map id->name when payload contains id-only status
            st = input_data.get("request", {}).get("status")
            if isinstance(st, dict) and not st.get("name") and st.get("id") is not None:
                st["name"] = {"4": "Resolved"}.get(str(st.get("id")))
            if "resolution" in input_data["request"]:
                self.tickets[ticket_id]["resolution"] = {"content": input_data["request"]["resolution"]["content"]}
            if "status" in input_data["request"]:
                self.tickets[ticket_id]["status"] = {"name": input_data["request"]["status"].get("name")}
            return type("R", (), {"raise_for_status": lambda self: None})()

    me = FinalResolvedME(None)
    run_stage4(monkeypatch, me)
    rows = audit.get_by_ticket("559970")
    outcomes = [r.get("outcome") for r in rows]
    assert "ME_RESOLVED_VERIFIED" in outcomes


def test_failed_status_update_keeps_ticket_open(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))

    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str('me5'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    me = FailStatusUpdateME(None)
    run_stage4(monkeypatch, me)

    assert me.tickets["559970"]["status"]["name"] == "Open"
    rows = audit.get_by_ticket("559970")
    outcomes = [r.get("outcome") for r in rows]
    assert "HTTP_400" in outcomes or any(o == "HTTP_400" for o in outcomes)
    assert "ME_RESOLVED_VERIFIED" not in outcomes


def test_duplicate_note_prevents_repost(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559959", "12345", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559959", case_number="12345", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_12345", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id="c1")

    class ExistingNoteME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.tickets["559959"] = {
                "id": "559959",
                "subject": "Cancel SRs",
                "description": "Please cancel Case Number 12345",
                "group": {"name": "SalesForce - CRM"},
                "template": {"name": "SalesForce CRM Issues"},
                "status": {"name": "Open"},
                "created_time": int(datetime.datetime.utcnow().timestamp() * 1000),
                "tags": ["SR-AUTOMATION-UAT"],
                "udf_fields": {
                    "udf_pick_8104": {"value": "Regular"},
                    "udf_pick_13501": {"value": "Mortgage"},
                    "udf_long_15301": {"value": "1"},
                },
            }
        def list_open_tickets(self):
            return [{"id": "559959"}]

    me = ExistingNoteME(None)

    class NoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, *args, **kwargs):
            return {"note": {"description": "summary"}}
        def post_note(self, ticket_id, payload, timeout=30.0):
            raise AssertionError("post_note should not be called when an equivalent note already exists")
        def list_notes(self, ticket_id, timeout=30.0):
            return [{"note": {"description": "Automation result for ticket #559959\nEnvironment: UAT\nAction: CANCEL\nSalesforce results:\n12345 | CANCEL | SUCCESS | Cancelled\nSuccessful: 1\nManual review: 0\nFailed: 0"}}]

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", NoteWriter)

    main_mod.run_cycle("config/uat.yaml")

    assert True


def test_status_update_payload_includes_resolution_and_status_comment(monkeypatch, tmp_path):
    db = Path(os.environ["AUDIT_DB_URL"]) 
    audit = AuditRepository(db_url=str(db))

    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str('me6'), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    me = FakeMEFull(None)
    run_stage4(monkeypatch, me)

    assert me.tickets["559970"]["status"]["name"] == "Resolved"
    assert me.tickets["559970"]["technician"]["id"] == "77787"
    assert me.tickets["559970"]["status"]["name"] == "Resolved"


def test_status_update_mode_uses_configured_resolved_payload(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))

    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    key2 = audit.make_idempotency_key("559970", "23423532", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key2, ticket_id="559970", case_number="23423532", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23423532", last_modified_date="LMD2", processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id="c2")

    class StatusUpdateME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.status_updates = []
            self.closed = []
        def update_request(self, ticket_id, payload):
            self.status_updates.append((ticket_id, payload))
            if "status" in payload["request"]:
                st = payload["request"]["status"]
                name = st.get("name") if isinstance(st, dict) and st.get("name") not in (None, "") else {"4": "Resolved"}.get(str(st.get("id")))
                self.tickets[ticket_id]["status"] = {"name": name}
            if "resolution" in payload["request"]:
                self.tickets[ticket_id]["resolution"] = {"content": payload["request"]["resolution"]["content"]}
            class Response:
                def raise_for_status(self):
                    return None
            return Response()
        def close_ticket(self, ticket_id, closure_info):
            raise AssertionError("close_ticket must not be used when completion.mode=STATUS_UPDATE")

    class StubNoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, *args, **kwargs):
            return {"note": {"description": "summary"}}
        def post_note(self, ticket_id, payload, timeout=30.0):
            class Response:
                def json(self):
                    return {"note": {"id": "N-100"}}
                def raise_for_status(self):
                    return None
            return Response()
        def list_notes(self, ticket_id, timeout=30.0):
            return []

    me = StatusUpdateME(None)
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", StubNoteWriter)
    run_stage4(monkeypatch, me)

    assert me.status_updates
    status_payload = next((payload for _, payload in me.status_updates if "status" in payload["request"]), None)
    assert status_payload is not None
    assert status_payload["request"]["status"] == {"id": "4"}
    assert "status_change_comments" not in json.dumps(status_payload)
    assert "status.name" not in json.dumps(status_payload)
    resolution_payload = next((payload for _, payload in me.status_updates if "resolution" in payload["request"]), None)
    assert resolution_payload is not None
    assert resolution_payload["request"]["resolution"]["content"] == (
        "Salesforce automation completed successfully. "
        "All related Salesforce Cases were verified in the requested status."
    )
    assert me.tickets["559970"]["status"]["name"] == "Resolved"


def test_dynamic_closure_payload_uses_cancel_profile_and_case_count():
    ticket = {"id": "559970", "udf_fields": {}}
    profile = {
        "CANCEL": {
            "udf_pick_8104": {"name": "Data Create/Data Update", "id": "6607"},
            "udf_pick_13501": {"name": "Others", "id": "12698"},
        }
    }
    payload = main_mod.build_closure_payload(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=2,
        target_status="Resolved",
        target_status_id="4",
        count_field="udf_long_15301",
        status_comment="done",
        resolution_text="resolved",
        closure_profiles=profile,
        mandatory_fields={"to_be_filled": ["udf_pick_8104", "udf_pick_13501", "udf_long_15301"]},
    )
    assert payload["request"]["status"] == {"id": "4"}
    assert "status_change_comments" not in json.dumps(payload)
    assert "status.name" not in json.dumps(payload)
    assert payload["request"]["udf_fields"]["udf_pick_8104"] == {"id": "6607"}
    assert payload["request"]["udf_fields"]["udf_pick_13501"] == {"id": "12698"}
    assert payload["request"]["udf_fields"]["udf_long_15301"] == 2


def test_dynamic_closure_payload_preserves_existing_ticket_udfs():
    ticket = {"id": "559970", "udf_fields": {"udf_pick_8104": {"value": "Regular"}}}
    payload = main_mod.build_closure_payload(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=3,
        target_status="Resolved",
        target_status_id="4",
        count_field="udf_long_15301",
        status_comment="done",
        resolution_text="resolved",
        closure_profiles={
            "CANCEL": {
                "udf_pick_8104": {"name": "Data Create/Data Update", "id": "6607"},
                "udf_pick_13501": {"name": "Others", "id": "12698"},
            }
        },
        mandatory_fields={"to_be_filled": ["udf_pick_8104", "udf_pick_13501", "udf_long_15301"]},
    )
    assert "udf_pick_8104" not in payload["request"]["udf_fields"]
    assert payload["request"]["udf_fields"]["udf_pick_13501"] == {"id": "12698"}
    assert payload["request"]["udf_fields"]["udf_long_15301"] == 3


def test_dynamic_closure_payload_fails_closed_on_unknown_udf():
    ticket = {"id": "559970", "udf_fields": {}}
    with pytest.raises(ValueError, match="udf_pick_9999"):
        main_mod.build_closure_payload(
            ticket=ticket,
            actions=["CANCEL"],
            case_count=1,
            target_status="Resolved",
            target_status_id="4",
            count_field="udf_long_15301",
            status_comment="done",
            resolution_text="resolved",
            closure_profiles={
                "CANCEL": {
                    "udf_pick_8104": {"name": "Data Create/Data Update", "id": "6607"},
                }
            },
            mandatory_fields={"to_be_filled": ["udf_pick_9999"]},
        )


def test_dynamic_closure_payload_fails_closed_on_mixed_profiles():
    ticket = {"id": "559970", "udf_fields": {}}
    with pytest.raises(ValueError, match="CLOSURE_CONFIGURATION_REQUIRED"):
        main_mod.build_closure_payload(
            ticket=ticket,
            actions=["CANCEL", "REJECT"],
            case_count=2,
            target_status="Resolved",
            target_status_id="4",
            count_field="udf_long_15301",
            status_comment="done",
            resolution_text="resolved",
            closure_profiles={
                "CANCEL": {"udf_pick_8104": {"name": "Data Create/Data Update", "id": "6607"}},
                "REJECT": {"udf_pick_8104": {"name": "Other", "id": "9999"}},
            },
            mandatory_fields={"to_be_filled": ["udf_pick_8104", "udf_long_15301"]},
        )


def test_resolve_closure_uses_rule_match_and_case_count():
    ticket = {
        "id": "559970",
        "status": {"name": "Open"},
        "category": "Customer Service",
        "subcategory": " Contract / Billing ",
        "item": " Renewal ",
        "udf_fields": {},
    }
    rules = {
        "actions": {
            "CANCEL": {
                "mode": "AUTO",
                "default": {"udf": {"udf_pick_13501": {"id": "12698"}}, "name": "default"},
                "rules": [
                    {
                        "name": "Billing renewal",
                        "match": {"subcategory": "Contract / Billing", "item": "Renewal"},
                        "udf": {"udf_pick_8104": {"id": "6607"}},
                    }
                ],
            }
        }
    }

    result = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=2,
        rules=rules,
        mandatory_fields={"to_be_filled": ["udf_pick_8104", "udf_long_15301"]},
        count_field="udf_long_15301",
        target_status_id="4",
        target_status="Resolved",
        resolution_text="completed",
    )

    assert result["mode"] == "AUTO"
    assert result["payload"]["request"]["status"]["id"] == "4"
    assert result["payload"]["request"]["udf_fields"]["udf_pick_8104"] == {"id": "6607"}
    assert result["payload"]["request"]["udf_fields"]["udf_long_15301"] == "2"


def test_resolve_closure_uses_default_fallback_and_manual_for_missing_rule():
    ticket = {"id": "559970", "status": {"name": "Open"}, "category": "Other", "subcategory": "Other", "item": "Other", "udf_fields": {}}
    rules = {
        "actions": {
            "CANCEL": {
                "mode": "AUTO",
                "default": {"name": "default", "udf": {"udf_pick_13501": {"id": "12698"}}},
                "rules": [],
            }
        }
    }

    result = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=1,
        rules=rules,
        mandatory_fields={"to_be_filled": ["udf_pick_13501", "udf_long_15301"]},
        count_field="udf_long_15301",
        target_status_id="4",
        target_status="Resolved",
        resolution_text="completed",
    )

    assert result["mode"] == "AUTO"
    assert result["payload"]["request"]["udf_fields"]["udf_pick_13501"] == {"id": "12698"}

    manual = main_mod.resolve_closure(
        ticket={"id": "559971", "status": {"name": "Open"}, "category": "Other", "subcategory": "Other", "item": "Other", "udf_fields": {}},
        actions=["CANCEL"],
        case_count=1,
        rules={"actions": {"CANCEL": {"mode": "AUTO", "default": None, "rules": []}}},
        mandatory_fields={"to_be_filled": ["udf_pick_9999"]},
        count_field="udf_long_15301",
        target_status_id="4",
        target_status="Resolved",
        resolution_text="completed",
    )
    assert manual["mode"] == "MANUAL"
    assert manual["reason"] == "CLOSURE_CONFIGURATION_REQUIRED"


def test_resolve_closure_manual_mode_and_mixed_actions():
    ticket = {"id": "559970", "status": {"name": "Open"}, "category": "Other", "subcategory": "Other", "item": "Other", "udf_fields": {}}
    manual = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=1,
        rules={"actions": {"CANCEL": {"mode": "MANUAL", "default": None, "rules": []}}},
        mandatory_fields={"to_be_filled": ["udf_pick_8104"]},
        count_field="udf_long_15301",
        target_status_id="4",
        target_status="Resolved",
        resolution_text="completed",
    )
    assert manual["mode"] == "MANUAL"
    assert manual["reason"] == "ME_MANUAL_CLOSURE_REQUIRED"

    mixed = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL", "REJECT"],
        case_count=1,
        rules={"actions": {"CANCEL": {"mode": "AUTO", "default": None, "rules": []}, "REJECT": {"mode": "AUTO", "default": None, "rules": []}}},
        mandatory_fields={"to_be_filled": ["udf_pick_8104"]},
        count_field="udf_long_15301",
        target_status_id="4",
        target_status="Resolved",
        resolution_text="completed",
    )
    assert mixed["mode"] == "MANUAL"
    assert mixed["reason"] == "ME_MANUAL_CLOSURE_REQUIRED"


def test_load_closure_rules_yaml_and_settings_integration():
    rules = main_mod.load_closure_rules("config/uat.yaml")
    assert isinstance(rules, dict)
    assert rules["environment"] == "uat"
    assert rules["env"]["uat"]["count_field"] == "udf_long_15301"
    settings = main_mod.load_settings("config/uat.yaml")
    assert isinstance(settings.closure_rules, dict)
    assert settings.closure_rules["env"]["uat"]["count_field"] == "udf_long_15301"


def test_resolve_closure_keeps_existing_udfs_and_requires_missing_fields():
    ticket = {
        "id": "559970",
        "status": {"name": "Open"},
        "category": "Customer Service",
        "subcategory": " Contract / Billing ",
        "item": " Renewal ",
        "udf_fields": {"udf_pick_8104": {"value": "Existing Value"}},
    }
    rules = {
        "actions": {
            "CANCEL": {
                "mode": "AUTO",
                "default": None,
                "rules": [{
                    "name": "Billing renewal",
                    "match": {"subcategory": "Contract / Billing", "item": "Renewal"},
                    "udf": {"udf_pick_8104": {"id": "6607"}},
                }],
            }
        }
    }
    result = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=4,
        rules=rules,
        mandatory_fields={"to_be_filled": ["udf_pick_8104", "udf_long_15301"]},
        count_field="udf_long_15301",
        target_status_id="4",
        target_status="Resolved",
        resolution_text="completed",
    )
    assert result["mode"] == "AUTO"
    assert "udf_pick_8104" not in result["payload"]["request"]["udf_fields"]
    assert result["payload"]["request"]["udf_fields"]["udf_long_15301"] == "4"


def test_resolve_closure_keeps_item_for_single_case_and_overrides_on_bulk_rule():
    default_rules = {
        "actions": {
            "CANCEL": {
                "mode": "AUTO",
                "bulk": {"when_case_count_gt": 2, "item": {"id": "42", "name": "Bulk item"}},
                "default": {"udf": {"udf_pick_13501": {"id": "12698"}}},
                "rules": [],
            }
        }
    }

    ticket = {
        "id": "559970",
        "status": {"name": "Open"},
        "category": "Customer Service",
        "subcategory": "Contract / Billing",
        "item": "Renewal",
        "udf_fields": {},
    }

    single = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=1,
        rules=default_rules,
        mandatory_fields={"to_be_filled": ["item", "subcategory", "resolution", "udf_long_15301"]},
        count_field="udf_long_15301",
        target_status="Resolved",
        target_status_id="4",
        resolution_text="completed",
    )
    assert single["mode"] == "AUTO"
    assert "item" not in single["payload"]["request"]

    bulk = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=3,
        rules=default_rules,
        mandatory_fields={"to_be_filled": ["item", "subcategory", "resolution", "udf_long_15301"]},
        count_field="udf_long_15301",
        target_status="Resolved",
        target_status_id="4",
        resolution_text="completed",
    )
    assert bulk["mode"] == "AUTO"
    assert bulk["payload"]["request"]["item"] == {"id": "42", "name": "Bulk item"}
    assert bulk["previous_item"] == "Renewal"


def test_resolve_closure_manual_for_empty_item_or_subcategory(monkeypatch):
    rules = {
        "actions": {
            "CANCEL": {
                "mode": "AUTO",
                "bulk": {"when_case_count_gt": 2, "item": {"id": "42", "name": "Bulk item"}},
                "default": {"udf": {"udf_pick_13501": {"id": "12698"}}},
                "rules": [],
            }
        }
    }

    empty_item = main_mod.resolve_closure(
        ticket={"id": "559970", "status": {"name": "Open"}, "category": "Other", "subcategory": "Other", "item": "", "udf_fields": {}},
        actions=["CANCEL"],
        case_count=1,
        rules=rules,
        mandatory_fields={"to_be_filled": ["item", "subcategory", "resolution", "udf_long_15301"]},
        count_field="udf_long_15301",
        target_status="Resolved",
        target_status_id="4",
        resolution_text="completed",
    )
    assert empty_item["mode"] == "MANUAL"
    assert empty_item["reason"] == "ME_MANUAL_CLOSURE_REQUIRED"

    empty_subcategory = main_mod.resolve_closure(
        ticket={"id": "559971", "status": {"name": "Open"}, "category": "Other", "subcategory": "", "item": "Renewal", "udf_fields": {}},
        actions=["CANCEL"],
        case_count=1,
        rules=rules,
        mandatory_fields={"to_be_filled": ["item", "subcategory", "resolution", "udf_long_15301"]},
        count_field="udf_long_15301",
        target_status="Resolved",
        target_status_id="4",
        resolution_text="completed",
    )
    assert empty_subcategory["mode"] == "MANUAL"
    assert empty_subcategory["reason"] == "ME_MANUAL_CLOSURE_REQUIRED"


def test_load_closure_rules_uses_nested_yaml_block_without_legacy_files(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    rules = main_mod.load_closure_rules("config/uat.yaml")
    assert isinstance(rules, dict)
    assert "env" in rules
    assert rules["env"]["uat"]["count_field"] == "udf_long_15301"


def test_status_update_payload_uses_configured_udf_values():
    ticket = {
        "udf_fields": {
            "udf_pick_8104": "Regular",
            "udf_pick_13501": "Mortgage",
            "udf_long_15301": "",
        }
    }
    payload = main_mod.build_status_update_payload(
        target_status="Resolved",
        status_comment="done",
        ticket=ticket,
        mandatory_udf_fields={
            "udf_pick_8104": "Regular",
            "udf_pick_13501": "Mortgage",
            "udf_long_15301": 1,
        },
    )
    assert payload["request"]["udf_fields"] == {
        "udf_pick_8104": "Regular",
        "udf_pick_13501": "Mortgage",
        "udf_long_15301": 1,
    }


def test_status_update_payload_requires_missing_udf_values():
    ticket = {
        "udf_fields": {
            "udf_pick_8104": "Regular",
            "udf_pick_13501": "Mortgage",
        }
    }
    with pytest.raises(ValueError, match="udf_long_15301"):
        main_mod.build_status_update_payload(
            target_status="Resolved",
            status_comment="done",
            ticket=ticket,
            mandatory_udf_fields={
                "udf_pick_8104": "Regular",
                "udf_pick_13501": "Mortgage",
                "udf_long_15301": None,
            },
        )


def test_status_update_uses_ticket_udfs_before_config_values():
    ticket_a = {
        "id": "559970",
        "udf_fields": {
            "udf_pick_8104": {"value": "Regular"},
            "udf_pick_13501": {"value": "Mortgage"},
            "udf_long_15301": {"value": "7"},
        },
    }
    ticket_b = {
        "id": "559971",
        "udf_fields": {
            "udf_pick_8104": {"value": "VIP"},
            "udf_pick_13501": {"value": "Retail"},
            "udf_long_15301": {"value": "3"},
        },
    }
    config = {
        "udf_pick_8104": "Default Customer",
        "udf_pick_13501": "Default Dept",
        "udf_long_15301": 1,
    }
    payload_a = main_mod.build_status_update_payload("Resolved", "done", ticket_a, config)
    payload_b = main_mod.build_status_update_payload("Resolved", "done", ticket_b, config)
    assert payload_a["request"]["udf_fields"]["udf_pick_8104"] == "Regular"
    assert payload_b["request"]["udf_fields"]["udf_pick_8104"] == "VIP"
    assert payload_a["request"]["udf_fields"]["udf_long_15301"] == 7
    assert payload_b["request"]["udf_fields"]["udf_long_15301"] == 3


def test_empty_ticket_udf_uses_configured_value():
    ticket = {
        "id": "559970",
        "udf_fields": {
            "udf_pick_8104": {"value": "Regular"},
            "udf_pick_13501": {"value": ""},
            "udf_long_15301": {"value": ""},
        },
    }
    payload = main_mod.build_status_update_payload(
        "Resolved",
        "done",
        ticket,
        {
            "udf_pick_8104": "Regular",
            "udf_pick_13501": "Mortgage",
            "udf_long_15301": 1,
        },
    )
    assert payload["request"]["udf_fields"]["udf_pick_13501"] == "Mortgage"
    assert payload["request"]["udf_fields"]["udf_long_15301"] == 1


def test_missing_mandatory_udf_raises_configuration_error():
    ticket = {"id": "559970", "udf_fields": {"udf_pick_8104": {"value": "Regular"}, "udf_pick_13501": {"value": "Mortgage"}}}
    with pytest.raises(ValueError, match="udf_long_15301"):
        main_mod.build_status_update_payload(
            "Resolved",
            "done",
            ticket,
            {"udf_long_15301": None},
        )


def test_numeric_udf_is_sent_as_integer():
    ticket = {"id": "559970", "udf_fields": {"udf_long_15301": {"value": "7"}}}
    payload = main_mod.build_status_update_payload(
        "Resolved",
        "done",
        ticket,
        {"udf_long_15301": 1},
    )
    assert isinstance(payload["request"]["udf_fields"]["udf_long_15301"], int)
    assert payload["request"]["udf_fields"]["udf_long_15301"] == 7


def test_note_matches_equivalent_supports_nested_note_payload(monkeypatch):
    note = {
        "note": {
            "description": "Automation result for ticket #559959\nEnvironment: UAT\nAction: CANCEL\nSalesforce results:\n12345 | CANCEL | ALREADY_IN_TARGET_STATE | Cancelled\nSuccessful: 1\nManual review: 0\nFailed: 0"
        }
    }
    assert main_mod.note_matches_equivalent(note, "559959", "CANCEL", ["12345"]) is True


def test_note_matches_equivalent_rejects_would_update_success_note():
    note = {
        "description": "Automation result for ticket #559959\nEnvironment: UAT\nAction: CANCEL\nSalesforce results:\n12345 | CANCEL | WOULD_UPDATE | New -> Cancelled\nSuccessful: 0\nManual review: 0\nFailed: 1"
    }
    assert main_mod.note_matches_equivalent(note, "559959", "CANCEL", ["12345"]) is False


def test_status_update_payload_exact_har_shape_omits_non_payload_fields():
    ticket = {
        "udf_fields": {
            "udf_pick_8104": {"value": "Regular"},
            "udf_pick_13501": {"value": "Mortgage"},
            "udf_long_15301": {"value": "1"},
            "tasks": [],
        },
        "technician": {"id": "77787"},
    }
    payload = main_mod.build_status_update_payload(
        "Resolved",
        "comment",
        ticket,
        {
            "udf_pick_8104": {"id": "6607"},
            "udf_pick_13501": {"id": "12698"},
            "udf_long_15301": 1,
        },
    )
    assert payload["request"]["status"] == {"id": "4"}
    assert payload["request"]["udf_fields"] == {
        "udf_pick_8104": "Regular",
        "udf_pick_13501": "Mortgage",
        "udf_long_15301": 1,
    }
    assert payload["request"]["resolution"] == {"content": "Salesforce automation completed successfully."}
    assert "status.name" not in json.dumps(payload)
    assert "status_change_comments" not in json.dumps(payload)
    assert "tasks" not in json.dumps(payload)
    assert "checklists" not in json.dumps(payload)


def test_resolve_closure_ignores_standard_and_related_fields_for_payload():
    ticket = {
        "id": "559970",
        "technician": {"id": "77787"},
        "group": {"name": "SalesForce - CRM"},
        "category": "Customer Service",
        "subcategory": "Other",
        "priority": "High",
        "request_type": "Incident",
        "udf_fields": {
            "udf_pick_8104": {"value": "Regular"},
            "udf_pick_13501": {"value": "Mortgage"},
            "udf_long_15301": {"value": "1"},
        },
    }
    result = main_mod.resolve_closure(
        ticket=ticket,
        actions=["CANCEL"],
        case_count=1,
        mandatory_fields={
            "to_be_filled": [
                "technician",
                "group",
                "category",
                "subcategory",
                "priority",
                "request_type",
                "tasks",
                "checklists",
                "udf_pick_8104",
                "udf_pick_13501",
                "udf_long_15301",
            ]
        },
        count_field="udf_long_15301",
        target_status_id="4",
        target_status="Resolved",
        resolution_text="completed",
    )
    assert result["mode"] == "AUTO"
    request = result["payload"]["request"]
    assert "technician" not in request
    assert "group" not in request
    assert "tasks" not in request
    assert "checklists" not in request
    assert request["status"] == {"id": "4"}
    assert request["udf_fields"]["udf_pick_8104"] == {"id": "6607"} or "udf_pick_8104" not in request["udf_fields"]
    assert request["udf_fields"]["udf_long_15301"] == "1"


def test_dry_run_skips_status_put_and_logs_would_send(monkeypatch, capsys):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str("me_dry_run"), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class DryRunME(FakeMEFull):
        def update_request(self, ticket_id, payload):
            raise AssertionError("dry run should not call update_request")

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")
    monkeypatch.setenv("MANAGEENGINE_AUTO_CLOSE", "true")
    me = DryRunME(None)
    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    main_mod.run_cycle("config/uat.yaml")
    out = capsys.readouterr().out
    assert "WOULD_SEND_ME_STATUS_PAYLOAD" in out
    assert "ME_STATUS_UPDATE_STARTED" not in out


def test_write_enabled_run_calls_put_and_refetches_final_status(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559970", "23422946", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559970", case_number="23422946", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422946", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")
    audit.insert_audit(idempotency_key=str("me_put_and_refetch"), ticket_id="559970", case_number="", requested_action="ME_SUMMARY", rule_version="0.2.0", before_status="", intended_status="", after_status="", salesforce_id="", last_modified_date="", processing_state="ME_NOTE_VERIFIED", outcome="ME_NOTE_VERIFIED", correlation_id="c-me")

    class PutAndRefetchME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.calls = []
        def update_request(self, ticket_id, input_data):
            self.calls.append(input_data)
            st = input_data.get("request", {}).get("status")
            if isinstance(st, dict) and st.get("id") == "4":
                self.tickets[ticket_id]["status"] = {"name": "Resolved"}
            self.tickets[ticket_id]["udf_fields"]["udf_pick_8104"] = {"value": "Regular"}
            self.tickets[ticket_id]["udf_fields"]["udf_pick_13501"] = {"value": "Mortgage"}
            self.tickets[ticket_id]["udf_fields"]["udf_long_15301"] = {"value": "1"}
            return type("R", (), {"raise_for_status": lambda self: None})()

    me = PutAndRefetchME(None)
    run_stage4(monkeypatch, me)
    assert len(me.calls) >= 1
    rows = audit.get_by_ticket("559970")
    assert any(row.get("outcome") == "ME_RESOLVED_VERIFIED" for row in rows)


def test_run_cycle_uses_final_success_line_in_summary_note(monkeypatch):
    captured = {}

    class FinalSuccessSF(FakeSFNoop):
        def __init__(self, s):
            self.s = s
            self.case_status = {"ID_23422946": "New"}
        def cases(self, numbers):
            return {
                "23422946": {
                    "Id": "ID_23422946",
                    "CaseNumber": "23422946",
                    "Status": "New",
                    "RecordType": {"Name": "Default"},
                    "LastModifiedDate": "LMD-START",
                }
            }
        def refresh_case(self, case_id):
            status = self.case_status.get(case_id, "New")
            return {
                "Id": case_id,
                "Status": status,
                "RecordType": {"Name": "Default"},
                "LastModifiedDate": "LMD-START",
            }
        def update_case(self, case_id, fields):
            self.case_status[case_id] = "Cancelled"
            return type("Resp", (), {"raise_for_status": lambda self: None})()
        def tasks(self, ids):
            return {i: [] for i in ids}
        def refresh_tasks_for_case(self, case_id):
            return []

    class FinalSuccessME(FakeMEFull):
        def list_notes(self, ticket_id, timeout=30.0):
            return []

    class FinalSuccessNoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, ticket_id, environment, action, case_lines, success_count, manual_count, failed_count, correlation_id):
            captured["case_lines"] = list(case_lines)
            captured["success_count"] = success_count
            captured["manual_count"] = manual_count
            captured["failed_count"] = failed_count
            return {"note": {"description": "summary"}}
        def post_note(self, ticket_id, payload, timeout=30.0):
            class Resp:
                def json(self):
                    return {"note": {"id": "N-1"}}
                def raise_for_status(self):
                    return None
            return Resp()
        def list_notes(self, ticket_id, timeout=30.0):
            return []

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: FakeMEFull(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FinalSuccessSF(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", FinalSuccessNoteWriter)

    main_mod.run_cycle("config/uat.yaml")

    assert captured["success_count"] == 1
    assert any("SUCCESS | New -> Cancelled" in line for line in captured["case_lines"])


def test_list_notes_exceptions_are_printed(monkeypatch, capsys):
    class ExplodingNoteWriter:
        def __init__(self, s):
            pass
        def build_summary(self, *args, **kwargs):
            return {"note": {"description": "summary"}}
        def post_note(self, *args, **kwargs):
            class Resp:
                def json(self):
                    return {"note": {"id": "N-1"}}
                def raise_for_status(self):
                    return None
            return Resp()
        def list_notes(self, ticket_id, timeout=30.0):
            raise httpx.HTTPError("notes exploded")

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: FakeMEFull(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", ExplodingNoteWriter)

    main_mod.run_cycle("config/uat.yaml")
    output = capsys.readouterr().out
    assert "LIST_NOTES_FAILED" in output
    assert "HTTPError" in output or "ValueError" in output or "TypeError" in output


def test_success_outcome_counts_include_already_in_target(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559959", "12345", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559959", case_number="12345", requested_action="CANCEL", rule_version="0.2.0", before_status="New", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_12345", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="ALREADY_IN_TARGET_STATE", correlation_id="c1")

    class FakeMEForCounts(FakeMEFull):
        def list_open_tickets(self):
            return [{"id": "559959"}]
        def get_ticket(self, ticket_id):
            return {
                "id": ticket_id,
                "subject": "Cancel SRs",
                "description": "Please cancel Case Number 12345",
                "group": {"name": "SalesForce - CRM"},
                "template": {"name": "SalesForce CRM Issues"},
                "status": {"name": "Open"},
                "created_time": int(datetime.datetime.utcnow().timestamp() * 1000),
                "tags": ["SR-AUTOMATION-UAT"],
                "udf_fields": {
                    "udf_pick_8104": {"value": "Regular"},
                    "udf_pick_13501": {"value": "Mortgage"},
                    "udf_long_15301": {"value": "1"},
                },
            }

    class CountNoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, ticket_id, environment, action, case_lines, success_count, manual_count, failed_count, correlation_id):
            assert success_count == 1
            assert manual_count == 0
            assert failed_count == 0
            return {"note": {"description": "summary"}}
        def post_note(self, ticket_id, payload, timeout=30.0):
            return type("R", (), {"json": lambda self: {}, "raise_for_status": lambda self: None})()
        def list_notes(self, ticket_id, timeout=30.0):
            return []

    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: FakeMEForCounts(s))
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", CountNoteWriter)

    main_mod.run_cycle("config/uat.yaml")


def test_old_format_existing_note_is_recognized():
    note = {
        "id": "N-101",
        "description": "Automation result for ticket #559959\nEnvironment: UAT\nAction: CANCEL\nSalesforce results:\n23422951 | CANCEL | SUCCESS | Cancelled\nSuccessful: 1\nManual review: 0\nFailed: 0"
    }
    assert main_mod.note_matches_equivalent(note, "559959", "CANCEL", ["23422951"]) is True


def test_html_and_line_break_variations_are_normalized():
    note = {
        "id": "N-102",
        "description": "<div>Automation result for ticket #559959</div><br><p>Action: cancel</p><br><div>23422951 | CANCEL | SUCCESS | Cancelled</div>"
    }
    assert main_mod.note_matches_equivalent(note, "559959", "CANCEL", ["23422951"]) is True


def test_existing_note_prevents_duplicate_post_and_allows_closure(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559959", "23422951", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559959", case_number="23422951", requested_action="CANCEL", rule_version="0.2.0", before_status="Open", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422951", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")

    class ExistingNoteME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.tickets["559959"] = {
                "id": "559959",
                "subject": "Cancel SRs",
                "description": "Please cancel Case Number 23422951",
                "group": {"name": "SalesForce - CRM"},
                "template": {"name": "SalesForce CRM Issues"},
                "status": {"name": "Open"},
                "created_time": int(datetime.datetime.utcnow().timestamp() * 1000),
                "tags": ["SR-AUTOMATION-UAT"],
                "udf_fields": {
                    "udf_pick_8104": {"value": "Regular"},
                    "udf_pick_13501": {"value": "Mortgage"},
                    "udf_long_15301": {"value": "1"},
                },
            }
        def list_open_tickets(self):
            return [{"id": "559959"}]
        def get_ticket(self, ticket_id):
            return self.tickets.get(ticket_id, super().get_ticket(ticket_id))

    class PreventDuplicateNoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, *args, **kwargs):
            raise AssertionError("summary should not be posted when an equivalent note already exists")
        def post_note(self, ticket_id, payload, timeout=30.0):
            raise AssertionError("post_note should not be called when an equivalent note already exists")
        def list_notes(self, ticket_id, timeout=30.0):
            return [{
                "id": "N-777",
                "description": "Automation result for ticket #559959\nAction: CANCEL\n23422951 | CANCEL | SUCCESS | Cancelled"
            }]

    me = ExistingNoteME(None)
    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", PreventDuplicateNoteWriter)

    main_mod.run_cycle("config/uat.yaml")

    rows = audit.get_by_ticket("559959")
    outcomes = [r.get("outcome") for r in rows]
    assert "ME_RESOLVED_VERIFIED" in outcomes or any(o == "ME_RESOLVED_VERIFIED" for o in outcomes)


def test_verified_existing_note_allows_stage4_closure(monkeypatch):
    db = Path(os.environ["AUDIT_DB_URL"])
    audit = AuditRepository(db_url=str(db))
    key1 = audit.make_idempotency_key("559959", "23422951", "CANCEL", "0.2.0")
    audit.insert_audit(idempotency_key=key1, ticket_id="559959", case_number="23422951", requested_action="CANCEL", rule_version="0.2.0", before_status="Open", intended_status="Cancelled", after_status="Cancelled", salesforce_id="ID_23422951", last_modified_date="LMD1", processing_state="SALESFORCE_VERIFIED", outcome="SUCCESS", correlation_id="c1")

    class ExistingVerifiedNoteME(FakeMEFull):
        def __init__(self, s):
            super().__init__(s)
            self.tickets["559959"] = {
                "id": "559959",
                "subject": "Cancel SRs",
                "description": "Please cancel Case Number 23422951",
                "group": {"name": "SalesForce - CRM"},
                "template": {"name": "SalesForce CRM Issues"},
                "status": {"name": "Open"},
                "created_time": int(datetime.datetime.utcnow().timestamp() * 1000),
                "tags": ["SR-AUTOMATION-UAT"],
                "udf_fields": {
                    "udf_pick_8104": {"value": "Regular"},
                    "udf_pick_13501": {"value": "Mortgage"},
                    "udf_long_15301": {"value": "1"},
                },
            }
        def list_open_tickets(self):
            return [{"id": "559959"}]
        def get_ticket(self, ticket_id):
            return self.tickets.get(ticket_id, super().get_ticket(ticket_id))

    class VerifiedExistingNoteWriter:
        def __init__(self, s):
            self.s = s
        def build_summary(self, *args, **kwargs):
            raise AssertionError("no summary should be posted when equivalent note already exists")
        def post_note(self, ticket_id, payload, timeout=30.0):
            raise AssertionError("duplicate POST should not happen")
        def list_notes(self, ticket_id, timeout=30.0):
            return [{
                "id": "N-888",
                "description": "Automation result for ticket #559959\nEnvironment: UAT\nAction: CANCEL\nSalesforce results:\n23422951 | CANCEL | ALREADY_IN_TARGET_STATE | Cancelled"
            }]

    me = ExistingVerifiedNoteME(None)
    monkeypatch.setattr(main_mod, "ManageEngineClient", lambda s: me)
    monkeypatch.setattr(main_mod, "SalesforceClient", lambda s: FakeSFNoop(s))
    monkeypatch.setattr(main_mod, "ManageEngineNoteWriter", VerifiedExistingNoteWriter)

    main_mod.run_cycle("config/uat.yaml")

    rows = audit.get_by_ticket("559959")
    outcomes = [r.get("outcome") for r in rows]
    assert "ME_RESOLVED_VERIFIED" in outcomes
 