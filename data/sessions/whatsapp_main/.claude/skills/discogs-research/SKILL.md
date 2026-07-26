---
name: discogs-research
description: Systematically research artists, labels, and releases on Discogs using the custom MCP tools, following the bands-research vault conventions. Use whenever researching music artists, bands, labels, or releases for the bands-research Obsidian vault.
---

# Discogs Research Workflow

Systematic workflow for mining the Discogs DuckDB via MCP tools and writing findings to the `bands-research/` Obsidian vault at `/workspace/group/bands-research/`.

## Hard Rule: Discogs First, then Offline Wikipedia, then Web on Permission

1. Use the Discogs MCP tools as the source of truth for discographies, credits, catalog numbers, and the collaborator graph — exhaust them first.
2. For **context Discogs can't give** — band history, genre/scene background, member biographies, label backstory, cultural context — use the **offline Wikipedia** tools (`search_wikipedia`, `get_wikipedia_article`). These are a **local June-2026 snapshot**, not the live internet: no network, no API cost, no permission needed. Use them freely, the same as any other MCP tool. They do **not** count as "web".
3. Write findings to `bands-research/` as you go.
4. Only the **live web** (WebSearch/WebFetch/agent-browser) is gated: when Discogs *and* offline Wikipedia are both exhausted, summarize findings, name specific gaps, and **ask the user** whether to search the live web. Never call those without explicit user approval in that turn.

**Cite Wikipedia as Wikipedia.** When a fact comes from an article, attribute it (the tool returns a `url`) so vault pages distinguish Discogs-sourced structure from Wikipedia-sourced prose.

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

### Offline Wikipedia (local snapshot, June 2026)
```
mcp__custom__search_wikipedia(query, limit=10)
    → {query, estimated_matches, results:[{title, path, snippet}]}
      full-text ranked over article bodies
mcp__custom__get_wikipedia_article(title, max_chars=40000)
    → {found, title, url, chars, text}  (clean plain text; redirects resolved;
      infobox facts kept, references/navboxes stripped)
      on a miss: {found:False, suggestions:[...]}
```

Workflow: `search_wikipedia` to find the right article, then `get_wikipedia_article`
with a result's `title`. If you already know the exact title, call
`get_wikipedia_article` directly — on a miss it returns `suggestions`. Use for the
prose *around* the discography (history, scenes, biographies), never as a
substitute for Discogs credit/catalog data. Text-only dump — no images.

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

## Trawl depth rules

Keep payloads bounded — don't load an entity's entire footprint when a representative selection suffices:
- **Labels with fewer than 1,000 releases**: pull the full catalog — no cap.
- **Labels with 1,000+ releases** (major/large labels): focus on the most significant 30–40 — prioritise original LPs, landmark releases, and releases most relevant to the vault's scene focus.
- **Artists, bands, and people with more than 50 credits**: focus on the most significant 30–40 (LPs over singles, originals over reissues, credited over uncredited). A representative selection is sufficient for prolific session players.
- **For people (session players, engineers, producers)**: call `get_artist_discography` with `as_main_only=True` first to get primary credits; only fetch full contributor credits for the most relevant projects if the primary-credit picture is sparse. This avoids loading hundreds of guest appearances.
- **If Discogs has no matching data** (search returns nothing, or only obviously wrong matches): add `discogs_dry: true` to the page's YAML frontmatter (with `sources: discogs_dry`), leave `## Stub — needs full research` in place, note it in the return summary, and stop.

## When to create a stub file

**Person** — only if they appear in credits across more than one band/project (not just the entity being researched) AND have more than one Discogs credit total. Do NOT stub one-off session players, guest vocalists, or anyone whose entire Discogs footprint is credits with only the band being researched.

**Band/Artist** — only if they have a meaningful independent Discogs presence of their own, not just a cameo on one release by the entity being researched.

**Label** — only if it has more than one release in Discogs. Do NOT stub one-release labels.

**Dead links are preferred over low-value stubs.** Writing `[[Some Band]]` in prose without creating the file is fine — the link is still useful and the stub can be created later if the name recurs. Don't create stubs just to avoid dead links.

## Research Queue — quality gate

Every Research Queue entry must be a concrete question answerable by reading ONE authoritative web page (Wikipedia, AllMusic, an official site, a named interview, contemporary press). If it would need 3+ sources or synthesis, sharpen it or leave it out.

**Do NOT add:**
- "Confirm Discogs ID" — trivial
- "Full discography / full catalog / full roster" — that's what Discogs trawls are for
- "Founding year / years active / dates of operation" (labels or bands)
- "Distribution arrangements / parent company / who distributes"
- "Studio location", "rights/licensing", or pressing-plant questions (which plant, quantities, third-party pressing)
- Anything directly answerable from Discogs in a future trawl
- Vague open-ended prompts ("more about X's career", "explore the X/Y connection") — discarded in triage

**Good question shapes:**
- "Confirm whether [person] played on [specific release] or only on the [reissue]"
- "What was [person]'s role after leaving [band] in [year] — did they join [specific band]?"
- "Sleeve credits [release] as produced by X but Discogs says Y — which is correct?"
- "Was [album] recorded at [studio] per [interview], or [alternate studio] per liner notes?"
- "Identify [recurring uncredited person] on [releases A, B, C] with no Discogs profile"

Ten specific questions beat five vague ones — no limit; the only gate is concrete + single-source-resolvable.

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
