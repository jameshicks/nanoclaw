# NanoClaw v1 → v2 Migration Plan

Written 2026-07-26 against local `main` @ `3b9771c2` (v1.2.52) and
`upstream/main` @ `f1e66179` (v2.1.53).

---

## 1. Shape of the migration

v2 is **not a merge**. Upstream ships `migrate-v2.sh`, which expects a fresh
v2 checkout in a **sibling directory** of the v1 install, auto-detects v1 by
looking for `store/messages.db`, and copies state across. v1 is never
modified — it is paused, not upgraded.

```
/home/james/nanoclaw/
├── nanoclaw/       ← v1.2.52, current install, stays untouched
└── nanoclaw-v2/    ← new clone of nanocoai/nanoclaw
```

Do **not** run `/migrate-nanoclaw` or `/update-nanoclaw`. Those are v1→v1
tools; the gap here is 1,564 upstream commits and a restructured `src/`.

**Rollback at any point** is `systemctl --user stop <v2-unit> && systemctl
--user start nanoclaw`. The script disables the v1 unit but leaves it on
disk precisely for this.

### Remotes

- `origin` = `jameshicks/nanoclaw` (personal fork, public) — safe to push
- `upstream` = `qwibitai/nanoclaw` → 301-redirects to `nanocoai/nanoclaw` (the org) — never push

Clone v2 from `https://github.com/nanocoai/nanoclaw.git`. Afterwards decide
whether the v2 checkout should point `origin` at your own fork too.

---

## 2. Guiding principle: check before you port

Roughly half the port list below exists because v1 had a problem. v2 may
have fixed it upstream. **For every item, first search v2 for an existing
solution; only port if it is genuinely absent.** Porting a workaround onto
a codebase that no longer needs it is the main way this migration turns
into a mess.

Second principle: **get v2 answering real messages before porting
anything.** A broken router with eight customizations layered on is
undebuggable.

---

## 3. Phase 0 — Pre-flight (DONE)

- [x] All 129 commits pushed to `origin/main` (in sync, verified)
- [x] Wikipedia integration committed (`4bb747a2`)
- [x] `authoring-jobs` skill committed (`3060fb60`)
- [x] Duplicate-relay fix committed (`d3f06bd7`)
- [x] `__pycache__` gitignored (`299c496e`)
- [x] Architecture rationale committed (`3b9771c2`)
- [ ] Copy `tradeoffs-advocacy.md` out — it is gitignored and **will not be
      carried over** by `migrate-v2.sh`
- [ ] Note the Discogs DuckDB + Wikipedia ZIM paths (~49 GB ZIM); these live
      outside the repo and must stay reachable from the v2 install

---

## 4. Phase 1 — Automated migration

Run in a **real terminal** — the script refuses to run inside Claude Code
because it has interactive prompts and streams progress.

```bash
git clone https://github.com/nanocoai/nanoclaw.git /home/james/nanoclaw/nanoclaw-v2
cd /home/james/nanoclaw/nanoclaw-v2
bash migrate-v2.sh
```

What it does, in order:

| Phase | Step | Notes |
|---|---|---|
| 0 | Bootstrap Node/pnpm, find v1, validate `store/messages.db` | aborts if `registered_groups` missing |
| 1 | Merge `.env`; seed v2 DB; copy group folders, session data, scheduled tasks | group `CLAUDE.md` → staged as `CLAUDE.local.md` |
| 2 | Channel select (interactive) → copy auth → install channel code | pick **whatsapp** and **telegram** |
| 3 | Docker check, OneCLI setup, copy container skills v2 lacks, build image | OneCLI already running locally |
| — | Service switchover (interactive), then keep-or-revert prompt | send a real test message before answering |
| 4 | Write `logs/setup-migration/handoff.json`, `exec claude /migrate-from-v1` | |

**Watch for:** step `3d` copies only container skills v2 *doesn't already
have* — `authoring-jobs` should come across; anything name-colliding with a
v2 skill is silently skipped.

---

## 5. Phase 2 — `/migrate-from-v1`

The script hands off to this skill automatically. It covers:

