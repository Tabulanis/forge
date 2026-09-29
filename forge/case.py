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


def symptom_of(text: str) -> str:
    """The client's own words when the brief quotes them (> lines), else the opening lines."""
    quoted = [ln.lstrip("> ").strip() for ln in (text or "").splitlines() if ln.strip().startswith(">")]
    if quoted:
        return " ".join(quoted)[:400]
    return " ".join((text or "").split())[:300]


def new(symptom: str = "") -> dict:
    return {
        "symptom": symptom_of(symptom),
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


_PROSE_PREFIX = re.compile(r"^\W*(commit message|diff comment|comment|the comment|readme|docs?|documentation|message)\b", re.I)
_COMMENT_LINE = re.compile(r"^\s*[+-]?\s*(//|#|\*|/\*|<!--|;|--|\"\"\")")


def receipt_is_evidence(receipt: str) -> str:
    """'' if the receipt can count as evidence, else why not. A receipt is a line of
    CODE or DATA from the diff, or command output. Commit messages, code comments and
    README prose are the author's CLAIMS about behaviour — the 2026-09-29 14:51 run
    marked the trap commit YES on the strength of a comment that 'matches the symptom'."""
    r = (receipt or "").strip()
    if not r:
        return "empty"
    if _PROSE_PREFIX.match(r):
        return "that is a commit message / comment / doc: the author's claim, not evidence of behaviour"
    if _COMMENT_LINE.match(r):
        return "that is a comment line: a claim, not evidence — quote the code or data line instead"
    words = re.findall(r"[A-Za-z]{3,}", r)
    codey = len(re.findall(r"[=:<>(){}\[\]/_\.\-\$\|\\]", r))
    if len(words) >= 12 and codey < 3:
        return "that reads as prose, not a code/data line or command output"
    return ""


def review_commit(case: dict, hash: str, verdict: str, why: str = "", receipt: str = "",
                  next_check: str = "") -> str:
    h = (hash or "").strip()
    q = next((q for q in case["queue"] if q["hash"].startswith(h) or h.startswith(q["hash"])), None)
    if q is None:
        raise ValueError(f"{h!r} is not in the queue")
    v = (verdict or "").strip().upper()
    if v not in VERDICTS:
        raise ValueError("verdict must be YES, NO or UNKNOWN")
    if v == "NO" and q.get("platform"):
        unresolved = [x for x in case["external_questions"] if x.get("auto") and x.get("artifact") in q["platform"]
                      and (x["status"] in ("open", "partial") or (x["status"] == "answered" and x.get("verified") is False))]
        if unresolved:
            v = "UNKNOWN"; next_check = next_check or ("resolve: " + unresolved[0]["question"][:120])
            why = (why or "") + " [recorded as UNKNOWN: this change writes a platform-defined value whose rule is not yet established]"
    _need(why, "one or two sentences of why")
    if v in ("YES", "NO"):
        _need(receipt, f"a receipt for {v} — quote the exact output (diff lines or command output) that shows it")
        bad = receipt_is_evidence(receipt)
        if bad:
            raise ValueError(f"receipt refused: {bad}. Commit messages, comments and docs are claims; if the claim is "
                             f"all you have, the verdict is UNKNOWN with the check that would test it")
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
    bad = receipt_is_evidence(receipt)
    if bad:
        raise ValueError("receipt refused: " + bad)
    s["evidence"].append(receipt.strip()[:400])
    s["status"] = "supported"
    if confidence:
        s["confidence"] = confidence.strip()[:40]
    return f"{s['id']} supported ({len(s['evidence'])} receipt(s))."


def clear(case: dict, sid: str, receipt: str) -> str:
    s = _find(case, sid)
    _need(receipt, "a receipt — a suspect is cleared only by quoting the check that cleared it; "
                   "'it would look different' is a next_check, not a receipt")
    bad = receipt_is_evidence(receipt)
    if bad:
        raise ValueError("receipt refused: " + bad)
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


def answer(case: dict, question: str, answer_text: str, source: str, quote: str = "") -> str:
    q = next((q for q in case["external_questions"]
              if q["question"].lower().startswith((question or "").strip().lower()[:40])), None)
    if q is None:
        raise ValueError("no such open question")
    if q.get("auto") and (source or "").strip().lower() not in ("unanswerable", "none", "n/a", "cannot"):
        # 2026-09-29: she "answered" from an EMPTY fetch. A platform rule must be QUOTED
        # from a page she fetched; the quote is verified against her tool output at land.
        if len((quote or "").strip()) < 25:
            raise ValueError("quote the rule verbatim from the page you fetched (quote=..., at least 25 characters); "
                             "if no page states it, pass source=unanswerable with what you tried")
        q["quote"] = quote.strip()[:400]; q["verified"] = False
        art = (q.get("artifact") or "").lower()
        if "version" in art or "build" in art:
            # A version rule has three parts. Say what you found for each, or "unknown";
            # "satisfies" without the limits is how the 14:51 run went wrong.
            low = (answer_text or "").lower()
            if not ("component" in low and "digit" in low):
                raise ValueError("for a version key state the limits you checked — number of components, "
                                 "maximum digits per component, allowed characters — and write unknown for any "
                                 "you could not find a source for")
            if "unknown" in low:
                q.update({"status": "partial", "answer": answer_text.strip()[:400], "source": source.strip()[:200]})
                return "recorded as PARTIAL: the unknown limit stays an open question (it goes in the solve request)."
    _need(answer_text, "the answer (or why it cannot be answered)")
    if (source or "").strip().lower() in ("unanswerable", "none", "n/a", "cannot"):
        q.update({"status": "unanswerable", "answer": answer_text.strip()[:400], "source": ""})
        return "recorded as unanswerable (it stays visible in the answer)."
    _need(source, "the source (URL or document) — an answer without a source is a guess; "
                  "if it truly cannot be answered, pass source=unanswerable with the reason")
    q.update({"status": "answered", "answer": answer_text.strip()[:400], "source": source.strip()[:200]})
    return "answered (the quote will be checked against the page you fetched)."


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
    pri = [x for x in case["queue"] if x.get("priority", True)]
    left = [x for x in pri if x["status"] == "unreviewed"]
    if left:
        left.sort(key=lambda x: -x.get("score", 0))
        first = [x["hash"] + " " + x["subject"][:40] for x in left[:3]]
        out.append(f"queue: {len(left)} of the top {len(pri)} commits unreviewed (next_commit hands you the diff) — next: " + "; ".join(first))
    for x in case["queue"]:
        if x["status"] == "UNKNOWN" and not x.get("next_check"):
            out.append(f"queue: {x['hash']} is UNKNOWN with no next check")
    for s in case["suspects"]:
        if s["status"] == "open" and not s.get("next_check"):
            out.append(f"{s['id']} is open with no next check — what would settle it?")
    for q in case["external_questions"]:
        if q["status"] == "open":
            out.append(f"external question open: {q['question'][:90]}")
        elif q["status"] == "answered" and q.get("auto") and q.get("verified") is False:
            out.append(f"external answer not backed by a fetched page (quote not found): {q['question'][:70]}")
        elif q["status"] == "partial":
            out.append(f"external answer PARTIAL (a limit is unknown): {q['question'][:70]}")
    return out


def render(case: dict, verify=None) -> str:
    """The four boxes, generated. `verify(receipt) -> bool` says whether a receipt
    appears in this job's real output; unverified receipts cannot make anything Proven."""
    _ok = verify or (lambda r: True)
    verified_receipts = {x.get("receipt") for x in case["queue"] if x.get("receipt_verified")}
    ok = lambda r: (r in verified_receipts) or _ok(r)
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
    npri = sum(1 for x in case["queue"] if x.get("priority", True))
    L.append(f"- queue: {cov['total'] - cov['unreviewed']} of {cov['total']} reviewed — "
             f"{cov['YES']} YES, {cov['NO']} NO, {cov['UNKNOWN']} UNKNOWN"
             + (f" (top {npri} block a diagnosis; {cov['total'] - npri} deferred)" if npri < cov["total"] else ""))
    eq = case["external_questions"]
    if eq:
        L.append(f"- external questions: {sum(1 for q in eq if q['status'] == 'answered')} answered, "
                 f"{sum(1 for q in eq if q['status'] == 'unanswerable')} unanswerable, "
                 f"{sum(1 for q in eq if q['status'] == 'open')} open")
        L += [f"  - {q['question'][:100]} — {q['status']}" + (f": {q['answer'][:120]} [{q['source'][:60]}]" if q['status'] == 'answered' else "")
              for q in eq]
    proven_yes = sum(1 for x in case["queue"] if x["status"] == "YES" and ok(x.get("receipt", "")))
    status = "**Status: best guess**" if (proven + proven_yes) == 0 else "**Status: proven items exist; the rest is best guess**"
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
    n = add_queue(case, commits(repo, w.get("good", ""), w.get("bad", "")))
    tag_platform(root, case)
    prioritise(case)
    return n


# ----------------------------------------------------------------- contract trigger (external facts the repo cannot vouch for)

# Values the PLATFORM defines and the repository cannot validate. Seeing one of
# these change is the compiler-error moment: "this value is judged by a rule that
# lives outside this repo — go and check the rule." Curated, small on purpose.
PLATFORM_KEYS = {
    "apple": ["CFBundleVersion", "CFBundleShortVersionString", "CFBundleIdentifier",
              "CFBundleIdentifierKernel", "IOUserServerName", "IOUserClass", "IOProviderClass",
              "LSMinimumSystemVersion", "MinimumOSVersion", "OSBundleUsageDescription",
              "com.apple.developer.driverkit", "com.apple.security.", "IOKitPersonalities"],
    "android": ["versionCode", "versionName", "minSdkVersion", "targetSdkVersion", "<uses-permission"],
    "python": ["python_requires", "install_requires", "entry_points"],
}
_PLATFORM_LOOKUP = {k: eco for eco, keys in PLATFORM_KEYS.items() for k in keys}


def platform_hits(diff_text: str) -> list:
    """Platform-defined keys that ADDED or CHANGED lines of a diff mention, in order."""
    hits = []
    for ln in (diff_text or "").splitlines():
        if not ln.startswith("+") or ln.startswith("+++"):
            continue
        for k in _PLATFORM_LOOKUP:
            if k in ln and k not in hits:
                hits.append(k)
    return hits


def diff_of(repo, hash: str, limit: int = 200_000) -> str:
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(repo), "show", "--format=%h %s", hash],
                              capture_output=True, text=True, timeout=20).stdout[:limit]
    except Exception:
        return ""


