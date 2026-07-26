---
name: build-out-name
description: End-to-end macro for researching a name (artist, band, label, or release) and writing a full vault article with stubs for everyone it mentions. Use whenever the user says "build out X", "research X", "look into X", or asks for a full writeup on a specific music name. Runs deterministically via the article_facts and build_article MCP tools — no discography pulled into context, no subagent.
---

# Build Out Name — Research Macro

One named sequence that takes a music name from zero → full vault article → stubs for every connection → summary and web-search ask. Codifies the multi-step workflow so it survives context compaction.

The build-out itself is two tool calls — `article_facts` then `build_article`. The tables, stubs, back-links, depth rules and stub gating all live in the MCP server now, so this skill is the sequence and the judgement, not a tool reference. You do **not** need to load `discogs-research` for a build-out; load it only if you end up doing wider research through `discogs-trawler`.

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

## Phase 1 — Fact sheet

```
mcp__custom__article_facts("Folder/Name")
```

Returns a few hundred tokens: Discogs ID, total credits, first year, releases
per decade, top styles, members, bands they belong to, top collaborators with
shared-release counts, label summary, and detected gaps.

That is the whole research step. Do **not** call `get_artist_discography`,
`get_release`, `get_label_releases`, `get_label_roster`, `find_collaborators`
or `run_readonly_sql` alongside it. The release rows go straight into the page
without passing through your context — that is the point of these tools, and
pulling them yourself is what used to make a build-out cost about a dollar.

If it returns `resolved: false`, the vault name does not match Discogs. It
gives candidate spellings — report them and stop. Do not guess an ID.

If it returns `discogs_dry: true`, call `build_article` with no overview: it
flags the page's frontmatter, leaves the stub marker in place, and stops.

## Phase 2 — Write the article

From the fact sheet **alone**, write:

- **overview** — a short paraphrase, 2 to 4 sentences.

  If the fact sheet carries a `profile`, that is Discogs' own prose. The page
  block-quotes it verbatim under "From Discogs" with attribution, so your job
  is to **paraphrase, not repeat**: compress its substance into a sentence or
  two in your own words, then add what it doesn't say and the data does — the
  shape of the catalogue (`eras`), label relationships, recurring
  collaborators. When `profile_via` is set the prose describes the entity
  behind the alias, so say whose biography it is.

  Never quote the profile back and never copy its phrasing. Where profile and
  counts disagree, prefer the profile for biography and the counts for
  catalogue.

  With no `profile`, write from `eras`, `styles`, `labels`, `collaborators`
  and `members` alone. State only what the data supports; invent no biography.
- **questions** — Research Queue entries. Each must be answerable from **one**
  authoritative page. Never ask anything Discogs can answer — discography,
  roster, founding year, catalogue numbers, label structure.

  **If you can see it in the fact sheet or the quoted profile, it is not a
  question.** Both of these were produced by the scheduled job and both are
  wrong: "identify which release the 'Mixed By' credit is on" (the release
  table names it) and "confirm his specific role on each release" (a Discogs
  credit lookup). Zero questions is the correct answer for a thinly
  documented entity; one vague question is worse than none.

Then:

```
mcp__custom__build_article("Folder/Name", overview=..., questions=[...])
```

It writes the release or catalogue table, members, label summary, roster,
connections with wiki-links, and the Discogs header. It returns counts, not
content — do not read the article back to check it.

Target folders are unchanged: `Bands/`, `People/`, `Labels/`, `Releases/`,
`Topics/`. Filenames use the Discogs display name with spaces.

**Never run this over an existing full article.** `build_article` overwrites,
and a trawler-written or web-researched page carries prose that Discogs cannot
regenerate. Build out stubs, not finished work.

## Phase 3 — Stubs and back-links (automatic)

`build_article` already did both:

- **Stubs** for newly linked entities, gated by the vault rules — labels need
  more than one release, people and bands need more than one credit and a
  footprint beyond this entity. Dead links beat low-value stubs, so the rest
  are deliberately left unlinked. The returned `stubs.gated_out` counts them.
- **Back-link repair** across the vault. Multi-word names are linked on sight;
  single-word names (Low, Swans, Wire) only on pages that corroborate the
  subject, so ordinary prose is never rewritten.

Nothing to do here. Do not write stubs by hand.

## Phase 4 — Summarize and gate the web search

Send a `send_message` that:

1. Lists what was written (main article + N stubs).
2. Names **specific** gaps Discogs couldn't fill (not vague "needs more detail" — e.g. "no birth year for X", "producer credit on Y is blank in Discogs").
3. Explicitly asks whether to search the web.

Then **stop**. Do not pre-emptively call web tools. Wait for the user's answer in the next turn.

If the user approves web research, supplement the article and stubs. If not, the sequence is complete.

---

## Efficiency notes

- The whole macro is now two tool calls. There is nothing to parallelise and nothing to fan out.
- Send one progress ping between Phase 1 and Phase 2 ("got the facts, writing it up") so the user isn't left guessing — the build call takes a few seconds.
- The Discogs ID lands in the page header automatically; it is the fastest re-entry point for future sessions.

## Common mistakes

- **Pulling a discography.** `article_facts` is the only lookup. Calling `get_artist_discography` "to see the releases" reintroduces the entire cost this macro was rewritten to remove — the rows are already going into the page.
- **Running `build_article` over a finished article.** It overwrites. Prose from a trawler or web pass is unrecoverable. Check for `## Stub — needs full research` first.
- **Writing stubs or fixing back-links by hand.** `build_article` does both, with gating and safety rules you will not reproduce manually.
- **Reading the article back to verify.** Trust the returned counts.
- Framing the web search as already-decided in Phase 4 ("I'll also check the web for…"). CLAUDE.md explicitly forbids this. Ask, wait.
