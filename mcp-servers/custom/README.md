# Discogs DuckDB MCP

FastMCP sidecar that exposes a read-only Discogs DuckDB dump to NanoClaw agents over HTTP at
`host.docker.internal:8765/mcp`. Wiring lives in `container/agent-runner/src/index.ts` under
the `custom` key — no `.mcp.json` edits needed.

## Tools

| Tool | Purpose |
|---|---|
| `search_artist(name, limit=10)` | FTS fuzzy search. Returns id, name, realname, years_active, top_labels |
| `search_release(title, artist=None, year=None, limit=10)` | FTS fuzzy search with filters |
| `search_label(name, limit=10)` | FTS fuzzy search |
| `get_artist(artist_id)` | Full record + aliases, name variations, groups, members |
| `get_release(release_id)` | Full record + tracklist, credits, formats, genres, styles |
| `get_release_credits(release_id)` | Personnel only — split into primary / additional. Smaller payload than `get_release`. |
| `get_release_by_catalog_number(cat_no, label_id=None)` | Direct lookup by Discogs cat# (e.g. `SSQ225`, `S-005`). |
| `get_label(label_id)` | Full record + parent, sublabels |
| `get_artist_discography(artist_id, role=None, as_main_only=False, year_range=None, unique_masters_only=True)` | Role aliases: `performer`, `producer`, `writer`, `engineer`. Default collapses pressings to one row per master. |
| `get_person_credits(person_id, year_range=None, unique_masters_only=True)` | All-roles credit history for a person — defaults tuned for engineer/designer/contributor lookups. |
| `get_label_releases(label_id, year_range=None, unique_masters_only=True)` | Release-level catalog for a label, with primary_artists and catno per row |
| `get_label_roster(label_id, year_range=None)` | Primary artists by release count |
| `get_label_credits_summary(label_id, year_range=None, top_n_per_role=10)` | Top-N people per role bucket (performer/producer/engineer/writer/design/other) across the label's catalog. |
| `find_collaborators(artist_id, depth=1, min_shared_releases=1, roles=None)` | BFS, depth ≤ 3. `roles` aliases: `musical` (blocklist), `performer`, `producer`, `writer`, `engineer`. Results include `top_shared_titles`. |
| `find_path_between_artists(a_id, b_id, max_depth=4)` | Shortest path, max_depth ≤ 6 |
| `get_scene_snapshot(label_ids=None, year_range=None, country=None)` | Multi-dim slice |
| `list_compilations_featuring_artist(artist_id)` | Filtered on `release_format.descriptions` |
| `describe_schema()` | Prose overview agents can fetch on demand |
| `run_readonly_sql(query, row_limit=1000)` | sqlglot-validated SELECT-only escape hatch |
| `search_wikipedia(query, limit=10)` | Full-text search over the offline Wikipedia ZIM. Returns `{title, path, snippet}` hits. |
| `get_wikipedia_article(title, max_chars=40000)` | Fetch one article as clean plain text (redirects resolved, chrome stripped). Miss → `{found:False, suggestions}`. |
| `write_stubs(targets, dry_run=False)` | Write deterministic vault stubs for `Folder/Name` targets. Never overwrites; returns `discovered` links to feed back in. |
| `article_facts(target)` | Compact fact sheet (~250–500 tokens) for writing a full article from — counts, eras, collaborators, labels, gaps. |
| `build_article(target, overview, questions, ...)` | Write the full article: tables, members, labels, connections, gated stubs, back-link repair. Returns counts, not content. |

The two `*_wikipedia` tools read a local Kiwix ZIM (text-only English Wikipedia) via
libzim — an offline snapshot, not the live web. They boot lazily: if the ZIM is
absent they return a clear "not available" message instead of failing the server.
See `wikipedia.py`. Discogs stays the source of truth for discographies/credits;
Wikipedia is for surrounding context (history, biographies, scenes).

## One-time setup

Build the FTS indexes into the existing `.duckdb` file. Takes ~30–60 minutes and adds
~2–4 GB to the file. Must be run against the file directly (writable), while nothing
else holds it open.

```bash
python3 -m pip install duckdb
python3 mcp-servers/custom/setup_fts.py --db-path ~/projects/discogs_db/discogs.duckdb
# --force to rebuild
```

Then build the collaboration edge table. Same requirement (writable, nothing else
holding the file — stop the MCP container first). Takes a few minutes and adds
~1–2 GB. This backs `find_collaborators` / `find_path_between_artists`; the server
falls back to the full `release_artist` table (slower) and logs a warning if it's
absent.

