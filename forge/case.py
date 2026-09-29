"""case.py — the investigation case file (CASE.yaml).

The bug-hunt rebuild (2026-09-29, his design + the second opinion): the answer
is RENDERED from a worksheet the model can only write through tools, never
written as prose and parsed afterwards. Free reasoning between tool calls is
allowed and never inspected; the ledger is what gets checked.

What the file holds:
  stage      which stage of the failure is evidenced (or unknown, with a reason)
  window     last-known-good / first-known-bad commits (or unknown, with a reason)
  queue      commits the HARNESS put in front of her, each unreviewed | YES | NO | UNKNOWN
  suspects   hypotheses: open | supported | cleared — supported/cleared need a receipt
  external_questions   facts the repo cannot supply (platform contracts), open | answered
  checkpoints  rendered snapshots on the clock

Rules encoded here, not in prompts:
  - YES / NO on a commit needs a receipt (a quote from this job's real output);
    UNKNOWN is cheap: it only needs a next_check.
  - A suspect is cleared only with a receipt. "It would present differently" is
    reasoning, not a check; it goes in next_check, the suspect stays open.
  - Nothing is "final": the render says best guess unless a suspect is supported
    by a VERIFIED receipt (the agent verifies receipts against real tool output).
  - Diagnosis is allowed only when the coverage invariants hold (see blockers()).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import yaml

FILE = "CASE.yaml"
STAGES = ("unknown", "build", "install", "load", "discovery", "matching", "transport", "app")
VERDICTS = ("YES", "NO", "UNKNOWN")


# ----------------------------------------------------------------- storage

def path(root) -> Path:
    return Path(root) / FILE


def new(symptom: str = "") -> dict:
    return {
        "symptom": symptom,
        "stage": {"value": "unknown", "evidence": [], "reason": ""},
        "window": {"good": "unknown", "bad": "unknown", "evidence": [], "reason": ""},
        "queue": [],
        "suspects": [],
        "external_questions": [],
        "checkpoints": [],
        "created": time.time(),
    }


def load(root, symptom: str = "") -> dict:
    p = path(root)
    if p.exists():
        try:
            c = yaml.safe_load(p.read_text()) or {}
            if isinstance(c, dict) and "queue" in c:
                return c
        except Exception:
            pass
    return new(symptom)


def save(root, case: dict) -> None:
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(case, sort_keys=False, allow_unicode=True))


# ----------------------------------------------------------------- writes (the tools call these)

def _need(text: str, what: str) -> str:
    if not (text or "").strip():
        raise ValueError(f"{what} is required")
    return text.strip()


def set_stage(case: dict, value: str, evidence: str = "", reason: str = "") -> str:
    value = (value or "").strip().lower()
    if value not in STAGES:
        raise ValueError(f"stage must be one of {', '.join(STAGES)}")
    if value == "unknown":
        _need(reason, "a reason why the stage is unknown")
        case["stage"] = {"value": "unknown", "evidence": [], "reason": reason.strip()}
        return "stage: unknown — every stage's causes stay live; nothing upstream may be crossed off."
    _need(evidence, "evidence (a quote from this job's output) for the stage")
    case["stage"] = {"value": value, "evidence": [evidence.strip()], "reason": ""}
    return f"stage: {value}"


def set_window(case: dict, good: str = "", bad: str = "", evidence: str = "", reason: str = "") -> str:
    good = (good or "unknown").strip() or "unknown"
    bad = (bad or "unknown").strip() or "unknown"
    if good == "unknown" and bad == "unknown":
        _need(reason, "a reason why the window is unknown")
        case["window"] = {"good": "unknown", "bad": "unknown", "evidence": [], "reason": reason.strip()}
        return "window: unknown — the whole history stays in the queue."
    _need(evidence, "evidence for the window (quote the output that dates it)")
    case["window"] = {"good": good, "bad": bad, "evidence": [evidence.strip()], "reason": (reason or "").strip()}
    return f"window: good={good} bad={bad}"


def add_queue(case: dict, commits: list) -> int:
    """Triage puts commits in front of her. Each: {hash, subject, files}. Idempotent."""
    have = {q["hash"] for q in case["queue"]}
    n = 0
    for c in commits:
        h = str(c.get("hash", "")).strip()
        if not h or h in have:
            continue
        case["queue"].append({"hash": h, "subject": str(c.get("subject", ""))[:120],
                              "files": list(c.get("files", []))[:12], "status": "unreviewed",
                              "why": "", "receipt": "", "next_check": ""})
        have.add(h); n += 1
    return n


def review_commit(case: dict, hash: str, verdict: str, why: str = "", receipt: str = "",
                  next_check: str = "") -> str:
    h = (hash or "").strip()
    q = next((q for q in case["queue"] if q["hash"].startswith(h) or h.startswith(q["hash"])), None)
    if q is None:
        raise ValueError(f"{h!r} is not in the queue")
    v = (verdict or "").strip().upper()
    if v not in VERDICTS:
        raise ValueError("verdict must be YES, NO or UNKNOWN")
    _need(why, "one or two sentences of why")
    if v in ("YES", "NO"):
        _need(receipt, f"a receipt for {v} — quote the exact output (diff lines or command output) that shows it")
    else:
        _need(next_check, "for UNKNOWN, the one check that would settle it")
    q.update({"status": v, "why": why.strip()[:400], "receipt": (receipt or "").strip()[:400],
              "next_check": (next_check or "").strip()[:300]})
    left = sum(1 for x in case["queue"] if x["status"] == "unreviewed")
    return f"{q['hash']} {v}. {left} left unreviewed."


def suspect(case: dict, hypothesis: str, next_check: str = "") -> str:
    _need(hypothesis, "a hypothesis")
    sid = f"S{len(case['suspects']) + 1}"
    case["suspects"].append({"id": sid, "hypothesis": hypothesis.strip()[:300], "status": "open",
                             "evidence": [], "next_check": (next_check or "").strip()[:300],
                             "confidence": ""})
    return f"{sid} recorded (open)."


def _find(case: dict, sid: str) -> dict:
    s = next((s for s in case["suspects"] if s["id"].upper() == (sid or "").strip().upper()), None)
    if s is None:
        raise ValueError(f"no suspect {sid!r} — suspects: {', '.join(s['id'] for s in case['suspects']) or 'none'}")
    return s


def support(case: dict, sid: str, receipt: str, confidence: str = "") -> str:
    s = _find(case, sid)
    _need(receipt, "a receipt — the exact output that supports it")
    s["evidence"].append(receipt.strip()[:400])
    s["status"] = "supported"
    if confidence:
        s["confidence"] = confidence.strip()[:40]
    return f"{s['id']} supported ({len(s['evidence'])} receipt(s))."


def clear(case: dict, sid: str, receipt: str) -> str:
    s = _find(case, sid)
    _need(receipt, "a receipt — a suspect is cleared only by quoting the check that cleared it; "
                   "'it would look different' is a next_check, not a receipt")
    s["evidence"].append("cleared by: " + receipt.strip()[:400])
    s["status"] = "cleared"
    return f"{s['id']} cleared."


def set_next_check(case: dict, sid: str, next_check: str) -> str:
    s = _find(case, sid)
    s["next_check"] = _need(next_check, "the check")[:300]
    return f"{s['id']} next check noted."


def ask(case: dict, question: str, artifact: str = "", auto: bool = False) -> str:
    _need(question, "a question")
    if any(q["question"].lower() == question.strip().lower() for q in case["external_questions"]):
        return "already asked."
    case["external_questions"].append({"question": question.strip()[:300], "artifact": (artifact or "")[:120],
                                       "status": "open", "answer": "", "source": "", "auto": bool(auto)})
    return f"question {len(case['external_questions'])} opened."


def answer(case: dict, question: str, answer_text: str, source: str) -> str:
    q = next((q for q in case["external_questions"]
              if q["question"].lower().startswith((question or "").strip().lower()[:40])), None)
    if q is None:
        raise ValueError("no such open question")
    _need(answer_text, "the answer"); _need(source, "the source (URL or document) — an answer without a source is a guess")
    q.update({"status": "answered", "answer": answer_text.strip()[:400], "source": source.strip()[:200]})
    return "answered."


# ----------------------------------------------------------------- reading

def coverage(case: dict) -> dict:
    q = case["queue"]
    return {"total": len(q), "unreviewed": sum(1 for x in q if x["status"] == "unreviewed"),
            "YES": sum(1 for x in q if x["status"] == "YES"), "NO": sum(1 for x in q if x["status"] == "NO"),
            "UNKNOWN": sum(1 for x in q if x["status"] == "UNKNOWN")}


def blockers(case: dict) -> list:
    """Why a diagnosis is not yet allowed. Empty list = allowed."""
    out = []
    st = case["stage"]
    if st["value"] == "unknown" and not st.get("reason"):
        out.append("stage: not set — which stage failed (or unknown, with a reason)?")
    w = case["window"]
    if w["good"] == "unknown" and w["bad"] == "unknown" and not w.get("reason"):
        out.append("window: not set — last-known-good / first-known-bad (or unknown, with a reason)")
    cov = coverage(case)
    if cov["unreviewed"]:
        first = [x["hash"] + " " + x["subject"][:50] for x in case["queue"] if x["status"] == "unreviewed"][:4]
        out.append(f"queue: {cov['unreviewed']} of {cov['total']} commits unreviewed — next: " + "; ".join(first))
    for x in case["queue"]:
        if x["status"] == "UNKNOWN" and not x.get("next_check"):
            out.append(f"queue: {x['hash']} is UNKNOWN with no next check")
    for s in case["suspects"]:
        if s["status"] == "open" and not s.get("next_check"):
            out.append(f"{s['id']} is open with no next check — what would settle it?")
    for q in case["external_questions"]:
        if q["status"] == "open":
            out.append(f"external question open: {q['question'][:90]}")
    return out


def render(case: dict, verify=None) -> str:
    """The four boxes, generated. `verify(receipt) -> bool` says whether a receipt
    appears in this job's real output; unverified receipts cannot make anything Proven."""
    ok = verify or (lambda r: True)
    cov = coverage(case)
    L = ["## Proven"]
    proven = 0
    for s in case["suspects"]:
        if s["status"] == "supported":
            good = [e for e in s["evidence"] if ok(e)]
            if good:
                proven += 1
                L.append(f"- {s['hypothesis']}" + (f" ({s['confidence']})" if s.get("confidence") else ""))
                L += [f"  `{e[:200]}`" for e in good[:3]]
    for x in case["queue"]:
        if x["status"] == "YES" and ok(x.get("receipt", "")):
            L.append(f"- change {x['hash']} can produce the symptom: {x['why']}\n  `{x['receipt'][:200]}`")
    if proven == 0 and not any(x["status"] == "YES" and ok(x.get("receipt", "")) for x in case["queue"]):
        L.append("- nothing yet")
    L.append("\n## Believed")
    for s in case["suspects"]:
        if s["status"] == "open" or (s["status"] == "supported" and not any(ok(e) for e in s["evidence"])):
            L.append(f"- {s['hypothesis']}" + (f" ({s['confidence']})" if s.get("confidence") else "")
                     + (f" — next check: {s['next_check']}" if s.get("next_check") else ""))
    for x in case["queue"]:
        if x["status"] == "UNKNOWN":
            L.append(f"- change {x['hash']} ({x['subject'][:60]}): unknown — {x['why']} — next check: {x['next_check']}")
    if len(L) and L[-1] == "\n## Believed":
        L.append("- nothing open")
    L.append("\n## Ruled out")
    ruled = 0
    for s in case["suspects"]:
        if s["status"] == "cleared":
            rec = [e for e in s["evidence"] if e.startswith("cleared by: ")]
            if rec and ok(rec[-1][len("cleared by: "):]):
                ruled += 1
                L.append(f"- {s['hypothesis']}\n  `{rec[-1][len('cleared by: '):][:200]}`")
            else:
                L.append(f"- (NOT cleared — receipt not found in this job's output; treat as open) {s['hypothesis']}")
    for x in case["queue"]:
        if x["status"] == "NO" and ok(x.get("receipt", "")):
            ruled += 1
            L.append(f"- change {x['hash']} ({x['subject'][:60]}): {x['why']}")
    if ruled == 0:
        L.append("- none yet")
    L.append("\n## What would prove me wrong")
    checks = [s["next_check"] for s in case["suspects"] if s.get("next_check")] + \
             [x["next_check"] for x in case["queue"] if x.get("next_check")]
    L += [f"- {c}" for c in dict.fromkeys(checks)] or ["- (no checks named)"]
    L.append("\n## Coverage")
    L.append(f"- stage: {case['stage']['value']}" + (f" ({case['stage']['reason']})" if case['stage'].get('reason') else ""))
    L.append(f"- window: good={case['window']['good']} bad={case['window']['bad']}"
             + (f" ({case['window']['reason']})" if case['window'].get('reason') else ""))
    L.append(f"- queue: {cov['total'] - cov['unreviewed']} of {cov['total']} reviewed — "
             f"{cov['YES']} YES, {cov['NO']} NO, {cov['UNKNOWN']} UNKNOWN")
    eq = case["external_questions"]
    if eq:
        L.append(f"- external questions: {sum(1 for q in eq if q['status'] == 'answered')} of {len(eq)} answered")
        L += [f"  - {q['question'][:100]} — {q['status']}" + (f": {q['answer'][:120]} [{q['source'][:60]}]" if q['status'] == 'answered' else "")
              for q in eq]
    status = "**Status: best guess**" if proven == 0 else "**Status: proven items exist; the rest is best guess**"
    bl = blockers(case)
    if bl:
        status += " — diagnosis not yet allowed: " + "; ".join(bl[:3])
    return status + "\n\n" + "\n".join(L)


