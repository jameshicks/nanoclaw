"""Deterministic full-article assembly for the bands-research vault.

The hourly stub trawl spends ~94% of its tokens on cache reads: the agent pulls
~180k tokens of Discogs payloads into context and re-reads them every turn,
mostly to retype rows as markdown tables. Only ~0.3% of the spend is output.

This moves the table-building next to the database. Two calls:

    facts(...)  → a few hundred tokens: counts, eras, top labels, members,
                  collaborators, detected gaps. Enough to write prose from.
    build(...)  → writes the article, with the caller's overview and questions
                  slotted into an otherwise query-generated page.

The model never sees a release row. It writes the overview paragraph and the
Research Queue questions — the two things a query genuinely cannot produce.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import queries as Q
import stubgen

# Depth rules from the trawler brief: a representative selection beats an
# exhaustive dump for prolific artists, and a small label deserves its whole
# catalogue.
BIG_CATALOG = 50
SELECT_CAP = 40
BIG_LABEL = 1000


def _first(d: dict, *names, default=None):
    for n in names:
        if d.get(n) is not None:
            return d[n]
    return default


# ─── fact gathering ──────────────────────────────────────────────────────


def _pressing_counts(conn, master_ids: list[int]) -> dict[int, int]:
    """How many pressings each master has.

    This is the significance signal. A landmark album is pressed and repressed
    for decades; a bootleg or a one-off live tape exists once. Nothing else in
    the dump ranks releases, and sorting by year instead — as this did first —
    truncates a career at whatever the cap allows, which dropped Synchronicity
    from The Police.
    """
    ids = [m for m in master_ids if m]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    return dict(
        conn.execute(
            f"SELECT master_id, COUNT(*) FROM release WHERE master_id IN ({placeholders}) GROUP BY 1",
            ids,
        ).fetchall()
    )


def _rank_and_cap(conn, rows: list[dict]) -> list[dict]:
    if len(rows) > SELECT_CAP:
        pressings = _pressing_counts(conn, [r.get("master_id") for r in rows])
        rows.sort(
            key=lambda r: (
                not _first(r, "as_primary", default=False),
                -pressings.get(r.get("master_id"), 1),
                _first(r, "year", default=9999) or 9999,
            )
        )
        rows = rows[:SELECT_CAP]
    rows.sort(key=lambda r: (_first(r, "year", default=9999) or 9999, r.get("title") or ""))
    return rows


def _artist_releases(conn, artist_id: int, credit_count: int) -> list[dict]:
    """Significant releases, listed chronologically.

    Past BIG_CATALOG credits we switch to primary credits only — the brief's
    rule for session players, whose guest appearances would otherwise swamp
    their own work.
    """
    as_main_only = credit_count > BIG_CATALOG
    rows = Q.get_artist_discography(conn, artist_id, None, as_main_only, None, True)
    return _rank_and_cap(conn, rows)


def _label_releases(conn, label_id: int, total: int) -> list[dict]:
    rows = Q.get_label_releases(conn, label_id, None, True)
    if total >= BIG_LABEL and len(rows) > SELECT_CAP:
        rows = rows[:SELECT_CAP]
    return rows


def _eras(rows: list[dict]) -> list[dict]:
    """Release counts per decade — the shape of a career, in ~20 tokens."""
    tally: dict[int, int] = {}
    for r in rows:
        y = _first(r, "year", "released_year")
        if y:
            tally[(int(y) // 10) * 10] = tally.get((int(y) // 10) * 10, 0) + 1
    return [{"decade": d, "releases": n} for d, n in sorted(tally.items())]


# Roles the precomputed `is_non_musical` flag misses. Its blocklist catches
# "Design" but not bare "Art Direction", and nothing about mastering — so
# "Art Direction, Design" is flagged while "Art Direction" alone is not. On a
# major-label catalogue that fills the top shared credits with art directors
# and mastering engineers who touched every reissue. Producers and engineers
# are deliberately NOT here: Nigel Gray and Hugh Padgham belong in Connections.
_TECHNICAL_ROLES = (
    "%Art Direction%",
    "%Master%",
    "%Lacquer%",
    "%Compiled%",
    "%Compilation%",
)


def _bandmate_ids(conn, artist_id: int) -> set[int]:
    """Members of this act, plus their aliases.

    Aliases matter: group_member records The Police's singer as Gordon Sumner
    (207403) while his credits are under Sting (13961). Without expanding
    aliases he lands in Members *and* tops Connections as a collaborator.
    """
    ids = {
        r[0]
        for r in conn.execute(
            "SELECT member_artist_id FROM group_member WHERE group_artist_id = ?"
            " AND member_artist_id IS NOT NULL",
            [artist_id],
        ).fetchall()
    }
    if not ids:
        return ids
    ph = ",".join("?" * len(ids))
    aliased = conn.execute(
        f"""
        SELECT aa.alias_artist_id FROM artist_alias aa
         WHERE aa.artist_id IN ({ph}) AND aa.alias_artist_id IS NOT NULL
        UNION
        SELECT aa.artist_id FROM artist_alias aa
         WHERE aa.alias_artist_id IN ({ph})
        UNION
        SELECT a2.id FROM artist_alias aa
          JOIN artist a1 ON a1.id = aa.artist_id
          JOIN artist a2 ON a2.name = aa.alias_name
         WHERE aa.artist_id IN ({ph})
        """,
        list(ids) * 3,
    ).fetchall()
    return ids | {r[0] for r in aliased if r[0]}


def _collaborators(conn, artist_id: int, limit: int = 12) -> list[dict]:
    """Most-shared *musical* co-credits.

    A direct join, not the BFS in find_collaborators — depth-1 is all an article
    needs and the BFS is far more expensive. `is_non_musical` matters here: on
    a major-label catalogue the top shared credits are otherwise the sleeve
    photographers, designers, mastering engineers and label executives, who
    appear on every pressing.
    """
    exclude = {artist_id} | set(stubgen.SPECIAL_ARTIST_IDS) | _bandmate_ids(conn, artist_id)
    ph = ",".join("?" * len(exclude))
    role_filter = " AND ".join(["ras.role NOT ILIKE ?"] * len(_TECHNICAL_ROLES))
    cur = conn.execute(
        f"""
        SELECT a.name, COUNT(DISTINCT ras.release_id) n
          FROM release_artist_slim me
          JOIN release_artist_slim ras ON ras.release_id = me.release_id
          JOIN artist a ON a.id = ras.artist_id
         WHERE me.artist_id = ? AND ras.is_non_musical = FALSE
           AND ras.artist_id NOT IN ({ph})
           AND (ras.role IS NULL OR ({role_filter}))
         GROUP BY 1 HAVING COUNT(DISTINCT ras.release_id) > 1
         ORDER BY n DESC, a.name LIMIT ?
        """,
        [artist_id, *exclude, *_TECHNICAL_ROLES, limit],
    )
    return [{"name": r[0], "shared": r[1]} for r in cur.fetchall()]


def _label_summary(conn, artist_id: int, limit: int = 12) -> list[dict]:
    """Labels ranked by distinct works, not pressings.

    No minimum: counting masters, a single work is a real release, and small
    artists have nothing else. Requiring two — which made sense when this
    counted pressings — hid every Fe-mail label but one, while the overview
    the model wrote from the fact sheet named them all. Noise sorts to the
    bottom and the LIMIT handles it.
    """
    cur = conn.execute(
        """
        SELECT rl.label_name, COUNT(DISTINCT COALESCE(r.master_id, -r.id)) n,
               MIN(r.released_year) y0, MAX(r.released_year) y1
          FROM release_artist_slim ras
          JOIN release r ON r.id = ras.release_id
          JOIN release_label rl ON rl.release_id = r.id
         WHERE ras.artist_id = ? AND rl.label_name IS NOT NULL
           AND rl.label_name NOT ILIKE 'Not On Label%' AND r.released_year > 0
         GROUP BY 1
         ORDER BY n DESC, rl.label_name LIMIT ?
        """,
        [artist_id, limit],
    )
    return [{"name": r[0], "works": r[1], "years": f"{r[2]}–{r[3]}"} for r in cur.fetchall()]


def facts(conn, folder: str, name: str) -> dict[str, Any]:
    """Compact fact sheet — everything needed to write an overview, nothing more."""
    entity_id, warnings = stubgen.resolve(conn, folder, name)
    if entity_id is None:
        return {"resolved": False, "target": f"{folder}/{name}", "warnings": warnings}

    if folder == "Labels":
        f = stubgen._label_facts(conn, entity_id)
        rows = _label_releases(conn, entity_id, f["credit_count"] or 0)
        out = {
            "kind": "label",
            "parent": f.get("parent_name"),
            "sublabels": f.get("sublabels"),
            "roster": f.get("roster"),
        }
    else:
        f = stubgen._artist_facts(conn, entity_id)
        rows = _artist_releases(conn, entity_id, f["credit_count"] or 0)
        out = {
            "kind": f["kind"],
            "realname": f.get("realname"),
            "members": f.get("members"),
            "member_of": f.get("member_of"),
            "roles": f.get("roles"),
            "collaborators": _collaborators(conn, entity_id),
            "labels": _label_summary(conn, entity_id),
        }

    prof_raw, via = _profile_text(conn, entity_id, out["kind"])
    prof, prof_refs = _resolve_refs(conn, prof_raw) if prof_raw else ("", [])
    out.update(
        {
            "profile": prof,
            "profile_via": via,
            "profile_refs": prof_refs,
            "resolved": True,
            "target": f"{folder}/{name}",
            "id": entity_id,
            "name": f["name"],
            "total_credits": f.get("credit_count"),
            "selected_releases": len(rows),
            "first_year": f.get("year_min"),
            "styles": f.get("styles"),
            "eras": _eras(rows),
            "has_profile": bool(prof),
            "gaps": stubgen._gaps(f),
            "discogs_dry": not rows and not f.get("credit_count"),
        }
    )
    return out


# ─── rendering ───────────────────────────────────────────────────────────


def _esc(s: Any) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


def _release_table(rows: list[dict], kind: str) -> list[str]:
    if not rows:
        return []
    if kind == "label":
        head = ["| Release | Year | Artists | Cat # |", "|---|---|---|---|"]
    else:
        # Only carry a Role column when some row actually has one — a band's
        # own releases carry no role and the column is "—" all the way down.
        show_role = any(_esc(r.get("role")) for r in rows)
        head = (
            ["| Release | Year | Label | Role |", "|---|---|---|---|"]
            if show_role
            else ["| Release | Year | Label |", "|---|---|---|"]
        )
    body = []
    for r in rows:
        year = _first(r, "year", "released_year", default="—")
        if kind == "label":
            body.append(
                f"| *{_esc(r.get('title'))}* | {year} | {_esc(_first(r,'primary_artists','artists'))} | {_esc(r.get('catno'))} |"
            )
        elif show_role:
            body.append(
                f"| *{_esc(r.get('title'))}* | {year} | {_esc(r.get('labels'))} | {_esc(r.get('role')) or '—'} |"
            )
        else:
            body.append(f"| *{_esc(r.get('title'))}* | {year} | {_esc(r.get('labels'))} |")
    return head + body


def render(conn, fx: dict, rows: list[dict], overview: str, questions: list[str], index: dict) -> str:
    L = ["---", "sources: discogs", "---", "", f"# {fx['name']}", ""]

    meta = [f"**Discogs ID:** {fx['id']}"]
    if fx.get("realname"):
        meta.append(f"**Real name:** {fx['realname']}")
    if fx.get("first_year"):
        meta.append(f"**First Discogs release:** {fx['first_year']}")
    if fx.get("total_credits"):
        meta.append(f"**Total Discogs credits:** {fx['total_credits']:,}")
    if fx.get("styles"):
        meta.append(f"**Top styles:** {'; '.join(fx['styles'])}")
    L += ["  \n".join(meta), ""]

    if fx.get("profile"):
        kindword = "label" if fx["kind"] == "label" else "artist"
        via = fx.get("profile_via")
        cite = (
            f"— Discogs {kindword} profile for {via['name']} (ID {via['id']}), "
            f"the entity behind {fx['name']}"
            if via
            else f"— Discogs {kindword} profile, ID {fx['id']}"
        )
        L += [
            "## From Discogs",
            "",
            "> " + fx["profile"].replace("\n", "\n> "),
            ">",
            "> " + cite,
            "",
        ]

    if overview:
        L += ["## Overview", "", overview.strip(), ""]

    label = "Catalogue" if fx["kind"] == "label" else "Releases"
    table = _release_table(rows, fx["kind"])
    if table:
        note = (
            f" — {fx['selected_releases']} most-pressed of "
            f"{fx['total_credits']:,} credits, chronological"
            if (fx.get("total_credits") or 0) > fx["selected_releases"]
            else ""
        )
        L += [f"## {label}{note}", "", *table, ""]

    if fx.get("members"):
        L += ["## Members", ""] + [
            f"- {stubgen._link(conn, m, 'person', index)}" for m in fx["members"]
        ] + [""]

    if fx.get("labels"):
        L += ["## Labels", "", "| Label | Works | Years |", "|---|---|---|"] + [
            f"| {stubgen._link(conn, l['name'], 'label', index)} | {l['works']} | {l['years']} |"
            for l in fx["labels"]
        ] + [""]

    if fx.get("roster"):
        L += ["## Roster", ""] + [
            f"- {stubgen._link(conn, a, 'person', index)}" for a in fx["roster"]
        ] + [""]

    conns = []
    if fx.get("parent"):
        conns.append(f"- {stubgen._link(conn, fx['parent'], 'label', index)} — parent label")
    for s in fx.get("sublabels") or []:
        conns.append(f"- {stubgen._link(conn, s, 'label', index)} — sublabel")
    for g in fx.get("member_of") or []:
        conns.append(f"- {stubgen._link(conn, g, 'group', index)} — member of")
    # Bandmates already have their own section; repeating them as
    # "collaborators" is noise, and they always top the shared-release count.
    listed = set(fx.get("members") or []) | set(fx.get("member_of") or []) | set(fx.get("roster") or [])
    for c in fx.get("collaborators") or []:
        if c["name"] in listed:
            continue
        conns.append(
            f"- {stubgen._link(conn, c['name'], 'person', index)} — {c['shared']} shared releases"
        )
    listed_names = {c.split("/")[-1].rstrip("]") for c in conns}
    for r in fx.get("profile_refs") or []:
        if r["name"] not in listed_names and r["name"] != fx["name"]:
            conns.append(
                f"- {stubgen._link(conn, r['name'], r['kind'], index)} — named in Discogs profile"
            )

    if conns:
        L += ["## Connections", "", *conns, ""]

    all_q = list(questions or []) + [f"{g} — verify off-Discogs" for g in fx.get("gaps") or []]
    if all_q:
        L += ["## Research Queue", ""] + [f"- [ ] {q}" for q in all_q] + [""]

    return "\n".join(L).rstrip() + "\n"


# ─── back-link repair ────────────────────────────────────────────────────

_MULTIWORD = re.compile(r"^[\w'’.&-]+(?: [\w'’.&-]+)+$", re.UNICODE)

# How many associated names must appear before a single-word name is treated
# as the band rather than the English word.
_CORROBORATION = 2


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Regions a link must never be inserted into: YAML frontmatter, fenced and
    inline code, existing wiki-links, and markdown link targets."""
    spans: list[tuple[int, int]] = []
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            spans.append((0, end + 4))
    for pat in (
        r"(?ms)^```.*?^```",
        r"`[^`\n]+`",
        r"\[\[[^\]]*\]\]",
        r"\]\([^)]*\)",
        # Headings are structure, not prose. Every page in this vault carries a
        # "## Research Queue" heading, and there is a page called Research
        # Queue — without this, linking it rewrites five thousand headings.
        r"(?m)^#{1,6} .*$",
        # Table header separators and the bold key of a `**Key:** value` line.
        r"(?m)^\*\*[^*]+:\*\*",
        # Release titles are italicised throughout this vault, and a release
        # often shares its name with an act — *Ghost Dance* on a credits line
        # is Ken McMullen's film score, not the band Ghost Dance.
        r"(?<!\*)\*[^*\n]+\*(?!\*)",
    ):
        spans += [m.span() for m in re.finditer(pat, text)]
    return spans


