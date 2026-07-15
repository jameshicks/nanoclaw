---
name: discogs-research
description: Systematically research artists, labels, and releases on Discogs using the custom MCP tools, following the bands-research vault conventions. Use whenever researching music artists, bands, labels, or releases for the bands-research Obsidian vault.
---

# Discogs Research Workflow

Systematic workflow for mining the Discogs DuckDB via MCP tools and writing findings to the `bands-research/` Obsidian vault at `/workspace/group/bands-research/`.

## Hard Rule: Discogs First, Web on Permission

1. Use **only** Discogs MCP tools until fully exhausted
2. Write findings to `bands-research/` as you go
3. When done, summarize findings, name specific gaps, and **ask the user** whether to search the web
4. Never call WebSearch/WebFetch/agent-browser for music research without explicit user approval in that turn

---

## MCP Tools Reference

### Search Tools
```
mcp__custom__search_artist(name, limit=10, exact=False)
mcp__custom__search_label(name, limit=10, exact=False)
mcp__custom__search_release(title, limit=10)
```

**When you already know the exact name and just need its id, pass `exact=True`.**
It skips BM25 and the enrichment, matching the name directly (case-insensitive,
tolerant of "&"/"and" and "(2)" disambiguators):
- `search_artist("Rhys Chatham", exact=True)` → id/name/realname only, ~1 call
- `search_label("ZE Records", exact=True)` → id/name/parent_name **plus
  `release_count`** — use that count for the label-size decision below; do NOT
  run a separate `SELECT COUNT(*)` query for it.

Prefer `exact=True` over a raw `run_readonly_sql` name lookup — it's faster,
leaner, and returns the canonical entity instead of namesake noise.

### Entity Lookup
```
mcp__custom__get_artist(artist_id)                       → profile, aliases, members, groups
mcp__custom__get_label(label_id)                         → profile, parent/sublabels
mcp__custom__get_release(release_id)                     → full credits, tracklist, labels, notes
mcp__custom__get_release_credits(release_id)             → personnel only (smaller than get_release)
mcp__custom__get_release_by_catalog_number(cat_no, label_id?) → direct cat# lookup ("SSQ225", "S-005")
```

### Discography / Catalog
```
mcp__custom__get_artist_discography(artist_id, role?, as_main_only?, year_range?, unique_masters_only=True)
mcp__custom__get_person_credits(person_id, year_range?, unique_masters_only=True)
    → use for engineers/designers/contributors with sparse "artist" pages.
      Same data as get_artist_discography with defaults tuned for non-primary credits.
mcp__custom__get_label_releases(label_id, year_range?, unique_masters_only=True, include_credits=False)
mcp__custom__get_label_roster(label_id, year_range?)
mcp__custom__get_label_credits_summary(label_id, year_range?, top_n_per_role=10)
    → top-N people bucketed by role (performer/producer/engineer/writer/design/other)
      across the label's catalog. Use when surveying who recurs behind a label.
mcp__custom__list_compilations_featuring_artist(artist_id, limit=20)
```

### Graph / Analysis
```
mcp__custom__find_collaborators(artist_id, min_shared_releases=2, limit=20)
mcp__custom__find_path_between_artists(artist_id_1, artist_id_2)
mcp__custom__get_scene_snapshot(label_ids?, artist_ids?, year_from?, year_to?)
```

### Raw SQL
```
mcp__custom__run_readonly_sql(query, row_limit=1000)
```

**Always include an explicit LIMIT clause** — `row_limit=1000` is a ceiling, not a target. Use the smallest limit that answers the question (e.g., `LIMIT 50` for a quick name lookup, `LIMIT 200` for a label catalog survey). Unbounded queries on large tables waste tokens and slow the trawl.

**Key DB schema:**
- Tables: `artist`, `release`, `label`, `release_artist`, `release_label`
- Column: `released_year` (not `release_year`). There is **no `formats` column on `release`** — format/media info lives in the `release_format` table (`name` e.g. 'Vinyl'/'CD', `descriptions` e.g. 'LP; Album', `qty`), joined on `release_id`
- `release_artist.role` is NULL for primary artist credits (not missing data)

