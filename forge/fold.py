"""Rolling memory — Patch B1a (2026-10-27): the fold's pure core.

No agent.py edits here: the fold's selection, distillation, and rebuild are
pure functions over plain history dicts, so they unit-test without an agent.
agent.py wiring (the between-turn trigger) is a later piece.

The invariant this exists to hold (DESIGN.md): the prompt is append-only in
front — bytes before the boundary never change across folds.
"""
import re

# Extractive keep-patterns: decisions, stated numbers, file paths, open
# threads survive the fold; chatter does not (the brief's own list).
_FOLD_KEEP = re.compile(
    r"(\d"
    r"|~?/?[\w.-]+/[\w./-]+"
    r"|\b(decided|agreed|approved|committed|installed|landed|shipped|wired|"
    r"disabled|enabled|replaced|retired|parked|claimed|closed|paused|resumed|"
    r"deferred|postponed|refused|denied|banned|forbidden|allowed|must|never|"
    r"stop|halt|abort|do not|don't|won't|will not)\b"
    r"|\b(TODO|FIXME|TBD|outstanding|remaining|unresolved|unverified|"
    r"untested|blocked|awaiting|pending|uncommitted|half-built|next step|"
    r"open question)\b)", re.I)

SUMMARY_MARKER = "[Running summary of folded history]"


def entry_chars(m: dict) -> int:
    """Rough size of one history entry (mirrors agent._entry_chars)."""
    if m.get("role") == "tool_use":
        calls = m.get("calls") or []
        args = "".join(str(getattr(c, "args", "")) for c in calls)
        return len(m.get("text") or "") + len(args) + 40 * len(calls)
    return len(str(m.get("content") or ""))


def select_block(history, window_cap_tokens, chars_per_token=4) -> int:
    """Oldest complete-turn boundary whose retained tail fits under the cap.
    Returns the cut index into history, or 0 when nothing may be folded
    (already under the cap, or no foldable prefix)."""
    total = sum(entry_chars(m) for m in history) // chars_per_token
    if total <= window_cap_tokens:
        return 0
    user_idxs = [i for i, m in enumerate(history)
                 if m.get("role") == "user" and not m.get("synthetic")]
    for i in user_idxs:
        tail = sum(entry_chars(m) for m in history[i:]) // chars_per_token
        if tail <= window_cap_tokens:
            return i
    return 0


def _transcript(turns) -> str:
    """Plain text of a turn block — every word in it was said."""
    lines = []
    for m in turns:
        role = m.get("role")
        if role in ("user", "assistant"):
            lines.append(str(m.get("content", "")))
        elif role == "tool_use":
            for c in (m.get("calls") or []):
                lines.append(f"ran {getattr(c, 'name', '?')}({str(getattr(c, 'args', ''))[:200]})")
        elif role == "tool_result":
            lines.append(str(m.get("content", ""))[:400])
    return "\n".join(lines)


def distill_extractive(turns, char_cap=900) -> list:
    """Extractive distiller: keep the sentences that carry decisions, stated
    numbers, file paths, open threads; drop the rest. Cannot invent — every
    kept word was said. Returns summary lines, each under char_cap."""
    kept = []
    turns = [m for m in turns if not m.get("synthetic")]   # never re-distill an existing summary
    for sent in re.split(r"(?<=[.!?])\s+", _transcript(turns)):
        s = " ".join(sent.split())
        if s and _FOLD_KEEP.search(s):
            kept.append(s)
    out = " ".join(kept) or "(folded; nothing of record)"
    lines = []
    while len(out) > char_cap:
        cut = out.rfind(" ", 0, char_cap)
        if cut < char_cap // 2:
            cut = char_cap
        lines.append(out[:cut])
        out = out[cut:].lstrip()
    if out:
        lines.append(out)
    return lines


