"""
Legal citation verification — the hallucination guard for case law.

The thing that gets real lawyers sanctioned is citing cases that do not
exist. Language models invent them constantly and sound confident about it.
This module exists so that no case name ever leaves our output without first
being checked against a real database.

Backend: CourtListener REST API v4 (free, no key). US courts only.
The user has verified the endpoint behavior; the code below follows their
verified spec exactly:

  GET https://www.courtlistener.com/api/rest/v4/search/?q=<query>&type=o
  - MUST send a User-Agent header or the request 000s.
  - A real case returns {"count": N>0, "results":[{caseName, citation[],
    court, dateFiled, absolute_url, docketNumber}, ...]}.
  - An invented case ("Thompson v. Zephyr Dynamics") returns count: 0.
    That zero is the hallucination detector.

Rules this module enforces by construction:
  - NEVER guess. A network failure says "the check could not run" — it does
    not fall back to what the model thinks the answer is.
  - count: 0 means NOT FOUND and must not be cited.
  - This is legal INFORMATION, not advice. Jurisdiction matters; law
    changes, so anything time-sensitive gets checked fresh.
  - CourtListener covers US courts only. Foreign law = say so plainly, give
    general principles, and defer to a local source. Never fake confidence.
"""

from __future__ import annotations

import re

import httpx

_API = "https://www.courtlistener.com/api/rest/v4/search/"
_UA = "Mozilla/5.0 (research)"
_TIMEOUT = 20  # seconds; a hung connection means "could not run", not a guess
_URL_PREFIX = "https://www.courtlistener.com"

DISCLAIMER = (
    "Legal information, not legal advice. Nothing here creates an "
    "attorney-client relationship. Jurisdiction matters, and law changes — "
    "anything time-sensitive must be checked fresh against current law. "
    "This tool covers US courts (CourtListener) only; for foreign law, "
    "treat anything said as general principles and verify with a local "
    "source. Verify every case before citing it; never cite one that has "
    "not been verified."
)


def _search(query: str) -> dict:
    """One search round-trip. Returns {'ok': bool, 'data': dict|None, 'error': str}."""
    params = {"q": query, "type": "o"}
    try:
        resp = httpx.get(_API, params=params, headers={"User-Agent": _UA},
                         timeout=_TIMEOUT)
    except httpx.HTTPError as exc:
        return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}"}
    if resp.status_code != 200:
        return {"ok": False, "data": None,
                "error": f"HTTP {resp.status_code} from CourtListener"}
    try:
        return {"ok": True, "data": resp.json(), "error": ""}
    except ValueError as exc:
        return {"ok": False, "data": None, "error": f"unparseable response: {exc}"}


def _fmt_hit(i: int, r: dict) -> str:
    """One formatted result line."""
    name = r.get("caseName") or "(no name given)"
    cites = r.get("citation") or []
    cite = "; ".join(c for c in cites if c) if cites else "(no citation)"
    court = r.get("court") or "(court not stated)"
    date = r.get("dateFiled") or "(date not stated)"
    docket = r.get("docketNumber") or ""
    url = r.get("absolute_url") or ""
    if url and not url.startswith("http"):
        url = _URL_PREFIX + url
    line = f"{i}. {name} — {cite} — {court}, {date}"
    if docket:
        line += f" [docket {docket}]"
    if url:
        line += f"\n   {url}"
    return line


def _fmt_not_found(query: str) -> str:
    return (
        f"NOT FOUND — no case matching \"{query}\" came back from CourtListener "
        "(count: 0).\n"
        "This case must not be cited. It may not exist — invented case names "
        "return exactly this result, which is how the check catches "
        "hallucinated citations. Do not cite it, do not paraphrase it into "
        "existence, and do not soften this into 'a case like...'. If you "
        "believe a real case was intended, find its actual name and verify "
        "that instead.\n"
        + DISCLAIMER
    )