def tag_platform(root, case: dict) -> int:
    """At triage: note on each queued commit which platform-defined keys it touches."""
    repo = find_repo(root)
    if repo is None:
        return 0
    n = 0
    for q in case["queue"]:
        if "platform" in q:
            continue
        q["platform"] = platform_hits(diff_of(repo, q["hash"]))
        n += bool(q["platform"])
    return n


def auto_question(case: dict, hash: str) -> str:
    """When a reviewed commit touches platform-defined keys, the HARNESS opens the
    question the repo cannot answer. One per commit. Returns the question or ""."""
    q = next((q for q in case["queue"] if q["hash"].startswith(hash) or hash.startswith(q["hash"])), None)
    if not q or not q.get("platform"):
        return ""
    # One question per KEY, not per commit (2026-09-29: four commits opened four
    # near-identical questions and none got answered).
    asked = {x.get("artifact", "") for x in case["external_questions"]}
    new = [k for k in q["platform"] if k not in asked]
    if not new:
        return ""
    k = new[0]
    eco = _PLATFORM_LOOKUP.get(k, "platform")
    text = (f"{k} (changed in {q['hash']}): do the values this project writes to it satisfy the "
            f"{eco} platform's rule for that key? The repository cannot vouch for this — check the rule.")
    ask(case, text, artifact=k, auto=True)
    return text


