"""Discogs DuckDB MCP server. Exposes 15 tools over FastMCP HTTP on port 8765.

Expects a read-only DuckDB file at /data/discogs.duckdb (override with DISCOGS_DB_PATH)
with FTS indexes already built via setup_fts.py. Fails fast at import if either is missing.
"""

import contextvars
import datetime as _dt
import functools
import json
import os
import sys
import threading
import time
from typing import Any, Optional

import duckdb
from fastmcp import FastMCP

import queries as Q
import sql_guard


QUERY_LOG_PATH = os.environ.get("QUERY_LOG_PATH", "/logs/queries.jsonl")
_sql_capture: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "sql_capture", default=None
)
_log_write_lock = threading.Lock()


def _append_query_log(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(QUERY_LOG_PATH), exist_ok=True)
        line = json.dumps(entry, default=str) + "\n"
        with _log_write_lock:
            with open(QUERY_LOG_PATH, "a") as f:
                f.write(line)
    except Exception as e:
        print(f"[query-log] failed to write: {e!r}", file=sys.stderr, flush=True)


def _capture_execute(real_execute, sql, args, kwargs):
    """Run real_execute while recording SQL into the active capture buffer, if any."""
    buf = _sql_capture.get()
    if buf is None:
        return real_execute(sql, *args, **kwargs)
    start = time.time()
    params = args[0] if args else kwargs.get("parameters")
    try:
        cur = real_execute(sql, *args, **kwargs)
        buf.append(
            {
                "sql": sql,
                "params": params,
                "duration_ms": int((time.time() - start) * 1000),
            }
        )
        return cur
    except Exception as e:
        buf.append(
            {
                "sql": sql,
                "params": params,
                "duration_ms": int((time.time() - start) * 1000),
                "error": repr(e),
            }
        )
        raise


class _CursorProxy:
    """Proxies a DuckDB cursor so execute() calls are captured."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, *args, **kwargs):
        return _capture_execute(self._cur.execute, sql, args, kwargs)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)


class _ConnProxy:
    """Proxies a DuckDB connection so execute()/cursor().execute() are captured."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args, **kwargs):
        return _capture_execute(self._conn.execute, sql, args, kwargs)

    def cursor(self):
        return _CursorProxy(self._conn.cursor())

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _log_call(fn):
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        start = time.time()
        payload = {**{f"arg{i}": a for i, a in enumerate(args)}, **kwargs}
        try:
            compact = json.dumps(payload, default=str)[:300]
        except Exception:
            compact = repr(payload)[:300]
        sqls: list = []
        token = _sql_capture.set(sqls)
        error: Optional[str] = None
        size: Any = None
        try:
            result = fn(*args, **kwargs)
            size = len(result) if isinstance(result, list) else (len(result) if isinstance(result, (str, bytes)) else 1)
            print(f"[tool] {fn.__name__} {compact} -> {size} rows in {time.time() - start:.2f}s", flush=True)
            return result
        except Exception as e:
            error = repr(e)
            print(f"[tool] {fn.__name__} {compact} -> ERROR {error} in {time.time() - start:.2f}s", flush=True)
            raise
        finally:
            _sql_capture.reset(token)
            _append_query_log(
                {
                    "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                    "tool": fn.__name__,
                    "args": payload,
                    "queries": sqls,
                    "row_count": size,
                    "duration_ms": int((time.time() - start) * 1000),
                    "status": "error" if error else "success",
                    "error": error,
                }
            )

    return wrap

DB_PATH = os.environ.get("DISCOGS_DB_PATH", "/data/discogs.duckdb")

if not os.path.exists(DB_PATH):
    print(f"FATAL: DuckDB file not found at {DB_PATH}", file=sys.stderr)
    sys.exit(1)

_CONN = duckdb.connect(DB_PATH, read_only=True)
_CONN.execute("LOAD fts")
try:
    _CONN.execute("SELECT 1 FROM fts_main_artist.docs LIMIT 1").fetchone()
except Exception as e:
    print(f"FATAL: FTS indexes missing. Run setup_fts.py first. ({e})", file=sys.stderr)
    sys.exit(1)

_CONN.execute("SET memory_limit='8GB'")
_CONN.execute("SET threads=4")

_CONN = _ConnProxy(_CONN)


def _year_range(v: Any) -> Optional[tuple[int, int]]:
    if v is None:
        return None
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return (int(v[0]), int(v[1]))
    raise ValueError("year_range must be [start, end]")


mcp = FastMCP("nanoclaw-discogs")


@mcp.tool
@_log_call
def search_artist(name: str, limit: int = 10) -> list[dict]:
    """Fuzzy search for artists by name (FTS / BM25). Returns id, name, realname,
    years_active (min-max of released_year across their credits), and top_labels
    (top 3 labels by count of their primary-credited releases)."""
    return Q.search_artist(_CONN, name, limit)


@mcp.tool
@_log_call
def search_release(
    title: str,
    artist: Optional[str] = None,
    year: Optional[int] = None,
    limit: int = 10,
) -> list[dict]:
    """Fuzzy search for releases by title (FTS / BM25). Optional filters:
    artist (ILIKE match on primary credit), year (exact released_year)."""
    return Q.search_release(_CONN, title, artist, year, limit)


