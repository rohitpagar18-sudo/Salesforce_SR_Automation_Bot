import sys, os
# Ensure src is on sys.path for tests
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC = os.path.join(ROOT, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest


@pytest.fixture(autouse=True)
def default_audit_env(tmp_path, monkeypatch):
    dbfile = tmp_path / "audit.db"
    monkeypatch.setenv("AUDIT_DB_URL", str(dbfile))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_SALESFORCE_WRITES", "true")
    monkeypatch.setenv("ENABLE_MANAGEENGINE_WRITES", "true")
    monkeypatch.setenv("MANAGEENGINE_AUTO_CLOSE", "true")
    return dbfile
