---
name: authoring-jobs
description: How to design a recurring/scheduled job so it stays cheap and reliable. Load this BEFORE calling schedule_task for any job that reads or edits a data file (a queue, log, index, backlog, or vault) on every run — or whenever the user asks you to "make a job/task", "schedule something recurring", or "process my queue". Prevents the whole-file-read token blowups that naive job prompts cause.
---

# Authoring recurring jobs

When a user asks you to build a recurring job (`schedule_task`), the way you write the **prompt and script** decides whether it stays cheap or silently balloons into a huge recurring token bill. Naive job prompts are the #1 cause of runaway cost here. Read this before you author the job.

## The golden rule

**A job's per-run context must be small and bounded — it must NOT grow as its data file grows.** The model should never read a large, growing file on every run. If a job touches a queue / log / index / backlog, the model sees only a *small batch*; deterministic scripts do the bulk file I/O.

## Anti-patterns — the "goofy" job (do NOT write prompts like this)

1. **"Read the whole queue file, find the open items, edit them in place."** As the file grows past ~2000 lines, the Read tool paginates it, so the agent re-reads the *entire* file many times per run (once to scan, again to locate each item it edits). A 150k-token file read 15× = ~2M tokens **per run**, growing forever. This is the classic blowup.
2. **Append-only data files that never prune.** Resolved/done items pile up and are re-read every run. Files reach hundreds of KB of dead weight.
3. **Editing the big file with the `Edit` tool.** `Edit` requires the file to be Read first → pulls the whole file into model context. Never mutate a large data file through the model.
4. **No wake-gate.** The agent wakes on every tick even when there's nothing to do.
5. **High frequency with no gate.** Many wake-ups/day burn credits and risk rate limits.

## The correct pattern: gate → batch → apply

Split every data-processing job into three deterministic pieces so the model only ever sees a tiny slice:

1. **Wake-gate (`script`)** — a fast check that prints `{"wakeAgent": <bool>, "data": {...}}`. Wake the agent only when there's work. Keep `data` small (counts, not payloads).
2. **Batch extraction** — the script (or a helper it calls) writes just the N items to process into a small `_*_work.md` file. The prompt tells the agent to **read ONLY that work file** and explicitly **forbids reading the big file**.
3. **Deterministic apply-back** — the agent writes its results to a small JSON, then runs a `node` helper that mutates the big file with `fs` (no model tokens). The model never Reads or Edits the big file.

Plus: **prune** terminal items out of the live file periodically (archive to `_*_resolved.md`), and **cap** per-run edits (e.g. ≤60) so a bad run can't loop into thousands of file ops.

## Reuse the existing helper

This group already has a battle-tested helper: **`bands-research/_queue_tools.mjs`** (`/workspace/group/bands-research/_queue_tools.mjs`). It handles `---`/`**Status:**`-formatted markdown queues with these commands:

- `prepare <queueFile> <n> <workFile>` — extract N open items → work file. Prints `{wakeAgent, data:{openCount, batchCount}}`.
- `apply-answers <queueFile> <answersJson>` — flip `open`→`resolved` + write `**Answer:**` for `[{ref, answer}]`.
- `triage-prepare` / `triage-apply` — route items between two queues (move or tag) without reading either into the model.
- `wiki-prepare <queueFile> <n> <workFile>` / `wiki-apply <queueFile> <decisionsJson> <discogsQueue> <webHandoff>` — resolve-with-routing: flip items `resolved` with `**Answer:**`/`**Routed:**` and append residual blocks to other queues in one deterministic pass.
- `enqueue <queueFile> <entriesJson>` — append new items via `fs` (never Read+Edit the target).

Items are keyed by `ref` = `sha1(question)[:10]`, stable across runs. **Prefer extending `_queue_tools.mjs` with a new subcommand over inventing a new whole-file-reading job.** If the data isn't in that markdown format, write a small analogous helper following the same gate→batch→apply shape.

## Job template

Wake-gate + batch `script`:

```bash
node /workspace/group/<dir>/_queue_tools.mjs prepare /workspace/group/<dir>/_myqueue.md 5 /workspace/group/<dir>/_my_work.md
```

Prompt skeleton:

```
## <Job name>  — AUTOMATED TASK, no acknowledgment/preamble; one final send_message only.

A pre-flight script wrote your batch to `/workspace/group/<dir>/_my_work.md`.
Read ONLY that file. Do NOT read or edit `_myqueue.md` — it is large; reading it wastes huge token budget.

For each item (keyed by `## ref:`): <do the work; update small source files as needed>.

When done:
1. Write `/workspace/group/<dir>/_my_answers.json` = [{ "ref": "...", "answer": "..." }, ...].
2. Run: node /workspace/group/<dir>/_queue_tools.mjs apply-answers /workspace/group/<dir>/_myqueue.md /workspace/group/<dir>/_my_answers.json
3. send_message with a short summary.

Hard rules: NEVER Read the big queue file. Max N items/run. Edit at most ~60 source files/run.
```

## Checklist before you call `schedule_task`

- [ ] Does the prompt read a small work file, never the big data file? (grep your own prompt for "read the whole" / the big filename)
- [ ] Is there a wake-gate script so the agent only runs when there's work?
- [ ] Are results applied back with an `fs` helper, not the `Edit` tool on the big file?
- [ ] Is there a prune/archive story so the file doesn't grow forever?
- [ ] Is there a per-run edit cap to prevent runaways?
- [ ] Is the frequency the minimum that meets the need? (See the `admin` skill's "Frequent task guidance".)
- [ ] Did you **test the script and any helper** in your sandbox before scheduling?

If any box is unchecked, fix the design before scheduling — a bad recurring job costs money on every single run until someone notices.
