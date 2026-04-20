#!/usr/bin/env python3
"""One-time FTS index builder. Host-side, writable. Run before starting the MCP container.

Builds BM25 indexes on artist/release/label in the existing discogs.duckdb file.
Expect ~30-60 minutes and ~2-4 GB of disk growth. Idempotent: skips tables whose
fts_main_* schema already exists unless --force is given.
"""

import argparse
import sys
import time
from pathlib import Path

import duckdb

INDEXES = [
    ("artist", "id", ["name", "realname"]),
    ("release", "id", ["title"]),
    ("label", "id", ["name"]),
]


def fts_schema_exists(conn, table: str) -> bool:
    schema = f"fts_main_{table}"
    rows = conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = ?",
        [schema],
    ).fetchall()
    return len(rows) > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-path", default=str(Path.home() / "projects" / "discogs_db" / "discogs.duckdb"))
    ap.add_argument("--force", action="store_true", help="drop existing indexes and rebuild")
    args = ap.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 2

    print(f"Opening {db_path} (writable)")
    conn = duckdb.connect(str(db_path))

    print("Installing and loading fts extension...")
    conn.execute("INSTALL fts")
    conn.execute("LOAD fts")

    for table, id_col, text_cols in INDEXES:
        exists = fts_schema_exists(conn, table)
        if exists and not args.force:
            print(f"  {table}: already indexed (fts_main_{table}), skipping")
            continue

        label = "rebuild" if exists else "build"
        print(f"  {table}: {label} FTS on ({', '.join(text_cols)})...", flush=True)
        start = time.time()
        quoted_cols = ", ".join(f"'{c}'" for c in text_cols)
        conn.execute(
            f"PRAGMA create_fts_index("
            f"'{table}', '{id_col}', {quoted_cols}, "
            f"stemmer='porter', stopwords='english', "
            f"strip_accents=1, lower=1, "
            f"overwrite={1 if args.force else 0})"
        )
        elapsed = time.time() - start
        print(f"  {table}: done in {elapsed:.1f}s")

    print("Checkpointing...")
    conn.execute("CHECKPOINT")
    conn.close()
    print("FTS setup complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
