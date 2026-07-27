"""Deterministic vault-stub generation.

Tier A of the stub pipeline: everything about a stub that is a database lookup
or a filesystem fact, with no model involved. Runs inside the sidecar, next to
the DuckDB file, so Discogs rows never travel through an agent's context — the
caller sends names and gets finished markdown back.

What this deliberately does NOT do: write the one-sentence scene/genre
characterisation that a full article carries. That is the only genuinely
generative part of a stub and is left to a Tier B pass.

Research Queue entries are gap-derived. A question is emitted only when the
underlying field is actually missing from Discogs. "Full discography" is never
a question here — we have the discography; if it is wanted, run the query.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Optional

MAX_LIST = 5
SPECIAL_ARTIST_IDS = (194, 355, 118760)  # Various, Unknown Artist, No Artist

_DISAMB = re.compile(r"\s*\(\d+\)$")


_ROLE_ANNOT = re.compile(r"\[[^\]]*\]")


# Characters a Discogs name may contain that a filename may not. A slash is the
# dangerous one: "CBS/Sony" written straight through becomes Labels/CBS/Sony.md,
# which silently creates a phantom "CBS" folder and a page called Sony. Roughly
# 12.6k artist and 12.6k label names contain one.
_UNSAFE = {"/": "-", "\\": "-", ":": " -", "*": "", "?": "", '"': "-", "<": "(", ">": ")", "|": "-"}


def sanitize(name: str) -> str:
    """Discogs name → vault filename, matching the convention already in use
    (AC/DC → AC-DC, Kenny "Dope" Gonzalez → Kenny -Dope- Gonzalez)."""
    out = name
    for bad, good in _UNSAFE.items():
        out = out.replace(bad, good)
    return re.sub(r"\s{2,}", " ", out).strip(" .")


def unsanitize_candidates(filename: str) -> list[str]:
    """Filename → the Discogs names that could have produced it.

    Sanitising is lossy — a hyphen in a filename may be a real hyphen, a slash
    or a quote — so resolution tries the candidates rather than guessing.
    """
    out = [filename]
    if "-" in filename:
        out.append(filename.replace(" - ", "/"))
        out.append(filename.replace("-", "/"))
        out.append(re.sub(r"(?<= )-([^-\s][^-]*?)-(?= )", r'"\1"', filename))
    return list(dict.fromkeys(c for c in out if c))


# Ways a vault page name drifts from the Discogs name without meaning anything
# different. Each was a page that resolved to nothing:
#   Pepsi &amp; Shirlie       an HTML entity written into the filename
#   Siouxsie And The Banshees Discogs writes "&"
#   Sisters of Mercy          Discogs writes "The Sisters Of Mercy"
#   Wax Trax!                 Discogs writes "Wax Trax! Records"
_ENTITIES = (("&amp;", "&"), ("&#38;", "&"), ("&quot;", '"'), ("&apos;", "'"), ("&nbsp;", " "))
_LABEL_SUFFIXES = (" Records", " Recordings", " Record Co.", " Music", " Ltd.", ", Inc.")


def _variants(name: str, is_label: bool) -> list[str]:
    """Spellings to try after the exact name fails. Order is widest-first only
    in the sense of cheapness; every one is still required to match exactly one
    row, so a variant can never silently pick the wrong entity."""
    seed = {name}
    for ent, ch in _ENTITIES:
        if ent in name:
            seed.add(name.replace(ent, ch))

    out: list[str] = []
    for base in seed:
        out.append(base)
        # "And" ↔ "&", both directions.
        if re.search(r"\band\b", base, re.I):
            out.append(re.sub(r"\s+\band\b\s+", " & ", base, flags=re.I))
        if "&" in base:
            out.append(re.sub(r"\s*&\s*", " And ", base))
            out.append(re.sub(r"\s*&\s*", " and ", base))
        # A leading article the vault dropped, or added.
        if not re.match(r"^the\s", base, re.I):
            out.append("The " + base)
        else:
            out.append(re.sub(r"^the\s+", "", base, flags=re.I))
        # Labels are routinely filed without their corporate suffix.
        if is_label:
            for suf in _LABEL_SUFFIXES:
                if not base.endswith(suf):
                    out.append(base + suf)
    return [c for c in dict.fromkeys(out) if c and c != name]


def _strip_disamb(name: str) -> str:
    return _DISAMB.sub("", name)


def _top_roles(rows: list[tuple]) -> list[str]:
    """Compound Discogs roles → distinct primitives, most credited first."""
    tally: dict[str, int] = {}
    for role, n in rows:
        for part in _ROLE_ANNOT.sub("", role).split(","):
            part = part.strip()
            if part:
                tally[part] = tally.get(part, 0) + n
    return [r for r, _ in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_LIST]]


# ─── fact gathering ──────────────────────────────────────────────────────


def _artist_facts(conn, artist_id: int) -> dict[str, Any]:
    f: dict[str, Any] = {}

    row = conn.execute(
        "SELECT id, name, realname, profile FROM artist WHERE id = ?", [artist_id]
    ).fetchone()
    f["id"], f["name"], f["realname"], f["profile"] = row

    span = conn.execute(
        """
        SELECT MIN(r.released_year), MAX(r.released_year), COUNT(*)
        FROM release_artist_slim ras
        JOIN release r ON r.id = ras.release_id
        WHERE ras.artist_id = ? AND r.released_year > 0
        """,
        [artist_id],
    ).fetchone()
    f["year_min"], f["year_max"], f["credit_count"] = span

    # `Not On Label` is a Discogs sentinel for self-releases, not a label, and
    # one-off appearances are usually bootlegs or mis-tagged pressings.
    f["labels"] = [
        r[0]
        for r in conn.execute(
            """
            SELECT rl.label_name, COUNT(*) n
            FROM release_artist_slim ras
            JOIN release_label rl ON rl.release_id = ras.release_id
            WHERE ras.artist_id = ? AND rl.label_name IS NOT NULL
              AND rl.label_name NOT ILIKE 'Not On Label%'
            GROUP BY 1 HAVING COUNT(*) >= 2
            ORDER BY n DESC, rl.label_name LIMIT ?
            """,
            [artist_id, MAX_LIST],
        ).fetchall()
    ]

    f["styles"] = [
        r[0]
        for r in conn.execute(
            """
            SELECT rs.style, COUNT(*) n
            FROM release_artist_slim ras
            JOIN release_style rs ON rs.release_id = ras.release_id
            WHERE ras.artist_id = ? AND rs.style IS NOT NULL
            GROUP BY 1 ORDER BY n DESC, rs.style LIMIT ?
            """,
            [artist_id, MAX_LIST],
        ).fetchall()
    ]

    # Discogs role strings are compound and annotated — "Producer, Arranged By",
    # "Noises [All Noises On This Album...]". Split to primitives so the stub
    # lists distinct roles instead of five spellings of "Producer".
    raw_roles = conn.execute(
        """
        SELECT role, COUNT(*) n
        FROM release_artist_slim
        WHERE artist_id = ? AND role IS NOT NULL AND role <> ''
          AND is_non_musical = FALSE
        GROUP BY 1 ORDER BY n DESC, role LIMIT 50
        """,
        [artist_id],
    ).fetchall()
    f["roles"] = _top_roles(raw_roles)

    # Bands this artist belongs to.
    f["member_of"] = [
        r[0]
        for r in conn.execute(
            """
            SELECT a.name FROM group_member gm
            JOIN artist a ON a.id = gm.group_artist_id
            WHERE gm.member_artist_id = ? ORDER BY a.name LIMIT ?
            """,
            [artist_id, MAX_LIST],
        ).fetchall()
    ]

    # People in this act, if it is a group.
    f["members"] = [
        r[0]
        for r in conn.execute(
            "SELECT member_name FROM group_member WHERE group_artist_id = ?"
            " ORDER BY member_name LIMIT ?",
            [artist_id, MAX_LIST],
        ).fetchall()
    ]

    f["kind"] = "group" if f["members"] else "person"
    return f


def _label_facts(conn, label_id: int) -> dict[str, Any]:
    f: dict[str, Any] = {"kind": "label"}

    row = conn.execute(
        "SELECT id, name, parent_name, profile FROM label WHERE id = ?", [label_id]
    ).fetchone()
    f["id"], f["name"], f["parent_name"], f["profile"] = row

    span = conn.execute(
        """
        SELECT MIN(r.released_year), MAX(r.released_year), COUNT(DISTINCT r.id)
        FROM release_label rl
        JOIN release r ON r.id = rl.release_id
        WHERE rl.label_id = ? AND r.released_year > 0
        """,
        [label_id],
    ).fetchone()
    f["year_min"], f["year_max"], f["credit_count"] = span

    f["sublabels"] = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM label WHERE parent_id = ? ORDER BY name LIMIT ?",
            [label_id, MAX_LIST],
        ).fetchall()
    ]

    f["styles"] = [
        r[0]
        for r in conn.execute(
            """
            SELECT rs.style, COUNT(*) n
            FROM release_label rl
            JOIN release_style rs ON rs.release_id = rl.release_id
            WHERE rl.label_id = ? AND rs.style IS NOT NULL
            GROUP BY 1 ORDER BY n DESC, rs.style LIMIT ?
            """,
            [label_id, MAX_LIST],
        ).fetchall()
    ]

    f["roster"] = [
        r[0]
        for r in conn.execute(
            f"""
            SELECT a.name, COUNT(*) n
            FROM release_label rl
            JOIN release_artist_slim ras ON ras.release_id = rl.release_id
            JOIN artist a ON a.id = ras.artist_id
            WHERE rl.label_id = ? AND ras.artist_id NOT IN ({','.join(map(str, SPECIAL_ARTIST_IDS))})
            GROUP BY 1 ORDER BY n DESC, a.name LIMIT ?
            """,
            [label_id, MAX_LIST],
        ).fetchall()
    ]
    return f


# ─── resolution ──────────────────────────────────────────────────────────


def resolve(conn, folder: str, name: str) -> tuple[Optional[int], list[str]]:
    """Map a vault page to a Discogs id. Exact match only — a wrong id is worse
    than an unresolved stub, so near-misses are reported, never guessed."""
    table = "label" if folder == "Labels" else "artist"
    for cand in unsanitize_candidates(name):
        rows = conn.execute(f"SELECT id FROM {table} WHERE name = ?", [cand]).fetchall()
        if len(rows) == 1:
            return rows[0][0], ([] if cand == name else [f"matched Discogs name {cand!r}"])
        if len(rows) > 1:
            return None, [f"ambiguous: {len(rows)} {table} rows named {cand!r}"]

    # Case-insensitive, then the spelling variants. Still exact-match semantics:
    # a candidate is accepted only when it hits exactly one row, so "The
    # Sisters Of Mercy" resolves while anything genuinely ambiguous does not.
    for cand in [name] + _variants(name, table == "label"):
        rows = conn.execute(
            f"SELECT id, name FROM {table} WHERE lower(name) = lower(?)", [cand]
        ).fetchall()
        if len(rows) == 1:
            return rows[0][0], [f"matched Discogs name {rows[0][1]!r}"]
        if len(rows) > 1:
            return None, [f"ambiguous: {len(rows)} {table} rows matching {cand!r}"]

    bare = _strip_disamb(name)
    near = conn.execute(
        f"SELECT name FROM {table} WHERE name = ? OR name ILIKE ? LIMIT 5",
        [bare, bare + " (%"],
    ).fetchall()
    if near:
        return None, [f"no exact match; candidates: {', '.join(r[0] for r in near)}"]
    return None, ["no match in Discogs"]


# ─── rendering ───────────────────────────────────────────────────────────


def _in_index(name: str, index: dict[str, set]) -> Optional[str]:
    for folder in ("Labels", "Bands", "People"):
        if name in index.get(folder, ()):
            return folder
    return None


def _wiki(folder: str, real: str) -> str:
    """A link whose target is a legal filename but which reads as the real name."""
    safe = sanitize(real)
    return f"[[{folder}/{safe}]]" if safe == real else f"[[{folder}/{safe}|{real}]]"


def _link(conn, name: str, kind: str, index: dict[str, set]) -> str:
    """Wiki-link pointing at the page the vault actually has.

    Discogs stores a group's members under their legal name ("Gordon Sumner")
    while the vault files them under the name they perform as ("Sting"). Linking
    the Discogs spelling manufactures a dead link for vault-maintenance to find
    later, so try the entity's aliases against the index before giving up.
    """
    folder = _in_index(name, index) or _in_index(sanitize(name), index)
    if folder:
        return _wiki(folder, name)

    if kind != "label":
        aliases = conn.execute(
            """
            SELECT aa.alias_name FROM artist_alias aa
            JOIN artist a ON a.id = aa.artist_id
            WHERE a.name = ?
            UNION
            SELECT a.name FROM artist_alias aa
            JOIN artist a ON a.id = aa.artist_id
            WHERE aa.alias_name = ?
            """,
            [name, name],
        ).fetchall()
        for (alias,) in aliases:
            folder = _in_index(alias, index) or _in_index(sanitize(alias), index)
            if folder:
                return f"[[{folder}/{sanitize(alias)}|{name}]]"

    # No page yet, so the folder we pick here is where a future stub gets filed.
    # The caller's `kind` is only a hint — a label's roster entry is passed as
    # "person" but is just as often a band (The Cosmic Dead), and a misfiled
    # link costs more to clean up later than to get right now. Ask Discogs
    # whether the name has a lineup.
    default = {"label": "Labels", "group": "Bands", "person": "People"}[kind]
    if kind != "label":
        row = conn.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM group_member gm
                JOIN artist a ON a.id = gm.group_artist_id
                WHERE a.name = ?
            )
            """,
            [name],
        ).fetchone()
        if row and row[0]:
            default = "Bands"
    return _wiki(default, name)