1. **Triage** any failed steps in `handoff.json`
2. **Smoke test** — v2 answers a real message on a real channel
3. **Seed owner** — v2 retired "main channel = admin" for user-level roles.
   Your user id will be `whatsapp:<phone>@s.whatsapp.net` and/or
   `telegram:<numeric_id>`. Grant `owner` via
   `src/modules/permissions/db/user-roles.ts`.
4. **`/migrate-memory`** — folds staged `CLAUDE.local.md` into v2's composed
   `CLAUDE.md` (shared base + per-group fragments)
5. **Reconcile `container.json`** mount paths

Also decide here: **channel isolation mode.** v2 offers three levels — separate
agent groups, shared agent with independent conversations
(`session_mode: 'shared'`), or one merged session
(`session_mode: 'agent-shared'`). Given group-session token cost is already
the dominant expense, prefer **separate or `shared`** over `agent-shared`.

---

## 6. Phase 3 — Port customizations

Ordered by value and by what blocks what. Verify each against v2 first.

### 6.1 Discogs + Wikipedia MCP sidecar — **BLOCKER, do first**

**What v1 does:** `container/agent-runner/src/index.ts` registers the server
as an HTTP MCP endpoint:

```ts
{ type: 'http', url: 'http://host.docker.internal:8765/mcp' }
```

with `mcp__custom__*` in the allowed-tools list. The server itself is a
Python sidecar (`mcp-servers/custom/`) — DuckDB over the Discogs dump plus
libzim over a ~49 GB Wikipedia ZIM. It is *not* a mount and *not* stdio.

**The problem:** v2 models MCP servers as **stdio only**.
`src/container-config.ts`:

```ts
export interface McpServerConfig {
  command: string;
  args?: string[];
  env?: Record<string, string>;
  instructions?: string;
}
```

No `type`, no `url`. `container/agent-runner/src/config.ts` narrows it
further to `{ command, args, env }`. An HTTP MCP server cannot be expressed
in v2's `container_configs` table as shipped.

**Three options, best first:**

1. **Extend `McpServerConfig`** with optional `type: 'http' | 'stdio'` and
   `url`, thread it through `container-config.ts`, the agent-runner config
   parser, and the SDK call site. Small, surgical, and a plausible upstream
   PR — v2 having no HTTP MCP support at all looks like an oversight rather
   than a decision.
2. **stdio→HTTP bridge** declared as a stdio command (an `mcp-remote`-style
   proxy pointing at `:8765`). No core changes, one more process per turn.
3. **Convert the sidecar to stdio.** Rejected — it would drag DuckDB and a
   49 GB ZIM into the agent container. The sidecar exists for good reason.

**Also:** container names prefixed `nanoclaw-` get killed by
`cleanupOrphans()` on service restart. Keep the sidecar's name off that
prefix, and re-verify the rule still holds in v2.

**Depends on:** container networking. Confirm `host.docker.internal` still
resolves under v2's container hardening (`--init`, `--shm-size`, dropped
per-group overrides landed in `f1e66179`).

### 6.2 Agent-runner observability (~336 lines)

Three separate things, all in `container/agent-runner/src/index.ts`:

- **Tool-call log** → `/workspace/group/logs/tool-calls.jsonl` via
  `PreToolUse`/`PostToolUse` hooks
- **Per-run usage log** → `/workspace/group/logs/usage.jsonl`, one line per
  run with token totals and cost
- **Subagent token recovery** — the SDK does not deliver subagent usage to
  the parent stream, so this sums per-turn usage out of
  `~/.claude/projects/<escaped-cwd>/<sessionId>/subagents/*.jsonl`
- **Duplicate-relay suppression** (`d3f06bd7`) — scheduled task that already
  sent via `send_message` must not also relay its final text

**v2 status:** no hook wiring in the v2 agent-runner at all, and no usage
logging anywhere. This is a from-scratch re-add, not a merge.

**Complications:**
- Agent-runner runtime is **Bun, not Node** in v2. Check `fs` usage and
  JSONL append paths.
- Agent-runner is **shared-source and mounted read-only** across all groups;
  per-group `agent-runner-src/` overlays are gone. The log paths are already
  per-group (`/workspace/group/logs/...`), so this should still work — but
  verify that path exists and is writable under v2's mount layout.
