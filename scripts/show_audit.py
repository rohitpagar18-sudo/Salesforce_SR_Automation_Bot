import sqlite3, sys
tid = sys.argv[1] if len(sys.argv) > 1 else None
db = sys.argv[2] if len(sys.argv) > 2 else "local_audit.db"
q = "select id, ticket_id, requested_action, processing_state, outcome, attempt_time from audit"
args = ()
if tid:
    q += " where ticket_id=?"
    args = (tid,)
q += " order by id desc limit 20"
for r in sqlite3.connect(db).execute(q, args):
    print(r)