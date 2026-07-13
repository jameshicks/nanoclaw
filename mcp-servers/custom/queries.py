"""SQL queries for the Discogs MCP. Each public function takes a DuckDB connection plus params
and returns JSON-serializable dicts/lists. No FastMCP dependencies here so this file is unit-testable
in isolation."""

import re
from typing import Any, Iterable, Optional


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _one(cur) -> Optional[dict]:
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


# Name normalization for exact-match boost. Without this, BM25 ranks short
# namesakes above the canonical entity — "Thes" beats "James White & The Blacks"
# on "james white and the blacks" because BM25 is length-normalized, and the
# simple LOWER(name)=LOWER(?) fallback misses "&" vs "and" differences and
# Discogs-style "(2)" disambiguators.
_NORM_DISAMB = re.compile(r"\(\d+\)")
_NORM_AMP = re.compile(r"\s*&\s*")
_NORM_PUNCT = re.compile(r"[^a-z0-9 ]+")
_NORM_WS = re.compile(r"\s+")


def _normalize_name(s: str) -> str:
    s = s.lower()
    s = _NORM_DISAMB.sub("", s)
    s = _NORM_AMP.sub(" and ", s)
    s = _NORM_PUNCT.sub(" ", s)
    return _NORM_WS.sub(" ", s).strip()


def _sql_normalize(col: str) -> str:
    """SQL expression mirroring _normalize_name applied to a column."""
    return (
        "TRIM(regexp_replace("
        f"regexp_replace("
        f"regexp_replace("
        f"regexp_replace(LOWER({col}), '\\(\\d+\\)', '', 'g'),"
        f"'\\s*&\\s*', ' and ', 'g'),"
        f"'[^a-z0-9 ]+', ' ', 'g'),"
        f"'\\s+', ' ', 'g'))"
    )


# -------- Search (FTS-backed) --------


def _exact_artist(conn, name: str, limit: int) -> list[dict]:
    """Exact-name resolution: skip BM25 and the labels/years enrichment, match
    name directly. Returns only id/name/realname — a cheap, precise lookup for
    when the caller already knows the entity name.

    Two-step so the common case stays fast: a raw case-insensitive match first
    (a plain scan, ~1s), and only if that finds nothing do we pay for the
    normalized form that bridges "&"/"and" and Discogs "(2)" disambiguators
    (the regex normalize over every row roughly triples the cost)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name, realname
          FROM artist
         WHERE LOWER(name) = LOWER(?)
         ORDER BY LENGTH(name) ASC
         LIMIT ?
        """,
        [name, limit],
    )
    rows = _rows(cur)
    if rows:
        return rows
    name_norm_sql = _sql_normalize("name")
    cur.execute(
        f"""
        SELECT id, name, realname
          FROM artist
         WHERE {name_norm_sql} = ?
         ORDER BY LENGTH(name) ASC
         LIMIT ?
        """,
        [_normalize_name(name), limit],
    )
    return _rows(cur)


def search_artist(conn, name: str, limit: int, exact: bool = False) -> list[dict]:
    limit = _clamp(limit, 1, 100)
    if exact:
        return _exact_artist(conn, name, limit)
    # Pull a wider FTS candidate set, then re-rank in three tiers:
    #   0 — raw lowercase name matches the query verbatim (canonical entity
    #       always wins here — "Aphex Twin" beats "Aphex Twin (69)")
    #   1 — normalized match after stripping disambiguators, "&"/"and", punct
    #       (bridges "James White & The Blacks" vs "James White and the Blacks")
    #   2 — plain BM25 candidates
    # Within each tier: BM25 score DESC, then shorter name wins.
    fts_fetch = max(limit * 5, 50)
    norm_query = _normalize_name(name)
    name_norm_sql = _sql_normalize("a.name")
    hits_norm_sql = _sql_normalize("name")
    match_tier_sql_hits = (
        f"CASE WHEN LOWER(name) = LOWER(?) THEN 0 "
        f"WHEN {hits_norm_sql} = ? THEN 1 ELSE 2 END"
    )
    match_tier_sql_out = (
        f"CASE WHEN LOWER(a.name) = LOWER(?) THEN 0 "
        f"WHEN {name_norm_sql} = ? THEN 1 ELSE 2 END"
    )
    cur = conn.cursor()
    cur.execute(
        f"""
        WITH hits AS (
          SELECT id, score
            FROM (
              SELECT id, name, fts_main_artist.match_bm25(id, ?) AS score
                FROM artist
            ) t
           WHERE score IS NOT NULL
           ORDER BY {match_tier_sql_hits},
                    score DESC
           LIMIT ?
        ),
        labels AS (
          SELECT artist_id,
                 STRING_AGG(label_name, '; ' ORDER BY cnt DESC) AS top_labels
            FROM (
              SELECT ra.artist_id, rl.label_name,
                     COUNT(DISTINCT rl.release_id) AS cnt
                FROM release_artist ra
                JOIN release_label rl ON rl.release_id = ra.release_id
               WHERE ra.artist_id IN (SELECT id FROM hits)
                 AND ra.extra = 0
                 AND rl.label_name IS NOT NULL
               GROUP BY ra.artist_id, rl.label_name
              QUALIFY ROW_NUMBER() OVER (PARTITION BY ra.artist_id ORDER BY cnt DESC) <= 3
            )
            GROUP BY artist_id
        ),
        years AS (
          SELECT ra.artist_id,
                 MIN(r.released_year) AS y_start,
                 MAX(r.released_year) AS y_end
            FROM release_artist ra
            JOIN release r ON r.id = ra.release_id
           WHERE ra.artist_id IN (SELECT id FROM hits)
             AND r.released_year IS NOT NULL
           GROUP BY ra.artist_id
        )
        SELECT a.id, a.name, a.realname,
               CASE WHEN y.y_start IS NULL THEN NULL
                    WHEN y.y_start = y.y_end THEN CAST(y.y_start AS VARCHAR)
                    ELSE y.y_start || '-' || y.y_end
               END AS years_active,
               l.top_labels
          FROM hits h
          JOIN artist a ON a.id = h.id
          LEFT JOIN labels l ON l.artist_id = h.id
          LEFT JOIN years y ON y.artist_id = h.id
         ORDER BY {match_tier_sql_out},
                  h.score DESC,
                  LENGTH(a.name) ASC
         LIMIT ?
        """,
        [name, name, norm_query, fts_fetch, name, norm_query, limit],
    )
    return _rows(cur)


