#!/usr/bin/env node
// Vault audit — single-pass walk over bands-research/*.md.
// Bounded output (counts + top-K) so context cost stays flat regardless of vault size.
//
// Usage:
//   node audit.mjs                       # summary (default)
//   node audit.mjs --detail "Some Name"  # list source files referencing one broken target
//   node audit.mjs --vault <path>        # override default vault path
//   node audit.mjs --top <N>             # change top-K cutoff (default 10)

import fs from 'node:fs';
import path from 'node:path';

const args = process.argv.slice(2);
function flag(name, fallback) {
  const i = args.indexOf(name);
  if (i === -1) return fallback;
  return args[i + 1];
}
const VAULT = flag('--vault', '/workspace/group/bands-research');
const TOP = parseInt(flag('--top', '10'), 10);
const DETAIL = flag('--detail', null);

const FOLDERS = ['Bands', 'People', 'Labels', 'Releases', 'Topics'];

function walk(dir, out = []) {
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (e.isFile() && e.name.endsWith('.md')) out.push(p);
  }
  return out;
}

const files = walk(VAULT);
if (files.length === 0) {
  console.error(`No .md files under ${VAULT}`);
  process.exit(1);
}

// Index of existing files: "Folder/Name" -> path; bare "Name" -> [folders]
const existingByPath = new Set();
const existingByBare = new Map();
for (const f of files) {
  const rel = path.relative(VAULT, f).replace(/\.md$/, '');
  existingByPath.add(rel);
  const slash = rel.indexOf('/');
  if (slash !== -1) {
    const folder = rel.slice(0, slash);
    const name = rel.slice(slash + 1);
    if (!existingByBare.has(name)) existingByBare.set(name, []);
    existingByBare.get(name).push(folder);
  }
}

const LINK_RE = /\[\[([^\]]+)\]\]/g;
const RQ_HEADER = '## Research Queue';

const missingRefs = new Map();   // target -> ref count
const ambiguousRefs = new Map(); // target -> ref count
const missingSources = new Map(); // target -> [files referencing it] (only when --detail)
const rqOpen = new Map();         // file -> open count
const stubRefs = new Map();       // stub target -> ref count
let totalLinks = 0;

// Index stub files before main pass
const stubPaths = new Set();
for (const f of files) {
  let text;
  try { text = fs.readFileSync(f, 'utf8'); } catch { continue; }
  if (text.includes('## Stub \u2014 needs full research')) {
    stubPaths.add(path.relative(VAULT, f).replace(/\.md$/, ''));
  }
}

// Undirected adjacency list + incoming-link counts for connectivity
const adj = new Map();
const inLinks = new Map();
for (const f of files) {
  const rel = path.relative(VAULT, f).replace(/\.md$/, '');
  adj.set(rel, new Set());
  inLinks.set(rel, 0);
}

