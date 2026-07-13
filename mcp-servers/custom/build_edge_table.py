#!/usr/bin/env python3
"""One-time collaboration edge-table builder. Host-side, writable. Run before
starting the MCP container (same maintenance window as setup_fts.py).

The graph tools (find_collaborators, find_path_between_artists) self-join
release_artist on release_id. That table is ~92M rows and 10 columns wide
(several long VARCHARs: artist_name, join_string, tracks, anv). DuckDB executes
the self-join as a hash join and scans the whole probe side every hop, so the
wide rows dominate wall-clock.

This builds `release_artist_slim` — the four columns the self-joins actually
touch (release_id, artist_id, extra, role) — physically ordered by artist_id so
zone-maps prune the seed-side `artist_id IN (...)` filter, and with the Discogs
"special" placeholder artists (Various / Unknown Artist / No Artist) removed so
they can never explode the join or surface as collaborators.

Idempotent: skips if the table already exists unless --force. Expect a few
minutes and ~1-2 GB of file growth.
"""

import argparse
import sys
import time
from pathlib import Path

import duckdb

# Keep in sync with SPECIAL_ARTIST_IDS in queries.py.
SPECIAL_ARTIST_IDS = (194, 355, 118760)  # Various, Unknown Artist, No Artist
TABLE = "release_artist_slim"

# Keep in sync with _NON_MUSICAL_PATTERNS in queries.py. The "musical" role
# alias in find_collaborators used to apply these 18 `role NOT ILIKE ?` patterns
# across the whole ~90M-row self-join on every call (~60s in production). We
# precompute the classification into an is_non_musical boolean here so the query
# filter collapses to a single boolean read.
NON_MUSICAL_PATTERNS = (
    "%Photograph%",
    "%Design%",
    "%Artwork%",
    "%Illustration%",
    "%Sleeve%",
    "%Layout%",
    "%Cover %",
    "%Liner Notes%",
    "%Translation%",
    "%Translator%",
    "%Management%",
    "%Manager%",
    "%A&R%",
    "%Legal%",
    "%Booking%",
    "%Coordinator%",
    "%Executive%",
    "%Supervisor%",
)


def table_exists(conn, table: str) -> bool:
    rows = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchall()
    return len(rows) > 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db-path",
        default=str(Path.home() / "projects" / "discogs_db" / "discogs.duckdb"),
    )
    ap.add_argument("--force", action="store_true", help="drop and rebuild")
    args = ap.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 2

    print(f"Opening {db_path} (writable)")
    conn = duckdb.connect(str(db_path))

    if table_exists(conn, TABLE) and not args.force:
        print(f"  {TABLE}: already exists, skipping (use --force to rebuild)")
        conn.close()
        return 0

    specials = ", ".join(str(i) for i in SPECIAL_ARTIST_IDS)
    # role matches any non-musical pattern -> TRUE; NULL role -> FALSE.
    non_musical = " OR ".join("role ILIKE ?" for _ in NON_MUSICAL_PATTERNS)
    print(f"  {TABLE}: building (excluding special artists {specials})...", flush=True)
    start = time.time()
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
    # ORDER BY artist_id clusters the seed-side filter column so zone-maps can
    # skip row groups; the probe side still scans, but over the narrow columns.
    # is_non_musical is precomputed so the "musical" role filter is one boolean
    # read instead of 18 ILIKEs per row.
    conn.execute(
        f"""
        CREATE TABLE {TABLE} AS
        SELECT release_id, artist_id, extra, role,
               CASE WHEN role IS NULL THEN FALSE ELSE ({non_musical}) END AS is_non_musical
          FROM release_artist
         WHERE artist_id NOT IN ({specials})
         ORDER BY artist_id
        """,
        list(NON_MUSICAL_PATTERNS),
    )
    n = conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    print(f"  {TABLE}: {n:,} rows in {time.time() - start:.1f}s")

    print("Checkpointing...")
    conn.execute("CHECKPOINT")
    conn.close()
    print("Edge-table build complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