def search_release(
    conn,
    title: str,
    artist: Optional[str],
    year: Optional[int],
    limit: int,
) -> list[dict]:
    limit = _clamp(limit, 1, 100)
    fts_fetch = max(limit * 5, 50)
    norm_query = _normalize_name(title)
    title_norm_hits = _sql_normalize("rel.title")
    title_norm_out = _sql_normalize("h.title")
    match_tier_hits = (
        f"CASE WHEN LOWER(rel.title) = LOWER(?) THEN 0 "
        f"WHEN {title_norm_hits} = ? THEN 1 ELSE 2 END"
    )
    match_tier_out = (
        f"CASE WHEN LOWER(h.title) = LOWER(?) THEN 0 "
        f"WHEN {title_norm_out} = ? THEN 1 ELSE 2 END"
    )

    extra_clauses: list[str] = []
    extra_params: list[Any] = []
    if year is not None:
        extra_clauses.append("rel.released_year = ?")
        extra_params.append(int(year))
    if artist:
        extra_clauses.append(
            """EXISTS (
                SELECT 1 FROM release_artist ra
                LEFT JOIN artist a ON a.id = ra.artist_id
                WHERE ra.release_id = rel.id
                  AND ra.extra = 0
                  AND (ra.artist_name ILIKE ? OR a.name ILIKE ?)
            )"""
        )
        like = f"%{artist}%"
        extra_params.extend([like, like])
    extra_where = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""

    params = [
        title,
        *extra_params,
        title, norm_query,
        fts_fetch,
        title, norm_query,
        limit,
    ]

    cur = conn.cursor()
    cur.execute(
        f"""
        WITH hits AS (
          SELECT rel.id, rel.title, rel.released_year, rel.country, rel.master_id, rel.score
            FROM (
              SELECT r.id, r.title, r.released_year, r.country, r.master_id,
                     fts_main_release.match_bm25(r.id, ?) AS score
                FROM release r
            ) rel
           WHERE rel.score IS NOT NULL{extra_where}
           ORDER BY {match_tier_hits},
                    rel.score DESC
           LIMIT ?
        )
        SELECT h.id, h.title, h.released_year AS year, h.country, h.master_id,
               (SELECT STRING_AGG(DISTINCT artist_name, ' / ')
                  FROM release_artist ra WHERE ra.release_id = h.id AND ra.extra = 0) AS primary_artists,
               (SELECT STRING_AGG(DISTINCT label_name, '; ')
                  FROM release_label rl WHERE rl.release_id = h.id) AS labels
          FROM hits h
         ORDER BY {match_tier_out},
                  h.score DESC,
                  LENGTH(h.title) ASC
         LIMIT ?
        """,
        params,
    )
    return _rows(cur)


def _exact_label(conn, name: str, limit: int) -> list[dict]:
    """Exact-name label resolution: skip BM25, match name directly. Returns
    id/name/parent_name and release_count — the count directly informs the
    "stub a label only if it has more than one release" rule, without a
    follow-up call. Two-step (raw case-insensitive first, normalized only on a
    miss) to keep the common case fast."""
    count_sql = (
        "(SELECT COUNT(*) FROM release_label rl WHERE rl.label_id = l.id)"
        " AS release_count"
    )
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT l.id, l.name, l.parent_name, {count_sql}
          FROM label l
         WHERE LOWER(l.name) = LOWER(?)
         ORDER BY LENGTH(l.name) ASC
         LIMIT ?
        """,
        [name, limit],
    )
    rows = _rows(cur)
    if rows:
        return rows
    name_norm_sql = _sql_normalize("l.name")
    cur.execute(
        f"""
        SELECT l.id, l.name, l.parent_name, {count_sql}
          FROM label l
         WHERE {name_norm_sql} = ?
         ORDER BY LENGTH(l.name) ASC
         LIMIT ?
        """,
        [_normalize_name(name), limit],
    )
    return _rows(cur)


def search_label(conn, name: str, limit: int, exact: bool = False) -> list[dict]:
    limit = _clamp(limit, 1, 100)
    if exact:
        return _exact_label(conn, name, limit)
    fts_fetch = max(limit * 5, 50)
    norm_query = _normalize_name(name)
    hits_norm_sql = _sql_normalize("name")
    name_norm_sql = _sql_normalize("l.name")
    match_tier_hits = (
        f"CASE WHEN LOWER(name) = LOWER(?) THEN 0 "
        f"WHEN {hits_norm_sql} = ? THEN 1 ELSE 2 END"
    )
    match_tier_out = (
        f"CASE WHEN LOWER(l.name) = LOWER(?) THEN 0 "
        f"WHEN {name_norm_sql} = ? THEN 1 ELSE 2 END"
    )
    cur = conn.cursor()
    cur.execute(
        f"""
        WITH hits AS (
          SELECT id, score
            FROM (
              SELECT id, name, fts_main_label.match_bm25(id, ?) AS score
                FROM label
            ) t
           WHERE score IS NOT NULL
           ORDER BY {match_tier_hits},
                    score DESC
           LIMIT ?
        )
        SELECT l.id, l.name, l.parent_id, l.parent_name,
               (SELECT COUNT(*) FROM label sl WHERE sl.parent_id = l.id) AS sublabel_count,
               (SELECT COUNT(*) FROM release_label rl WHERE rl.label_id = l.id) AS release_count
          FROM hits h
          JOIN label l ON l.id = h.id
         ORDER BY {match_tier_out},
                  h.score DESC,
                  LENGTH(l.name) ASC
         LIMIT ?
        """,
        [name, name, norm_query, fts_fetch, name, norm_query, limit],
    )
    return _rows(cur)


# -------- Entity fetch --------


def get_artist(conn, artist_id: int, compact: bool = False) -> Optional[dict]:
    cur = conn.cursor()
    cols = "a.id, a.name, a.realname" if compact else (
        "a.id, a.name, a.realname, a.profile"
    )
    cur.execute(
        f"""
        SELECT {cols},
          (SELECT LIST({{'alias_artist_id': alias_artist_id, 'alias_name': alias_name}}
                       ORDER BY alias_name)
             FROM artist_alias WHERE artist_id = a.id) AS aliases,
          (SELECT LIST(name ORDER BY name)
             FROM artist_namevariation WHERE artist_id = a.id) AS name_variations,
          (SELECT LIST(url ORDER BY url)
             FROM artist_url WHERE artist_id = a.id) AS urls,
          (SELECT LIST({{'artist_id': gm.group_artist_id, 'name': ga.name}}
                       ORDER BY ga.name)
             FROM group_member gm
             LEFT JOIN artist ga ON ga.id = gm.group_artist_id
            WHERE gm.member_artist_id = a.id) AS groups,
          (SELECT LIST({{'artist_id': member_artist_id, 'name': member_name}}
                       ORDER BY member_name)
             FROM group_member WHERE group_artist_id = a.id) AS members
          FROM artist a WHERE a.id = ?
        """,
        [artist_id],
    )
    artist = _one(cur)
    if artist is None:
        return None
    for key in ("aliases", "name_variations", "urls", "groups", "members"):
        if artist.get(key) is None:
            artist[key] = []
    return artist


def get_release(conn, release_id: int, include_notes: bool = False) -> Optional[dict]:
    # Split into three queries. A single-SELECT design with LIST subqueries used
    # to work, but the double-correlated LIST-of-tracks-with-LIST-of-credits
    # triggers a cartesian blowup in DuckDB's planner (OOMs at 7+ GiB on a
    # 7-track release). The single-level LIST subqueries decorrelate fine, so
    # we keep those inline; only the tracklist moves to a separate fetch.
    cur = conn.cursor()
    notes_col = "r.notes" if include_notes else "NULL AS notes"
    cur.execute(
        """
        SELECT r.id, r.title, r.released_year, r.country, __NOTES__, r.master_id,
          (SELECT LIST({'artist_id': artist_id, 'artist_name': artist_name,
                        'extra': extra, 'anv': anv, 'position': position,
                        'join_string': join_string, 'role': role, 'tracks': tracks}
                       ORDER BY extra, position)
             FROM release_artist WHERE release_id = r.id) AS credits,
          (SELECT LIST({'name': name, 'qty': qty, 'descriptions': descriptions}
                       ORDER BY name)
             FROM release_format WHERE release_id = r.id) AS formats,
          (SELECT LIST({'label_id': label_id, 'label_name': label_name, 'catno': catno}
                       ORDER BY label_name)
             FROM release_label WHERE release_id = r.id) AS labels,
          (SELECT LIST(DISTINCT genre ORDER BY genre)
             FROM release_genre WHERE release_id = r.id) AS genres,
          (SELECT LIST(DISTINCT style ORDER BY style)
             FROM release_style WHERE release_id = r.id) AS styles,
          (SELECT LIST({'type': "type", 'value': "value", 'description': description}
                       ORDER BY "type", "value")
             FROM release_identifier WHERE release_id = r.id) AS identifiers
          FROM release r WHERE r.id = ?
        """.replace("__NOTES__", notes_col),
        [release_id],
    )
    row = _one(cur)
    if row is None:
        return None

    cur.execute(
        """
        SELECT track_id, sequence, position, parent, title, duration
          FROM release_track
         WHERE release_id = ?
         ORDER BY sequence, position
        """,
        [release_id],
    )
    tracks = _rows(cur)

    cur.execute(
        """
        SELECT track_id, artist_id, artist_name, extra, anv, position,
               join_string, role
          FROM release_track_artist
         WHERE release_id = ?
         ORDER BY track_id, extra, position
        """,
        [release_id],
    )
    track_credits: dict[Any, list] = {}
    for ta in _rows(cur):
        tid = ta.pop("track_id")
        ta.pop("extra", None)
        track_credits.setdefault(tid, []).append(ta)
    for t in tracks:
        t["credits"] = track_credits.get(t["track_id"], [])

    release = {k: row[k] for k in (
        "id", "title", "released_year", "country", "notes", "master_id",
    )}
    creds = row["credits"] or []
    primary = [{k: v for k, v in c.items() if k != "extra"} for c in creds if c["extra"] == 0]
    additional = [{k: v for k, v in c.items() if k != "extra"} for c in creds if c["extra"] == 1]

    return {
        "release": release,
        "tracklist": tracks,
        "credits": {"primary": primary, "additional": additional},
        "formats": row["formats"] or [],
        "labels": row["labels"] or [],
        "genres": row["genres"] or [],
        "styles": row["styles"] or [],
        "identifiers": row["identifiers"] or [],
    }


def get_release_credits(conn, release_id: int) -> Optional[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.id AS release_id, r.title, r.released_year,
          (SELECT LIST({'artist_id': artist_id, 'artist_name': artist_name,
                        'anv': anv, 'position': position,
                        'join_string': join_string, 'role': role, 'tracks': tracks,
                        'extra': extra}
                       ORDER BY extra, position)
             FROM release_artist WHERE release_id = r.id) AS credits
          FROM release r WHERE r.id = ?
        """,
        [release_id],
    )
    row = _one(cur)
    if row is None:
        return None
    creds = row["credits"] or []
    primary = [{k: v for k, v in c.items() if k != "extra"} for c in creds if c["extra"] == 0]
    additional = [{k: v for k, v in c.items() if k != "extra"} for c in creds if c["extra"] == 1]
    return {
        "release_id": row["release_id"],
        "title": row["title"],
        "released_year": row["released_year"],
        "primary": primary,
        "additional": additional,
    }


