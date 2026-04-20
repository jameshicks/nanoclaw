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
| `get_label(label_id)` | Full record + parent, sublabels |
| `get_artist_discography(artist_id, role=None, as_main_only=False, year_range=None, unique_masters_only=True)` | Role aliases: `performer`, `producer`, `writer`, `engineer`. Default collapses pressings to one row per master. |
| `get_label_releases(label_id, year_range=None, unique_masters_only=True)` | Release-level catalog for a label, with primary_artists and catno per row |
| `get_label_roster(label_id, year_range=None)` | Primary artists by release count |
| `find_collaborators(artist_id, depth=1, min_shared_releases=1, roles=None)` | BFS, depth ≤ 3. `roles` aliases: `musical` (blocklist), `performer`, `producer`, `writer`, `engineer`. Results include `top_shared_titles`. |
| `find_path_between_artists(a_id, b_id, max_depth=4)` | Shortest path, max_depth ≤ 6 |
| `get_scene_snapshot(label_ids=None, year_range=None, country=None)` | Multi-dim slice |
| `list_compilations_featuring_artist(artist_id)` | Filtered on `release_format.descriptions` |
| `describe_schema()` | Prose overview agents can fetch on demand |
| `run_readonly_sql(query, row_limit=1000)` | sqlglot-validated SELECT-only escape hatch |

## One-time setup

Build the FTS indexes into the existing `.duckdb` file. Takes ~30–60 minutes and adds
~2–4 GB to the file. Must be run against the file directly (writable), while nothing
else holds it open.

```bash
python3 -m pip install duckdb
python3 mcp-servers/custom/setup_fts.py --db-path ~/projects/discogs_db/discogs.duckdb
# --force to rebuild
```

## Build and run

```bash
docker build -t nanoclaw-discogs-mcp mcp-servers/custom

docker run -d --name nanoclaw-discogs-mcp \
  --restart=unless-stopped \
  -v ~/projects/discogs_db:/data:ro \
  -v ~/discogs-mcp-logs:/logs \
  -p 8765:8765 \
  nanoclaw-discogs-mcp

docker logs -f nanoclaw-discogs-mcp
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
- **Year-range filters exclude `released_year IS NULL`** (~13% of releases). If agents need
  to include undated releases, use `run_readonly_sql` directly.
- **MCP request timeout should be ≥90s.** Some queries (deep BFS, wide scene snapshots) can
  run close to a minute.

## Smoke tests

See `smoke.py` (if present) or exercise manually:

```bash
# basic ping
curl -s http://localhost:8765/mcp/ -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# expect 15
```

Agent-side: ask "who is aphex twin and what are their top labels" — should invoke
`search_artist` under the `custom__` prefix.