def _norm(s: str) -> str:
    """Normalize a case name for comparison.

    Lowercase, collapse all whitespace runs to a single space, strip
    punctuation (periods, commas, hyphens, apostrophes, quotes), and map
    every form of "versus" ("versus", "vs.", "vs", "v.") to the bare word
    "v". So "Miranda v. Arizona" and "Miranda v Arizona" normalize to the
    same string. Stdlib only.
    """
    s = s.lower()
    s = s.replace("versus", " v ")
    s = s.replace("vs.", " v ").replace("vs", " v ")
    s = s.replace("v.", " v ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _is_name_match(query: str, rec: dict) -> bool:
    """Does this record actually match the case name we asked for?

    CourtListener's q= field is fuzzy — even a wholly invented case name
    returns partial matches ("Thompson v. Zephyr Dynamics" comes back with
    Duckworth v. Thompson). So a nonzero count is NOT proof the case
    exists. A record only counts as a hit when the case name we asked for
    appears inside the record's caseName.
    """
    q = _norm(query)
    name = _norm(rec.get("caseName") or "")
    return bool(q) and (q in name or name in q)


def _first_exact(query: str, results: list) -> dict | None:
    """The first record that is genuinely the case named in the query."""
    for rec in results:
        if _is_name_match(query, rec):
            return rec
    return None


def verify_case(query: str) -> str:
    """Verify that a case exists before it is cited.

    VERIFIED   -> the real caseName / citation / court / date / url.
    NOT FOUND  -> a blunt line: must not be cited, may not exist.
    CHECK FAILED -> the network died; the check could not run. Never a guess.

    A nonzero fuzzy-match count from unrelated cases does NOT count as
    verification: the record's caseName must actually contain the case we
    asked for, otherwise the answer is NOT FOUND.
    """
    if not isinstance(query, str) or not query.strip():
        return ("CHECK FAILED — empty query. Give a case name or citation to "
                "verify; nothing was checked, and nothing is verified.")
    query = query.strip()
    res = _search(query)
    if not res["ok"]:
        return (
            "CHECK FAILED — the network check could not run "
            f"({res['error']}).\n"
            "No case has been verified here. Do not cite any case from memory "
            "in place of this check — either retry when connectivity is back "
            "or treat the citation as unverified.\n"
            + DISCLAIMER
        )
    data = res["data"]
    count = data.get("count", 0)
    results = data.get("results") or []
    exact = _first_exact(query, results)
    if exact is None:
        note = (
            f"Note: the search returned {count} record(s) that only partially "
            "match (e.g. sharing one party name) — none of them is the case "
            "you asked for."
            if count > 0
            else "No matching records at all."
        )
        return (
            f'NOT FOUND — no case named "{query}" exists in CourtListener. '
            f"{note} It must not be cited; it may not exist at all. Do not "
            "cite it, and do not soften this into a plausible-sounding "
            "citation from memory.\n"
            + DISCLAIMER
        )
    lines = [
        f'VERIFIED — "{query}" resolves to a real case in CourtListener '
        f"({count} matching record{'s' if count != 1 else ''}).",
        _fmt_hit(1, exact),
        "",
    ]
    if count > 1:
        lines.append(
            f"Note: {count - 1} further matching record(s) exist — if you "
            "meant a different court or year, say so and re-verify that one "
            "before citing it."
        )
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)

def find_cases(topic: str, limit: int = 5) -> str:
    """Search by topic and list real hits with citations and urls."""
    if not isinstance(topic, str) or not topic.strip():
        return ("No results — empty topic. Give a topic or subject to search "
                "for; nothing was searched.")
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 5
    res = _search(topic.strip())
    if not res["ok"]:
        return (
            "SEARCH FAILED — the network check could not run "
            f"({res['error']}).\n"
            "Nothing was returned and nothing is verified. Retry when "
            "connectivity is back.\n"
            + DISCLAIMER
        )
    data = res["data"]
    count = data.get("count", 0)
    results = (data.get("results") or [])[:limit]
    if not results:
        return (
            f"NO RESULTS — no cases matching \"{topic}\" (count: 0).\n"
            "Nothing here is a verified case. Do not fill the gap with a "
            "plausible-sounding citation.\n"
            + DISCLAIMER
        )
    lines = [
        f"Found {count} matching record{'s' if count != 1 else ''}; showing "
        f"the first {len(results)}. Every one below is a real, verified hit — "
        "cite only what you actually need, and re-verify before relying on it."
        "",
    ]
    lines.extend(_fmt_hit(i, r) for i, r in enumerate(results, 1))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def legal_notice() -> str:
    """The standing disclaimer text."""
    return DISCLAIMER
