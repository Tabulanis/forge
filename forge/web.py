"""Web access for Merge: search (DuckDuckGo, no API key) and page-reading
(httpx + BeautifulSoup → clean text). Read-only; she can look things up and
read a page, nothing more."""
from __future__ import annotations

MAX_RESULTS = 8
MAX_PAGE_CHARS = 6000


def grep_text(text: str, pattern: str, ctx: int = 2, cap: int = 18000):
    """Return only the lines of `text` that match `pattern` (regex, case-
    insensitive; falls back to plain substring), each with a couple of lines
    of surrounding context and 1-based line numbers. This is the grep-not-dump
    core: it lets a big file or page be searched without pouring the whole
    thing into the model's context. Returns (rendered_text, match_count)."""
    import re
    lines = text.splitlines()
    try:
        rx = re.compile(pattern, re.I); test = rx.search
    except re.error:
        pat = (pattern or "").lower(); test = lambda s: pat in s.lower()
    hits = [i for i, l in enumerate(lines) if test(l)]
    if not hits:
        return "", 0
    ranges = []
    for i in hits:
        lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
        if ranges and lo <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    out, used = [], 0
    for lo, hi in ranges:
        if out:
            out.append("        ⋯"); used += 10
        for i in range(lo, hi):
            entry = f"{i + 1:6d}\t{lines[i]}"
            if used + len(entry) > cap:
                out.append(f"... (more matches — capped at {cap // 1000}KB; "
                           f"narrow the pattern)")
                return "\n".join(out), len(hits)
            out.append(entry); used += len(entry) + 1
    return "\n".join(out), len(hits)


def web_search(query: str, max_results: int = 6) -> str:
    """Search the web and return titles, links, and snippets."""
    query = (query or "").strip()
    if not query:
        return "Error: give a search query."
    n = max(1, min(int(max_results), MAX_RESULTS))
    try:
        from ddgs import DDGS
    except Exception:
        return "Error: the web-search library isn't installed on this machine."
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=n))
    except Exception as e:
        return f"Error searching the web: {type(e).__name__}: {e}"
    if not results:
        return f"No web results for {query!r}."
    lines = [f"Web results for {query!r}:"]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = r.get("href") or r.get("url") or r.get("link") or ""
        body = (r.get("body") or r.get("snippet") or "").strip()
        lines.append(f"\n{i}. {title}\n   {url}\n   {body[:280]}")
    lines.append("\n(Use fetch_url on a link to read the full page.)")
    return "\n".join(lines)


def fetch_url(url: str, contains: str = "") -> str:
    """Fetch a web page and return its readable text (scripts/nav stripped).
    Pass `contains` to get back ONLY the lines matching that pattern (with a
    little context) instead of the whole page — grep-not-dump for the web."""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: give a full http(s):// URL (search first with web_search)."
    import httpx
    try:
        r = httpx.get(url, timeout=20.0, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; Merge/1.0)"})
        r.raise_for_status()
    except Exception as e:
        return f"Error fetching the page: {type(e).__name__}: {e}"
    ctype = r.headers.get("content-type", "").lower()
    if "html" not in ctype and "text" not in ctype:
        return f"Fetched {url} ({ctype or 'unknown type'}) — not a readable text/HTML page."
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "aside", "noscript", "form"]):
        tag.decompose()
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    if contains:
        body, n = grep_text(soup.get_text("\n"), contains)
        if not n:
            return f"{title}\n{url}\n\nFetched, but no lines match {contains!r}."
        return f"{title}\n{url}\n\n{n} match(es) for {contains!r}:\n{body}"
    text = " ".join(soup.get_text(" ").split())
    if not text:
        return f"Fetched {url} but found no readable text on it."
    out = f"{title}\n{url}\n\n{text[:MAX_PAGE_CHARS]}"
    if len(text) > MAX_PAGE_CHARS:
        out += f"\n\n... (page continues — {len(text) - MAX_PAGE_CHARS} more characters)"
    return out