def _link_bare(text: str, name: str, target: str) -> tuple[str, int]:
    """Case-sensitive whole-word replace, skipping protected regions."""
    spans = _protected_spans(text)
    pat = re.compile(r"(?<![\w'’\-/|])" + re.escape(name) + r"(?![\w'’\-|])")
    out: list[str] = []
    last = n = 0
    for m in pat.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        out.append(text[last : m.start()])
        out.append(target)
        last = m.end()
        n += 1
    out.append(text[last:])
    return "".join(out), n


def _corroborated(text: str, target: str, associates: list[str]) -> bool:
    """Is this page actually talking about the entity?

    Either it already links to it — so the subject is established — or enough
    of its associated names (bandmates, labels, collaborators) appear that the
    context is unambiguous. A page about low temperatures will not mention two
    of a band's collaborators.
    """
    if target in text:
        return True
    return sum(1 for a in associates if a and a in text) >= _CORROBORATION


def repair_backlinks(
    vault: str,
    folder: str,
    name: str,
    skip: str,
    associates: Optional[list[str]] = None,
) -> dict:
    """Link bare mentions of `name` across the vault.

    Multi-word names are unambiguous enough to link on sight. Single-word ones
    are not — the vault has pages called Low, Suicide, Swans and Wire, and a
    bare-word replace across 17,650 files would turn ordinary prose into links
    with no undo. Those are linked only on pages that corroborate the subject,
    which keeps them working instead of skipping them wholesale.
    """
    target = f"[[{folder}/{name}]]"
    associates = [a for a in (associates or []) if a and a != name]
    needs_context = not _MULTIWORD.match(name)
    if needs_context and not associates:
        return {
            "repaired": 0,
            "files": 0,
            "skipped_reason": "single-word name with no associated names to corroborate against",
        }

    changed = files = skipped_uncorroborated = 0
    for root, dirs, fnames in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        for fn in fnames:
            if not fn.endswith(".md") or fn.startswith("_"):
                continue
            path = os.path.join(root, fn)
            if os.path.abspath(path) == os.path.abspath(skip):
                continue
            try:
                text = open(path, encoding="utf-8").read()
            except OSError:
                continue
            if name not in text:
                continue
            if needs_context and not _corroborated(text, target, associates):
                skipped_uncorroborated += 1
                continue
            new, n = _link_bare(text, name, target)
            if n:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(new)
                stubgen._match_dir_owner(path)
                changed += n
                files += 1

    out = {"repaired": changed, "files": files}
    if needs_context:
        out["mode"] = "corroborated (single-word name)"
        out["skipped_uncorroborated"] = skipped_uncorroborated
    return out