for (const f of files) {
  let text;
  try {
    text = fs.readFileSync(f, 'utf8');
  } catch {
    continue;
  }

  // Links
  for (const m of text.matchAll(LINK_RE)) {
    totalLinks++;
    const target = m[1].split('|')[0].trim();
    if (!target) continue;
    const srcRel = path.relative(VAULT, f).replace(/\.md$/, '');
    if (target.includes('/')) {
      // Folder/Name form
      if (existingByPath.has(target) && stubPaths.has(target)) {
        stubRefs.set(target, (stubRefs.get(target) || 0) + 1);
        // valid link — track connectivity
        adj.get(srcRel)?.add(target); adj.get(target)?.add(srcRel);
        inLinks.set(target, (inLinks.get(target) || 0) + 1);
      } else if (existingByPath.has(target)) {
        // valid non-stub link — track connectivity
        adj.get(srcRel)?.add(target); adj.get(target)?.add(srcRel);
        inLinks.set(target, (inLinks.get(target) || 0) + 1);
      } else {
        missingRefs.set(target, (missingRefs.get(target) || 0) + 1);
        if (DETAIL && target === DETAIL) {
          if (!missingSources.has(target)) missingSources.set(target, new Set());
          missingSources.get(target).add(path.relative(VAULT, f));
        }
      }
    } else {
      // Bare Name — check across folders
      const matches = existingByBare.get(target);
      if (!matches || matches.length === 0) {
        missingRefs.set(target, (missingRefs.get(target) || 0) + 1);
        if (DETAIL && target === DETAIL) {
          if (!missingSources.has(target)) missingSources.set(target, new Set());
          missingSources.get(target).add(path.relative(VAULT, f));
        }
      } else if (matches.length === 1) {
        // unambiguous valid bare link — track connectivity
        const resolvedTarget = matches[0] + '/' + target;
        adj.get(srcRel)?.add(resolvedTarget); adj.get(resolvedTarget)?.add(srcRel);
        inLinks.set(resolvedTarget, (inLinks.get(resolvedTarget) || 0) + 1);
      } else {
        ambiguousRefs.set(target, (ambiguousRefs.get(target) || 0) + 1);
      }
    }
  }

  // Research Queue
  const rqStart = text.indexOf(RQ_HEADER);
  if (rqStart !== -1) {
    // Section ends at next ^## or EOF
    const after = text.slice(rqStart + RQ_HEADER.length);
    const nextHeader = after.search(/\n## /);
    const section = nextHeader === -1 ? after : after.slice(0, nextHeader);
    const matches = section.match(/^- \[ \]/gm);
    if (matches && matches.length) {
      rqOpen.set(path.relative(VAULT, f), matches.length);
    }
  }
}

// --detail short-circuit: list source files for one target
if (DETAIL) {
  const sources = missingSources.get(DETAIL);
  console.log(`Detail for missing target: ${DETAIL}`);
  console.log(`Reference count: ${missingRefs.get(DETAIL) || 0}`);
  if (!sources || sources.size === 0) {
    console.log('(no source files found — may already exist or be unreferenced)');
  } else {
    console.log(`Referenced from ${sources.size} file(s):`);
    for (const s of [...sources].sort()) console.log(`  ${s}`);
  }
  process.exit(0);
}

// Summary
const totalMissingRefs = [...missingRefs.values()].reduce((a, b) => a + b, 0);
const totalAmbigRefs = [...ambiguousRefs.values()].reduce((a, b) => a + b, 0);
const totalRqOpen = [...rqOpen.values()].reduce((a, b) => a + b, 0);

const topMissing = [...missingRefs.entries()].sort((a, b) => b[1] - a[1]).slice(0, TOP);
const topAmbig = [...ambiguousRefs.entries()].sort((a, b) => b[1] - a[1]).slice(0, TOP);
const topRq = [...rqOpen.entries()].sort((a, b) => b[1] - a[1]).slice(0, TOP);

console.log(`# Vault audit — ${VAULT}`);
console.log(`Files scanned:      ${files.length}`);
console.log(`Wiki-links seen:    ${totalLinks}`);
console.log('');
console.log(`## Missing/dead links`);
console.log(`Unique targets:     ${missingRefs.size}`);
console.log(`Total references:   ${totalMissingRefs}`);
if (topMissing.length) {
  console.log(`Top ${topMissing.length} most-referenced missing:`);
  for (const [name, count] of topMissing) console.log(`  ${String(count).padStart(5)}  ${name}`);
}
console.log('');
console.log(`## Ambiguous bare links`);
console.log(`Unique targets:     ${ambiguousRefs.size}`);
console.log(`Total references:   ${totalAmbigRefs}`);
if (topAmbig.length) {
  console.log(`Top ${topAmbig.length} most-referenced ambiguous:`);
  for (const [name, count] of topAmbig) console.log(`  ${String(count).padStart(5)}  ${name}`);
}
console.log('');
const totalStubRefs = [...stubRefs.values()].reduce((a, b) => a + b, 0);
const topStubs = [...stubRefs.entries()].sort((a, b) => b[1] - a[1]).slice(0, TOP);

console.log(`## Most-linked stubs`);
console.log(`Stubs in vault:     ${stubPaths.size}`);
console.log(`Total refs to stubs: ${totalStubRefs}`);
if (topStubs.length) {
  console.log(`Top ${topStubs.length} most-referenced stubs:`);
  for (const [name, count] of topStubs) console.log(`  ${String(count).padStart(5)}  ${name}`);
}
console.log('');
console.log(`## Open Research Queue items`);
console.log(`Files with open RQ: ${rqOpen.size}`);
console.log(`Total open items:   ${totalRqOpen}`);
if (topRq.length) {
  console.log(`Top ${topRq.length} busiest files:`);
  for (const [f, count] of topRq) console.log(`  ${String(count).padStart(5)}  ${f}`);
}
console.log('');

// -- Connectivity analysis --
const UTILITY_FILES = new Set(['_Index', '_Research_Queue_Index', '_web_handoff', '_web_queue_pending']);
const allContent = [...adj.keys()].filter(p => !UTILITY_FILES.has(p) && !p.startsWith('.'));

// BFS from _Index to find the main component
const startNode = '_Index';
const visited = new Set();
if (adj.has(startNode)) {
  const queue = [startNode];
  visited.add(startNode);
  while (queue.length) {
    const node = queue.shift();
    for (const neighbor of (adj.get(node) || [])) {
      if (!visited.has(neighbor)) { visited.add(neighbor); queue.push(neighbor); }
    }
  }
}

const unreachable = allContent.filter(p => !visited.has(p)).sort();
const isolated    = allContent.filter(p => adj.get(p).size === 0).sort();
const orphans     = allContent.filter(p => (inLinks.get(p) || 0) === 0 && adj.get(p).size > 0).sort();

console.log(`## Connectivity`);
console.log(`Total content pages:  ${allContent.length}`);
console.log(`Main component:       ${[...visited].filter(p => allContent.includes(p)).length} reachable from _Index`);
console.log(`Unreachable pages:    ${unreachable.length}`);
console.log(`Orphan pages:         ${orphans.length}  (outgoing links, nothing points here)`);
console.log(`Isolated pages:       ${isolated.length}  (no links in or out)`);
if (unreachable.length) {
  console.log(`First ${Math.min(TOP, unreachable.length)} unreachable:`);
  for (const p of unreachable.slice(0, TOP)) console.log(`         ${p}`);
}
if (isolated.length) {
  console.log(`Isolated:`);
  for (const p of isolated.slice(0, TOP)) console.log(`         ${p}`);
}
