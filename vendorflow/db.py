"""SQLite persistence and database helpers for VendorFlow."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_DB_PATH = Path(os.getenv("VENDORFLOW_DB_PATH", "data/vendorflow.db"))


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS vendors (
    vendor_id TEXT PRIMARY KEY,
    registered_name TEXT NOT NULL,
    trading_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    country TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
    status TEXT NOT NULL,
    risk_category TEXT NOT NULL DEFAULT 'Medium',
    current_revision INTEGER NOT NULL DEFAULT 1,
    latest_run_id TEXT,
    automated_recommendation TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE TABLE IF NOT EXISTS application_revisions (
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    revision_no INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT 'Initial submission',
    PRIMARY KEY(application_id, revision_no)
);
CREATE TABLE IF NOT EXISTS uploaded_documents (
    document_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    revision_no INTEGER NOT NULL,
    document_type TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(application_id, revision_no) REFERENCES application_revisions(application_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_docs_app_revision ON uploaded_documents(application_id, revision_no);
CREATE TABLE IF NOT EXISTS verification_runs (
    run_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    revision_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'Running',
    risk_category TEXT,
    summary TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(application_id, revision_no) REFERENCES application_revisions(application_id, revision_no)
);
CREATE TABLE IF NOT EXISTS verification_checks (
    check_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES verification_runs(run_id),
    stage_name TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    findings_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_checks_run ON verification_checks(run_id);
CREATE TABLE IF NOT EXISTS extracted_fields (
    extracted_field_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    revision_no INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES verification_runs(run_id),
    document_id TEXT NOT NULL REFERENCES uploaded_documents(document_id),
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    extraction_method TEXT NOT NULL DEFAULT 'local_pdf_text',
    confidence REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    run_id TEXT NOT NULL REFERENCES verification_runs(run_id),
    decision_type TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviewer_actions (
    action_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    run_id TEXT NOT NULL REFERENCES verification_runs(run_id),
    reviewer TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS communications (
    communication_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    revision_no INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(application_id, revision_no) REFERENCES application_revisions(application_id, revision_no)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    application_id TEXT REFERENCES applications(application_id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_requests (
    request_signature TEXT NOT NULL,
    application_id TEXT NOT NULL,
    revision_no INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(request_signature, application_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_submission_requests_recent ON submission_requests(request_signature, created_at);
"""


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def initialize_database(db_path: str | Path | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def database(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_load(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {} if fallback is None else fallback


def query_one(sql: str, params: tuple[Any, ...] = (), db_path: str | Path | None = None) -> sqlite3.Row | None:
    with database(db_path) as conn:
        return conn.execute(sql, params).fetchone()


def query_all(sql: str, params: tuple[Any, ...] = (), db_path: str | Path | None = None) -> list[sqlite3.Row]:
    with database(db_path) as conn:
        return conn.execute(sql, params).fetchall()