# ─── Discogs profile prose ───────────────────────────────────────────────

# Discogs profiles are free text with BBCode-ish entity references. This is
# where the material that looks like outside research actually lives — Force
# Inc's label code, founding month and the EFA-Medien collapse are all in its
# profile field, and a trawler reading `get_label` was simply paraphrasing it.
_BB_ENTITY = re.compile(r"\[(a|l|m|r)(?:=)?([^\]]+)\]")
_BB_FORMAT = re.compile(r"\[/?(b|i|u|s|q|center)\]")
_BB_URL = re.compile(r"\[url=([^\]]+)\]|\[/url\]")
_PROFILE_CAP = 1500


def _profile_text(conn, entity_id: int, kind: str) -> tuple[str, Optional[dict]]:
    """This entity's profile, or its real-name entity's if it has none.

    Mapstation's own profile is empty; Stefan Schneider — the artist behind the
    alias — carries the Düsseldorf art-academy biography one hop away. The
    trawler finds it by walking aliases, so we do the same.
    """
    table = "label" if kind == "label" else "artist"
    row = conn.execute(f"SELECT profile FROM {table} WHERE id = ?", [entity_id]).fetchone()
    text = (row[0] if row else "") or ""
    if text.strip() or kind == "label":
        return text, None

    alt = conn.execute(
        """
        SELECT a2.id, a2.profile, a2.name
          FROM artist_alias aa
          JOIN artist a2 ON a2.id = aa.alias_artist_id
         WHERE aa.artist_id = ? AND a2.profile IS NOT NULL AND LENGTH(a2.profile) > 40
         UNION ALL
        SELECT a2.id, a2.profile, a2.name
          FROM artist_alias aa
          JOIN artist a2 ON a2.name = aa.alias_name
         WHERE aa.artist_id = ? AND a2.profile IS NOT NULL AND LENGTH(a2.profile) > 40
         LIMIT 1
        """,
        [entity_id, entity_id],
    ).fetchone()
    return (alt[1], {"id": alt[0], "name": alt[2]}) if alt else ("", None)