def checkpoint(case: dict, label: str, verify=None) -> str:
    text = render(case, verify)
    case["checkpoints"].append({"t": time.time(), "label": label, "rendered": text})
    return text


def summary(case: dict) -> str:
    """One compact paragraph for the steering critic / gap-finder digests."""
    cov = coverage(case)
    open_s = [f"{s['id']}:{s['hypothesis'][:60]}" + ("" if s.get("next_check") else " [no next check]")
              for s in case["suspects"] if s["status"] == "open"]
    return (f"stage={case['stage']['value']}; window={case['window']['good']}..{case['window']['bad']}; "
            f"queue {cov['total'] - cov['unreviewed']}/{cov['total']} reviewed "
            f"({cov['YES']} YES {cov['NO']} NO {cov['UNKNOWN']} UNKNOWN); "
            f"open suspects: {'; '.join(open_s) or 'none'}; "
            f"external open: {sum(1 for q in case['external_questions'] if q['status'] == 'open')}")


# ----------------------------------------------------------------- triage (harness-owned search space)

def find_repo(root):
    """The git repo at the workspace root or one level down."""
    import subprocess
    try:
        cands = [Path(root)] + sorted(p for p in Path(root).iterdir() if p.is_dir())
    except Exception:
        return None
    return next((p for p in cands if (p / ".git").exists()), None)


def commits(repo, good: str = "", bad: str = "", limit: int = 60) -> list:
    """Commits to review, newest first: good..bad when both known, bad's ancestry
    when only bad is known, else the recent history. Each: hash, subject, files."""
    import subprocess
    rng = f"{good}..{bad}" if good not in ("", "unknown") and bad not in ("", "unknown") else \
          (bad if bad not in ("", "unknown") else "HEAD")
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "--format=%h%x09%s", "--name-only", "-n", str(limit), rng],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    items, cur = [], None
    for ln in out.splitlines():
        if "\t" in ln and len(ln.split("\t")[0]) in range(7, 41):
            h, subj = ln.split("\t", 1)
            cur = {"hash": h, "subject": subj, "files": []}; items.append(cur)
        elif ln.strip() and cur is not None:
            cur["files"].append(ln.strip())
    return items


def triage(root, case: dict) -> int:
    """Fill the queue from the window. Returns how many commits were added."""
    repo = find_repo(root)
    if repo is None:
        return 0
    w = case["window"]
    return add_queue(case, commits(repo, w.get("good", ""), w.get("bad", "")))