def get_label(conn, label_id: int, compact: bool = False) -> Optional[dict]:
    cur = conn.cursor()
    cols = (
        "id, name, parent_id, parent_name"
        if compact
        else "id, name, contact_info, profile, parent_id, parent_name"
    )
    cur.execute(
        f"SELECT {cols} FROM label WHERE id = ?",
        [label_id],
    )
    label = _one(cur)
    if label is None:
        return None

    cur.execute(
        "SELECT id, name FROM label WHERE parent_id = ? ORDER BY name LIMIT 500",
        [label_id],
    )
    label["sublabels"] = _rows(cur)

    cur.execute(
        "SELECT url FROM label_url WHERE label_id = ? ORDER BY url",
        [label_id],
    )
    label["urls"] = [r["url"] for r in _rows(cur)]

    return label


# -------- Traversal --------


# Discogs "special" placeholder artists — not real entities. They carry enormous
# credit counts (Various alone: ~1.3M primary credits), so admitting them into
# the collaboration self-join both explodes the join and produces meaningless
# edges ("everyone collaborated with Various"). Excluded from every graph
# traversal, on both the seed and neighbor sides. Keep in sync with
# build_edge_table.py.
SPECIAL_ARTIST_IDS = (194, 355, 118760)  # Various, Unknown Artist, No Artist
_SPECIAL_IDS_SQL = ", ".join(str(i) for i in SPECIAL_ARTIST_IDS)


def _not_special(alias: str) -> str:
    """SQL predicate excluding the special placeholder artists for a table alias."""
    return f"{alias}.artist_id NOT IN ({_SPECIAL_IDS_SQL})"


def _int_in(ids) -> str:
    """Render an int-id collection as an inline SQL IN-list literal, e.g. '(1, 2, 3)'
    (empty -> '(NULL)'). Ids are coerced to int, so this is injection-safe.

    Why inline instead of a parameterized `IN (SELECT UNNEST(CAST(? AS BIGINT[])))`:
    DuckDB can't push the UNNEST-subquery form into the scan as a filter, so the
    seed-side predicate forces a full scan of the ~90M-row edge table (~13s for a
    single seed vs ~0.7s inlined). The frontiers here are bounded (<=2000 / capped
    at 5000) so the IN-list stays a reasonable size."""
    vals = [str(int(x)) for x in ids]
    return "(" + ", ".join(vals) + ")" if vals else "(NULL)"


