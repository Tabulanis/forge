#!/usr/bin/env python3
"""babysit — a supervisor's view of Merge, and a running tally of her flaws.

Watching her work is what actually finds bugs (unit tests never surfaced the
loop, the string-suspect crash, or the truncated-evidence false bounce). This
reads the same on-disk record she leaves behind — every session log plus the
superego ledger — so any session can be audited afterwards, including ones
started from the dashboard or another terminal.

  babysit tally              health across every session, and the open findings
  babysit sessions [n]       recent sessions, newest first, with health flags
  babysit check <id|last>    audit one session turn by turn
  babysit flag <id> <text>   record a finding (the running tally)
  babysit fixed <n>          mark finding #n fixed
  babysit findings           the running tally on its own

Read-only against her data; the only thing it writes is the findings file.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

FORGE = Path.home() / ".forge"
SESSIONS = FORGE / "sessions"
LEDGER = FORGE / "ledger.jsonl"
FINDINGS = FORGE / "babysit-findings.jsonl"

# Signals that something went wrong in a turn, and what each one means.
LOOP_AT = 6          # same tool this many times in one session = suspicious


def _sessions() -> list[dict]:
    out = []
    for f in SESSIONS.glob("*.json"):
        try:
            d = json.load(f.open())
            d["_file"] = f
            out.append(d)
        except Exception:
            continue
    return sorted(out, key=lambda d: d.get("last_used", 0), reverse=True)


def _ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _health(sess: dict) -> dict:
    """What went wrong in this session, mechanically."""
    log = sess.get("log", [])
    tools, errors, notes = {}, [], []
    for e in log:
        k = e.get("kind")
        if k == "tool":
            tools[e.get("tool", "?")] = tools.get(e.get("tool", "?"), 0) + 1
        elif k == "error":
            errors.append((e.get("text") or "")[:120])
        elif k == "result" and "budget spent" in str(e.get("text", "")):
            notes.append("hit the per-tool budget (loop stopped)")
        elif k == "note":
            t = str(e.get("text", ""))
            if "compact" in t.lower() or "condensed" in t.lower():
                notes.append("memory compaction fired")
            elif "Stopped" in t:
                notes.append("stopped early")
    loops = {k: v for k, v in tools.items() if v >= LOOP_AT}
    return {"tools": tools, "loops": loops, "errors": errors,
            "notes": sorted(set(notes)),
            "turns": sum(1 for e in log if e.get("kind") == "user"),
            "answered": sum(1 for e in log if e.get("kind") == "done")}


def _findings() -> list[dict]:
    if not FINDINGS.exists():
        return []
    out = []
    for line in FINDINGS.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _ago(ts: float) -> str:
    if not ts:
        return "?"
    d = time.time() - ts
    for n, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if d >= n:
            return f"{int(d // n)}{unit} ago"
    return "just now"


# ---- commands -----------------------------------------------------------
def cmd_tally() -> None:
    sess, led = _sessions(), _ledger()
    tools_total, loop_sessions, err_sessions, compacted = 0, 0, 0, 0
    for s in sess:
        h = _health(s)
        tools_total += sum(h["tools"].values())
        loop_sessions += 1 if h["loops"] else 0
        err_sessions += 1 if h["errors"] else 0
        compacted += 1 if any("compaction" in n for n in h["notes"]) else 0
    bounces = [l for l in led if l.get("verdict") == "bounce"]

    print("BABYSITTING TALLY")
    print(f"  sessions on record : {len(sess)}")
    print(f"  tool calls         : {tools_total}")
    print(f"  superego reviews   : {len(led)}  ({len(bounces)} bounced"
          f"{f', {100*len(bounces)//len(led)}%' if led else ''})")
    print(f"  sessions w/ a loop : {loop_sessions}"
          f"   (same tool >= {LOOP_AT}x)")
    print(f"  sessions w/ errors : {err_sessions}")
    print(f"  memory compactions : {compacted}")
    if bounces:
        print("\n  most recent bounce reasons:")
        for b in bounces[-3:]:
            print(f"    · {(b.get('reason') or '?')[:96]}")
    open_f = [f for f in _findings() if not f.get("fixed")]
    print(f"\n  OPEN FINDINGS: {len(open_f)}")
    for f in open_f:
        print(f"    #{f['n']} [{f.get('session','?')}] {f['text'][:88]}")
    if not open_f:
        print("    (none — nothing outstanding)")


def cmd_sessions(n: int = 12) -> None:
    print(f"{'id':14} {'when':10} {'turns':>5} {'tools':>6}  flags")
    for s in _sessions()[:n]:
        h = _health(s)
        flags = []
        if h["loops"]:
            flags.append("LOOP:" + ",".join(f"{k}x{v}" for k, v in h["loops"].items())[:34])
        if h["errors"]:
            flags.append(f"ERR x{len(h['errors'])}")
        if h["turns"] and h["answered"] < h["turns"]:
            flags.append(f"UNANSWERED {h['turns']-h['answered']}")
        flags += [n for n in h["notes"] if "compaction" in n or "stopped" in n]
        print(f"{s.get('id','?'):14} {_ago(s.get('last_used',0)):10} "
              f"{h['turns']:5} {sum(h['tools'].values()):6}  "
              f"{'; '.join(flags) if flags else 'clean'}")


def cmd_check(sid: str) -> None:
    sess = _sessions()
    if sid == "last":
        target = sess[0] if sess else None
    else:
        target = next((s for s in sess if str(s.get("id", "")).startswith(sid)), None)
    if not target:
        print(f"No session matching '{sid}'. Try: babysit sessions")
        return
    h = _health(target)
    print(f"SESSION {target.get('id')}  ({_ago(target.get('last_used',0))})")
    print(f"  workspace : {target.get('workspace','?')}")
    print(f"  model     : {target.get('model','?')}")
    print(f"  turns {h['turns']} · answered {h['answered']} · "
          f"tool calls {sum(h['tools'].values())}")
    if h["tools"]:
        print("  tools used: " + ", ".join(
            f"{k}x{v}" for k, v in sorted(h["tools"].items(), key=lambda x: -x[1])))
    for w in h["loops"]:
        print(f"  ⚠ LOOP: {w} called {h['loops'][w]}x")
    for e in h["errors"]:
        print(f"  ⚠ ERROR: {e}")
    for n in h["notes"]:
        print(f"  · {n}")

    print("\n  --- transcript ---")
    for e in target.get("log", []):
        k = e.get("kind")
        if k == "user":
            print(f"\n  USER: {(e.get('text') or '')[:300]}")
        elif k == "tool":
            print(f"    → {e.get('tool')}({str(e.get('summary',''))[:60]})")
        elif k == "result":
            t = str(e.get("text", "")).replace("\n", " ")[:140]
            print(f"      {t}")
        elif k == "text":
            print(f"  MERGE: {(e.get('text') or '')[:400]}")
        elif k == "error":
            print(f"  !! ERROR: {(e.get('text') or '')[:200]}")

    # superego verdicts that overlap this session's window
    t0 = min([e.get("t", 0) for e in target.get("log", []) if e.get("t")] or [0])
    t1 = target.get("last_used", 0) + 5
    rel = [l for l in _ledger() if t0 <= l.get("t", 0) <= t1]
    if rel:
        print("\n  --- superego verdicts this session ---")
        for l in rel:
            print(f"    [{l.get('verdict')}] {(l.get('reason') or '')[:120]}")


def cmd_flag(sid: str, text: str) -> None:
    n = len(_findings()) + 1
    rec = {"n": n, "session": sid, "text": text, "t": time.time(), "fixed": False}
    with FINDINGS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(f"Logged finding #{n}: {text}")


def cmd_fixed(n: int) -> None:
    recs = _findings()
    hit = False
    for r in recs:
        if r["n"] == n:
            r["fixed"] = True
            r["fixed_t"] = time.time()
            hit = True
    if not hit:
        print(f"No finding #{n}.")
        return
    FINDINGS.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    print(f"Finding #{n} marked fixed.")


def cmd_findings() -> None:
    recs = _findings()
    if not recs:
        print("No findings logged yet.")
        return
    for r in recs:
        mark = "✔ fixed" if r.get("fixed") else "○ open "
        print(f"  {mark} #{r['n']} [{r.get('session','?')}] {r['text']}")
    print(f"\n  {sum(1 for r in recs if not r.get('fixed'))} open, "
          f"{sum(1 for r in recs if r.get('fixed'))} fixed.")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "tally"
    if cmd == "tally":
        cmd_tally()
    elif cmd == "sessions":
        cmd_sessions(int(a[1]) if len(a) > 1 else 12)
    elif cmd == "check" and len(a) > 1:
        cmd_check(a[1])
    elif cmd == "flag" and len(a) > 2:
        cmd_flag(a[1], " ".join(a[2:]))
    elif cmd == "fixed" and len(a) > 1:
        cmd_fixed(int(a[1]))
    elif cmd == "findings":
        cmd_findings()
    else:
        print(__doc__)
