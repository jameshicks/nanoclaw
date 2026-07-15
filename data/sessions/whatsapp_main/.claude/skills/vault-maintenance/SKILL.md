---
name: vault-maintenance
description: Audit the bands-research vault for health issues — broken wiki-links, names mentioned without their own page, and open Research Queue items across all files. Read-only audit; doesn't modify files. Use when the user asks to check vault health, find stubs to fill, find dead links, or triage open research questions.
---

# Vault Maintenance — Dispatcher

This skill is a thin dispatcher. The actual audit runs in the **`vault-auditor` subagent**, so the raw scan output never enters this conversation's context. That matters: the vault is large (thousands of files), and a naive in-context bash audit would spend a lot of tokens before producing a useful summary.

## How to run an audit

Dispatch the `vault-auditor` subagent with a one-line brief:

- "Run the standard vault audit." → full summary (top-10 per category)
- "Run the vault audit, top 20." → wider report
- "Run the vault audit and tell me who references `[[People/Missing Guy]]`." → triggers `--detail` mode for that target

The subagent runs `node /home/node/.claude/skills/vault-maintenance/scripts/audit.mjs`, which does a single O(n) walk over `/workspace/group/bands-research/` and prints a fixed-size summary regardless of vault size. It returns a compact report (~25 lines) covering:

- Broken/missing wiki-links — refs, unique targets, top-K most-referenced (stub candidates)
- Ambiguous bare `[[Name]]` links matching multiple folders (source articles need qualifying)
- Open Research Queue items — total, busiest files

## After the report — fix flows

Audits are read-only by convention. The auditor never edits anything. After delivering the summary, **wait for the user to pick what to act on** before doing any of the below. Each category has a different fixer, with a different cost profile.

### Fixing missing stubs (most-referenced unwritten names)

These are the highest-leverage: a name referenced 30× from existing articles is a real gap.

1. The audit's "Top N most-referenced missing" list is the prioritized work queue.
2. For each name the user picks, dispatch the **`discogs-trawler`** subagent (or invoke `build-out-name` directly) with a one-line brief: `"Build out People/Arthur Russell — referenced 47× across the vault."`
3. The trawler does the Discogs research and writes the file under the right folder. It also stubs everyone *that* page mentions, by design.
4. After a batch of stubs lands, **re-run the audit** — the missing-link counts will have shifted (new stubs may have introduced new dead links).

Don't batch more than ~5 stubs in one go without checking back with the user — each stub is a research session and the user may want to course-correct between them.

### Fixing ambiguous bare links (`[[Name]]` matching multiple folders)

Mechanical once the human makes one decision. Workflow:

1. For each ambiguous name in the audit, ask the user which folder is correct: *"`[[DNA]]` matches `Bands/DNA` and `People/DNA` — which one?"*
2. Find the source files: dispatch `vault-auditor` with `--detail "<name>"` (or just grep — but only if it's a small handful, otherwise the subagent keeps the output out of context).
3. For each source file, `Edit` the bare `[[Name]]` → qualified `[[Folder/Name]]`. Be careful with display-text form: `[[Name|Display]]` becomes `[[Folder/Name|Display]]`.
4. Don't auto-resolve based on heuristics ("the band has more references, so it's probably this one") — ambiguity fixes are cheap to do right and expensive to do wrong (a mis-qualified link silently points to a different page).

If you find yourself fixing more than ~10 ambiguities at once, consider writing a small one-shot script rather than 30+ Edit calls — but keep it ad-hoc, not a permanent skill file.

### Fixing open Research Queue items

These are the hardest because they're *literally* "we don't know yet." Don't try to bulk-resolve. Workflow per item the user points at:

1. Read the source file to understand the context of the question.
2. If the answer is in the vault elsewhere → `Grep` the vault, write the answer into the file, check the box.
3. If the answer needs Discogs research → dispatch `discogs-trawler` with the specific question. When it returns, integrate the answer and check the box.
4. If the answer needs the open web → flag to the user; the auditor and trawler don't have web access. Don't fabricate.

Never check a box without writing the answer. An unchecked box is honest; a checked box without the answer below it silently corrupts the queue.

### What to never do automatically

- **Don't auto-stub the entire missing-links list.** A 312-target list would create 312 sparse pages and ~10× that in new dead links. Always work top-down with user direction.
- **Don't auto-resolve ambiguous links by frequency.** One human decision per name.
- **Don't delete dead links.** A `[[Name]]` pointing nowhere is a future stub candidate, not garbage. The audit's job is to surface it; deletion would lose that signal.

## Scope

The audit only looks at `/workspace/group/bands-research/`. If the vault expands beyond `Bands/`, `People/`, `Labels/`, `Releases/`, `Topics/`, the script's output stays correct (it walks recursively) but the per-folder ambiguity check assumes those five top-level folders — adjust `audit.mjs` if that ever changes.

## Why a subagent

A 5000-file vault has tens of thousands of wiki-links and potentially thousands of open Research Queue checkboxes. Running grep/find recipes in the main agent's context dumps all of that here even after sorting; running them as bash inside a subagent walls the raw output off, and the subagent only forwards a bounded summary. Token cost per audit is roughly constant — it doesn't grow with the vault.
