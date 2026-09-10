"""The elimination pad for a bug hunt — what has been RULED OUT, in writing.

Three runs of the capability test, and across all of them she ruled out nothing.
Eleven mentions of the theory the project's own documentation pushes, one
passing mention of anything else, and a confident wrong cause at the end. The
answer key scores an honest "I don't know, here is what I eliminated" ABOVE a
confident wrong answer, and she has never collected that credit either.

She already owns this discipline. The Deduction Pad corners an animal call's
meaning by ruling candidates out one at a time and reporting the negative space.
Nobody applied it to bugs.

    rule_out(theory, because)   strike a theory, and say what struck it
    open_theories()             what is still standing, and what has fallen
    clear_theories()            start a fresh hunt

The point is not bookkeeping. A theory you cannot strike is a theory you have
not tested, and writing down what killed it is what stops you circling the same
three commits for forty minutes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .paths import STATE_DIR

PAD = STATE_DIR / "ruled-out.jsonl"
MIN_BEFORE_A_CAUSE = 2


def _rows() -> list[dict]:
    if not PAD.is_file():
        return []
    out = []
    for line in PAD.read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def rule_out(theory: str, because: str = "", standing: str = "") -> str:
    """Strike a theory off, and say what struck it."""
    t = " ".join(str(theory or "").split())
    why = " ".join(str(because or "").split())
    if len(t) < 4:
        return "Name the theory you are striking, in a few words."
    if len(why) < 10:
        return (f"Say what RULED OUT '{t[:50]}' — the command you ran, the line you "
                f"read, the thing that did not happen. A theory struck without "
                f"evidence is a theory you just stopped liking.")
    PAD.parent.mkdir(parents=True, exist_ok=True)
    with PAD.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "theory": t[:300],
                            "because": why[:600], "standing": str(standing or "")[:300]}) + "\n")
    n = len(_rows())
    left = max(0, MIN_BEFORE_A_CAUSE - n)
    tail = (f" {left} more before you name a cause."
            if left else " That is enough to name a cause honestly, if you have one.")
    return f"Ruled out: {t[:80]} — {why[:90]}. {n} struck so far.{tail}"


def struck_since(ts: float) -> int:
    """How many theories have been struck off since `ts`.

    The pad is append-only and global, so a count of the whole file would let
    yesterday's work satisfy today's gate. The reviewer needs THIS session's
    number or the gate means nothing.
    """
    try:
        return sum(1 for r in _rows() if float(r.get("t", 0)) >= ts)
    except Exception:
        return 0


def open_theories() -> str:
    """The negative space: what has fallen, and what is still standing."""
    rows = _rows()
    if not rows:
        return ("Nothing ruled out yet. Before naming a cause, strike at least "
                f"{MIN_BEFORE_A_CAUSE} theories with rule_out and say what struck "
                f"each — including the one the documentation pushes, which is the "
                f"one most likely to be wrong and least likely to be tested.")
    lines = [f"RULED OUT ({len(rows)}):"]
    for r in rows:
        lines.append(f"  ✗ {r['theory'][:70]}")
        lines.append(f"      because {r['because'][:96]}")
    standing = [r["standing"] for r in rows if r.get("standing")]
    if standing:
        lines.append("")
        lines.append("STILL STANDING, from what you noted while striking:")
        for s in dict.fromkeys(standing):
            lines.append(f"  · {s[:88]}")
    lines.append("")
    lines.append("An honest 'I don't know, here is what I eliminated' is worth more "
                 "than a confident wrong cause — and it is worth more to whoever "
                 "reads it next.")
    return "\n".join(lines)


def clear_theories() -> str:
    n = len(_rows())
    PAD.unlink(missing_ok=True)
    return f"Pad cleared ({n} struck theories removed). Fresh hunt."


def enough_ruled_out() -> bool:
    return len(_rows()) >= MIN_BEFORE_A_CAUSE