# The table the graph self-joins scan. Defaults to the full release_artist;
# server.py flips this to the narrow `release_artist_slim` projection (built by
# build_edge_table.py) at startup when present — same columns the self-joins
# touch, far fewer bytes per row, specials pre-removed.
EDGE_TABLE = "release_artist"


def configure_edge_table(conn) -> str:
    """Detect the slim edge table and switch the graph queries to it if present.
    Returns the table name now in use. Called once at server startup."""
    global EDGE_TABLE
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'release_artist_slim'"
    ).fetchone()
    EDGE_TABLE = "release_artist_slim" if exists else "release_artist"
    return EDGE_TABLE


# Non-musical role patterns used by the "musical" alias below — package/art
# direction and business credits that agents generally want to exclude when
# asking "who played on this?"
_NON_MUSICAL_PATTERNS = (
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


def _role_predicate(
    alias: str, key: str, precomputed_musical: bool = False
) -> tuple[str, list[Any]]:
    """Build a SQL predicate fragment for role filtering, parameterized by table alias.

    Known keys map to curated clauses; anything else becomes a substring ILIKE.
    Returns (sql_fragment, params). Params is empty for curated clauses,
    non-empty for the substring fallthrough or the "musical" blocklist alias.

    precomputed_musical=True — the alias table carries the is_non_musical boolean
    (release_artist_slim), so the "musical" blocklist collapses from 18 per-row
    ILIKEs to a single boolean read. Only pass this for tables built with that
    column."""
    key = key.strip().lower()
    col = f"{alias}.role"
    if key == "performer":
        return f"({col} IS NULL OR {col} = '')", []
    if key == "producer":
        return f"{col} ILIKE '%Producer%'", []
    if key == "writer":
        return (
            f"({col} ILIKE '%Written-By%' OR {col} ILIKE '%Composed By%' OR {col} ILIKE '%Lyrics By%')",
            [],
        )
    if key == "engineer":
        return (
            f"({col} ILIKE '%Engineer%' OR {col} ILIKE '%Mix%' OR {col} ILIKE '%Mastered%')",
            [],
        )
    if key == "musical":
        if precomputed_musical:
            return f"({col} IS NULL OR NOT {alias}.is_non_musical)", []
        blocks = " AND ".join(f"{col} NOT ILIKE ?" for _ in _NON_MUSICAL_PATTERNS)
        return f"({col} IS NULL OR ({blocks}))", list(_NON_MUSICAL_PATTERNS)
    if key:
        return f"{col} ILIKE ?", [f"%{key}%"]
    return "TRUE", []


def _roles_clause(
    alias: str, roles: Optional[Any], precomputed_musical: bool = False
) -> tuple[Optional[str], list[Any]]:
    """Normalize roles arg (None | str | list[str]) into an OR-combined predicate
    against the given table alias. Returns (clause_or_None, params)."""
    if roles is None:
        return None, []
    if isinstance(roles, str):
        roles = [roles]
    if not roles:
        return None, []
    frags: list[str] = []
    params: list[Any] = []
    for r in roles:
        frag, fp = _role_predicate(alias, r, precomputed_musical)
        if frag == "TRUE":
            continue
        frags.append(frag)
        params.extend(fp)
    if not frags:
        return None, []
    return "(" + " OR ".join(frags) + ")", params


def get_artist_discography(
    conn,
    artist_id: int,
    role: Optional[str],
    as_main_only: bool,
    year_range: Optional[tuple[int, int]],
    unique_masters_only: bool = True,
) -> list[dict]:
    clauses = ["ra.artist_id = ?"]
    params: list[Any] = [artist_id]

    if as_main_only:
        clauses.append("ra.extra = 0")

    if year_range is not None:
        lo, hi = year_range
        clauses.append("r.released_year BETWEEN ? AND ?")
        params.extend([int(lo), int(hi)])

    if role is not None:
        frag, fp = _role_predicate("ra", role)
        if frag != "TRUE":
            clauses.append(frag)
            params.extend(fp)

    where = " AND ".join(clauses)
    cur = conn.cursor()

    # Deterministic aggregates per (artist, release):
    #   role       — join all distinct roles ("Producer; Written-By") so
    #                multi-role credits don't get truncated to an arbitrary pick.
    #   as_primary — true iff at least one credit on this release is primary
    #                (ra.extra=0). Distinguishes "the artist's own release" from
    #                "contributor-only appearance" (comps, guest credits, etc.).
    if unique_masters_only:
        # Collapse to one row per master (earliest-year, lowest-id pressing).
        # Standalone releases (master_id IS NULL) keyed by -id — Discogs IDs
        # are strictly positive so there's no collision with real master_ids.
        cur.execute(
            f"""
            WITH releases AS (
              SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                     STRING_AGG(DISTINCT ra.role, '; ' ORDER BY ra.role) AS role,
                     MIN(ra.extra) = 0 AS as_primary
                FROM release_artist ra
                JOIN release r ON r.id = ra.release_id
               WHERE {where}
               GROUP BY r.id, r.title, r.released_year, r.country, r.master_id
            ),
            deduped AS (
              SELECT *,
                     ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(master_id, -id)
                       ORDER BY year NULLS LAST, id
                     ) AS _rank,
                     COUNT(*) OVER (PARTITION BY COALESCE(master_id, -id)) AS pressings_count
                FROM releases
            ),
            top_rows AS (
              SELECT id, title, year, country, master_id, role, as_primary, pressings_count
                FROM deduped
               WHERE _rank = 1
               ORDER BY year NULLS LAST, title
               LIMIT 500
            ),
            labels_agg AS (
              SELECT rl.release_id,
                     STRING_AGG(DISTINCT rl.label_name, '; ' ORDER BY rl.label_name) AS labels
                FROM release_label rl
                JOIN top_rows t ON t.id = rl.release_id
               GROUP BY rl.release_id
            )
            SELECT t.id, t.title, t.year, t.country, t.master_id,
                   t.role, t.as_primary, t.pressings_count, la.labels
              FROM top_rows t
              LEFT JOIN labels_agg la ON la.release_id = t.id
             ORDER BY t.year NULLS LAST, t.title
            """,
            params,
        )
    else:
        cur.execute(
            f"""
            WITH base AS (
              SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                     STRING_AGG(DISTINCT ra.role, '; ' ORDER BY ra.role) AS role,
                     MIN(ra.extra) = 0 AS as_primary
                FROM release_artist ra
                JOIN release r ON r.id = ra.release_id
               WHERE {where}
               GROUP BY r.id, r.title, r.released_year, r.country, r.master_id
               ORDER BY year NULLS LAST, r.title
               LIMIT 500
            ),
            labels_agg AS (
              SELECT rl.release_id,
                     STRING_AGG(DISTINCT rl.label_name, '; ' ORDER BY rl.label_name) AS labels
                FROM release_label rl
                JOIN base b ON b.id = rl.release_id
               GROUP BY rl.release_id
            )
            SELECT b.id, b.title, b.year, b.country, b.master_id,
                   b.role, b.as_primary, la.labels
              FROM base b
              LEFT JOIN labels_agg la ON la.release_id = b.id
             ORDER BY b.year NULLS LAST, b.title
            """,
            params,
        )
    return _rows(cur)


def get_label_releases(
    conn,
    label_id: int,
    year_range: Optional[tuple[int, int]],
    unique_masters_only: bool = True,
    include_credits: bool = False,
) -> list[dict]:
    clauses = ["rl.label_id = ?"]
    params: list[Any] = [label_id]
    if year_range is not None:
        lo, hi = year_range
        clauses.append("r.released_year BETWEEN ? AND ?")
        params.extend([int(lo), int(hi)])
    where = " AND ".join(clauses)
    cur = conn.cursor()

    # Summary-only: role → [artist_name]. Primary artists are already in
    # `primary_artists`; this aggregates extra (non-primary) credits grouped
    # by role. For per-track detail, anv, join_string, etc., call
    # get_release_credits(release_id) on the specific release.
    credits_cte = """,
            credits_per_role AS (
              SELECT ra.release_id, ra.role,
                     LIST(DISTINCT ra.artist_name ORDER BY ra.artist_name) AS artists
                FROM release_artist ra
                JOIN top_rows t ON t.id = ra.release_id
               WHERE ra.extra = 1
                 AND ra.role IS NOT NULL
                 AND TRIM(ra.role) <> ''
               GROUP BY ra.release_id, ra.role
            ),
            credits_agg AS (
              SELECT release_id,
                     LIST({'role': role, 'artists': artists} ORDER BY role) AS credits
                FROM credits_per_role
               GROUP BY release_id
            )""" if include_credits else ""
    credits_select = ", ca.credits" if include_credits else ""
    credits_join = "LEFT JOIN credits_agg ca ON ca.release_id = t.id" if include_credits else ""

    # `catno` can have multiple distinct values per (release, label) in rare
    # cases (different sides/discs carrying different cat numbers). Join them
    # so the output is stable.
    if unique_masters_only:
        cur.execute(
            f"""
            WITH releases AS (
              SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                     STRING_AGG(DISTINCT rl.catno, '; ' ORDER BY rl.catno) AS catno
                FROM release_label rl
                JOIN release r ON r.id = rl.release_id
               WHERE {where}
               GROUP BY r.id, r.title, r.released_year, r.country, r.master_id
            ),
            deduped AS (
              SELECT *,
                     ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(master_id, -id)
                       ORDER BY year NULLS LAST, id
                     ) AS _rank,
                     COUNT(*) OVER (PARTITION BY COALESCE(master_id, -id)) AS pressings_count
                FROM releases
            ),
            top_rows AS (
              SELECT id, title, year, country, master_id, catno, pressings_count
                FROM deduped
               WHERE _rank = 1
               ORDER BY year NULLS LAST, title
               LIMIT 500
            ),
            artists_agg AS (
              SELECT ra.release_id,
                     STRING_AGG(DISTINCT ra.artist_name, ' / ' ORDER BY ra.artist_name) AS primary_artists
                FROM release_artist ra
                JOIN top_rows t ON t.id = ra.release_id
               WHERE ra.extra = 0
               GROUP BY ra.release_id
            ){credits_cte}
            SELECT t.id, t.title, t.year, t.country, t.master_id,
                   t.catno, t.pressings_count, aa.primary_artists{credits_select}
              FROM top_rows t
              LEFT JOIN artists_agg aa ON aa.release_id = t.id
              {credits_join}
             ORDER BY t.year NULLS LAST, t.title
            """,
            params,
        )
    else:
        # In the non-unique-masters branch the CTE name is `base`, not `top_rows`.
        credits_cte_base = credits_cte.replace("JOIN top_rows t", "JOIN base t")
        cur.execute(
            f"""
            WITH base AS (
              SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                     STRING_AGG(DISTINCT rl.catno, '; ' ORDER BY rl.catno) AS catno
                FROM release_label rl
                JOIN release r ON r.id = rl.release_id
               WHERE {where}
               GROUP BY r.id, r.title, r.released_year, r.country, r.master_id
               ORDER BY year NULLS LAST, r.title
               LIMIT 500
            ),
            artists_agg AS (
              SELECT ra.release_id,
                     STRING_AGG(DISTINCT ra.artist_name, ' / ' ORDER BY ra.artist_name) AS primary_artists
                FROM release_artist ra
                JOIN base b ON b.id = ra.release_id
               WHERE ra.extra = 0
               GROUP BY ra.release_id
            ){credits_cte_base}
            SELECT b.id, b.title, b.year, b.country, b.master_id,
                   b.catno, aa.primary_artists{credits_select.replace('ca.credits', 'ca.credits')}
              FROM base b
              LEFT JOIN artists_agg aa ON aa.release_id = b.id
              {credits_join.replace('t.id', 'b.id')}
             ORDER BY b.year NULLS LAST, b.title
            """,
            params,
        )

    rows = _rows(cur)
    if include_credits:
        for row in rows:
            entries = row.get("credits") or []
            row["credits"] = {e["role"]: e["artists"] for e in entries}
    return rows


def get_label_roster(
    conn,
    label_id: int,
    year_range: Optional[tuple[int, int]],
) -> list[dict]:
    clauses = ["rl.label_id = ?", "ra.extra = 0"]
    params: list[Any] = [label_id]
    if year_range is not None:
        lo, hi = year_range
        clauses.append("r.released_year BETWEEN ? AND ?")
        params.extend([int(lo), int(hi)])
    where = " AND ".join(clauses)

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT ra.artist_id, a.name,
               COUNT(DISTINCT r.id) AS release_count,
               MIN(r.released_year) AS first_year,
               MAX(r.released_year) AS last_year
          FROM release_label rl
          JOIN release r ON r.id = rl.release_id
          JOIN release_artist ra ON ra.release_id = r.id
          LEFT JOIN artist a ON a.id = ra.artist_id
         WHERE {where}
         GROUP BY ra.artist_id, a.name
         ORDER BY release_count DESC, a.name
         LIMIT 200
        """,
        params,
    )
    return _rows(cur)


def find_collaborators(
    conn,
    artist_id: int,
    depth: int,
    min_shared_releases: int,
    roles: Optional[Any] = None,
    include_top_shared_titles: bool = True,
) -> list[dict]:
    depth = _clamp(depth, 1, 3)
    min_shared = max(1, int(min_shared_releases))

    # The slim edge table carries the precomputed is_non_musical boolean, so the
    # "musical" blocklist becomes a single boolean read instead of 18 ILIKEs per
    # row of the ~90M-row self-join.
    role_clause, role_params = _roles_clause(
        "ra2", roles, precomputed_musical=(EDGE_TABLE == "release_artist_slim")
    )
    role_sql = f" AND {role_clause}" if role_clause else ""

    visited: set[int] = {artist_id}
    frontier: set[int] = {artist_id}
    results: list[dict] = []

    cur = conn.cursor()
    for d in range(1, depth + 1):
        if not frontier:
            break
        if include_top_shared_titles:
            # One pass per hop: MATERIALIZED edges CTE feeds both the neighbor
            # counts and the top-3 shared titles, avoiding the double self-join.
            # Title dedup is by title (not release_id) so pressings/editions
            # collapse; year-then-id ordering makes the titles read as a rough
            # timeline.
            cur.execute(
                f"""
                WITH edges AS MATERIALIZED (
                  SELECT ra2.artist_id AS neighbor_id, a.name,
                         ra2.release_id, r.title, r.released_year AS year
                    FROM {EDGE_TABLE} ra1
                    JOIN {EDGE_TABLE} ra2
                      ON ra2.release_id = ra1.release_id
                     AND ra2.artist_id <> ra1.artist_id
                    JOIN release r ON r.id = ra1.release_id
                    LEFT JOIN artist a ON a.id = ra2.artist_id
                   WHERE ra1.artist_id IN {_int_in(frontier)}
                     AND ra1.extra = 0
                     AND {_not_special('ra1')}
                     AND {_not_special('ra2')}
                     AND ra2.artist_id NOT IN {_int_in(visited)}
                     {role_sql}
                ),
                neighbor_stats AS (
                  SELECT neighbor_id, ANY_VALUE(name) AS name,
                         COUNT(DISTINCT release_id) AS shared_releases
                    FROM edges
                   GROUP BY neighbor_id
                  HAVING COUNT(DISTINCT release_id) >= ?
                   ORDER BY shared_releases DESC
                   LIMIT 2000
                ),
                title_stats AS (
                  SELECT e.neighbor_id, e.title,
                         MIN(COALESCE(e.year, 9999)) AS year_rank,
                         MIN(e.release_id) AS id_rank
                    FROM edges e
                   WHERE e.neighbor_id IN (SELECT neighbor_id FROM neighbor_stats)
                   GROUP BY e.neighbor_id, e.title
                ),
                ranked AS (
                  SELECT neighbor_id, title,
                         ROW_NUMBER() OVER (
                           PARTITION BY neighbor_id
                           ORDER BY year_rank, id_rank
                         ) AS rn
                    FROM title_stats
                )
                SELECT ns.neighbor_id AS artist_id, ns.name, ns.shared_releases,
                       (SELECT LIST(title ORDER BY rn)
                          FROM ranked
                         WHERE neighbor_id = ns.neighbor_id AND rn <= 3) AS top_shared_titles
                  FROM neighbor_stats ns
                 ORDER BY ns.shared_releases DESC
                """,
                [*role_params, min_shared],
            )
        else:
            cur.execute(
                f"""
                SELECT ra2.artist_id, a.name,
                       COUNT(DISTINCT ra2.release_id) AS shared_releases
                  FROM {EDGE_TABLE} ra1
                  JOIN {EDGE_TABLE} ra2
                    ON ra2.release_id = ra1.release_id
                   AND ra2.artist_id <> ra1.artist_id
                  LEFT JOIN artist a ON a.id = ra2.artist_id
                 WHERE ra1.artist_id IN {_int_in(frontier)}
                   AND ra1.extra = 0
                   AND {_not_special('ra1')}
                   AND {_not_special('ra2')}
                   AND ra2.artist_id NOT IN {_int_in(visited)}
                   {role_sql}
                 GROUP BY ra2.artist_id, a.name
                HAVING COUNT(DISTINCT ra2.release_id) >= ?
                 ORDER BY shared_releases DESC
                 LIMIT 2000
                """,
                [*role_params, min_shared],
            )

        new_frontier: set[int] = set()
        for row in _rows(cur):
            aid = row["artist_id"]
            if aid in visited:
                continue
            visited.add(aid)
            new_frontier.add(aid)
            results.append(
                {
                    "artist_id": aid,
                    "name": row["name"],
                    "distance": d,
                    "shared_releases": row["shared_releases"],
                    "top_shared_titles": row.get("top_shared_titles") or [],
                }
            )

        frontier = new_frontier

    results.sort(key=lambda r: (r["distance"], -r["shared_releases"]))
    results = results[:500]

    if results:
        neighbor_ids = [r["artist_id"] for r in results]
        cur.execute(
            f"""
            SELECT artist_id, alias_artist_id, alias_name
              FROM artist_alias
             WHERE artist_id IN {_int_in(neighbor_ids)}
             ORDER BY artist_id, alias_name
            """
        )
        aliases_by_artist: dict[int, list[dict]] = {}
        for aid, alias_id, alias_name in cur.fetchall():
            aliases_by_artist.setdefault(aid, []).append(
                {"id": alias_id, "name": alias_name}
            )
        for r in results:
            r["aliases"] = aliases_by_artist.get(r["artist_id"], [])

    return results


def find_path_between_artists(
    conn,
    artist_a_id: int,
    artist_b_id: int,
    max_depth: int,
) -> Optional[dict]:
    if artist_a_id == artist_b_id:
        return {"path": [{"id": artist_a_id}], "depth": 0}
    max_depth = _clamp(max_depth, 1, 6)

    # Bidirectional BFS. The graph is directed (edges go from a primary-credited
    # artist to anyone credited on their release), so we expand forward from A
    # (ra1.artist_id IN frontier) and backward from B (ra2.artist_id IN frontier,
    # collecting predecessors ra1). Each iteration expands whichever side has
    # the smaller frontier — this is where the big win comes from: on prolific
    # seeds the other side stays tiny and caps the total work.
    FRONTIER_CAP = 5000
    parents_a: dict[int, Optional[int]] = {artist_a_id: None}
    parents_b: dict[int, Optional[int]] = {artist_b_id: None}
    frontier_a: set[int] = {artist_a_id}
    frontier_b: set[int] = {artist_b_id}
    depth_a = 0
    depth_b = 0
    cur = conn.cursor()

    def build_path(meet: int) -> dict:
        forward: list[int] = []
        n: Optional[int] = meet
        while n is not None:
            forward.append(n)
            n = parents_a[n]
        forward.reverse()  # A ... meet
        backward: list[int] = []
        n = parents_b[meet]
        while n is not None:
            backward.append(n)
            n = parents_b[n]
        full = forward + backward  # A ... meet ... B
        cur.execute(
            f"SELECT id, name FROM artist WHERE id IN {_int_in(full)}"
        )
        name_map = {r[0]: r[1] for r in cur.fetchall()}
        return {
            "path": [{"id": i, "name": name_map.get(i)} for i in full],
            "depth": len(full) - 1,
        }

    def capped(frontier: set[int]) -> list[int]:
        if len(frontier) > FRONTIER_CAP:
            return list(frontier)[:FRONTIER_CAP]
        return list(frontier)

    while depth_a + depth_b < max_depth:
        if not frontier_a and not frontier_b:
            break
        # Prefer the smaller non-empty frontier.
        if not frontier_a:
            expand_a = False
        elif not frontier_b:
            expand_a = True
        else:
            expand_a = len(frontier_a) <= len(frontier_b)

        if expand_a:
            cur.execute(
                f"""
                SELECT DISTINCT ra1.artist_id AS from_id, ra2.artist_id AS to_id
                  FROM {EDGE_TABLE} ra1
                  JOIN {EDGE_TABLE} ra2
                    ON ra2.release_id = ra1.release_id
                   AND ra2.artist_id <> ra1.artist_id
                 WHERE ra1.artist_id IN {_int_in(capped(frontier_a))}
                   AND ra1.extra = 0
                   AND {_not_special('ra1')}
                   AND {_not_special('ra2')}
                """
            )
            new_frontier: set[int] = set()
            for from_id, to_id in cur.fetchall():
                if to_id in parents_a:
                    continue
                parents_a[to_id] = from_id
                new_frontier.add(to_id)
            depth_a += 1
            meet = new_frontier & parents_b.keys()
            if meet:
                return build_path(next(iter(meet)))
            frontier_a = new_frontier
        else:
            # Backward expand: find predecessors of nodes in frontier_b.
            cur.execute(
                f"""
                SELECT DISTINCT ra1.artist_id AS from_id, ra2.artist_id AS to_id
                  FROM {EDGE_TABLE} ra1
                  JOIN {EDGE_TABLE} ra2
                    ON ra2.release_id = ra1.release_id
                   AND ra2.artist_id <> ra1.artist_id
                 WHERE ra2.artist_id IN {_int_in(capped(frontier_b))}
                   AND ra1.extra = 0
                   AND {_not_special('ra1')}
                   AND {_not_special('ra2')}
                """
            )
            new_frontier = set()
            for from_id, to_id in cur.fetchall():
                # `from_id` is a predecessor; its parent in the B-tree points
                # toward B via `to_id` (an already-visited B-side node).
                if from_id in parents_b:
                    continue
                parents_b[from_id] = to_id
                new_frontier.add(from_id)
            depth_b += 1
            meet = new_frontier & parents_a.keys()
            if meet:
                return build_path(next(iter(meet)))
            frontier_b = new_frontier

    if not frontier_a and not frontier_b:
        return {"path": None, "reason": "no_path", "max_depth_searched": depth_a + depth_b}
    return {"path": None, "reason": "max_depth_exceeded", "max_depth_searched": max_depth}


# -------- Aggregates --------


def get_scene_snapshot(
    conn,
    label_ids: Optional[list[int]],
    year_range: Optional[tuple[int, int]],
    country: Optional[str],
) -> dict:
    release_filters = []
    params: list[Any] = []
    if label_ids:
        release_filters.append(
            "EXISTS (SELECT 1 FROM release_label rl "
            "WHERE rl.release_id = r.id "
            "AND rl.label_id IN (SELECT UNNEST(CAST(? AS BIGINT[]))))"
        )
        params.append([int(x) for x in label_ids])
    if year_range is not None:
        lo, hi = year_range
        release_filters.append("r.released_year BETWEEN ? AND ?")
        params.extend([int(lo), int(hi)])
    if country:
        release_filters.append("r.country ILIKE ?")
        params.append(country)
    if not release_filters:
        raise ValueError("get_scene_snapshot requires at least one filter (label_ids, year_range, or country)")

    where = " AND ".join(release_filters)

    # Single statement: MATERIALIZED matching_releases is computed once, then
    # shared across the count + five top-N aggregates. Replaces six separate
    # executions of the same CTE.
    cur = conn.cursor()
    cur.execute(
        f"""
        WITH matching_releases AS MATERIALIZED (
          SELECT r.id
            FROM release r
           WHERE {where}
        ),
        top_artists AS (
          SELECT ra.artist_id, ANY_VALUE(a.name) AS name,
                 COUNT(DISTINCT ra.release_id) AS release_count
            FROM release_artist ra
            JOIN matching_releases mr ON mr.id = ra.release_id
            LEFT JOIN artist a ON a.id = ra.artist_id
           WHERE ra.extra = 0
           GROUP BY ra.artist_id
           ORDER BY release_count DESC
           LIMIT 20
        ),
        top_releases AS (
          SELECT r.id, r.title, r.released_year AS year,
                 COUNT(*) AS credit_count
            FROM release_artist ra
            JOIN matching_releases mr ON mr.id = ra.release_id
            JOIN release r ON r.id = mr.id
           GROUP BY r.id, r.title, r.released_year
           ORDER BY credit_count DESC
           LIMIT 20
        ),
        top_formats AS (
          SELECT rf.name AS format, COUNT(*) AS n
            FROM release_format rf
            JOIN matching_releases mr ON mr.id = rf.release_id
           GROUP BY rf.name ORDER BY n DESC LIMIT 25
        ),
        top_genres AS (
          SELECT rg.genre, COUNT(*) AS n
            FROM release_genre rg
            JOIN matching_releases mr ON mr.id = rg.release_id
           GROUP BY rg.genre ORDER BY n DESC LIMIT 25
        ),
        top_styles AS (
          SELECT rs.style, COUNT(*) AS n
            FROM release_style rs
            JOIN matching_releases mr ON mr.id = rs.release_id
           GROUP BY rs.style ORDER BY n DESC LIMIT 25
        )
        SELECT
          (SELECT COUNT(*) FROM matching_releases) AS release_count,
          (SELECT LIST({{'artist_id': artist_id, 'name': name, 'release_count': release_count}}
                       ORDER BY release_count DESC)
             FROM top_artists) AS top_artists,
          (SELECT LIST({{'id': id, 'title': title, 'year': year, 'credit_count': credit_count}}
                       ORDER BY credit_count DESC)
             FROM top_releases) AS top_releases,
          (SELECT LIST({{'format': format, 'n': n}} ORDER BY n DESC)
             FROM top_formats) AS formats,
          (SELECT LIST({{'genre': genre, 'n': n}} ORDER BY n DESC)
             FROM top_genres) AS genres,
          (SELECT LIST({{'style': style, 'n': n}} ORDER BY n DESC)
             FROM top_styles) AS styles
        """,
        params,
    )
    row = _one(cur)
    return {
        "release_count": row["release_count"],
        "top_artists": row["top_artists"] or [],
        "top_releases": row["top_releases"] or [],
        "formats": row["formats"] or [],
        "genres": row["genres"] or [],
        "styles": row["styles"] or [],
    }


def list_compilations_featuring_artist(conn, artist_id: int) -> list[dict]:
    # Single pass over release_format per artist-release: one aggregate row per
    # release gives both the "is this a (non-unofficial) compilation" gate and
    # the Compilation-filtered descriptions, replacing three correlated scans.
    cur = conn.cursor()
    cur.execute(
        """
        WITH artist_releases AS (
          SELECT DISTINCT r.id, r.title, r.released_year AS year, r.country
            FROM release_artist ra
            JOIN release r ON r.id = ra.release_id
           WHERE ra.artist_id = ?
        ),
        compilation_info AS (
          SELECT rf.release_id,
                 STRING_AGG(DISTINCT rf.descriptions, ' | ')
                   FILTER (WHERE rf.descriptions ILIKE '%Compilation%') AS format_descriptions,
                 BOOL_OR(rf.descriptions ILIKE '%Compilation%'
                         AND (rf.descriptions NOT ILIKE '%Unofficial%'
                              OR rf.descriptions IS NULL)) AS is_compilation
            FROM release_format rf
            JOIN artist_releases ar ON ar.id = rf.release_id
           GROUP BY rf.release_id
        )
        SELECT ar.id, ar.title, ar.year, ar.country,
               (SELECT STRING_AGG(DISTINCT label_name, '; ')
                  FROM release_label rl WHERE rl.release_id = ar.id) AS labels,
               ci.format_descriptions
          FROM artist_releases ar
          JOIN compilation_info ci ON ci.release_id = ar.id
         WHERE ci.is_compilation
         ORDER BY ar.year NULLS LAST, ar.title
         LIMIT 500
        """,
        [artist_id],
    )
    return _rows(cur)


def get_release_by_catalog_number(
    conn,
    cat_no: str,
    label_id: Optional[int] = None,
) -> list[dict]:
    clauses = ["UPPER(TRIM(rl.catno)) = UPPER(TRIM(?))"]
    params: list[Any] = [cat_no]
    if label_id is not None:
        clauses.append("rl.label_id = ?")
        params.append(int(label_id))
    where = " AND ".join(clauses)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
               rl.label_id, rl.label_name, rl.catno,
               (SELECT STRING_AGG(DISTINCT artist_name, ' / ')
                  FROM release_artist ra
                 WHERE ra.release_id = r.id AND ra.extra = 0) AS primary_artists
          FROM release_label rl
          JOIN release r ON r.id = rl.release_id
         WHERE {where}
         ORDER BY rl.label_id, r.released_year NULLS LAST, r.id
         LIMIT 100
        """,
        params,
    )
    return _rows(cur)


