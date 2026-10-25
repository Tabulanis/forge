"""
The calculator: Merge's source of truth for anything deterministic. Certified answers, never opinions.

PROPOSAL for ~/forge/forge/ — Merge never edits forge; a human installs this.
Drop this file in as forge/alien_math.py and add the Tool block from README.md to tools.py.

One HTTP call to the Alien Math engine's door (GET /ask on its status port). Keys behind the door:
  law    equational laws of one operation ◇ and implications between them (records, ETP table, provers)
  calc   exact computation: arithmetic, algebra, number facts, calculus, dates, units (SymPy, exact)
  logic  first-order logic, premises |- conclusion (Prover9 proves, Mace4 refutes)
The engine answers from its own records first (a repeat question is instant), then catalogues, then exact
computation or the checkers. Every answer carries its certificate. Plain HTTP + JSON, so the same door
serves any model that is dropped in.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

ALIEN_MATH_URL = "http://127.0.0.1:8870/ask"
ASK_TIMEOUT = 240   # s; a fresh checker run can take a minute, a checked English read-back longer

ALIEN_MATH_DESCRIPTION = (
    "Your calculator and source of truth. ALWAYS use this instead of working out anything deterministic "
    "in your head: arithmetic, algebra, number facts, calculus, dates, unit conversions, whether a logical "
    "conclusion follows from premises, and anything about equational laws (magmas). The engine computes "
    "exactly or runs a prover, and every answer carries a CERTIFICATE (the exact computation, a proof length, "
    "a countermodel, or the record it was read from); your own reasoning on these questions is not accepted. "
    "Forms: calc - '2^100', '17/51 + 1/3', 'is 1000003 prime', 'factor 360', 'gcd(84, 36)', '100 mod 7', "
    "'solve x^2 - 5x + 6 = 0', 'is (x+1)^2 = x^2 + 2x + 1', 'simplify ...', 'expand ...', 'derivative of x^3 sin(x)', "
    "'integrate x^2 from 0 to 1', 'limit sin(x)/x as x -> 0', 'day of week 1969-07-20', 'days between D1 and D2', "
    "'2026-10-24 + 90 days', 'convert 5 miles to km', 'what is 15% of 80', 'sqrt(2) to 30 digits'. "
    "logic - Prover9 syntax, premises then |- then the conclusion: 'all x (man(x) -> mortal(x)). man(s). |- mortal(s).' "
    "law - 'x ◇ (y ◇ x) = y' or an implication 'A -> B' (variables x y z w u v, one operation ◇), or plain English. "
    "The key is picked from the question; set domain to force one (calc, logic, law). Set english=true for a "
    "checked plain-English read-back of a law. Every question you ask is recorded and grows the engine's library."
)


import re

# ---- the structural gate: a deterministic claim in a reply with no calculator call behind it ---------------
# Deterministic, no model: a few shapes of claim that have exactly one right answer and that she must not
# produce from her head. Deliberately narrow (false bounces cost a turn each); widen as the ledger shows misses.
_CLAIM_SHAPES = [
    (r"(?<![\w.])(?:\d[\d,]*(?:\.\d+)?|\d+/\d+)\s*(?:[-+*/^×÷]|\*\*)\s*(?:[\d(][\d,.()\s+\-*/^×÷]*)=\s*-?\d[\d,]*(?:\.\d+)?", "an evaluated arithmetic expression"),
    (r"(?<![\w.])\d[\d,]*[⁰¹²³⁴⁵⁶⁷⁸⁹]+\s*(?:=|is|equals)\s*-?\d", "an evaluated power (superscript)"),
    (r"\b(?:\d{3,}|\d+\s*\^\s*\d+|\d+!)\s+(?:is|is not|isn't)\s+(?:a\s+)?prime\b|\bprime\s+(?:after|before)\s+[\d,]+\s+is\s+[\d,]+", "a primality claim"),
    (r"\b(?:argument|inference|syllogism|reasoning)\s+is\s+(?:valid|invalid)\b|(?:is|are)\s+(?:a\s+)?(?:valid|invalid)\s+(?:argument|inference|syllogism)\b|\b(?:does|doesn't|does not)\s+(?:logically\s+)?follow\b", "an entailment claim"),
    (r"\b\d{4}-\d{2}-\d{2}\s+(?:was|is|falls on)\s+(?:a\s+)?(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b", "a day-of-week claim"),
    (r"\b\d[\d,]*(?:\.\d+)?\s*(?:miles?|km|kilomet(?:er|re)s?|feet|ft|met(?:er|re)s?|pounds?|lbs?|kg|kilograms?|°?[CF])\s*(?:=|is|equals|≈)\s*\d", "a unit conversion"),
    (r"\b(?:gcd|lcm|sqrt|log|ln|sin|cos|tan)\s*\([^)]*\)\s*(?:=|is|equals|≈)\s*-?\d", "an evaluated function"),
    (r"\b(?:derivative|integral|antiderivative|limit)\b[^.\n]{0,80}\b(?:is|=|equals)\s*[-\w(]", "a calculus result"),
    (r"\b(?:solutions?|roots?)\s+(?:is|are)\s*:?\s*(?:x\s*=\s*)?-?\d", "a solved equation"),
]


def deterministic_claims(text: str) -> list[str]:
    """Which deterministic claims does this reply make? Empty list = nothing to certify."""
    found = []
    for pat, what in _CLAIM_SHAPES:
        if re.search(pat, text, re.I):
            found.append(what)
    return found


def run_alien_math(question: str, english: bool = False, domain: str | None = None) -> str:
    params = {"q": question, "english": "1" if english else "0"}
    if domain:
        params["domain"] = domain
    q = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{ALIEN_MATH_URL}?{q}", timeout=ASK_TIMEOUT) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:                      # engine down, port busy, timeout
        return f"analytical engine not reachable ({type(e).__name__}: {e}); start it with ~/aidojo/current/alien-math/run.sh start"
    if d.get("error"):
        return "ENGINE: " + d["error"] + (f"\nhint: {d['hint']}" if d.get("hint") else "") \
               + (f"\nkeys: {d['keys']}" if d.get("keys") else "")
    lines = [f"[{d.get('domain', '?')}] {d.get('kind', '?')}: {d.get('statement', question)}",
             f"verdict: {d.get('verdict')}  (source: {d.get('source')}, {d.get('ms')} ms)"]
    cert = d.get("certificate")
    if isinstance(cert, dict):
        tb = cert.get("table")
        lines.append("certificate: " + cert.get("checker", "") + (f", {cert['steps']} proof steps" if cert.get("steps") else "")
                     + (f", countermodel of size {len(tb)}: {tb}" if tb else ""))
    elif cert:
        lines.append(f"certificate: {str(cert)[:600]}")
    if d.get("table") and not isinstance(cert, dict):
        lines.append(f"world ({len(d['table'])} elements): {d['table']}")
    for k in ("decimal", "exact", "solutions", "antiderivative", "factorization", "witness", "etp_id", "etp_class", "etp", "world", "why"):
        if d.get(k):
            lines.append(f"{k}: {d[k]}")
    if d.get("translation"):
        t = d["translation"]
        lines.append(f"translated as: {t.get('law')}  (agreement {t.get('agreement')}; {t.get('grade')})")
    if d.get("english"):
        lines.append(f"in English: {d['english']}  [{'checked' if d.get('english_checked') else 'NOT checked'}: {d.get('english_grade')}]")
    return "\n".join(lines)
