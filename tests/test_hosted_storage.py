from __future__ import annotations

from pathlib import Path

import vendorflow.db as db_module
from vendorflow.db import POSTGRES_RLS_TABLES, _PostgresConnection
from vendorflow.demo import run_demo_scenario
from vendorflow.migrate_supabase import load_source
from vendorflow.storage import storage_backend


class _FakeCursor:
    rowcount = 1


class _FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return _FakeCursor()

    def executescript(self, sql):
        self.calls.append(("SCHEMA", sql))

    def commit(self):
        pass

    def close(self):
        pass


def test_postgres_adapter_translates_placeholders_literals_and_write_lock():
    raw = _FakeConnection()
    conn = _PostgresConnection(raw)

    conn.execute("SELECT * FROM decisions WHERE run_id=? AND decision_type LIKE 'automated%'", ("RUN-1",))
    conn.execute("BEGIN IMMEDIATE")

    assert raw.calls[0] == (
        "SELECT * FROM decisions WHERE run_id=%s AND decision_type LIKE 'automated%%'",
        ("RUN-1",),
    )
    assert raw.calls[1][0] == "SELECT pg_advisory_xact_lock(hashtext('vendorflow-submit'))"


def test_custom_database_paths_keep_using_local_sqlite_when_supabase_is_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("VENDORFLOW_STORAGE_BACKEND", "supabase")
    assert storage_backend(tmp_path / "isolated.sqlite", tmp_path / "data/vendorflow.db") == "local"


def test_supabase_schema_locks_data_api_tables_behind_rls(monkeypatch):
    monkeypatch.setenv("VENDORFLOW_STORAGE_BACKEND", "supabase")
    raw = _FakeConnection()
    monkeypatch.setattr(db_module, "connect", lambda db_path=None: raw)
    monkeypatch.setattr(db_module, "initialize_supabase_storage", lambda _limit: None)

    db_module.initialize_database(db_module.DEFAULT_DB_PATH)

    rls_statements = [sql for sql, _params in raw.calls if sql.startswith('ALTER TABLE "')]
    assert len(rls_statements) == len(POSTGRES_RLS_TABLES)
    assert all(sql.endswith("ENABLE ROW LEVEL SECURITY") for sql in rls_statements)
    revoke_statements = [sql for sql, _params in raw.calls if sql.startswith('REVOKE ALL ON TABLE "')]
    assert len(revoke_statements) == len(POSTGRES_RLS_TABLES)


def test_migration_reads_local_database_and_documents_without_modifying_them(tmp_path):
    db_path = tmp_path / "vendorflow.db"
    uploads = tmp_path / "uploads"
    run_demo_scenario("complete", db_path, uploads)
    before = db_path.read_bytes()

    records, documents = load_source(db_path)

    after = db_path.read_bytes()
    assert before == after
    assert len(records["applications"]) == 2  # fictional seed plus complete scenario
    assert len(documents) == 6
    assert all(len(doc["_source_bytes"]) > 100 for doc in documents)
    assert all(doc["stored_path"].startswith(str(uploads)) for doc in documents)

\n