```bash
python3 mcp-servers/custom/build_edge_table.py --db-path ~/projects/discogs_db/discogs.duckdb
# --force to rebuild
```

## Offline Wikipedia (optional)

`search_wikipedia` / `get_wikipedia_article` read a Kiwix ZIM. Download the
text-only English dump into the `/data` mount so the container sees it:

```bash
mkdir -p ~/projects/discogs_db/wikipedia
curl -L -C - --retry 10 --retry-all-errors \
  -o ~/projects/discogs_db/wikipedia/wikipedia_en_all_nopic_2026-06.zim \
  https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_nopic_2026-06.zim   # ~49 GB
```

The default path is `/data/wikipedia/wikipedia_en_all_nopic_2026-06.zim`; override
with `WIKIPEDIA_ZIM_PATH`. To refresh with a newer monthly dump, drop the new ZIM
in place and point `WIKIPEDIA_ZIM_PATH` at it (or match the default filename), then
restart the container. No FTS build step — the ZIM ships its own Xapian full-text
index.

## Vault stubs (`write_stubs`)

Stub pages are almost entirely derivable — Discogs ID, real name, first release
year, credit count, top styles/labels, credited roles, band membership, label
parent/sublabels/roster — so `write_stubs` builds them with no model involved.
Because it runs here, next to the database, the rows never pass through an
agent's context: the caller sends names and gets back counts.

The Research Queue it emits is gap-derived — a question appears only when the
field is genuinely missing from Discogs. It never asks for something we can
already answer with a query.

Two safety properties worth knowing:

- **It never overwrites.** An existing page may be a finished article, and
  replacing one with a stub would destroy work Discogs cannot regenerate.
  Existing targets are skipped and counted.
- **It never guesses an ID.** A name that doesn't match Discogs exactly comes
  back in `unresolved` with candidate spellings.

Output is idempotent: every ranked list has a tiebreak, so re-running over an
unchanged database produces byte-identical files.

New stubs link to entities that may not have pages yet; those come back in
`discovered`. Feed them in as the next call's `targets` and repeat until
`discovered` is empty, otherwise the run leaves dead links behind.

Requires the vault mounted read-write at `/vault` (override with `VAULT_PATH`).

## Full articles (`article_facts` + `build_article`)

The hourly stub trawl spent ~94% of its tokens on cache reads — the agent
pulled ~180k tokens of Discogs payloads into context and re-read them each
turn, largely to retype rows as markdown tables. Output was 0.3% of the spend.

These two tools move that work next to the database:

1. `article_facts(target)` returns a few hundred tokens — identity, total
   credits, releases per decade, top styles, members, top collaborators with
   shared-release counts, label summary, detected gaps. Enough to write an
   overview paragraph from. **Do not follow it with `get_artist_discography`**;
   the release rows go straight into the page and never need to be seen.
2. `build_article(target, overview, questions)` writes the article. Everything
   but the prose is generated: releases/catalogue table, members, label
   summary, roster, connections, and the Discogs header. It also creates stubs
   for newly linked entities and repairs back-links across the vault.

The page also quotes the entity's Discogs **profile** — the free-text field
where the material that looks like outside research actually lives. Force Inc's
label code, founding month, founder and the EFA-Medien collapse are all in its
profile; a trawler reading `get_label` was paraphrasing that field, not
researching. When an alias has no profile of its own the real-name entity is
followed one hop (Mapstation is empty; Stefan Schneider carries the Düsseldorf
art-academy biography). BBCode entity references like `[a=Achim Szepanski]` and
`[l199815]` are resolved to names, linked, and returned in `profile_refs` —
they are real link targets, and most of why a trawler-written page carried
several times the wiki-links this used to emit.

Only ~7% of artists and ~9% of labels have a substantial profile, so this
closes the gap on notable entities and changes nothing for the long tail.

Depth rules match the trawler brief — primary credits only past 50, capped at
40 releases, full catalogue for labels under 1,000 releases.

Stub creation is gated the same way the brief gates it: labels need more than
one release, people and bands need more than one credit *and* a footprint
beyond the entity being written about. Dead links beat low-value stubs.

Back-link repair links bare mentions of the entity across the vault. Multi-word
names are unambiguous and linked on sight. Single-word ones — the vault has
pages called Low, Suicide, Swans and Wire — are linked only on pages that
corroborate the subject: the page already links the entity, or at least two of
its associated names (bandmates, labels, collaborators) appear in the text. A
page about low temperatures will not mention two of a band's collaborators.