@mcp.tool
@_log_call
def search_label(name: str, limit: int = 10) -> list[dict]:
    """Fuzzy search for labels by name (FTS / BM25). Includes parent name and
    sublabel/release counts."""
    return Q.search_label(_CONN, name, limit)


@mcp.tool
@_log_call
def get_artist(artist_id: int, compact: bool = False) -> Optional[dict]:
    """Full artist record: core fields plus aliases, name variations, URLs,
    groups the artist is a member of, and members (if this artist is a group).
    `compact=True` omits the `profile` field (the free-form bio, often the
    largest part of the response) — use for overview scans when you only need
    structured fields."""
    return Q.get_artist(_CONN, int(artist_id), bool(compact))


@mcp.tool
@_log_call
def get_release(release_id: int) -> Optional[dict]:
    """Full release record: tracklist (each track with its track-level credits),
    release-level credits (split into primary / additional by Discogs `extra` flag),
    formats, labels, genres, styles, identifiers."""
    return Q.get_release(_CONN, int(release_id))


@mcp.tool
@_log_call
def get_label(label_id: int) -> Optional[dict]:
    """Full label record including parent_id/parent_name (if a sublabel) and its
    own sublabels."""
    return Q.get_label(_CONN, int(label_id))


@mcp.tool
@_log_call
def get_artist_discography(
    artist_id: int,
    role: Optional[str] = None,
    as_main_only: bool = False,
    year_range: Optional[list[int]] = None,
    unique_masters_only: bool = True,
) -> list[dict]:
    """Releases crediting this artist. `role` accepts "performer" (empty role),
    "producer", "writer", "engineer" as grouped aliases; any other string is a
    substring ILIKE match against release_artist.role. `as_main_only=True`
    restricts to primary credits (extra=0). `year_range=[lo, hi]` filters on
    released_year (releases with NULL year are excluded). `unique_masters_only=True`
    (default) collapses pressings/editions to one row per Discogs master, picking
    the earliest-year pressing; each row carries `pressings_count` for how many
    variants it represents. Set False to see every pressing (can explode for
    prolific artists). Capped at 500 rows."""
    return Q.get_artist_discography(
        _CONN,
        int(artist_id),
        role,
        bool(as_main_only),
        _year_range(year_range),
        bool(unique_masters_only),
    )


@mcp.tool
@_log_call
def get_label_releases(
    label_id: int,
    year_range: Optional[list[int]] = None,
    unique_masters_only: bool = True,
) -> list[dict]:
    """Releases issued on this label. Each row: id, title, year, country,
    master_id, catno (Discogs catalog number), primary_artists, pressings_count.
    `year_range=[lo, hi]` filters on released_year (NULLs excluded).
    `unique_masters_only=True` (default) collapses pressings/editions to one
    row per Discogs master — essential for label narratives since labels
    re-press and re-release heavily across territories. Capped at 500 rows.
    For roster-level aggregates use `get_label_roster` instead."""
    return Q.get_label_releases(
        _CONN,
        int(label_id),
        _year_range(year_range),
        bool(unique_masters_only),
    )


@mcp.tool
@_log_call
def get_label_roster(
    label_id: int,
    year_range: Optional[list[int]] = None,
) -> list[dict]:
    """Primary artists on this label within an optional year window, ordered by
    release count. Session/engineer credits are excluded (extra=0 only).
    Capped at 200 artists."""
    return Q.get_label_roster(_CONN, int(label_id), _year_range(year_range))


@mcp.tool
@_log_call
def find_collaborators(
    artist_id: int,
    depth: int = 1,
    min_shared_releases: int = 1,
    roles: Optional[list[str]] = None,
    include_top_shared_titles: bool = True,
) -> list[dict]:
    """BFS over the collaboration graph. Edges go from an artist's own releases
    (they are primary, extra=0) to anyone else credited on those releases
    (primary or additional). `depth` hard-capped at 3. `min_shared_releases`
    gates weak edges. `roles` filters the neighbor side: accepts a list of
    aliases or arbitrary substrings — any match qualifies. Aliases:
    "musical" — the common "who played on the records" filter. Keeps rows
    with NULL/empty role (performers) plus anything whose role does NOT
    match these substrings: Photograph, Design, Artwork, Illustration,
    Sleeve, Layout, Cover, Liner Notes, Translation, Translator, Management,
    Manager, A&R, Legal, Booking, Coordinator, Executive, Supervisor.
    "performer" (strictly primary-credited, no role string); "producer";
    "writer"; "engineer". Each result includes `top_shared_titles` (up to 3
    release titles this artist shares with the seed side, deduped across
    pressings) and `aliases` (list of `{id, name}` for this artist's
    known alter-egos — useful for spotting when e.g. "JG Thirlwell" and
    "Clint Ruin" are the same person split across rows). Sorted by
    (distance asc, shared_releases desc). Capped at 500 rows.
    `include_top_shared_titles=False` skips the title lookup and returns
    empty lists — use for overview scans when titles will exhaust context;
    call again with True on a narrower artist set once you know who to focus
    on."""
    return Q.find_collaborators(
        _CONN,
        int(artist_id),
        int(depth),
        int(min_shared_releases),
        roles,
        bool(include_top_shared_titles),
    )


