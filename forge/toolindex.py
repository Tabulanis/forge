"""The tool index — everything she can reach, without carrying it all.

Every tool definition rides along on every single request. Measured on the
running 24,576-token server, all 70 schemas came to ~11,800 tokens: two-thirds
of the window spent before she read a word, which is what kept ending long jobs
with a bare 400. Cutting to a core set fixes the arithmetic but takes away
tools she genuinely uses.

So the rest stay reachable through a one-line index she can search. She finds
what she needs by describing the job, loads it, and it's there for the rest of
the turn. The index costs a fraction of the schemas it replaces.

Two rules that come from how a local model actually runs:

  * LOAD IN BATCHES, and load early. On llama.cpp the tool schema sits in the
    prompt prefix, so changing the toolset invalidates the cached prefix and
    forces a re-process. One load of four tools is cheap; four loads of one
    are not.
  * NEVER UNLOAD MID-TURN. Dropping a tool can't give the time back — the
    prefix has to be rebuilt either way — so shedding one mid-turn costs a
    re-process and saves nothing. The set resets when the turn ends.
"""
from __future__ import annotations

import re

# Loaded for the current turn, on top of the mode's core set.
_LOADED: set[str] = set()


def reset() -> None:
    """New turn: back to the core set."""
    _LOADED.clear()


def loaded() -> set[str]:
    return set(_LOADED)


def _summarize(desc: str, width: int = 110) -> str:
    """First sentence of a tool's description — enough to recognize it by."""
    text = re.sub(r"\s+", " ", str(desc or "")).strip()
    cut = text.find(". ")
    if 0 < cut < width:
        return text[:cut + 1]
    return text[:width] + ("…" if len(text) > width else "")


def build(all_tools, core_names: set[str]):
    """Return (index_text, resolver) for the tools NOT in the core set."""
    extra = [t for t in all_tools if t.name not in core_names]
    lines = [f"{t.name} — {_summarize(t.description)}" for t in sorted(
        extra, key=lambda t: t.name)]
    return "\n".join(lines), {t.name: t for t in extra}


def find_tools(query: str, registry: dict, limit: int = 8) -> str:
    """Search the index by what you're trying to DO, in plain words. Returns
    matching tool names with one-line summaries; load_tools makes them usable."""
    q = str(query or "").strip().lower()
    if not q:
        return ("Say what you're trying to do — 'search my email', 'design a "
                "part', 'check a drug name' — and I'll name the tools for it.")
    # Common verbs and filler match nearly every description, which drowns the
    # real signal — "make her funnier" scored build_sim above set_personality
    # purely on the word "make". Same failure the memory search had.
    STOP = {"the", "and", "for", "with", "from", "you", "your", "her", "his",
            "make", "made", "get", "got", "use", "using", "want", "need",
            "help", "how", "what", "that", "this", "can", "could", "would",
            "some", "any", "all", "out", "into", "over", "about", "when",
            "where", "which", "new", "one", "two", "look", "give", "run",
            "set", "find", "tool", "tools", "please", "just"}
    words = [w for w in re.findall(r"\w+", q) if len(w) > 2]
    words = [w for w in words if w not in STOP] or words
    scored = []
    for name, tool in registry.items():
        hay = f"{name} {tool.description}".lower()
        hits = sum(1 for w in words if w in hay)
        if name.lower() in q:
            hits += 5
        if hits:
            scored.append((hits, name, tool))
    if not scored:
        return (f"Nothing in the index matches '{query}'. These are the "
                "families available: files, shell, web, memory, sims and "
                "datasets, markets, law, medicine, CAD, audio, animal calls, "
                "your life-archive, credentials, personality.")
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = [f"Tools matching '{query}' — load_tools to use them:"]
    for _, name, tool in scored[:limit]:
        out.append(f"  {name} — {_summarize(tool.description, 100)}")
    return "\n".join(out)


def load_tools(names, registry: dict) -> str:
    """Load one or more tools for the rest of this turn, by exact name. Pass
    them ALL AT ONCE — each separate load costs a prompt re-process, so one
    call with four names is far cheaper than four calls."""
    if isinstance(names, str):
        names = [n for n in re.split(r"[,\s]+", names) if n]
    if not isinstance(names, list) or not names:
        return "Give me tool names to load — find_tools will tell you which."
    ok, unknown, already = [], [], []
    for raw in names[:12]:
        n = str(raw).strip()
        if n in _LOADED:
            already.append(n)
        elif n in registry:
            _LOADED.add(n)
            ok.append(n)
        else:
            unknown.append(n)
    parts = []
    if ok:
        parts.append(f"Loaded: {', '.join(ok)}. They're available for the rest "
                     "of this turn — use them now rather than loading again.")
    if already:
        parts.append(f"Already loaded: {', '.join(already)}.")
    if unknown:
        parts.append(f"No such tool: {', '.join(unknown)} — try find_tools.")
    return " ".join(parts)