Matching is case-sensitive and whole-word, and never writes into YAML
frontmatter, fenced or inline code, existing wiki-links, or markdown link
targets.

`discogs_dry: true` in the fact sheet means Discogs has nothing — `build_article`
flags the page's frontmatter, leaves the stub marker, and stops.

## Build and run

```bash
docker build -t nanoclaw-discogs-mcp mcp-servers/custom

docker run -d --name mcp-custom \
  --restart=unless-stopped \
  -v ~/projects/discogs_db:/data:ro \
  -v ~/discogs-mcp-logs:/logs \
  -v ~/nanoclaw/nanoclaw/groups/whatsapp_main/bands-research:/vault \
  -p 8765:8765 \
  nanoclaw-discogs-mcp

docker logs -f mcp-custom
```

Per-tool-invocation query logs are appended to `~/discogs-mcp-logs/queries.jsonl`
(one JSON object per line: `ts`, `tool`, `args`, `queries`, `row_count`,
`duration_ms`, `status`, `error`). Override the in-container path with
`QUERY_LOG_PATH` if you need it somewhere else.

On Linux the agent container reaches the host via `host.docker.internal` because
`src/container-runtime.ts` passes `--add-host=host.docker.internal:host-gateway`. No
extra config required on the agent side.

## Overrides

- `DISCOGS_DB_PATH` env var to point at a different DB file (default `/data/discogs.duckdb`).

## Design notes

- **Read-only DuckDB connection is the primary write-prevention gate.** sqlglot validation
  in `run_readonly_sql` is defense in depth and better error messages.
- **`extra=0` asymmetry in `find_collaborators`.** We require the *seed* side of each hop to
  be primary-credited on the release (i.e. "follow via the artist's own releases") but allow
  neighbors to be any credit. This surfaces producers/remixers/features while keeping the
  graph finite. Both-sides `extra=0` was tried and produced near-empty results for most
  seeds.
- **Special placeholder artists are excluded from graph traversals.** Discogs `Various`
  (id 194, ~1.3M primary credits), `Unknown Artist` (355) and `No Artist` (118760) aren't
  real entities; admitting them into the `find_collaborators` / `find_path_between_artists`
  self-join both explodes the join and yields meaningless edges. They're filtered on both
  seed and neighbor sides (`SPECIAL_ARTIST_IDS` in `queries.py`, kept in sync with
  `build_edge_table.py`).
- **Graph self-joins run against `release_artist_slim`.** The traversal tools only touch
  five columns (`release_id, artist_id, extra, role, is_non_musical`); `build_edge_table.py`
  materializes just those, ordered by `artist_id` (so zone-maps prune the seed-side filter)
  and with the special artists pre-removed. `find_collaborators` was ~71% of all DB time in
  the query log. The tool falls back to `release_artist` when the slim table is absent.
- **Frontier id-sets are inlined into the SQL, not passed as `IN (SELECT UNNEST(CAST(? AS
  BIGINT[])))`.** DuckDB can't push the UNNEST-subquery form into the scan, so the seed-side
  predicate forced a full scan of the ~90M-row edge table (~13s per single seed vs ~0.7s
  inlined — this was the dominant cost, pre-dating the slim table). `_int_in()` renders the
  bounded, int-coerced frontier as a literal `IN (…)` list. If you add new frontier filters,
  use it — don't reintroduce the UNNEST form.
- **The `musical` role blocklist is precomputed into `is_non_musical`.** It used to apply 18
  `role NOT ILIKE ?` patterns across the whole self-join on every call; `build_edge_table.py`
  evaluates them once at build time so the filter is a single boolean read. The 18 ILIKE
  patterns in `NON_MUSICAL_PATTERNS` (build script) must stay in sync with
  `_NON_MUSICAL_PATTERNS` in `queries.py` (used for the `release_artist` fallback path).
- **Year-range filters exclude `released_year IS NULL`** (~13% of releases). If agents need
  to include undated releases, use `run_readonly_sql` directly.
- **DuckDB memory/threads** default to 16GB / 8 threads, overridable via `DUCKDB_MEMORY_LIMIT`
  / `DUCKDB_THREADS` env vars (host is 12-core/31GB; container is uncapped).
- **MCP request timeout should be ≥90s.** Some queries (deep BFS, wide scene snapshots) can
  run close to a minute.

## Smoke tests

See `smoke.py` (if present) or exercise manually:

```bash
# basic ping
curl -s http://localhost:8765/mcp/ -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# expect 19
```

Agent-side: ask "who is aphex twin and what are their top labels" — should invoke
`search_artist` under the `custom__` prefix.