def _resolve_refs(conn, text: str) -> tuple[str, list[dict]]:
    """Strip BBCode and pull out the entities the profile names.

    References come as `[a=Achim Szepanski]` or by id as `[l199815]`. They are
    real link targets — resolving them is most of why a trawler-written page
    carries several times the wiki-links this used to emit.
    """
    refs: list[dict] = []
    by_id: dict[tuple[str, int], Optional[str]] = {}

    for tag, body in _BB_ENTITY.findall(text):
        if tag in ("a", "l") and body.isdigit():
            by_id[(tag, int(body))] = None
    for (tag, ident) in list(by_id):
        table = "artist" if tag == "a" else "label"
        row = conn.execute(f"SELECT name FROM {table} WHERE id = ?", [ident]).fetchone()
        by_id[(tag, ident)] = row[0] if row else None

    def sub(m: re.Match) -> str:
        tag, body = m.group(1), m.group(2)
        if tag in ("m", "r"):
            return ""  # release/master pointers add noise, not names
        name = by_id.get((tag, int(body))) if body.isdigit() else body
        if not name:
            return ""
        kind = "label" if tag == "l" else "person"
        if not any(r["name"] == name for r in refs):
            refs.append({"name": name, "kind": kind})
        return name

    out = _BB_ENTITY.sub(sub, text)
    out = _BB_FORMAT.sub("", out)
    out = _BB_URL.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if len(out) > _PROFILE_CAP:
        out = out[:_PROFILE_CAP].rsplit(" ", 1)[0] + " …"
    return out, refs