- Re-check the subagent transcript path; session storage changed.

**Priority:** high. This is the only instrumentation for rate-limit and cost
audits, and group-session cost is the known problem.

### 6.3 Session rotation

`SESSION_MAX_BYTES` (15 MB default) in `src/config.ts` +
`rotateGroupSessionIfLarge()` in `src/container-runner.ts`, called from
`src/index.ts`. Archives oversized transcripts so the next turn starts fresh
instead of re-hydrating and repeatedly auto-compacting.

**Check first.** v2 has `src/session-manager.ts` and the two-DB
`inbound.db`/`outbound.db` split. A grep of v2 for `SESSION_MAX_BYTES`/
rotation found nothing, so it is probably still needed — but re-measure
before porting. If v2 changed how transcripts are hydrated, the original
problem may not exist in the same shape.

### 6.4 `resetTimeout` plumbing

Inbound work resets the container's hard-kill timer, so a message arriving
late in the idle window isn't killed mid-turn. Threaded through
`src/group-queue.ts`, `src/task-scheduler.ts`, `src/index.ts`,
`src/container-runner.ts`.

**Check first** — this is a bug fix, and a plausible one for upstream to
have made independently. If v2 still has the bug, port it *and* consider
sending it upstream.

### 6.5 Scheduler interval parsing

`src/task-scheduler.ts`: `parseInt('30m', 10)` silently yields `30`, i.e. a
30 ms interval. Replaced with a strict `Number()` + `Number.isInteger()`
check.

Small, self-contained, and a clear correctness fix. **Strong upstream PR
candidate** — send it rather than carrying it.

### 6.6 `EYES_REACTION_FOLDERS`

Config-driven 👀 read receipt on inbound messages for listed group folders.
Depends on the reactions skill. Low priority, easy to re-add last.

### 6.7 `getMessageContentById` (`src/db.ts`)

Reply-context lookup by message id + chat jid. v2's DB layer is a directory
(`src/db/`) with numbered migrations; find the equivalent accessor rather
than re-adding the function verbatim.

### 6.8 Session skills and agents

`data/sessions/whatsapp_main/.claude/` — agents `discogs-trawler`,
`music-web-researcher`, `vault-auditor`; skills `discogs-research`,
`vault-maintenance`, `admin`, `build-out-name`, `authoring-jobs`.

Copied by `migrate-v2.sh` step 1d, but **verify** — session directory naming
changes under v2's entity model. Confirm the tool allowlists in
`discogs-trawler.md` still match the MCP tool names after 6.1.

### 6.9 Channels

Telegram and WhatsApp are reinstalled fresh from the `channels` branch by
Phase 2. Your v1 tree carries merges from the `telegram` and `whatsapp` fork
remotes (`src/channels/telegram.ts`, +469). After install, diff v2's channel
code against your v1 version and re-apply only genuinely local edits.

---

## 7. Phase 4 — Verification

Before declaring done:

- [ ] Real message answered on WhatsApp **and** Telegram
- [ ] Owner role granted; a non-owner sender is correctly gated
- [ ] Scheduled tasks fire (check one short-interval task end to end)
- [ ] Scheduled task output arrives **once**, not as message + summary (6.2)
- [ ] `mcp__custom__*` tools callable; `search_wikipedia` returns hits
- [ ] `logs/usage.jsonl` and `logs/tool-calls.jsonl` written per group
- [ ] Subagent tokens appear in usage log after a trawler run
- [ ] Group `CLAUDE.md` memory intact after `/migrate-memory`
- [ ] Container image rebuilds clean (prune the buildkit builder first —
      `--no-cache` alone does not invalidate `COPY`)

---

## 8. Decommissioning v1

Only after Phase 4 passes and v2 has run for a few days:

1. Confirm `origin/main` has everything (`git rev-list --left-right --count origin/main...main`)
2. Copy out gitignored local files (`tradeoffs-advocacy.md`, `.env` if not merged)
3. Keep the v1 directory until at least one full billing/usage cycle looks sane
4. Leave the disabled `nanoclaw` systemd unit in place until then