def dated_history(root, limit: int = 60) -> str:
    """The repo's commits, oldest first, one dated line each — the raw material for the window."""
    import subprocess
    repo = find_repo(root)
    if repo is None:
        return ""
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", "--reverse", "--date=short", "--format=%h %ad %s", "-n", str(limit)],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""
    return "\n".join(ln[:110] for ln in out.splitlines())


# ----------------------------------------------------------------- priority (an unknown window must not mean 43 diffs)

PRIORITY_N = 12


def _tokens(text: str) -> set:
    import re as _re
    return {w for w in _re.findall(r"[a-z][a-z0-9_]{4,}", (text or "").lower())}


def prioritise(case: dict) -> int:
    """Score every queued commit: platform-defined keys touched (x3), files whose path
    shares a word with the symptom (x2), recency (small). The top PRIORITY_N are the
    ones that block a diagnosis; the rest stay visible as deferred."""
    toks = _tokens(case.get("symptom", ""))
    n = len(case["queue"])
    # Regressions are usually LATE and SMALL; the giant early commit that created the
    # feature touches every platform key and is the least likely culprit. Queue is
    # newest first, so recency = (n - i) / n.
    for i, q in enumerate(case["queue"]):
        files = " ".join(q.get("files", [])).lower()
        match = sum(1 for t in toks if t in files)
        big = len(q.get("files", [])) > 15
        q["score"] = (3 * bool(q.get("platform")) + 2 * min(match, 3)
                      + 3 * (n - i) / max(n, 1) - (2 if big else 0))
    order = sorted(range(n), key=lambda i: -case["queue"][i]["score"])
    for rank, i in enumerate(order):
        case["queue"][i]["priority"] = rank < PRIORITY_N
    return min(n, PRIORITY_N)


