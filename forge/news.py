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

# Her own default sources: world, science, technology. Probed 2026-09-08 from
# this machine — every one below returned items; AP and Nature did not and were
# left out rather than shipped broken.
FEEDS = {
    "bbc-world":     "http://feeds.bbci.co.uk/news/world/rss.xml",
    "guardian":      "https://www.theguardian.com/world/rss",
    "npr":           "https://feeds.npr.org/1001/rss.xml",
    "ars-technica":  "http://feeds.arstechnica.com/arstechnica/index",
    "hacker-news":   "https://hnrss.org/frontpage",
    "science-daily": "https://www.sciencedaily.com/rss/all.xml",
    "phys-org":      "https://phys.org/rss-feed/",
}

# A PROJECT can add its own. Reading the news is a general capability and hers
# everywhere; WHICH sources matter is the project's business. MoneyLab ships a
# crypto list this way, so opening MoneyLab quietly gives her its feeds on top
# of these and nothing else has to change.
PROJECT_FEEDS_FILE = "merge-tools/feeds.json"


def feeds_for(ws_root=None) -> dict:
    """Her sources, plus whatever this project adds."""
    out = dict(FEEDS)
    if not ws_root:
        return out
    import json
    from pathlib import Path
    f = Path(ws_root) / PROJECT_FEEDS_FILE
    try:
        if f.is_file():
            extra = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(extra, dict):
                out.update({str(k): str(v) for k, v in extra.items()})
    except Exception:
        pass          # a project's bad feed file must never break her news
    return out
_TAG = re.compile(r"<[^>]+>")


def fetch(sources: list[str] | None = None, per_feed: int = 15, ws_root=None) -> list[dict]:
    """Structured, timestamped items from the requested feeds (all if None)."""
    # 2026-09-08: these were fetched one after another, each with its own 15s
    # timeout, so five feeds meant up to 75 seconds of waiting for work that has
    # no order to it. They go together now; the slowest feed sets the wait.
    all_feeds = feeds_for(ws_root)
    wanted = [(n, all_feeds[n]) for n in (sources or list(all_feeds)) if all_feeds.get(n)]

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


def feed(sources: list[str] | None = None, limit: int = 25, ws_root=None) -> str:
    """Recent headlines as clean text — paste-ready and ready to embed/tag."""
    items = fetch(sources, ws_root=ws_root)
    if not items:
        return "No news fetched (feeds down or blocked)."
    lines = [f"{len(items)} recent headlines from {len(set(i['source'] for i in items))} feeds:"]
    for it in items[:limit]:
        lines.append(f"[{it['date'][:22]} · {it['source']}] {it['title']}"
                     + (f"\n    {it['summary']}" if it['summary'] else ""))
    return "\n".join(lines)
