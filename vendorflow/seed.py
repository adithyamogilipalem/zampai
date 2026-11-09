"""Initialize and seed a local VendorFlow database."""

from __future__ import annotations

import argparse
from pathlib import Path

from .db import DEFAULT_DB_PATH, initialize_database
from .demo import seed_demo_data, write_sample_documents


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize VendorFlow and add one fictional approved vendor if empty.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database file")
    parser.add_argument("--uploads", default="data/uploads", help="Directory for uploaded demo PDFs")
    args = parser.parse_args()
    db_path = Path(args.db)
    initialize_database(db_path)
    seeded = seed_demo_data(db_path, args.uploads)
    sample_files = write_sample_documents()
    print(f"Database ready: {db_path.resolve()}")
    print(f"Sample PDFs ready: {len(sample_files)}")
    print(f"Seed application: {seeded or 'already present'}")


if __name__ == "__main__":
    main()