def next_commit(root, case: dict, chars: int = 6000) -> str:
    """The highest-priority unreviewed commit, with its DIFF, ready for review_commit."""
    repo = find_repo(root)
    todo = [q for q in case["queue"] if q["status"] == "unreviewed"]
    if not todo:
        return "Nothing left to review."
    todo.sort(key=lambda q: (-(q.get("priority", False)), -q.get("score", 0)))
    q = todo[0]
    diff = diff_of(repo, q["hash"], chars + 1) if repo is not None else ""
    more = " …(diff truncated; git show for the rest)" if len(diff) > chars else ""
    sc = q.get("scout") or {}
    note = (f"scout says {sc.get('verdict')}: {sc.get('why', '')[:160]}"
            + (f" (next check: {sc.get('next_check')})" if sc.get("next_check") and sc.get("next_check") != "-" else "")
            + "\n") if sc else ""
    return (f"COMMIT {q['hash']} — {q['subject']}\nfiles: {', '.join(q.get('files', [])[:8])}\n"
            f"platform-defined keys touched: {', '.join(q.get('platform') or []) or 'none'}\n{note}"
            f"{len(todo) - 1} more to review after this one.\n\n{diff[:chars]}{more}\n\n"
            f"Now: review_commit(\"{q['hash']}\", YES|NO|UNKNOWN, why, receipt|next_check).")


# ----------------------------------------------------------------- scout pre-review (coverage bought structurally)

SCOUT_SYSTEM = """You are a sealed reviewer of ONE commit. Given the SYMPTOM and the commit's DIFF,
decide whether this change could produce the symptom. Rules:
- YES or NO must quote, verbatim, one line of the diff as the RECEIPT.
- If the change writes or alters a platform-defined value (Info.plist keys, entitlements,
  version strings, build settings) whose rule you cannot verify from the diff alone,
  answer UNKNOWN and say what would settle it. Never guess a platform rule.
- Documentation-only or UI-only changes are NO with the line that shows it.
- Commit messages and code comments are the author's CLAIMS, never a RECEIPT; quote code or data lines only.
Answer in exactly this shape, nothing else:
VERDICT: YES|NO|UNKNOWN
WHY: <one sentence>
RECEIPT: <one verbatim diff line>
NEXT_CHECK: <the check that would settle it, or ->"""


def scout_review(root, case: dict, provider, limit_chars: int = 5000, only_priority: bool = True) -> int:
    """The harness reviews the priority set with sealed single-call scouts. A NO with a
    receipt that is really in the diff is filed as NO (scout); YES/UNKNOWN stay for her,
    annotated. Returns how many commits were scouted. Fails open per commit."""
    import re as _re
    repo = find_repo(root)
    if repo is None or provider is None:
        return 0
    n = 0
    for q in case["queue"]:
        if q["status"] != "unreviewed" or (only_priority and not q.get("priority", True)) or q.get("scout"):
            continue
        diff = diff_of(repo, q["hash"], limit_chars + 1)
        if not diff:
            continue
        digest = (f"SYMPTOM: {case.get('symptom', '')[:300]}\n\nCOMMIT {q['hash']} — {q['subject']}\n"
                  f"platform-defined keys touched: {', '.join(q.get('platform') or []) or 'none'}\n\nDIFF:\n{diff[:limit_chars]}")
        try:
            body = {"chat_template_kwargs": {"enable_thinking": True}, "temperature": 0.0, "top_k": 1,
                    "seed": 20260929, "max_tokens": 700}
            raw = (provider.complete(SCOUT_SYSTEM, [{"role": "user", "content": digest}], [], extra_body=body).text or "")
        except Exception:
            continue
        n += 1
        g = lambda k: (_re.search(rf"{k}:\s*(.+)", raw) or [None, ""])[1].strip() if _re.search(rf"{k}:\s*(.+)", raw) else ""
        verdict, why, receipt, nxt = g("VERDICT").upper()[:7], g("WHY")[:300], g("RECEIPT")[:300], g("NEXT_CHECK")[:300]
        verdict = "YES" if verdict.startswith("YES") else "NO" if verdict.startswith("NO") else "UNKNOWN"
        in_diff = bool(receipt) and receipt.strip("`'\" ") in diff
        q["scout"] = {"verdict": verdict, "why": why, "receipt": receipt, "next_check": nxt, "receipt_in_diff": in_diff}
        if verdict == "NO" and in_diff and not q.get("platform"):
            # a clean NO on a commit that touches no platform-defined value is filed; the
            # rest — YES, UNKNOWN, or any platform-touching commit — is hers to judge
            q.update({"status": "NO", "why": "(scout) " + why, "receipt": receipt.strip("`'\" "),
                      "next_check": "", "receipt_verified": True})
    return n