# ─── stub gating ─────────────────────────────────────────────────────────


def _stub_worthy(conn, target: str, subject_id: int) -> bool:
    """The brief's rule: dead links beat low-value stubs.

    A label needs more than one release. A person or band needs more than one
    credit *and* a footprint beyond the entity being written about — otherwise
    they are a one-off session player on this record and the stub would say
    nothing the article doesn't already.
    """
    folder, name = stubgen._split_target(target)
    if folder == "Labels":
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT rl.release_id) FROM release_label rl
             JOIN label l ON l.id = rl.label_id WHERE l.name = ?
            """,
            [name],
        ).fetchone()
        return bool(row and row[0] > 1)

    row = conn.execute(
        """
        SELECT COUNT(DISTINCT ras.release_id),
               COUNT(DISTINCT ras.release_id) FILTER (
                   WHERE ras.release_id NOT IN (
                       SELECT release_id FROM release_artist_slim WHERE artist_id = ?
                   )
               )
          FROM release_artist_slim ras
          JOIN artist a ON a.id = ras.artist_id
         WHERE a.name = ?
        """,
        [subject_id, name],
    ).fetchone()
    return bool(row and row[0] > 1 and row[1] > 0)


def inbound_count(vault: str, target: str, skip: str = "") -> int:
    """How many pages link `target`. Used to tell a real orphan from a page
    that simply lost one of several references."""
    n = 0
    needle = f"[[{target}"
    for root, dirs, fnames in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        for fn in fnames:
            if not fn.endswith(".md") or fn.startswith("_"):
                continue
            p = os.path.join(root, fn)
            if os.path.abspath(p) == os.path.abspath(skip):
                continue
            try:
                if needle in open(p, encoding="utf-8", errors="ignore").read():
                    n += 1
            except OSError:
                continue
    return n


def _reconnect_dropped(conn, vault: str, dropped: list[str], skip: str) -> dict:
    """Re-link pages this rewrite orphaned, and name the ones still stranded."""
    relinked, still_orphan, kept = [], [], 0
    for target in dropped:
        folder, tname = stubgen._split_target(target)
        if folder is None or not os.path.exists(os.path.join(vault, target + ".md")):
            continue
        if inbound_count(vault, target, skip=skip) > 0:
            kept += 1  # something else still links it
            continue
        res = repair_backlinks(vault, folder, tname, skip=skip)
        (relinked if res.get("repaired") else still_orphan).append(target)
    return {
        "dropped": len(dropped),
        "still_linked_elsewhere": kept,
        "relinked": relinked,
        "orphaned": still_orphan,
    }


# ─── entry point ─────────────────────────────────────────────────────────


def build(
    conn,
    vault: str,
    target: str,
    overview: str = "",
    questions: Optional[list[str]] = None,
    stub_links: bool = True,
    backlinks: bool = True,
    dry_run: bool = False,
) -> dict:
    folder, name = stubgen._split_target(target)
    if folder is None:
        return {"ok": False, "why": "expected 'Folder/Name'"}

    fx = facts(conn, folder, name)
    if not fx["resolved"]:
        return {"ok": False, "why": "; ".join(fx["warnings"]), "target": target}

    path = os.path.join(vault, folder, name + ".md")

    # What the page linked before we replace it. Regenerating drops whatever
    # the new queries no longer surface, and any stub created by an earlier run
    # of this tool is the likeliest casualty — improving the collaborator
    # filter orphaned seven pages the previous Police article had created.
    prior_links: set[str] = set()
    if os.path.exists(path):
        try:
            prior_links = set(stubgen._LINK.findall(open(path, encoding="utf-8").read()))
        except OSError:
            pass

    # Discogs has nothing: leave the stub marker in place and flag the page so
    # the trawl doesn't keep selecting it.
    if fx["discogs_dry"]:
        if not dry_run and os.path.exists(path):
            text = open(path, encoding="utf-8").read()
            if "discogs_dry" not in text:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("---\nsources: discogs_dry\ndiscogs_dry: true\n---\n\n" + text)
                stubgen._match_dir_owner(path)
        return {"ok": True, "discogs_dry": True, "target": target, "written": False}

    index = stubgen.read_index(vault)
    rows = (
        _label_releases(conn, fx["id"], fx["total_credits"] or 0)
        if folder == "Labels"
        else _artist_releases(conn, fx["id"], fx["total_credits"] or 0)
    )
    content = render(conn, fx, rows, overview, questions or [], index)

    if not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        stubgen._match_dir_owner(path)

    result = {
        "ok": True,
        "target": target,
        "written": not dry_run,
        "bytes": len(content),
        "releases_listed": len(rows),
    }

    if stub_links:
        linked = sorted(set(stubgen._LINK.findall(content)))
        missing = [
            t
            for t in linked
            if stubgen._split_target(t)[0]
            and not os.path.exists(os.path.join(vault, t + ".md"))
        ]
        worthy = [t for t in missing if _stub_worthy(conn, t, fx["id"])]
        result["stubs"] = stubgen.write_stubs(conn, vault, worthy, dry_run=dry_run)
        result["stubs"]["gated_out"] = len(missing) - len(worthy)

    # Anything this page used to link and no longer does. If nothing else in
    # the vault links it either, it is now an orphan we made — so try to
    # reconnect it where it is genuinely mentioned, and report what is left.
    dropped = sorted(prior_links - set(stubgen._LINK.findall(content)))
    if dropped and not dry_run:
        result["dropped_links"] = _reconnect_dropped(conn, vault, dropped, skip=path)

    if backlinks and not dry_run:
        associates = (
            list(fx.get("members") or [])
            + list(fx.get("member_of") or [])
            + list(fx.get("roster") or [])
            + [c["name"] for c in fx.get("collaborators") or []]
            + [l["name"] for l in fx.get("labels") or []]
            + ([fx["parent"]] if fx.get("parent") else [])
        )
        result["backlinks"] = repair_backlinks(
            vault, folder, name, skip=path, associates=associates
        )

    return result
