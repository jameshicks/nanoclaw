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


def search_artist(conn, name: str, limit: int) -> list[dict]:
    limit = _clamp(limit, 1, 100)
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
          SELECT id, fts_main_artist.match_bm25(id, ?) AS score
            FROM artist
           WHERE fts_main_artist.match_bm25(id, ?) IS NOT NULL
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
               l.top_labels,
               h.score AS bm25_score
          FROM hits h
          JOIN artist a ON a.id = h.id
          LEFT JOIN labels l ON l.artist_id = h.id
          LEFT JOIN years y ON y.artist_id = h.id
         ORDER BY {match_tier_sql_out},
                  h.score DESC,
                  LENGTH(a.name) ASC
         LIMIT ?
        """,
        [name, name, name, norm_query, fts_fetch, name, norm_query, limit],
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
    clauses = ["score IS NOT NULL"]
    params: list[Any] = [title, title]
    if year is not None:
        clauses.append("r.released_year = ?")
        params.append(int(year))
    if artist:
        clauses.append(
            """EXISTS (
                SELECT 1 FROM release_artist ra
                LEFT JOIN artist a ON a.id = ra.artist_id
                WHERE ra.release_id = r.id
                  AND ra.extra = 0
                  AND (ra.artist_name ILIKE ? OR a.name ILIKE ?)
            )"""
        )
        like = f"%{artist}%"
        params.extend([like, like])
    params.append(limit)
    where = " AND ".join(clauses)

    cur = conn.cursor()
    cur.execute(
        f"""
        WITH scored AS (
          SELECT r.id, r.title, r.released_year, r.country, r.master_id,
                 fts_main_release.match_bm25(r.id, ?) AS score
            FROM release r
           WHERE fts_main_release.match_bm25(r.id, ?) IS NOT NULL
        )
        SELECT s.id, s.title, s.released_year AS year, s.country, s.master_id,
               (SELECT STRING_AGG(DISTINCT artist_name, ' / ')
                  FROM release_artist ra WHERE ra.release_id = s.id AND ra.extra = 0) AS primary_artists,
               (SELECT STRING_AGG(DISTINCT label_name, '; ')
                  FROM release_label rl WHERE rl.release_id = s.id) AS labels,
               s.score AS bm25_score
          FROM scored s
          JOIN release r ON r.id = s.id
         WHERE {where}
         ORDER BY s.score DESC
         LIMIT ?
        """,
        params,
    )
    return _rows(cur)


def search_label(conn, name: str, limit: int) -> list[dict]:
    limit = _clamp(limit, 1, 100)
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
          SELECT id, fts_main_label.match_bm25(id, ?) AS score
            FROM label
           WHERE fts_main_label.match_bm25(id, ?) IS NOT NULL
           ORDER BY {match_tier_hits},
                    score DESC
           LIMIT ?
        )
        SELECT l.id, l.name, l.parent_id, l.parent_name,
               (SELECT COUNT(*) FROM label sl WHERE sl.parent_id = l.id) AS sublabel_count,
               (SELECT COUNT(*) FROM release_label rl WHERE rl.label_id = l.id) AS release_count,
               h.score AS bm25_score
          FROM hits h
          JOIN label l ON l.id = h.id
         ORDER BY {match_tier_out},
                  h.score DESC,
                  LENGTH(l.name) ASC
         LIMIT ?
        """,
        [name, name, name, norm_query, fts_fetch, name, norm_query, limit],
    )
    return _rows(cur)


# -------- Entity fetch --------