**Useful SQL patterns** (for genuine cross-entity questions — NOT for name→id
lookups, which `search_artist`/`search_label` with `exact=True` do faster):

```sql
-- Full personnel on a release
SELECT a.name, ra.role FROM release_artist ra
JOIN artist a ON ra.artist_id = a.id
WHERE ra.release_id = <id> ORDER BY ra.role

-- Artist discography with labels
SELECT r.id, r.title, r.released_year, ra.role, rl.name as label
FROM release r
JOIN release_artist ra ON r.id = ra.release_id
LEFT JOIN release_label rlab ON r.id = rlab.release_id
LEFT JOIN label rl ON rlab.label_id = rl.id
WHERE ra.artist_id = <id> ORDER BY r.released_year

-- Label catalog
SELECT r.id, r.title, r.released_year FROM release r
JOIN release_label rl ON r.id = rl.release_id
WHERE rl.label_id = <id> ORDER BY r.released_year
```

---

## Research Workflow

### Artist Deep Dive
1. `search_artist(name)` → confirm ID, check for duplicate entries (when the brief already gives the exact name, use `search_artist(name, exact=True)`)
2. `get_artist(id)` → profile, aliases, members/groups — note all IDs
3. `get_artist_discography(id)` + `find_collaborators(id)` in parallel
4. For each key release: `get_release(release_id)` → full personnel
5. Follow aliases and member IDs recursively
6. `list_compilations_featuring_artist(id)` → scene/era context

### Label Deep Dive
1. `search_label(name, exact=True)` → confirm ID and read `release_count` (drives the full-catalog-vs-sample decision)
2. `get_label(id)` → profile, parent label, sublabels
3. `get_label_roster(id)` + `get_label_releases(id)` in parallel
4. For key releases: `get_release(id)` → full personnel

### Release Research
1. `search_release(title)` → narrow to correct entry (check year/label)
2. `get_release(id)` → full credits
3. Follow each credited person → `get_artist(id)` for their page

---

## Vault Conventions (`/workspace/group/bands-research/`)

**Folders:** `People/` · `Bands/` · `Labels/` · `Releases/` · `Topics/`

**Wiki-links:** `[[Name]]` for every person, band, label mentioned. Create a stub for every linked name with no existing file.

**Stub format:**
```markdown
# Name

One-line intro (born/formed, role). Discogs artist ID: 12345

## Overview

Brief context.

## Key Credits / Key Releases

| Release | Year | Role | Notes |
|---------|------|------|-------|

## Connections

- [[Name]] — relationship

## Stub — needs full research
```

**Full article format** (major artists — see Arthur Russell.md, Peter Gordon.md):
- Biography sections by era
- Key Projects & Releases — DB-sourced personnel tables per release
- Key Collaborators table — name | shared releases | context
- Labels Summary table — label | years | key releases
- Connections section with [[wiki-links]] throughout
- Open Questions

---

## Efficiency Tips

- Fan out parallel MCP calls: after getting artist ID, call discography + collaborators simultaneously
- Reach for `run_readonly_sql` only for genuine cross-entity/relational questions the structured tools can't express (e.g. "releases by artist X on label Y in 1976–1980"). For a name→id lookup, an entity fetch, or a release's credits, the structured tools are cheaper and already shaped.
- Write stubs immediately as new names appear — don't batch them for the end
- Note all DB IDs in files for fast re-lookup across sessions
- For exact name→id resolution, use `search_artist(name, exact=True)` / `search_label(name, exact=True)` — NOT a raw `WHERE lower(name) = '...'` SQL query

## Common Pitfalls

- `formats` column doesn't exist on `release` — for format/media type, JOIN `release_format` on `release_id` (`SELECT name, descriptions FROM release_format WHERE release_id = <id>`). `release.notes` is free-text, not format data
- Always check for duplicate Discogs entries (e.g. "DNA (4)" = No Wave band, "DNA" = UK dance duo)
- `release_artist.role = NULL` means primary artist credit, not missing data
- DB is read-only — only `run_readonly_sql` works; no temp directory access