# ----------------------------------------------------------------- the solve request (his target, 2026-09-29)

def solve_request(case: dict, verify=None) -> str:
    """When nothing is proven: a short package to take to other people or tools.
    Ranked candidates with the check that settles each, the exact open questions
    with what was tried, and what was ruled out with receipts. No client names are
    added here — it only contains what the case already holds."""
    ok = verify or (lambda r: True)
    cands = []
    for sp in case["suspects"]:
        if sp["status"] in ("open", "supported"):
            cands.append((2 if sp["status"] == "supported" else 1, sp["hypothesis"], sp.get("next_check") or "", "suspect"))
    for x in case["queue"]:
        if x["status"] in ("YES", "UNKNOWN"):
            cands.append((x.get("score", 0) / 10 + (1 if x["status"] == "YES" else 0), f"change {x['hash']} ({x['subject'][:70]}): {x['why'][:160]}",
                          x.get("next_check") or "", "change"))
    cands.sort(key=lambda c: -c[0])
    cov = coverage(case)
    L = ["# SOLVE REQUEST — could not reach a definite answer", "",
         f"**Symptom:** {case.get('symptom', '')[:400]}", "",
         "## What is established",
         f"- failure stage: {case['stage']['value']}" + (f" — {case['stage']['reason']}" if case['stage'].get('reason') else ""),
         f"- working/broken window: good={case['window']['good']} bad={case['window']['bad']}"
         + (f" — {case['window']['reason']}" if case['window'].get('reason') else ""),
         f"- history reviewed: {cov['total'] - cov['unreviewed']} of {cov['total']} commits ({cov['YES']} could cause it, {cov['NO']} could not, {cov['UNKNOWN']} undecided)",
         "", "## Candidates, most likely first (none is proven)"]
    for i, (_, what, chk, kind) in enumerate(cands[:3], 1):
        L.append(f"{i}. {what}")
        L.append(f"   - what would settle it: {chk or 'not yet named'}")
    if not cands:
        L.append("- none recorded")
    L += ["", "## Questions we could not answer from here"]
    eq = [q for q in case["external_questions"] if q["status"] not in ("answered",) or (q.get("auto") and q.get("verified") is False)]
    for q in eq:
        L.append(f"- {q['question']}")
        L.append(f"  - status: {q['status']}" + (f" — {q['answer'][:200]}" if q.get("answer") else ""))
    if not eq:
        L.append("- none open")
    ans = [q for q in case["external_questions"] if q["status"] == "answered" and (not q.get("auto") or q.get("verified"))]
    if ans:
        L += ["", "## Facts checked"]
        L += [f"- {q['question'][:100]} → {q['answer'][:160]} [{q['source'][:80]}]" for q in ans]
    L += ["", "## Ruled out (with the check that cleared it)"]
    ruled = [(sp["hypothesis"], next((e[len("cleared by: "):] for e in sp["evidence"] if e.startswith("cleared by: ")), ""))
             for sp in case["suspects"] if sp["status"] == "cleared"]
    ruled += [(f"change {x['hash']} ({x['subject'][:60]})", x.get("receipt", "")) for x in case["queue"] if x["status"] == "NO" and ok(x.get("receipt", ""))]
    L += [f"- {h}\n  `{r[:160]}`" for h, r in ruled] or ["- nothing yet"]
    L += ["", "## What we tried",
          f"- {len(case['external_questions'])} outside question(s) opened; {sum(1 for q in case['external_questions'] if q['status'] == 'answered')} answered, "
          f"{sum(1 for q in case['external_questions'] if q['status'] == 'unanswerable')} unanswerable",
          f"- checkpoints: {len(case.get('checkpoints', []))}"]
    return "\n".join(L)
