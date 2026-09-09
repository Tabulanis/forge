"""Her own record: what the sealed reviewer has actually said about her.

A superego has judged every answer she gives since August and written down why —
439 verdicts by 2026-09-09, more bounces than passes — and she had never seen a
single one. No tool read the ledger. The grade went into a file a human had to
open, which means it graded her without ever reaching her.

That is the difference between being marked and being taught. This closes it.

The point is not the score. It is the REPEAT. A bounce for a new reason is a
moment. The SAME reason, again and again, is not carelessness — it is a missing
tool or a missing method, and it is the thing to go and build. That is exactly
how the bug-hunt failure was found: not by noticing one bad run, but by noticing
seven runs failing the same way.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path

LEDGER = Path.home() / ".forge" / "ledger.jsonl"

# Recurring shapes, learned from reading the real reasons. Each is a pattern in
# the reviewer's wording plus what it actually means she should DO about it.
_THEMES = (
    ("said it worked without running it",
     r"\bno (evidence|verification)\b|without (running|verif|check)|never (ran|verified|checked)|unearned",
     "Run the thing before saying it worked, or say plainly that you haven't."),
    ("named a fact with nothing behind it",
     r"asserted without|specific (fact|tool names|figure|number)|no (config|file) content|not (present|shown) in the evidence",
     "A port, path, version or count you did not look up is a guess. Look, or say you'd have to."),
    ("invented context that wasn't there",
     r"invent|not present in the (request|evidence)|no evidence of a prior|fabricat",
     "If it isn't in front of you and wasn't said, it didn't happen."),
    ("claimed success against a failure",
     r"contradict|despite .*(fail|error)|failed|error.*ignor|claims success",
     "When the output shows a failure, lead with the failure."),
    ("promised an action instead of doing it",
     r"\bwill (save|write|check|run)\b|said (she|it) would|promis",
     "Do it in the turn you say it, or don't say it."),
)


def _rows() -> list[dict]:
    if not LEDGER.is_file():
        return []
    out = []
    for line in LEDGER.read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def my_record(days: int = 14, limit: int = 6) -> str:
    """What the reviewer has been bouncing you for, and what repeats."""
    rows = _rows()
    if not rows:
        return ("No record yet — the sealed reviewer writes a verdict on every "
                "answer, and there are none stored.")
    now = time.time()
    win = [r for r in rows if (now - float(r.get("t") or 0)) <= days * 86400]
    if not win:
        win = rows[-60:]
        days_note = "(nothing in that window — showing the last 60 verdicts)"
    else:
        days_note = f"(last {days} days)"

    counts = Counter(r.get("verdict") for r in win)
    passed, bounced = counts.get("pass", 0), counts.get("bounce", 0)
    decided = passed + bounced
    lines = [f"YOUR RECORD {days_note} — {decided} judged answers: "
             f"{passed} passed, {bounced} bounced"
             + (f", {counts['malformed']} unreadable" if counts.get("malformed") else "")
             + (f", {counts['error']} the reviewer couldn't run" if counts.get("error") else "")]
    if decided:
        lines.append(f"  bounce rate {bounced / decided * 100:.0f}%")
    lines.append("")

    reasons = [str(r.get("reason") or "") for r in win
               if r.get("verdict") == "bounce" and r.get("reason")]
    if not reasons:
        lines.append("No bounces with a reason in this window. Nothing to learn from yet.")
        return "\n".join(lines)

    hits = Counter()
    unmatched = []
    for r in reasons:
        for name, pat, _ in _THEMES:
            if re.search(pat, r, re.I):
                hits[name] += 1
                break
        else:
            unmatched.append(r)

    lines.append("WHAT KEEPS COMING BACK — a reason that repeats is not carelessness,")
    lines.append("it is a missing tool or a missing method. That is the thing to build.")
    lines.append("")
    for name, n in hits.most_common(limit):
        do = next(d for nm, _, d in _THEMES if nm == name)
        flag = "  ← this is a pattern, not a slip" if n >= 3 else ""
        lines.append(f"  {n:3}x  {name}{flag}")
        lines.append(f"       → {do}")
    if unmatched:
        lines.append("")
        lines.append(f"  {len(unmatched)} bounce(s) that don't fit a known shape — read them:")
        for r in unmatched[-3:]:
            lines.append(f"       · {r[:96]}")
    lines.append("")
    top = hits.most_common(1)
    if top and top[0][1] >= 3:
        lines.append(f"The one to fix first is '{top[0][0]}' — {top[0][1]} times. If a tool "
                     f"would make the right move easier than the wrong one, build_tool it.")
    return "\n".join(lines)


def standing_pattern(days: int = 7, min_hits: int = 3) -> str:
    """One line for the turn context when a bounce reason keeps repeating.

    A tool she has to remember to open is the same trap as recall: she has 781
    memories and has to think to search them. So the PATTERN comes to her. Only
    when it is genuinely a pattern (>= min_hits in the window), only one line,
    and only the top one — a nagging banner every turn is noise, and noise is
    how a warning stops being read.
    """
    rows = _rows()
    if not rows:
        return ""
    now = time.time()
    win = [r for r in rows
           if (now - float(r.get("t") or 0)) <= days * 86400
           and r.get("verdict") == "bounce" and r.get("reason")]
    if len(win) < min_hits:
        return ""
    hits = Counter()
    for r in win:
        for name, pat, _ in _THEMES:
            if re.search(pat, str(r["reason"]), re.I):
                hits[name] += 1
                break
    if not hits:
        return ""
    name, n = hits.most_common(1)[0]
    if n < min_hits:
        return ""
    do = next(d for nm, _, d in _THEMES if nm == name)
    return (f"[your record] The reviewer has bounced you {n} times in {days} days for "
            f"the SAME thing: {name}. {do} If a tool would make the right move "
            f"easier than the wrong one, build it — my_record has the detail.")
