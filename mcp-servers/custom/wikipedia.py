"""Offline Wikipedia access for the MCP server.

Wraps a Kiwix ZIM (the text-only `wikipedia_en_all_nopic_*` dump) via libzim and
exposes full-text search plus clean-text article retrieval. The ZIM ships an
embedded Xapian full-text index, so search needs no external service — no
kiwix-serve process, just this in-process reader.

The archive is opened lazily on first use so the MCP server still boots (and its
Discogs tools keep working) while the ~49GB ZIM is still downloading. Every
public function tolerates a missing/unopenable ZIM and returns a structured
`{"error": ...}` instead of raising, so the agent gets a clear message rather
than a tool crash.
"""

import os
import re
import threading
from typing import Any, Optional

ZIM_PATH = os.environ.get(
    "WIKIPEDIA_ZIM_PATH", "/data/wikipedia/wikipedia_en_all_nopic_2026-06.zim"
)

_archive = None
_have_fulltext = False
_open_error: Optional[str] = None
_lock = threading.Lock()


def available() -> bool:
    """True if the ZIM file is present on disk (does not force it open)."""
    return os.path.exists(ZIM_PATH)


def _get():
    """Return (archive, error). Opens lazily and caches; thread-safe."""
    global _archive, _have_fulltext, _open_error
    if _archive is not None:
        return _archive, None
    if _open_error is not None:
        return None, _open_error
    with _lock:
        if _archive is not None:
            return _archive, None
        if _open_error is not None:
            return None, _open_error
        if not os.path.exists(ZIM_PATH):
            return None, (
                f"Wikipedia ZIM not found at {ZIM_PATH} — the offline dump may "
                "still be downloading. Try again later."
            )
        try:
            from libzim.reader import Archive

            arch = Archive(ZIM_PATH)
        except Exception as e:  # noqa: BLE001 — surface any open failure to the agent
            _open_error = f"failed to open Wikipedia ZIM: {e!r}"
            return None, _open_error
        _archive = arch
        try:
            _have_fulltext = bool(arch.has_fulltext_index)
        except Exception:  # noqa: BLE001
            _have_fulltext = False
        return _archive, None


# ---------------------------------------------------------------------------
# HTML → clean text
# ---------------------------------------------------------------------------

# Chrome that adds no research value; dropped before extracting text. Infobox
# tables are deliberately KEPT — they carry dates, members, labels, etc.
_DROP_SELECTORS = (
    ".mw-editsection, sup.reference, .reference, .noprint, .navbox, "
    ".vertical-navbox, .metadata, .mw-empty-elt, style, script, "
    "#mw-navigation, .catlinks, .printfooter, .mw-jump-link, .thumbcaption"
)


