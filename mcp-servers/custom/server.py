"""Discogs DuckDB MCP server. Exposes 19 tools over FastMCP HTTP on port 8765.

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
import stubgen
import wikipedia as W


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

# Host is 12-core / 31GB and the container is uncapped, so the old 8GB/4-thread
# ceiling left most of the box idle while single scans ran cold. Env-overridable
# for tuning without a rebuild.
_MEM_LIMIT = os.environ.get("DUCKDB_MEMORY_LIMIT", "16GB")
_THREADS = os.environ.get("DUCKDB_THREADS", "8")
_CONN.execute(f"SET memory_limit='{_MEM_LIMIT}'")
_CONN.execute(f"SET threads={int(_THREADS)}")
# /data is mounted read-only, so DuckDB's default spill dir ({dbfile}.tmp)
# fails when complex queries need to spill. Redirect to /tmp, which is
# writable inside the container.
_CONN.execute("SET temp_directory='/tmp/duckdb'")

# Point the graph self-joins at the narrow release_artist_slim projection when
# it's been built (build_edge_table.py); otherwise fall back to release_artist.
_edge_table = Q.configure_edge_table(_CONN)
if _edge_table == "release_artist_slim":
    print("[edge-table] using release_artist_slim for graph traversals", flush=True)
else:
    print(
        "[edge-table] release_artist_slim not found; graph traversals will be "
        "slow. Run build_edge_table.py to speed up find_collaborators / "
        "find_path_between_artists.",
        flush=True,
    )

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
def search_artist(name: str, limit: int = 10, exact: bool = False) -> list[dict]:
    """Fuzzy search for artists by name (FTS / BM25). Returns id, name, realname,
    years_active (min-max of released_year across their credits), and top_labels
    (top 3 labels by count of their primary-credited releases).

    `exact=True` — when you already know the precise artist name and just need to
    resolve it to an id, skip fuzzy ranking and enrichment: matches the name
    directly (case-insensitive, tolerant of "&"/"and" and "(2)" disambiguators)
    and returns only id/name/realname. Prefer this over a raw run_readonly_sql
    lookup for exact name-to-id resolution."""
    return Q.search_artist(_CONN, name, limit, exact)


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
def search_label(name: str, limit: int = 10, exact: bool = False) -> list[dict]:
    """Fuzzy search for labels by name (FTS / BM25). Includes parent name and
    sublabel/release counts.

    `exact=True` — when you already know the precise label name and just need to
    resolve it to an id, skip fuzzy ranking: matches the name directly
    (case-insensitive + normalized) and returns id/name/parent_name plus
    release_count. Prefer this over a raw run_readonly_sql lookup."""
    return Q.search_label(_CONN, name, limit, exact)


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
def get_release(release_id: int, include_notes: bool = False) -> Optional[dict]:
    """Full release record: tracklist (each track with its track-level credits),
    release-level credits (split into primary / additional by Discogs `extra` flag),
    formats, labels, genres, styles, identifiers. `include_notes=True` pulls
    `r.notes` (free-text release notes, often the largest single field) — leave
    off unless the user specifically asks about liner notes or release commentary."""
    return Q.get_release(_CONN, int(release_id), bool(include_notes))


@mcp.tool
@_log_call
def get_release_credits(release_id: int) -> Optional[dict]:
    """Personnel / credits for a release — who played on it, who produced it,
    who's credited in any role. Returns `{release_id, title, released_year,
    primary, additional}` where `primary` are the header artists (extra=0) and
    `additional` are everyone else credited (producers, remixers, engineers,
    featured artists, mastering, etc.). Each credit row: artist_id, artist_name,
    anv (artist-name-variation as printed on the sleeve), role, position,
    join_string, tracks (track positions if scoped to specific tracks).
    Use this instead of `get_release` when you only need personnel — response
    is much smaller since it omits tracklist, formats, labels, genres, styles,
    identifiers."""
    return Q.get_release_credits(_CONN, int(release_id))


@mcp.tool
@_log_call
def get_label(label_id: int, compact: bool = False) -> Optional[dict]:
    """Full label record including parent_id/parent_name (if a sublabel) and its
    own sublabels. `compact=True` omits `profile` and `contact_info` (the
    free-text bio and address/contact block, often the largest fields) — use
    for overview scans when you only need structured fields."""
    return Q.get_label(_CONN, int(label_id), bool(compact))


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
    restricts to primary credits only. Each row carries `as_primary` (bool) —
    true iff the artist is credited as a header/primary on the release, false
    for contributor-only appearances (comps, guest credits, etc.).
    `year_range=[lo, hi]` filters on released_year (releases with NULL year
    are excluded). `unique_masters_only=True` (default) collapses
    pressings/editions to one row per Discogs master, picking the earliest-year
    pressing; each row carries `pressings_count` for how many variants it
    represents. Set False to see every pressing (can explode for prolific
    artists). Capped at 500 rows."""
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
def get_person_credits(
    person_id: int,
    year_range: Optional[list[int]] = None,
    unique_masters_only: bool = True,
) -> list[dict]:
    """Full credit history for a person (artist.id) across every role they're
    credited in — performer, producer, engineer, writer, designer, etc. Use this
    when looking up engineers, designers, mastering people, photographers, or
    anyone whose Discogs page is sparsely populated under their "artist" entry
    but who appears across many releases as a contributor. Each row carries
    `role` (joined distinct roles for that release), `as_primary` (false for the
    contributor case this tool is built for), and the usual release fields.
    `year_range=[lo, hi]` filters released_year (NULLs excluded).
    `unique_masters_only=True` (default) collapses pressings to one row per
    master. Capped at 500 rows. For a band's catalog, prefer
    `get_artist_discography` with `as_main_only=True` instead."""
    return Q.get_artist_discography(
        _CONN,
        int(person_id),
        None,
        False,
        _year_range(year_range),
        bool(unique_masters_only),
    )


@mcp.tool
@_log_call
def get_release_by_catalog_number(
    cat_no: str,
    label_id: Optional[int] = None,
) -> list[dict]:
    """Direct lookup by Discogs catalog number (e.g. "SSQ225", "S-005",
    "DGC-24515"). Catalog numbers are scoped to a label but not globally
    unique — pass `label_id` to disambiguate when you know it, otherwise all
    matches across labels are returned. Match is case- and whitespace-
    insensitive but otherwise exact (does not strip hyphens — "S-005" and
    "S005" are different cat numbers on Discogs). Each row: id, title, year,
    country, master_id, label_id, label_name, catno, primary_artists. Capped
    at 100 rows."""
    return Q.get_release_by_catalog_number(
        _CONN,
        str(cat_no),
        int(label_id) if label_id is not None else None,
    )


@mcp.tool
@_log_call
def get_label_credits_summary(
    label_id: int,
    year_range: Optional[list[int]] = None,
    top_n_per_role: int = 10,
) -> dict:
    """Top credited people across a label's catalog, bucketed by role:
    `performer` (primary credits — empty role), `producer`, `engineer`
    (engineer/mix/master), `writer` (writer/composer/lyricist), `design`
    (design/artwork/photography/sleeve/illustration), `other` (everything
    else). Useful for spotting the recurring producers, engineers, designers,
    etc. behind a label without pulling individual releases one by one. Each
    bucket entry: artist_id, name, release_count, roles (distinct role
    strings encountered). Returns `{label_id, label_name, release_count,
    buckets}` where `buckets` is a dict keyed by bucket name. `year_range`
    filters released_year (NULLs excluded). `top_n_per_role` clamped to
    [1, 50]. Major labels with thousands of releases will be slow — use
    `year_range` to scope down."""
    return Q.get_label_credits_summary(
        _CONN,
        int(label_id),
        _year_range(year_range),
        int(top_n_per_role),
    )


@mcp.tool
@_log_call
def get_label_releases(
    label_id: int,
    year_range: Optional[list[int]] = None,
    unique_masters_only: bool = True,
    include_credits: bool = False,
) -> list[dict]:
    """Releases issued on this label. Each row: id, title, year, country,
    master_id, catno (Discogs catalog number), primary_artists, pressings_count.
    `year_range=[lo, hi]` filters on released_year (NULLs excluded).
    `unique_masters_only=True` (default) collapses pressings/editions to one
    row per Discogs master — essential for label narratives since labels
    re-press and re-release heavily across territories. Capped at 500 rows.
    `include_credits=True` attaches `credits: {role: [artist_name, ...]}`
    per release — a role → names summary of non-primary contributors only
    (primary artists are already in `primary_artists`). For per-track
    detail, anv/stage names, or join strings, call
    `get_release_credits(release_id)` on the specific release. Combine
    with `year_range` or a sublabel id to scope large catalogs. For
    roster-level aggregates use `get_label_roster` instead."""
    return Q.get_label_releases(
        _CONN,
        int(label_id),
        _year_range(year_range),
        bool(unique_masters_only),
        bool(include_credits),
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
- "what did this engineer/designer/producer work on?" → `search_artist` → `get_person_credits`.
  Same underlying data as `get_artist_discography` but with defaults tuned for
  contributor credits (no role filter, includes non-primary appearances).
- "tell me about this record" → `search_release` → `get_release`
- "I have a catalog number" → `get_release_by_catalog_number`
- "who played on / who produced / credits for a release" → `get_release_credits`
- "what does this label put out?" → `search_label` → `get_label_releases` (titles) or
  `get_label_roster` (artists + counts). Add `include_credits=True` to
  `get_label_releases` for a role→names summary per release; fan out to
  `get_release_credits` for the handful that warrant full detail.
- "who are the recurring producers/engineers/designers behind this label?" →
  `get_label_credits_summary` (top-N people per role bucket across the catalog,
  one query instead of pulling many releases).
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
    `{columns, rows, row_count}` on success or `{error, query}` on failure. A
    non-fatal `hint` field is added when the query pattern looks slow.

    Schema: every table name is SINGULAR. Core entities: `artist`, `release`,
    `label`, `master`. Release children: `release_artist`, `release_label`,
    `release_track`, `release_track_artist`, `release_format`, `release_genre`,
    `release_style`, `release_identifier`, `release_company`, `release_video`.
    Master children: `master_artist`, `master_genre`, `master_style`,
    `master_video`. Artist extras: `artist_alias`, `artist_namevariation`,
    `artist_url`, `group_member`. Label extras: `label_url`. Tables named
    `*_image` are empty (Discogs strips images from the public dump). Call
    `describe_schema` for the full orientation — entity semantics, the
    `extra` flag on credits, and how years/compilations are modeled.

    Perf note: `LIKE '%x%'` / `ILIKE '%x%'` (wildcard prefix) on `artist.name`,
    `artist.realname`, `release.title`, or `label.name` forces a full table
    scan — often >1s on 10M+ artists / 19M releases. Those columns have
    FTS/BM25 indexes; use `search_artist`, `search_release`, `search_label`
    instead for name/title search. LIKE is fine on free-text columns like
    `release.notes` or `artist.profile` (not indexed, no alternative)."""
    try:
        sql_guard.validate(query)
    except ValueError as e:
        return {"error": str(e), "query": query}
    result = Q.run_readonly_sql(_CONN, query, int(row_limit))
    if "error" not in result:
        hint = sql_guard.lint_slow_patterns(query)
        if hint:
            result["hint"] = hint
    return result


@mcp.tool
@_log_call
def search_wikipedia(query: str, limit: int = 10) -> dict:
    """Full-text search over an OFFLINE English Wikipedia dump (text-only, no
    images). Ranked by relevance against article bodies via the ZIM's embedded
    index. Returns `{query, estimated_matches, results:[{title, path, snippet}]}`.
    Pass a `title` from the results to `get_wikipedia_article` to read the full
    text. `limit` clamped to [1, 50].

    Use this for context Discogs can't give you — band histories, genre/scene
    background, member biographies, label backstory, cultural context. Discogs
    remains the source of truth for discographies, credits, and catalog numbers;
    Wikipedia is for the prose around them. This is a local snapshot (June 2026),
    not the live web."""
    return W.search(query, limit)


@mcp.tool
@_log_call
def get_wikipedia_article(title: str, max_chars: int = 40000) -> dict:
    """Fetch a single OFFLINE Wikipedia article as clean plain text (chrome,
    references, and navboxes stripped; infobox facts kept). Resolves redirects
    automatically. On an exact-title miss, returns
    `{found: False, suggestions:[...]}` so you can retry with a better title —
    or call `search_wikipedia` first and pass a result's `title`/`path` here.
    `max_chars` (default 40000) truncates very long articles; clamped to
    [500, 200000]. Local June-2026 snapshot, not the live web."""
    return W.get_article(title, max_chars)


VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")


@mcp.tool
@_log_call
def write_stubs(targets: list[str], dry_run: bool = False) -> dict:
    """Write deterministic vault stubs for a list of `Folder/Name` targets
    (e.g. `["Bands/The Police", "Labels/Dindisc"]`; folder is one of Bands,
    People, Labels). Each stub is built entirely from Discogs — ID, real name,
    first release year, credit count, top styles/labels, credited roles, band
    membership, label parent/sublabels/roster — plus a gap-derived Research
    Queue listing only fields Discogs actually lacks.

    Use this instead of writing stubs yourself. The rows never enter your
    context: pass names, get back counts. Returns `{written, skipped_existing,
    unresolved:[{target, why}], discovered:[...], dry_run}`.

    **Never overwrites.** A target that already has a page is skipped and
    counted in `skipped_existing`, because it may be a finished article.
    Names that don't match Discogs exactly are reported in `unresolved` with
    candidate spellings rather than guessed at.

    `discovered` lists pages these stubs now link to that still don't exist —
    feed it straight back in as the next call's `targets` to close the loop,
    repeating until `discovered` comes back empty. Pass `dry_run=true` to see
    what would happen without writing.

    Does NOT write scene/genre prose or a releases table — this is the factual
    skeleton only."""
    return stubgen.write_stubs(_CONN, VAULT_PATH, targets, dry_run)


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8765)
