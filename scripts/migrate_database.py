"""Copy every table from one database to another (e.g. local SQLite -> Neon Postgres), keeping ids.

    python -m scripts.migrate_database --source sqlite:///data/job_radar.db --target "$DATABASE_URL"

Notification history is copied too, so nothing already sent is sent again after the move.
The target must not contain jobs yet (refuses otherwise). Row counts are compared at the end.
"""

from __future__ import annotations

import argparse
import sys
from itertools import batched

from sqlalchemy import func, select, text

from database.models import Base
from database.repository import init_db, normalize_database_url


def _redact(url: str) -> str:
    return url.split("@")[-1] if "@" in url else url


def migrate(source_url: str, target_url: str, batch_size: int = 500) -> dict[str, tuple[int, int]]:
    source_engine = init_db(source_url).kw["bind"]  # also adds any missing columns to the source
    target_engine = init_db(target_url).kw["bind"]
    tables = Base.metadata.sorted_tables  # parents before children (foreign keys)

    with target_engine.connect() as conn:
        existing = conn.execute(select(func.count()).select_from(Base.metadata.tables["jobs"])).scalar()
    if existing:
        raise SystemExit(f"Target already has {existing} jobs; refusing to overwrite. Use an empty database.")

    counts: dict[str, tuple[int, int]] = {}
    with source_engine.connect() as src, target_engine.begin() as dst:
        for table in tables:
            rows = [dict(r._mapping) for r in src.execute(select(table))]
            for chunk in batched(rows, batch_size):
                dst.execute(table.insert(), list(chunk))
            counts[table.name] = (len(rows), 0)
        if target_engine.dialect.name == "postgresql":
            # Rows were inserted with explicit ids; move each id sequence past the copied maximum.
            for table in tables:
                if "id" in table.columns and table.columns["id"].autoincrement:
                    dst.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                        f"(SELECT MAX(id) FROM {table.name}) IS NOT NULL)"
                    ))
    with target_engine.connect() as conn:
        for table in tables:
            copied = conn.execute(select(func.count()).select_from(table)).scalar()
            counts[table.name] = (counts[table.name][0], copied)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args(argv)
    if normalize_database_url(args.source) == normalize_database_url(args.target):
        print("Source and target are the same database.", file=sys.stderr)
        return 2
    print(f"Copying {_redact(args.source)} -> {_redact(args.target)}", flush=True)
    counts = migrate(args.source, args.target)
    ok = True
    for name, (source_rows, target_rows) in counts.items():
        status = "ok" if source_rows == target_rows else "MISMATCH"
        ok &= source_rows == target_rows
        print(f"  {name:16s} {source_rows:6d} -> {target_rows:6d}  {status}")
    print("Done." if ok else "Row counts differ; do not use the target yet.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
