"""Copy a local VendorFlow SQLite database and PDFs to Supabase.

The source database is opened in SQLite read-only mode. Nothing in the local
database or upload tree is renamed, moved, edited, or deleted. Run without
``--apply`` first to validate the source files and see the migration inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .db import DEFAULT_DB_PATH, database, initialize_database
from .storage import StorageError, document_key, read_document, supabase_config, upload_document


TABLE_ORDER = (
    "vendors",
    "applications",
    "application_revisions",
    "uploaded_documents",
    "verification_runs",
    "verification_checks",
    "extracted_fields",
    "decisions",
    "reviewer_actions",
    "communications",
    "audit_events",
    "submission_requests",
)
APP_ROOT = Path(__file__).resolve().parents[1]


class MigrationError(RuntimeError):
    """A safe, user-facing migration problem without credential details."""


def open_source_readonly(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise MigrationError(f"Source database does not exist: {path}")
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as exc:
        raise MigrationError(f"Could not open the source database read-only ({type(exc).__name__}).") from None


def load_source(db_path: str | Path) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Read records and verify all source document bytes against stored hashes."""
    conn = open_source_readonly(db_path)
    try:
        present = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [table for table in TABLE_ORDER if table not in present]
        if missing:
            raise MigrationError("The source database is missing expected VendorFlow tables: " + ", ".join(missing))
        records = {
            table: [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
            for table in TABLE_ORDER
        }
    finally:
        conn.close()

    documents = records["uploaded_documents"]
    for record in documents:
        location = str(record["stored_path"])
        try:
            if location.startswith("supabase://"):
                data = read_document(location)
            else:
                path = Path(location).expanduser()
                if not path.is_absolute():
                    path = APP_ROOT / path
                data = path.read_bytes()
        except (OSError, StorageError) as exc:
            raise MigrationError(
                f"A source PDF could not be read ({type(exc).__name__}). Check that every referenced upload still exists."
            ) from None
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise MigrationError("A source PDF failed its saved SHA-256 check; no migration was started.")
        record["_source_bytes"] = data
    return records, documents


def _row_count(records: dict[str, list[dict[str, Any]]]) -> int:
    return sum(len(rows) for rows in records.values())


def _migrate_records(records: dict[str, list[dict[str, Any]]], bucket: str) -> dict[str, int]:
    """Insert records in dependency order; conflicting IDs are preserved as-is."""
    inserted: dict[str, int] = {}
    with database() as conn:
        for table in TABLE_ORDER:
            inserted[table] = 0
            for row in records[table]:
                values = {key: value for key, value in row.items() if key != "_source_bytes"}
                if table == "uploaded_documents":
                    key = document_key(values["application_id"], values["revision_no"], values["document_id"])
                    values["stored_path"] = upload_document(bucket, key, row["_source_bytes"], values["mime_type"])
                columns = list(values)
                quoted_columns = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
                placeholders = ",".join("?" for _ in columns)
                result = conn.execute(
                    f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING',
                    tuple(values[name] for name in columns),
                )
                inserted[table] += max(0, int(result.rowcount))
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a local VendorFlow database and, with --apply, copy its records and PDFs to Supabase."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Local SQLite source database (default: data/vendorflow.db)")
    parser.add_argument("--apply", action="store_true", help="Write verified records and PDFs to the configured Supabase project")
    args = parser.parse_args(argv)

    try:
        records, documents = load_source(args.db)
        print("Source validation passed (read-only).")
        print(f"Applications: {len(records['applications'])}")
        print(f"Revisions: {len(records['application_revisions'])}")
        print(f"PDF documents verified: {len(documents)}")
        print(f"Total records: {_row_count(records)}")
        if not args.apply:
            print("Dry run only. Re-run with --apply after configuring Supabase secrets to start the copy.")
            return 0

        config = supabase_config()
        initialize_database()
        inserted = _migrate_records(records, config["bucket"])
        print("Migration finished. Existing local SQLite records and source PDFs were left untouched.")
        for table in TABLE_ORDER:
            print(f"{table}: {inserted[table]} inserted")
        return 0
    except (MigrationError, StorageError, OSError, sqlite3.Error) as exc:
        print(f"Migration stopped safely: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
