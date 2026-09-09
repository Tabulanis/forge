"""Real-world news ingestion — the world-data firehose, structured.

Pulls free, timestamped RSS headlines into clean items she can reason over,
embed (text -> vectors), or tag (sentiment / entity / event). Read-only. This is
the raw material for turning world events into signals — the one frontier that
ISN'T derived from price. Honest caveat baked in: public headlines are usually
already priced in by the time you read them, so the edge (if any) is in
INTERPRETATION or NICHE feeds the crowd isn't watching, not the headline itself.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor

import httpx

FEEDS = {
    "cointelegraph": "https://cointelegraph.com/rss",
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "decrypt": "https://decrypt.co/feed",
    "bitcoinmagazine": "https://bitcoinmagazine.com/.rss/full/",
    "theblock": "https://www.theblock.co/rss.xml",
}
_TAG = re.compile(r"<[^>]+>")


def fetch(sources: list[str] | None = None, per_feed: int = 15) -> list[dict]:
    """Structured, timestamped items from the requested feeds (all if None)."""
    # 2026-09-08: these were fetched one after another, each with its own 15s
    # timeout, so five feeds meant up to 75 seconds of waiting for work that has
    # no order to it. They go together now; the slowest feed sets the wait.
    wanted = [(n, FEEDS[n]) for n in (sources or list(FEEDS)) if FEEDS.get(n)]

    def grab(item):
        name, url = item
        try:
            r = httpx.get(url, timeout=15, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
            root = ET.fromstring(r.text)
            return [{
                "source": name,
                "title": (it.findtext("title", "") or "").strip(),
                "date": (it.findtext("pubDate", "") or "").strip(),
                # 2026-09-08: summaries were cut to 220 chars. A headline plus a
                # sentence is not a story, and on a 131k window the saving was
                # a rounding error.
                "summary": _TAG.sub("", it.findtext("description", "") or "")[:1200].strip(),
            } for it in root.findall(".//item")[:per_feed]]
        except Exception:
            return []

    out = []
    with ThreadPoolExecutor(max_workers=min(8, len(wanted) or 1)) as pool:
        for got in pool.map(grab, wanted):
            out.extend(got)
    return out


def feed(sources: list[str] | None = None, limit: int = 25) -> str:
    """Recent headlines as clean text — paste-ready and ready to embed/tag."""
    items = fetch(sources)
    if not items:
        return "No news fetched (feeds down or blocked)."
    lines = [f"{len(items)} recent headlines from {len(set(i['source'] for i in items))} feeds:"]
    for it in items[:limit]:
        lines.append(f"[{it['date'][:22]} · {it['source']}] {it['title']}"
                     + (f"\n    {it['summary']}" if it['summary'] else ""))
    return "\n".join(lines)
