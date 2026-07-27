"""Deterministic fact bundles for the Discogs resolution queue.

The queue-resolve job used to spend most of a run rediscovering things the
database already knows. In a representative 20-item run, 18 of 38 MCP calls
were `search_artist` doing nothing but turning a name the work file already
carried into an id, and the remaining lookups were the same three or four
queries over and over.

Every open queue item carries a `**File to update:**`, so the entity is known
without reading the question at all — no intent classification, no guessing.
That is what this module leans on: one call per file group returns everything
the questions in that group could plausibly need.

Questions are used for exactly one thing: pulling out release titles that are
already marked up (`*Rosemary Lane*`, `"Kiss"`, `Discogs release 139921`) so
per-release credits can be attached. That is string extraction, not
comprehension — an unrecognised title is reported back, never guessed at.

Sizing is the whole game. A bundle that sprawls costs more in cache writes
than the exploration it replaces, so every list here is capped and the caps
are deliberately tight.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import article
import queries as Q
import stubgen

# Caps. A queue answer needs enough discography to spot a label sequence or a
# gap year, not a complete catalogue — that is what build_article is for.
DISCOG_CAP = 30
NAMED_RELEASE_CAP = 6
CREDITS_PER_RELEASE = 25
COLLAB_CAP = 8
LABEL_CAP = 10
ROSTER_CAP = 15


# ─── target parsing ──────────────────────────────────────────────────────

_STRIP = re.compile(r"^[`'\"\s]+|[`'\"\s]+$")


def split_target(target: str) -> tuple[Optional[str], str, list[str]]:
    """`Bands/Coil.md` → ('Bands', 'Coil'). Tolerates the shapes the queue
    actually contains: backticked values, absolute container paths, .md
    suffixes. Four live items are malformed in exactly these ways."""
    warnings: list[str] = []
    t = _STRIP.sub("", target or "")
    if not t:
        return None, "", ["empty target"]

    if "/bands-research/" in t:
        t = t.split("/bands-research/", 1)[1]
        warnings.append("stripped absolute path prefix")
    if t.endswith(".md"):
        t = t[:-3]

    parts = [p for p in t.split("/") if p]
    if len(parts) < 2:
        return None, t, warnings + [f"no folder in target {target!r}"]
    folder, name = parts[0], "/".join(parts[1:])
    if folder not in ("Bands", "People", "Labels", "Releases", "Topics"):
        return None, name, warnings + [f"unknown folder {folder!r}"]
    return folder, name, warnings


# ─── title extraction from question text ─────────────────────────────────

# Only markup a human deliberately applied. Bare capitalised phrases are far
# too noisy — "Discogs shows no releases on Too Small To Fail" would yield a
# release title that does not exist.
_TITLE_PATTERNS = (
    re.compile(r"\*([^*\n]{2,70})\*"),
    re.compile(r'"([^"\n]{2,70})"'),
    re.compile(r"[“”]([^“”\n]{2,70})[“”]"),
)
_RELEASE_ID = re.compile(r"(?:Discogs\s+)?release\s*(?:id)?\s*:?\s*(\d{3,9})", re.I)
_NOISE = re.compile(r"^(discogs|wikipedia|not on label|various|unknown)\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def extract_titles(questions: list[str]) -> tuple[list[str], list[int]]:
    """Release titles and explicit ids mentioned in a group's questions."""
    titles: list[str] = []
    ids: list[int] = []
    for q in questions or []:
        for m in _RELEASE_ID.finditer(q):
            rid = int(m.group(1))
            if rid not in ids:
                ids.append(rid)
        for pat in _TITLE_PATTERNS:
            for raw in pat.findall(q):
                t = raw.strip()
                # Drop parenthetical tails Discogs does not carry in the title
                # ("Shift The Blame (Spy Records, 1979)").
                t = re.sub(r"\s*\((?:[^()]*\b(19|20)\d{2}\b[^()]*)\)$", "", t).strip()
                if len(t) < 2 or _NOISE.match(t):
                    continue
                if t not in titles:
                    titles.append(t)
    return titles, ids


# ─── bundle assembly ─────────────────────────────────────────────────────


def _discography(conn, artist_id: int, credit_count: int) -> tuple[list[dict], int]:
    as_main_only = (credit_count or 0) > article.BIG_CATALOG
    rows = Q.get_artist_discography(conn, artist_id, None, as_main_only, None, True)
    total = len(rows)
    if total > DISCOG_CAP:
        pressings = article._pressing_counts(conn, [r.get("master_id") for r in rows])
        rows.sort(
            key=lambda r: (
                not article._first(r, "as_primary", default=False),
                -pressings.get(r.get("master_id"), 1),
                article._first(r, "year", default=9999) or 9999,
            )
        )
        rows = rows[:DISCOG_CAP]
    rows.sort(key=lambda r: (article._first(r, "year", default=9999) or 9999, r.get("title") or ""))
    out = []
    for r in rows:
        # `label` is deliberately omitted — the `labels` summary already carries
        # label affiliations with year spans, and repeating it per row was 13%
        # of an early bundle for no extra answering power.
        row = {
            "year": article._first(r, "year", "released_year"),
            "title": r.get("title"),
            "release_id": r.get("release_id") or r.get("id"),
        }
        role = r.get("role")
        if role:
            row["role"] = role
        if not article._first(r, "as_primary", default=False):
            row["guest"] = True
        out.append(row)
    return out, total