def get_artist(conn, artist_id: int, compact: bool = False) -> Optional[dict]:
    cur = conn.cursor()
    cols = "id, name, realname, data_quality" if compact else "id, name, realname, profile, data_quality"
    cur.execute(
        f"SELECT {cols} FROM artist WHERE id = ?",
        [artist_id],
    )
    artist = _one(cur)
    if artist is None:
        return None

    cur.execute(
        "SELECT alias_artist_id, alias_name FROM artist_alias WHERE artist_id = ? ORDER BY alias_name",
        [artist_id],
    )
    artist["aliases"] = _rows(cur)

    cur.execute(
        "SELECT name FROM artist_namevariation WHERE artist_id = ? ORDER BY name",
        [artist_id],
    )
    artist["name_variations"] = [r["name"] for r in _rows(cur)]

    cur.execute(
        "SELECT url FROM artist_url WHERE artist_id = ? ORDER BY url",
        [artist_id],
    )
    artist["urls"] = [r["url"] for r in _rows(cur)]

    cur.execute(
        """
        SELECT gm.group_artist_id AS artist_id, a.name
          FROM group_member gm
          LEFT JOIN artist a ON a.id = gm.group_artist_id
         WHERE gm.member_artist_id = ?
         ORDER BY a.name
        """,
        [artist_id],
    )
    artist["groups"] = _rows(cur)

    cur.execute(
        """
        SELECT gm.member_artist_id AS artist_id, gm.member_name AS name
          FROM group_member gm
         WHERE gm.group_artist_id = ?
         ORDER BY gm.member_name
        """,
        [artist_id],
    )
    artist["members"] = _rows(cur)

    return artist