# Role buckets used by get_label_credits_summary. Order matters — first match
# wins, so producer comes before engineer (so "Mix Producer" buckets to producer
# not engineer). "performer" catches NULL/empty roles (the primary credit case).
_LABEL_CREDITS_BUCKETS_SQL = """
CASE
  WHEN ra.role IS NULL OR TRIM(ra.role) = '' THEN 'performer'
  WHEN ra.role ILIKE '%Producer%' THEN 'producer'
  WHEN ra.role ILIKE '%Engineer%' OR ra.role ILIKE '%Mix%' OR ra.role ILIKE '%Master%' THEN 'engineer'
  WHEN ra.role ILIKE '%Written-By%' OR ra.role ILIKE '%Composed%' OR ra.role ILIKE '%Lyrics%' THEN 'writer'
  WHEN ra.role ILIKE '%Design%' OR ra.role ILIKE '%Photograph%' OR ra.role ILIKE '%Artwork%'
       OR ra.role ILIKE '%Illustration%' OR ra.role ILIKE '%Sleeve%' OR ra.role ILIKE '%Layout%'
       OR ra.role ILIKE '%Cover %' THEN 'design'
  ELSE 'other'
END
"""


def get_label_credits_summary(
    conn,
    label_id: int,
    year_range: Optional[tuple[int, int]],
    top_n_per_role: int = 10,
) -> dict:
    top_n = _clamp(int(top_n_per_role), 1, 50)
    clauses = ["rl.label_id = ?"]
    params: list[Any] = [label_id]
    if year_range is not None:
        lo, hi = year_range
        clauses.append("r.released_year BETWEEN ? AND ?")
        params.extend([int(lo), int(hi)])
    where = " AND ".join(clauses)

    cur = conn.cursor()

    cur.execute(
        f"""
        WITH label_releases AS (
          SELECT DISTINCT rl.release_id
            FROM release_label rl
            JOIN release r ON r.id = rl.release_id
           WHERE {where}
        ),
        bucketed AS (
          SELECT ra.artist_id, ra.artist_name, ra.release_id,
                 CASE WHEN TRIM(COALESCE(ra.role, '')) = '' THEN NULL ELSE ra.role END AS role,
                 {_LABEL_CREDITS_BUCKETS_SQL} AS bucket
            FROM release_artist ra
            JOIN label_releases lr ON lr.release_id = ra.release_id
        ),
        per_artist AS (
          SELECT bucket, artist_id,
                 MIN(artist_name) AS name,
                 COUNT(DISTINCT release_id) AS release_count,
                 STRING_AGG(DISTINCT role, '; ' ORDER BY role) AS roles
            FROM bucketed
           GROUP BY bucket, artist_id
        ),
        ranked AS (
          SELECT bucket, artist_id, name, release_count, roles,
                 ROW_NUMBER() OVER (
                   PARTITION BY bucket
                   ORDER BY release_count DESC, name
                 ) AS rn
            FROM per_artist
        )
        SELECT bucket, artist_id, name, release_count, roles
          FROM ranked
         WHERE rn <= ?
         ORDER BY bucket, release_count DESC, name
        """,
        [*params, top_n],
    )
    rows = _rows(cur)

    cur.execute("SELECT name FROM label WHERE id = ?", [label_id])
    label_row = _one(cur)

    cur.execute(
        f"""
        SELECT COUNT(DISTINCT rl.release_id) AS n
          FROM release_label rl
          JOIN release r ON r.id = rl.release_id
         WHERE {where}
        """,
        params,
    )
    rel_count_row = _one(cur)
    rel_count = rel_count_row["n"] if rel_count_row else 0

    buckets: dict[str, list[dict]] = {}
    for row in rows:
        bucket = row.pop("bucket")
        buckets.setdefault(bucket, []).append(row)

    return {
        "label_id": label_id,
        "label_name": label_row["name"] if label_row else None,
        "release_count": rel_count,
        "buckets": buckets,
    }


# -------- Escape hatch --------


def run_readonly_sql(conn, query: str, row_limit: int) -> dict:
    row_limit = _clamp(row_limit, 1, 10000)
    wrapped = f"SELECT * FROM (\n{query}\n) AS _user_query LIMIT {row_limit}"
    cur = conn.cursor()
    try:
        cur.execute(wrapped)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"columns": cols, "rows": rows, "row_count": len(rows)}
    except Exception as e:  # duckdb.Error, binder errors, etc.
        return {"error": str(e), "query": query}