@mcp.tool
@_log_call
def find_path_between_artists(
    artist_a_id: int,
    artist_b_id: int,
    max_depth: int = 4,
) -> Optional[dict]:
    """Shortest collaboration path between two artists. Uses same edge semantics
    as find_collaborators (follow via each artist's own primary-credited releases).
    `max_depth` hard-capped at 6. Returns `{path: [{id,name}, ...], depth: N}` or
    `{path: null, reason: "no_path" | "max_depth_exceeded"}`."""
    return Q.find_path_between_artists(
        _CONN,
        int(artist_a_id),
        int(artist_b_id),
        int(max_depth),
    )


@mcp.tool
@_log_call
def get_scene_snapshot(
    label_ids: Optional[list[int]] = None,
    year_range: Optional[list[int]] = None,
    country: Optional[str] = None,
) -> dict:
    """Multi-dimensional slice of a release subset. Requires at least one filter.
    Returns release_count plus top 20 artists (by primary-release count),
    top 20 releases (by credit count), and format/genre/style distributions
    (top 25 each). `country` is ILIKE-matched."""
    return Q.get_scene_snapshot(
        _CONN,
        label_ids,
        _year_range(year_range),
        country,
    )


@mcp.tool
@_log_call
def list_compilations_featuring_artist(artist_id: int) -> list[dict]:
    """Releases where this artist appears AND at least one format row contains
    "Compilation" in its descriptions (and not "Unofficial"). Capped at 500 rows."""
    return Q.list_compilations_featuring_artist(_CONN, int(artist_id))


SCHEMA_DOC = """# Discogs DuckDB — quick orientation for agents

## Core entities

- **artist** (10M) — individuals and groups. Group membership lives in `group_member`.
- **master** (2.5M) — one row per musical work. Groups all pressings of the same album.
- **release** (19M) — a specific pressing/edition. `release.master_id` points at the master
  (NULLABLE: standalone releases exist).
- **label** (2.4M) — labels, imprints, sublabels (self-linked via `parent_id`).

## Credits: the `extra` flag

`release_artist.extra` and `release_track_artist.extra` are **0 for primary / header
credits** (the artists headlining the release or track) and **1 for additional credits**
(producers, remixers, featured artists, engineers, mastering, etc.). About 25% of
release_artist rows are primary, 75% additional.

Use `extra = 0` to answer "releases by X"; include both when the question is "who worked
with X" or "full credit list".

## Year of release

`release.released_raw` is freeform VARCHAR ("1983", "1983-04-01", "c. 1983", "Unknown").
`release.released_year` is a pre-computed INTEGER derived from the first 4 characters via
TRY_CAST — NULL when the string doesn't start with 4 digits. About 13% of releases have
NULL released_year; they're excluded from year_range filters.

## Compilations

A release is a compilation when at least one `release_format` row has `descriptions ILIKE
'%Compilation%'`. Exclude `'%Unofficial%'` — ~6% of compilation-tagged rows also carry the
Unofficial marker and are bootlegs.

## Tool routing

- "who is X? what did they release?" → `search_artist` → `get_artist_discography`
- "tell me about this record" → `search_release` → `get_release`
- "what does this label put out?" → `search_label` → `get_label_releases` (titles) or
  `get_label_roster` (artists + counts)
- "who has X worked with?" → `find_collaborators`
- "are X and Y connected via collaborators?" → `find_path_between_artists`
- "snapshot of a scene" (by label, year, country) → `get_scene_snapshot`
- "compilations featuring X" → `list_compilations_featuring_artist`
- everything else → `run_readonly_sql` (SELECT only, single statement, no duckdb_*
  catalog tables, no information_schema, row_limit ≤ 10000)

## Tables that are empty

All `*_image` tables (artist_image, label_image, master_image, release_image) are empty —
Discogs strips images from the monthly public dump. Don't waste a query there.

## Odd edge

`release_company.company_id` points at `artist.id`: Discogs models pressing plants, PR
agencies, distributors etc. as artists. Keep that in mind if you join via `run_readonly_sql`.
"""


@mcp.tool
@_log_call
def describe_schema() -> str:
    """Prose overview of entities, tool routing, and the key query idioms. Call on demand
    rather than pulling into the system prompt."""
    return SCHEMA_DOC


@mcp.tool
@_log_call
def run_readonly_sql(query: str, row_limit: int = 1000) -> dict:
    """Escape hatch: run an ad-hoc SELECT against the DB. Enforced constraints:
    exactly one statement; must be SELECT (WITH permitted); no `duckdb_*` catalog
    tables; no information_schema. `row_limit` clamped to [1, 10000]. Returns
    `{columns, rows, row_count}` on success or `{error, query}` on failure."""
    try:
        sql_guard.validate(query)
    except ValueError as e:
        return {"error": str(e), "query": query}
    return Q.run_readonly_sql(_CONN, query, int(row_limit))


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8765)