def _gaps(f: dict[str, Any]) -> list[str]:
    """Only genuinely-missing fields. Never 'run this query for us'."""
    out = []
    if not f.get("profile"):
        out.append("No Discogs profile text — biography is unsourced")
    if f["kind"] == "person" and not f.get("realname"):
        out.append("Real name not recorded in Discogs")
    if not f.get("credit_count"):
        out.append("No dated releases in Discogs under this ID — active period unknown")
    if not f.get("styles"):
        out.append("No style tags on any release — genre placement unverified")
    if f["kind"] == "label" and not f.get("parent_name") and not f.get("sublabels"):
        out.append("No parent or sublabel recorded — corporate structure unknown")
    if f["kind"] == "group" and not f.get("members"):
        out.append("No lineup recorded in Discogs")
    return out


def render(conn, f: dict[str, Any], index: dict[str, set]) -> str:
    L: list[str] = [f"# {f['name']}", ""]

    meta = [f"**Discogs ID:** {f['id']}"]
    if f.get("realname"):
        meta.append(f"**Real name:** {f['realname']}")
    if f.get("year_min"):
        meta.append(f"**First Discogs release:** {f['year_min']}")
    if f.get("credit_count"):
        label = "Releases" if f["kind"] == "label" else "Credits"
        meta.append(f"**{label}:** {f['credit_count']:,}")
    if f.get("styles"):
        meta.append(f"**Top styles:** {'; '.join(f['styles'])}")
    if f.get("labels"):
        meta.append(f"**Top labels:** {'; '.join(f['labels'])}")
    if f.get("roles"):
        meta.append(f"**Credited as:** {'; '.join(f['roles'])}")
    L.append("  \n".join(meta))
    L.append("")

    # One factual sentence — assembled from fields, not written.
    kindword = {"label": "Label", "group": "Group", "person": "Artist"}[f["kind"]]
    bits = [kindword]
    if f.get("year_min"):
        bits.append(f"first credited in Discogs {f['year_min']}")
    if f["kind"] == "label" and f.get("parent_name"):
        bits.append(f"sublabel of {f['parent_name']}")
    L.append(", ".join(bits) + ".")
    L.append("")

    conns: list[str] = []
    if f["kind"] == "label":
        if f.get("parent_name"):
            conns.append(f"- {_link(conn, f['parent_name'], 'label', index)} — parent label")
        for s in f.get("sublabels", []):
            conns.append(f"- {_link(conn, s, 'label', index)} — sublabel")
        for a in f.get("roster", []):
            conns.append(f"- {_link(conn, a, 'person', index)} — roster")
    else:
        for g in f.get("member_of", []):
            conns.append(f"- {_link(conn, g, 'group', index)} — member of")
        for m in f.get("members", []):
            conns.append(f"- {_link(conn, m, 'person', index)} — member")
        for lb in f.get("labels", [])[:3]:
            conns.append(f"- {_link(conn, lb, 'label', index)} — released on")

    if conns:
        L += ["## Connections", "", *conns, ""]

    L += ["## Stub — needs full research", ""]

    gaps = _gaps(f)
    if gaps:
        L += ["## Research Queue", ""] + [f"- [ ] {g}" for g in gaps] + [""]

    return "\n".join(L).rstrip() + "\n"


