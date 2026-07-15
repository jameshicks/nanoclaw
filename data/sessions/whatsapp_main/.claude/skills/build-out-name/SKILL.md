---
name: build-out-name
description: End-to-end macro for researching a name (artist, band, label, or release) and writing a full vault article with stubs for everyone it mentions. Use whenever the user says "build out X", "research X", "look into X", or asks for a full writeup on a specific music name. Relies on the discogs-research skill for MCP tool and SQL reference.
---

# Build Out Name — Research Macro

One named sequence that takes a music name from zero → full vault article → stubs for every connection → summary and web-search ask. Codifies the multi-step workflow so it survives context compaction.

Load `discogs-research` for MCP tool signatures, SQL patterns, and article/stub formats. This skill is the sequence; that skill is the reference.

---

## Phase 0 — Acknowledge and identify

1. `send_message` acknowledging what you're building out (CLAUDE.md requires this for non-trivial requests).
2. Decide entity type. If the user's name is ambiguous, run in parallel:
   ```
   search_artist(name)
   search_label(name)
   search_release(name)   # only if the name looks like a title
   ```
   Pick the highest-confidence match. If results are equally plausible (e.g. a label and a band share the name), ask the user which one.

## Phase 1 — Exhaust Discogs

Follow the **Discogs-first, web-on-permission** hard rule (see CLAUDE.md). Do not call WebSearch, WebFetch, or agent-browser in this phase.

### Artist or band

```
get_artist(id)                                          # profile, aliases, members, groups
get_artist_discography(id)     ──┐
find_collaborators(id)           ├── in parallel
list_compilations_featuring_artist(id)  ──┘
```

Then for each of the top ~8 releases that look pivotal (era-defining, on a key label, heavily collaborated):

```
get_release(release_id)   # full personnel
```

Walk aliases and member IDs recursively — one hop deep minimum. Capture every Discogs ID you touch.

### Label

```
get_label(id)                                           # profile, parent, sublabels
get_label_roster(id)       ──┐
get_label_releases(id)       ├── in parallel
```

Then `get_release(id)` for the 5–10 most significant catalog entries.

### Release

```
get_release(id)            # full credits
```

Then `get_artist(id)` for each credited person whose role is non-trivial (composer, producer, core personnel — not every tape-op).

---

## Phase 2 — Write the main article

Target path:
- Artist/band → `/workspace/group/bands-research/Bands/<Name>.md` (bands) or `People/<Name>.md` (solo artists)
- Label → `/workspace/group/bands-research/Labels/<Name>.md`
- Release → `/workspace/group/bands-research/Releases/<Name>.md`
- Cross-cutting scene/concept → `/workspace/group/bands-research/Topics/<Name>.md`

Filenames use the Discogs display name with spaces (not hyphens). See existing files like `Bush Tetras.md`, `99 Records.md`.

Follow the full-article format from `discogs-research`: bio sections by era, personnel tables per key release, collaborators table, labels summary, connections with `[[wiki-links]]`, Open Questions.

Use `[[Folder/Name]]` wiki-link form (matching `_Index.md` convention), or `[[Folder/Name|Display]]` when display text differs.

## Phase 3 — Stub every mentioned name

CLAUDE.md rule: **every person, band, and label mentioned deserves its own page — even with one or two facts.** Don't batch this for the end; do it as a final pass before summarizing.

For each `[[...]]` link in the article whose target file doesn't exist:

1. Decide the folder from context (person/band/label).
2. Write a stub using the format in `discogs-research` — the short version:

   ```markdown
   # <Name>

   <One-line intro: what they are, when active, Discogs ID if known>

   <Short sentence placing them in context of the article that mentioned them>

   ## Research Queue
   - [ ] <specific open question 1>
   - [ ] <specific open question 2>
   ```

Parallelize stub file writes — there's no ordering dependency.

## Phase 4 — Summarize and gate the web search

Send a `send_message` that:

1. Lists what was written (main article + N stubs).
2. Names **specific** gaps Discogs couldn't fill (not vague "needs more detail" — e.g. "no birth year for X", "producer credit on Y is blank in Discogs").
3. Explicitly asks whether to search the web.

Then **stop**. Do not pre-emptively call web tools. Wait for the user's answer in the next turn.

If the user approves web research, supplement the article and stubs. If not, the sequence is complete.

---

## Efficiency notes

- Phase 1 parallelism matters — Discogs MCP calls are independent; fanning them out cuts wall time substantially.
- Phase 3 stub writes can also parallel.
- Don't collapse the whole macro into a silent run — send progress pings between Phase 1 and Phase 2 ("Discogs exhausted, writing article now") so the user isn't left guessing.
- Every Discogs ID you've touched should end up in a file — either in the main article or in a stub. IDs are the fastest re-entry point for future sessions.

## Common mistakes

- Skipping Phase 3 — leaves `[[dead links]]` that `vault-maintenance` will flag later. Cheaper to stub now.
- Writing the main article before walking aliases/members — you'll miss the obvious collaborators and have to edit.
- Framing the web search as already-decided in Phase 4 ("I'll also check the web for…"). CLAUDE.md explicitly forbids this. Ask, wait.