def get_release(conn, release_id: int) -> Optional[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, released_raw, released_year, country, notes,
               data_quality, master_id
          FROM release WHERE id = ?
        """,
        [release_id],
    )
    release = _one(cur)
    if release is None:
        return None

    cur.execute(
        """
        SELECT release_id, sequence, position, parent, title, duration, track_id
          FROM release_track
         WHERE release_id = ?
         ORDER BY sequence, position
        """,
        [release_id],
    )
    tracks = _rows(cur)

    cur.execute(
        """
        SELECT track_id, artist_id, artist_name, extra, anv, position, join_string, role
          FROM release_track_artist
         WHERE release_id = ?
         ORDER BY track_id, extra, position
        """,
        [release_id],
    )
    track_credits: dict[str, list[dict]] = {}
    for row in _rows(cur):
        tid = row.pop("track_id")
        track_credits.setdefault(tid, []).append(row)
    for t in tracks:
        t["credits"] = track_credits.get(t["track_id"], [])

    cur.execute(
        """
        SELECT artist_id, artist_name, extra, anv, position, join_string, role, tracks
          FROM release_artist WHERE release_id = ?
         ORDER BY extra, position
        """,
        [release_id],
    )
    creds = _rows(cur)
    primary = [c for c in creds if c["extra"] == 0]
    additional = [c for c in creds if c["extra"] == 1]

    cur.execute(
        "SELECT name, qty, descriptions, text_string FROM release_format WHERE release_id = ? ORDER BY name",
        [release_id],
    )
    formats = _rows(cur)

    cur.execute(
        "SELECT label_id, label_name, catno FROM release_label WHERE release_id = ? ORDER BY label_name",
        [release_id],
    )
    labels = _rows(cur)

    cur.execute(
        "SELECT DISTINCT genre FROM release_genre WHERE release_id = ? ORDER BY genre",
        [release_id],
    )
    genres = [r["genre"] for r in _rows(cur)]

    cur.execute(
        "SELECT DISTINCT style FROM release_style WHERE release_id = ? ORDER BY style",
        [release_id],
    )
    styles = [r["style"] for r in _rows(cur)]

    cur.execute(
        "SELECT type, value, description FROM release_identifier WHERE release_id = ? ORDER BY type, value",
        [release_id],
    )
    identifiers = _rows(cur)

    return {
        "release": release,
        "tracklist": tracks,
        "credits": {"primary": primary, "additional": additional},
        "formats": formats,
        "labels": labels,
        "genres": genres,
        "styles": styles,
        "identifiers": identifiers,
    }


def get_label(conn, label_id: int) -> Optional[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name, contact_info, profile, parent_id, parent_name, data_quality
          FROM label WHERE id = ?
        """,
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


def _role_predicate(alias: str, key: str) -> tuple[str, list[Any]]:
    """Build a SQL predicate fragment for role filtering, parameterized by table alias.

    Known keys map to curated clauses; anything else becomes a substring ILIKE.
    Returns (sql_fragment, params). Params is empty for curated clauses,
    non-empty for the substring fallthrough or the "musical" blocklist alias."""
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
        blocks = " AND ".join(f"{col} NOT ILIKE ?" for _ in _NON_MUSICAL_PATTERNS)
        return f"({col} IS NULL OR ({blocks}))", list(_NON_MUSICAL_PATTERNS)
    if key:
        return f"{col} ILIKE ?", [f"%{key}%"]
    return "TRUE", []


def _roles_clause(alias: str, roles: Optional[Any]) -> tuple[Optional[str], list[Any]]:
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
        frag, fp = _role_predicate(alias, r)
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

    if unique_masters_only:
        # Collapse to one row per master (earliest-year, lowest-id pressing).
        # Standalone releases (master_id IS NULL) keyed by -id — Discogs IDs
        # are strictly positive so there's no collision with real master_ids.
        cur.execute(
            f"""
            WITH releases AS (
              SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                     FIRST(ra.role) AS role,
                     FIRST(ra.extra) AS extra
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
            )
            SELECT id, title, year, country, master_id, role, extra, pressings_count,
                   (SELECT STRING_AGG(DISTINCT label_name, '; ')
                      FROM release_label rl WHERE rl.release_id = deduped.id) AS labels
              FROM deduped
             WHERE _rank = 1
             ORDER BY year NULLS LAST, title
             LIMIT 500
            """,
            params,
        )
    else:
        cur.execute(
            f"""
            SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                   FIRST(ra.role) AS role,
                   FIRST(ra.extra) AS extra,
                   (SELECT STRING_AGG(DISTINCT label_name, '; ')
                      FROM release_label rl WHERE rl.release_id = r.id) AS labels
              FROM release_artist ra
              JOIN release r ON r.id = ra.release_id
             WHERE {where}
             GROUP BY r.id, r.title, r.released_year, r.country, r.master_id
             ORDER BY year NULLS LAST, r.title
             LIMIT 500
            """,
            params,
        )
    return _rows(cur)


def get_label_releases(
    conn,
    label_id: int,
    year_range: Optional[tuple[int, int]],
    unique_masters_only: bool = True,
) -> list[dict]:
    clauses = ["rl.label_id = ?"]
    params: list[Any] = [label_id]
    if year_range is not None:
        lo, hi = year_range
        clauses.append("r.released_year BETWEEN ? AND ?")
        params.extend([int(lo), int(hi)])
    where = " AND ".join(clauses)
    cur = conn.cursor()

    if unique_masters_only:
        cur.execute(
            f"""
            WITH releases AS (
              SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                     FIRST(rl.catno) AS catno
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
            )
            SELECT id, title, year, country, master_id, catno, pressings_count,
                   (SELECT STRING_AGG(DISTINCT artist_name, ' / ')
                      FROM release_artist ra WHERE ra.release_id = deduped.id AND ra.extra = 0) AS primary_artists
              FROM deduped
             WHERE _rank = 1
             ORDER BY year NULLS LAST, title
             LIMIT 500
            """,
            params,
        )
    else:
        cur.execute(
            f"""
            SELECT r.id, r.title, r.released_year AS year, r.country, r.master_id,
                   FIRST(rl.catno) AS catno,
                   (SELECT STRING_AGG(DISTINCT artist_name, ' / ')
                      FROM release_artist ra WHERE ra.release_id = r.id AND ra.extra = 0) AS primary_artists
              FROM release_label rl
              JOIN release r ON r.id = rl.release_id
             WHERE {where}
             GROUP BY r.id, r.title, r.released_year, r.country, r.master_id
             ORDER BY year NULLS LAST, r.title
             LIMIT 500
            """,
            params,
        )
    return _rows(cur)


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

    role_clause, role_params = _roles_clause("ra2", roles)
    role_sql = f" AND {role_clause}" if role_clause else ""

    visited: set[int] = {artist_id}
    frontier: set[int] = {artist_id}
    results: list[dict] = []
    # Keyed by (seed_at_this_level, neighbor) but we only surface per-neighbor titles
    # relative to the seed artist, not the cumulative path. That means "top_shared_titles"
    # is scoped to this BFS hop, which is the right question the agent is asking.
    titles_by_neighbor: dict[int, list[str]] = {}

    cur = conn.cursor()
    for d in range(1, depth + 1):
        if not frontier:
            break
        cur.execute(
            f"""
            SELECT ra2.artist_id, a.name,
                   COUNT(DISTINCT ra2.release_id) AS shared_releases
              FROM release_artist ra1
              JOIN release_artist ra2
                ON ra2.release_id = ra1.release_id
               AND ra2.artist_id <> ra1.artist_id
              LEFT JOIN artist a ON a.id = ra2.artist_id
             WHERE ra1.artist_id IN (SELECT UNNEST(CAST(? AS BIGINT[])))
               AND ra1.extra = 0
               AND ra2.artist_id NOT IN (SELECT UNNEST(CAST(? AS BIGINT[])))
               {role_sql}
             GROUP BY ra2.artist_id, a.name
            HAVING COUNT(DISTINCT ra2.release_id) >= ?
             ORDER BY shared_releases DESC
             LIMIT 2000
            """,
            [list(frontier), list(visited), *role_params, min_shared],
        )
        new_rows = _rows(cur)
        new_frontier: set[int] = set()
        level_neighbor_ids: list[int] = []
        for row in new_rows:
            aid = row["artist_id"]
            if aid in visited:
                continue
            visited.add(aid)
            new_frontier.add(aid)
            level_neighbor_ids.append(aid)
            results.append(
                {
                    "artist_id": aid,
                    "name": row["name"],
                    "distance": d,
                    "shared_releases": row["shared_releases"],
                    "top_shared_titles": [],
                }
            )

        # Batch fetch top 3 distinct shared release titles for the neighbors at this
        # level. Group by title (not release_id) to dedupe across pressings/editions —
        # e.g. "Press Color" on US + UK + France collapses to one entry. Ordered by
        # earliest-known year so the titles make a rough timeline.
        if level_neighbor_ids and include_top_shared_titles:
            cur.execute(
                f"""
                WITH edges AS (
                  SELECT DISTINCT ra2.artist_id AS neighbor_id,
                         r.id AS release_id, r.title, r.released_year AS year
                    FROM release_artist ra1
                    JOIN release_artist ra2
                      ON ra2.release_id = ra1.release_id
                     AND ra2.artist_id <> ra1.artist_id
                    JOIN release r ON r.id = ra1.release_id
                   WHERE ra1.artist_id IN (SELECT UNNEST(CAST(? AS BIGINT[])))
                     AND ra1.extra = 0
                     AND ra2.artist_id IN (SELECT UNNEST(CAST(? AS BIGINT[])))
                     {role_sql}
                ),
                titles AS (
                  SELECT neighbor_id, title,
                         MIN(COALESCE(year, 9999)) AS year_rank,
                         MIN(release_id) AS id_rank
                    FROM edges
                   GROUP BY neighbor_id, title
                )
                SELECT neighbor_id, title
                  FROM (
                    SELECT neighbor_id, title,
                           ROW_NUMBER() OVER (
                             PARTITION BY neighbor_id
                             ORDER BY year_rank, id_rank
                           ) AS rn
                      FROM titles
                  )
                 WHERE rn <= 3
                 ORDER BY neighbor_id, rn
                """,
                [list(frontier), level_neighbor_ids, *role_params],
            )
            for neighbor_id, title in cur.fetchall():
                titles_by_neighbor.setdefault(neighbor_id, []).append(title)

        frontier = new_frontier

    for r in results:
        r["top_shared_titles"] = titles_by_neighbor.get(r["artist_id"], [])

    results.sort(key=lambda r: (r["distance"], -r["shared_releases"]))
    results = results[:500]

    if results:
        neighbor_ids = [r["artist_id"] for r in results]
        cur.execute(
            """
            SELECT artist_id, alias_artist_id, alias_name
              FROM artist_alias
             WHERE artist_id IN (SELECT UNNEST(CAST(? AS BIGINT[])))
             ORDER BY artist_id, alias_name
            """,
            [neighbor_ids],
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

    frontier: dict[int, list[int]] = {artist_a_id: [artist_a_id]}
    visited: set[int] = {artist_a_id}
    cur = conn.cursor()

    for d in range(1, max_depth + 1):
        if not frontier:
            return {"path": None, "reason": "no_path", "max_depth_searched": d - 1}

        # Frontier size cap to prevent runaway cost on prolific seeds.
        if len(frontier) > 5000:
            frontier = dict(list(frontier.items())[:5000])

        cur.execute(
            """
            SELECT DISTINCT ra1.artist_id AS from_id, ra2.artist_id AS to_id
              FROM release_artist ra1
              JOIN release_artist ra2
                ON ra2.release_id = ra1.release_id
               AND ra2.artist_id <> ra1.artist_id
             WHERE ra1.artist_id IN (SELECT UNNEST(CAST(? AS BIGINT[])))
               AND ra1.extra = 0
            """,
            [list(frontier.keys())],
        )
        edges = cur.fetchall()

        next_frontier: dict[int, list[int]] = {}
        for from_id, to_id in edges:
            if to_id in visited or to_id in next_frontier:
                continue
            path = frontier[from_id] + [to_id]
            if to_id == artist_b_id:
                cur.execute(
                    "SELECT id, name FROM artist WHERE id IN (SELECT UNNEST(CAST(? AS BIGINT[])))",
                    [path],
                )
                name_map = {r[0]: r[1] for r in cur.fetchall()}
                return {
                    "path": [{"id": i, "name": name_map.get(i)} for i in path],
                    "depth": d,
                }
            next_frontier[to_id] = path

        visited.update(next_frontier.keys())
        frontier = next_frontier

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

    cte = f"""
      WITH matching_releases AS (
        SELECT r.id
          FROM release r
         WHERE {where}
      )
    """

    cur = conn.cursor()

    cur.execute(f"{cte} SELECT COUNT(*) AS n FROM matching_releases", params)
    release_count = cur.fetchone()[0]

    cur.execute(
        f"""{cte}
        SELECT ra.artist_id, a.name, COUNT(DISTINCT ra.release_id) AS release_count
          FROM release_artist ra
          JOIN matching_releases mr ON mr.id = ra.release_id
          LEFT JOIN artist a ON a.id = ra.artist_id
         WHERE ra.extra = 0
         GROUP BY ra.artist_id, a.name
         ORDER BY release_count DESC
         LIMIT 20
        """,
        params,
    )
    top_artists = _rows(cur)

    cur.execute(
        f"""{cte}
        SELECT r.id, r.title, r.released_year AS year,
               COUNT(*) AS credit_count
          FROM release_artist ra
          JOIN matching_releases mr ON mr.id = ra.release_id
          JOIN release r ON r.id = mr.id
         GROUP BY r.id, r.title, r.released_year
         ORDER BY credit_count DESC
         LIMIT 20
        """,
        params,
    )
    top_releases = _rows(cur)

    cur.execute(
        f"""{cte}
        SELECT rf.name AS format, COUNT(*) AS n
          FROM release_format rf
          JOIN matching_releases mr ON mr.id = rf.release_id
         GROUP BY rf.name ORDER BY n DESC LIMIT 25
        """,
        params,
    )
    formats = _rows(cur)

    cur.execute(
        f"""{cte}
        SELECT rg.genre, COUNT(*) AS n
          FROM release_genre rg
          JOIN matching_releases mr ON mr.id = rg.release_id
         GROUP BY rg.genre ORDER BY n DESC LIMIT 25
        """,
        params,
    )
    genres = _rows(cur)

    cur.execute(
        f"""{cte}
        SELECT rs.style, COUNT(*) AS n
          FROM release_style rs
          JOIN matching_releases mr ON mr.id = rs.release_id
         GROUP BY rs.style ORDER BY n DESC LIMIT 25
        """,
        params,
    )
    styles = _rows(cur)

    return {
        "filters": {
            "label_ids": label_ids,
            "year_range": list(year_range) if year_range else None,
            "country": country,
        },
        "release_count": release_count,
        "top_artists": top_artists,
        "top_releases": top_releases,
        "formats": formats,
        "genres": genres,
        "styles": styles,
    }


def list_compilations_featuring_artist(conn, artist_id: int) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT r.id, r.title, r.released_year AS year, r.country,
               (SELECT STRING_AGG(DISTINCT label_name, '; ')
                  FROM release_label rl WHERE rl.release_id = r.id) AS labels,
               (SELECT STRING_AGG(DISTINCT rf.descriptions, ' | ')
                  FROM release_format rf WHERE rf.release_id = r.id
                    AND rf.descriptions ILIKE '%Compilation%') AS format_descriptions
          FROM release_artist ra
          JOIN release r ON r.id = ra.release_id
         WHERE ra.artist_id = ?
           AND EXISTS (
             SELECT 1 FROM release_format rf
              WHERE rf.release_id = r.id
                AND rf.descriptions ILIKE '%Compilation%'
                AND (rf.descriptions NOT ILIKE '%Unofficial%' OR rf.descriptions IS NULL)
           )
         ORDER BY year NULLS LAST, r.title
         LIMIT 500
        """,
        [artist_id],
    )
    return _rows(cur)


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