def apply_fold(system, summary, window, block) -> tuple:
    """Rebuild after a fold: distill the block, append its lines to the
    summary at the boundary, drop the block. Returns (new_summary,
    new_window). Append-only invariant: earlier summary bytes are never
    rewritten — a fold adds lines at the end of the summary block and
    nothing before the boundary changes. `system` is carried through
    untouched and is asserted unchanged."""
    lines = distill_extractive(block)
    if summary is None:
        new_summary = {"role": "user", "synthetic": True,
                       "content": SUMMARY_MARKER + "\n" + "\n".join(lines)}
    else:
        new_summary = dict(summary)
        new_summary["content"] = summary["content"] + "\n" + "\n".join(lines)
    return new_summary, list(window)


def check_invariant(before, after) -> bool:
    """Append-only check across one fold: `before`/`after` are (summary,
    window) pairs. True iff the earlier summary bytes survive verbatim (new
    lines appended only) and the window shrank without gaining entries."""
    b_sum, b_win = before
    a_sum, a_win = after
    if b_sum is None:
        if a_sum is None:
            return True
        return str(a_sum.get("content", "")).startswith(SUMMARY_MARKER)
    old = str(b_sum.get("content", ""))
    new = str(a_sum.get("content", ""))
    if not new.startswith(old):
        return False
    if not new[len(old):].lstrip("\n"):
        return False   # a fold must add something, or not have happened
    return all(m in b_win for m in a_win) and len(a_win) <= len(b_win)


# ---------------------------------------------------------------- B2 (2026-10-27)
# The summary block's own cap (DESIGN.md addition 3). Every line already in the
# summary passed _FOLD_KEEP, so re-running the extractive distiller over it
# would keep nearly everything and shrink nothing. Shrinking therefore ranks the
# summary's OWN sentences: the strong ones (decisions, paths, open threads) are
# kept first, newest first, then the number-only ones, until the budget is
# spent. Still extractive — every kept word was said; dropped sentences stay
# retrievable verbatim through recall (the fold filed each block there).
_FOLD_STRONG = re.compile(_FOLD_KEEP.pattern.replace("(\\d|", "(", 1), re.I)
assert _FOLD_STRONG.pattern != _FOLD_KEEP.pattern, "strong pattern derivation broke"

_NOTHING = "(folded; nothing of record)"


def summary_tokens(summary, chars_per_token=4) -> int:
    """Rough token size of the summary message (0 when there is none)."""
    return entry_chars(summary) // chars_per_token if summary else 0


def shrink_summary(summary, char_budget, char_cap=900) -> dict:
    """Fold the summary into a shorter summary, ONCE, when it passes its cap.
    This is the one deliberate break of the append-only prefix (one bigger
    re-read, accepted by design). Returns a new summary dict that still starts
    with SUMMARY_MARKER; returns `summary` unchanged if nothing would shrink."""
    body = str(summary.get("content", ""))
    if body.startswith(SUMMARY_MARKER):
        body = body[len(SUMMARY_MARKER):]
    body = body.split("\n", 1)[1] if body.startswith(" (condensed") else body
    flat = " ".join(body.split())
    sents = [s for s in re.split(r"(?<=[.!?])\s+", flat) if s and s != _NOTHING]
    seen, uniq = set(), []
    for s in reversed(sents):          # a fact restated later wins its later slot
        k = s.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    uniq.reverse()
    newest_first = list(reversed(range(len(uniq))))
    strong = [i for i in newest_first if _FOLD_STRONG.search(uniq[i])]
    weak = [i for i in newest_first if not _FOLD_STRONG.search(uniq[i])]
    keep, used = set(), 0
    for i in strong + weak:
        n = len(uniq[i]) + 1
        if used + n <= char_budget:
            keep.add(i)
            used += n
    kept = [uniq[i] for i in sorted(keep)]
    out = " ".join(kept) or _NOTHING
    lines = []
    while len(out) > char_cap:
        cut = out.rfind(" ", 0, char_cap)
        if cut < char_cap // 2:
            cut = char_cap
        lines.append(out[:cut])
        out = out[cut:].lstrip()
    if out:
        lines.append(out)
    head = (SUMMARY_MARKER + f" (condensed once: {len(uniq) - len(kept)} older "
            f"sentence(s) dropped; recall holds them verbatim)")
    new = dict(summary)
    new["content"] = head + "\n" + "\n".join(lines)
    if len(new["content"]) >= len(str(summary.get("content", ""))):
        return summary
    return new