# ─── entry point ─────────────────────────────────────────────────────────


def build(conn, folder: str, name: str, index: dict[str, set]) -> dict[str, Any]:
    entity_id, warnings = resolve(conn, folder, name)
    if entity_id is None:
        return {"folder": folder, "name": name, "resolved": False, "warnings": warnings}
    facts = _label_facts(conn, entity_id) if folder == "Labels" else _artist_facts(conn, entity_id)
    return {
        "folder": folder,
        "name": name,
        "resolved": True,
        "id": entity_id,
        "content": render(conn, facts, index),
        "warnings": warnings,
    }


# ─── vault I/O ───────────────────────────────────────────────────────────

FOLDERS = ("Bands", "People", "Labels")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def read_index(vault: str) -> dict[str, set]:
    """Every page that already exists, so links land on real files."""
    index = {}
    for folder in FOLDERS:
        d = os.path.join(vault, folder)
        index[folder] = (
            {f[:-3] for f in os.listdir(d) if f.endswith(".md")}
            if os.path.isdir(d)
            else set()
        )
    return index


def _match_dir_owner(path: str) -> None:
    """Give a new stub the same owner as the folder holding it.

    This process runs as root inside the container, so a freshly written file
    lands as root:root on the host bind mount. The agent container runs
    unprivileged and would then be unable to build the stub into a full
    article — the exact next step in the pipeline. Best-effort: silently skip
    when not permitted (e.g. running as a non-root user already).
    """
    try:
        st = os.stat(os.path.dirname(path))
        os.chown(path, st.st_uid, st.st_gid)
    except (OSError, AttributeError):
        pass