def _aliases(conn, artist_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT alias_name FROM artist_alias
         WHERE artist_id = ? AND alias_name IS NOT NULL
         ORDER BY alias_name LIMIT 12
        """,
        [artist_id],
    ).fetchall()
    return [r[0] for r in rows]


def _named_releases(
    conn, titles: list[str], ids: list[int], discog: list[dict], entity_id: Optional[int]
) -> tuple[list[dict], list[str]]:
    """Full credits for releases the questions actually named.

    Titles are matched against this entity's own discography first — that is
    what makes it cheap and unambiguous. A title we cannot place is returned
    as unmatched rather than searched for; the caller can fall back to
    search_release for the rare case it matters.
    """
    want: list[int] = list(ids)
    by_norm = {}
    for r in discog:
        if r.get("release_id"):
            by_norm.setdefault(_norm(r["title"]), r["release_id"])

    unmatched = []
    for t in titles:
        rid = by_norm.get(_norm(t))
        if rid is None:
            # Prefix match catches "Blue Velvet Revisited" vs the dump's
            # "Blue Velvet Revisited (Original Soundtrack)".
            n = _norm(t)
            hits = [v for k, v in by_norm.items() if k.startswith(n) or n.startswith(k)]
            rid = hits[0] if len(hits) == 1 else None
        if rid is None:
            unmatched.append(t)
        elif rid not in want:
            want.append(rid)

    out = []
    for rid in want[:NAMED_RELEASE_CAP]:
        rec = Q.get_release_credits(conn, rid)
        if not rec:
            continue
        for key in ("primary", "additional"):
            rec[key] = [
                {k: v for k, v in c.items() if v not in (None, "", 0)}
                for c in (rec.get(key) or [])[:CREDITS_PER_RELEASE]
            ]
        out.append(rec)
    return out, unmatched


def facts(conn, target: str, questions: Optional[list[str]] = None) -> dict[str, Any]:
    """One call per file group: everything that group's questions could need."""
    folder, name, warnings = split_target(target)
    if folder is None:
        return {"resolved": False, "target": target, "warnings": warnings}

    entity_id, rw = stubgen.resolve(conn, folder, name)
    warnings = warnings + rw
    if entity_id is None:
        return {"resolved": False, "target": target, "warnings": warnings}

    titles, ids = extract_titles(questions or [])
    out: dict[str, Any] = {
        "resolved": True,
        "target": f"{folder}/{name}",
        "id": entity_id,
        "warnings": warnings,
    }

    if folder == "Labels":
        f = stubgen._label_facts(conn, entity_id)
        rows = Q.get_label_releases(conn, entity_id, None, True)
        total = len(rows)
        catalog = [
            {
                "year": article._first(r, "year", "released_year"),
                "title": r.get("title"),
                "artist": r.get("artist") or r.get("artist_name"),
                "catno": r.get("catno"),
                "release_id": r.get("release_id") or r.get("id"),
            }
            for r in rows[:DISCOG_CAP]
        ]
        named, unmatched = _named_releases(
            conn, titles, ids, [{"title": c["title"], "release_id": c["release_id"]} for c in catalog], entity_id
        )
        out.update(
            {
                "kind": "label",
                "name": f["name"],
                "parent": f.get("parent_name"),
                "sublabels": f.get("sublabels"),
                "roster": (f.get("roster") or [])[:ROSTER_CAP],
                "styles": f.get("styles"),
                "years": f"{f.get('year_min')}–{f.get('year_max')}",
                "total_releases": total,
                "catalog": catalog,
                "catalog_truncated": total > len(catalog),
                "named_releases": named,
                "unmatched_titles": unmatched,
            }
        )
    else:
        f = stubgen._artist_facts(conn, entity_id)
        discog, total = _discography(conn, entity_id, f.get("credit_count") or 0)
        named, unmatched = _named_releases(conn, titles, ids, discog, entity_id)
        out.update(
            {
                "kind": f["kind"],
                "name": f["name"],
                "realname": f.get("realname"),
                "aliases": _aliases(conn, entity_id),
                "members": f.get("members"),
                "member_of": f.get("member_of"),
                "roles": f.get("roles"),
                "styles": f.get("styles"),
                "years": f"{f.get('year_min')}–{f.get('year_max')}",
                "total_credits": f.get("credit_count"),
                "labels": article._label_summary(conn, entity_id, LABEL_CAP),
                # No collaborator graph here. It is article-writing material —
                # queue questions ask what someone did, not who else was in the
                # room — and it cost 7% of the bundle to answer nothing.
                "discography": discog,
                "discography_truncated": total > len(discog),
                "total_releases": total,
                "named_releases": named,
                "unmatched_titles": unmatched,
            }
        )

    prof_raw, via = article._profile_text(conn, entity_id, out["kind"])
    if prof_raw:
        prof, _ = article._resolve_refs(conn, prof_raw)
        out["profile"] = prof
        out["profile_via"] = via

    out["discogs_dry"] = not out.get("total_releases") and not out.get("total_credits")
    return out
