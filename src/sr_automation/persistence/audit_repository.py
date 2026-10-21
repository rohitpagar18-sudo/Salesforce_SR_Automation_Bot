import sqlite3
from dataclasses import asdict
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Optional

DB_SCHEMA = '''
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT,
    ticket_id TEXT,
    case_number TEXT,
    requested_action TEXT,
    rule_version TEXT,
    before_status TEXT,
    intended_status TEXT,
    after_status TEXT,
    salesforce_id TEXT,
    last_modified_date TEXT,
    processing_state TEXT,
    outcome TEXT,
    attempt_number INTEGER DEFAULT 0,
    attempt_time TEXT,
    correlation_id TEXT
    ,me_note_id TEXT
    ,me_note_state TEXT
);
'''

class AuditRepository:
    def __init__(self, db_url: Optional[str] = None):
        if not db_url:
            db_url = "local_audit.db"
        self.path = Path(db_url)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.execute(DB_SCHEMA)
        self.conn.commit()

    def make_idempotency_key(self, ticket_id: str, case_number: str, action: str, rule_version: str) -> str:
        raw = f"{ticket_id}|{case_number}|{action}|{rule_version}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def next_attempt_number(self, idempotency_key: str) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COALESCE(MAX(attempt_number), 0) FROM audit WHERE idempotency_key = ?", (idempotency_key,))
        row = cur.fetchone()
        max_attempt = row[0] if row and row[0] is not None else 0
        return int(max_attempt) + 1

    def insert_audit(self, *, idempotency_key: str, ticket_id: str, case_number: str, requested_action: str,
                     rule_version: str, before_status: str, intended_status: str, after_status: str,
                     salesforce_id: str, last_modified_date: str, processing_state: str, outcome: str,
                     correlation_id: str, attempt_number: Optional[int] = None) -> None:
        now = datetime.utcnow().isoformat()
        if attempt_number is None:
            attempt_number = self.next_attempt_number(idempotency_key)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO audit (idempotency_key,ticket_id,case_number,requested_action,rule_version,before_status,intended_status,after_status,salesforce_id,last_modified_date,processing_state,outcome,attempt_number,attempt_time,correlation_id,me_note_id,me_note_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (idempotency_key, ticket_id, case_number, requested_action, rule_version, before_status, intended_status, after_status, salesforce_id, last_modified_date, processing_state, outcome, attempt_number, now, correlation_id, None, None),
        )
        self.conn.commit()

    def get_by_idempotency(self, key: str) -> Optional[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM audit WHERE idempotency_key = ? ORDER BY attempt_number DESC, id DESC LIMIT 1", (key,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

    def get_by_ticket(self, ticket_id: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM audit WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,))
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in rows]

    def set_me_note(self, ticket_id: str, note_id: str, state: str, correlation_id: str) -> None:
        """Record ManageEngine note metadata as a dedicated audit row."""
        now = datetime.utcnow().isoformat()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO audit (idempotency_key,ticket_id,case_number,requested_action,rule_version,before_status,intended_status,after_status,salesforce_id,last_modified_date,processing_state,outcome,attempt_number,attempt_time,correlation_id,me_note_id,me_note_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"me_note|{ticket_id}|{note_id}", ticket_id, "", "ME_SUMMARY", "", "", "", "", "", "", state, state, 0, now, correlation_id, note_id, state),
        )
        self.conn.commit()