def _split_target(target: str) -> tuple[Optional[str], str]:
    folder, _, name = target.partition("/")
    return (folder, name) if folder in FOLDERS and name else (None, target)


def write_stubs(conn, vault: str, targets: list[str], dry_run: bool = False) -> dict:
    """Generate and write a stub per target. Targets are `Folder/Name`.

    Never overwrites. An existing page may be a finished article, and silently
    replacing one with a stub would destroy work that cannot be recovered from
    Discogs — so anything already on disk is skipped and reported.
    """
    index = read_index(vault)
    written, skipped, unresolved, discovered = [], [], [], set()

    for target in targets:
        folder, name = _split_target(target)
        if folder is None:
            unresolved.append({"target": target, "why": "expected 'Folder/Name'"})
            continue

        path = os.path.join(vault, folder, sanitize(name) + ".md")
        if os.path.exists(path):
            skipped.append(target)
            continue

        result = build(conn, folder, name, index)
        if not result["resolved"]:
            unresolved.append({"target": target, "why": "; ".join(result["warnings"])})
            continue

        if not dry_run:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(result["content"])
            _match_dir_owner(path)
            index[folder].add(name)
        written.append(target)

        # Links to pages that still don't exist are the next round's work.
        for link in _LINK.findall(result["content"]):
            lf, ln = _split_target(link)
            if lf and ln not in index[lf]:
                discovered.add(link)

    return {
        "written": len(written),
        "skipped_existing": len(skipped),
        "unresolved": unresolved,
        "discovered": sorted(discovered),
        "dry_run": dry_run,
    }


def main() -> None:
    """stdin: {"targets": [["Bands","The Police"], ...], "index": {"Bands": [...]}}
    stdout: {"results": [...]}"""
    import duckdb

    payload = json.load(sys.stdin)
    index = {k: set(v) for k, v in payload.get("index", {}).items()}
    conn = duckdb.connect(payload.get("db", "/data/discogs.duckdb"), read_only=True)
    conn.execute("SET temp_directory='/tmp/duckdb'")
    results = [build(conn, f, n, index) for f, n in payload["targets"]]
    json.dump({"results": results}, sys.stdout)


if __name__ == "__main__":
    main()