def _clean_html(html: str, max_chars: Optional[int]) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for node in soup.select(_DROP_SELECTORS):
        node.decompose()
    body = soup.find("body") or soup
    text = body.get_text("\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if max_chars and len(text) > max_chars:
        cut = text[:max_chars]
        # avoid slicing mid-line
        nl = cut.rfind("\n")
        if nl > max_chars * 0.6:
            cut = cut[:nl]
        text = cut.rstrip() + "\n\n…[truncated]"
    return text


# Sections that never answer a research question. Discography/Awards are kept
# deliberately — they carry dates and titles.
_BOILERPLATE = re.compile(
    r"^(references|external links|see also|notes|citations|sources|"
    r"further reading|bibliography|footnotes|general references)\b",
    re.I,
)
_SEC = "\x00SEC\x00"
_STOP = set(
    "what is are the a an of on in for do does did any which who whom how and or to with by "
    "across when where why was were has have had this that his her their its from at about".split()
)


def _split_sections(html: str) -> list[tuple[str, str]]:
    """[(heading, text)] with the lead under heading ''.

    `get_text` flattens headings into indistinguishable lines, so they are
    replaced with a sentinel first — otherwise there is no way to tell an
    article's structure from its prose, and truncation can only chop the front.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for node in soup.select(_DROP_SELECTORS):
        node.decompose()
    for h in soup.find_all(["h2", "h3", "h4"]):
        h.replace_with(soup.new_string(f"\n{_SEC}{h.get_text(' ', strip=True)}\n"))
    body = soup.find("body") or soup
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", body.get_text("\n"))).strip()

    out: list[tuple[str, str]] = []
    parts = text.split(_SEC)
    out.append(("", parts[0].strip()))
    for p in parts[1:]:
        head, _, rest = p.partition("\n")
        out.append((head.strip(), rest.strip()))
    return [(h, t) for h, t in out if t and not _BOILERPLATE.match(h)]


def _relevant(html: str, query: str, max_chars: int) -> tuple[str, list[str], list[str]]:
    """Lead plus the sections that match `query`, within a char budget.

    A question usually needs one section of a long article; the mean article in
    this snapshot is ~9.7k tokens and U2 is 40k. Returning the whole thing and
    truncating from the front pays for the entire prefix to reach a fact that
    may sit two thirds down.
    """
    sections = _split_sections(html)
    if not sections:
        return "", [], []
    terms = {w for w in re.findall(r"[a-z0-9']+", query.lower()) if w not in _STOP and len(w) > 2}

    lead_h, lead_t = sections[0] if sections[0][0] == "" else ("", "")
    rest = sections[1:] if sections[0][0] == "" else sections

    def score(h: str, t: str) -> float:
        ht = {w for w in re.findall(r"[a-z0-9']+", h.lower()) if w not in _STOP}
        body = t.lower()
        # A heading hit is worth much more than a body mention.
        return 3.0 * len(terms & ht) + sum(1 for w in terms if w in body) / max(1, len(terms))

    ranked = sorted(rest, key=lambda s: -score(*s))
    chunks: list[str] = []
    included: list[str] = []
    budget = max_chars
    if lead_t:
        chunks.append(lead_t[:budget])
        budget -= len(chunks[0])
        included.append("(lead)")
    for h, t in ranked:
        if budget < 400:
            break
        if score(h, t) <= 0:
            continue
        body = t if len(t) <= budget else t[:budget].rsplit("\n", 1)[0] + " …"
        chunks.append(f"== {h} ==\n{body}")
        budget -= len(body) + len(h) + 6
        included.append(h)
    omitted = [h for h, _ in rest if h not in included]
    return "\n\n".join(chunks), included, omitted


def _entry_html(entry) -> str:
    item = entry.get_item()
    return bytes(item.content).decode("utf-8", "replace")


def _snippet(entry, length: int = 240) -> str:
    try:
        text = _clean_html(_entry_html(entry), None)
    except Exception:  # noqa: BLE001
        return ""
    text = text.replace("\n", " ").strip()
    return text[:length] + ("…" if len(text) > length else "")


def _wiki_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")


# ---------------------------------------------------------------------------
# Public API (called from server.py tools)
# ---------------------------------------------------------------------------


def search(pattern: str, limit: int = 10, snippets: bool = True) -> dict:
    """Full-text search over article bodies. Returns ranked title/snippet hits."""
    archive, err = _get()
    if err:
        return {"error": err}
    limit = max(1, min(int(limit), 50))
    paths: list[str] = []
    est = 0
    try:
        from libzim.search import Query, Searcher

        with _lock:
            searcher = Searcher(archive)
            search_obj = searcher.search(Query().set_query(pattern))
            est = int(search_obj.getEstimatedMatches())
            paths = list(search_obj.getResults(0, limit))
    except Exception as e:  # noqa: BLE001 — fall back to title suggestions below
        sugg = _suggest(archive, pattern, limit)
        if sugg is not None:
            return {
                "query": pattern,
                "mode": "title-suggestion",
                "note": f"full-text search unavailable ({e!r}); showing title matches",
                "results": sugg,
            }
        return {"error": f"search failed: {e!r}", "query": pattern}

    results = []
    for path in paths:
        try:
            entry = archive.get_entry_by_path(path)
        except Exception:  # noqa: BLE001
            continue
        row = {"title": entry.title, "path": path}
        if snippets:
            row["snippet"] = _snippet(entry)
        results.append(row)
    return {"query": pattern, "estimated_matches": est, "results": results}


def _suggest(archive, pattern: str, limit: int) -> Optional[list[dict]]:
    try:
        from libzim.suggestion import SuggestionSearcher

        with _lock:
            ss = SuggestionSearcher(archive)
            s = ss.suggest(pattern)
            paths = list(s.getResults(0, limit))
        out = []
        for path in paths:
            try:
                entry = archive.get_entry_by_path(path)
            except Exception:  # noqa: BLE001
                continue
            out.append({"title": entry.title, "path": path})
        return out
    except Exception:  # noqa: BLE001
        return None


def get_article(title: str, max_chars: int = 40000, query: Optional[str] = None) -> dict:
    """Fetch one article as clean text. Resolves redirects. On a miss, returns
    `{found: False, suggestions: [...]}` from a title/full-text search.

    With `query`, returns the lead plus only the sections that match it, and
    reports which sections were included and which were left out."""
    archive, err = _get()
    if err:
        return {"error": err}
    max_chars = max(500, min(int(max_chars), 200000))

    entry = None
    try:
        if archive.has_entry_by_title(title):
            entry = archive.get_entry_by_title(title)
    except Exception:  # noqa: BLE001
        entry = None
    if entry is None:
        # Some callers pass a raw entry path (e.g. from search results).
        try:
            if archive.has_entry_by_path(title):
                entry = archive.get_entry_by_path(title)
        except Exception:  # noqa: BLE001
            entry = None
    if entry is None:
        s = search(title, 5, snippets=False)
        suggestions = [r["title"] for r in s.get("results", [])]
        return {"found": False, "query": title, "suggestions": suggestions}

    try:
        if entry.is_redirect:
            entry = entry.get_redirect_entry()
    except Exception:  # noqa: BLE001
        pass

    try:
        html = _entry_html(entry)
    except Exception as e:  # noqa: BLE001
        return {"error": f"failed to read article: {e!r}", "title": entry.title}

    extra: dict = {}
    if query:
        text, included, omitted = _relevant(html, query, max_chars)
        if not text:  # no section scored; fall back rather than return nothing
            text = _clean_html(html, max_chars)
        else:
            extra = {"sections_included": included, "sections_omitted": omitted}
    else:
        text = _clean_html(html, max_chars)
    return {
        **extra,
        "found": True,
        "title": entry.title,
        "path": entry.path,
        "url": _wiki_url(entry.title),
        "chars": len(text),
        "text": text,
    }
