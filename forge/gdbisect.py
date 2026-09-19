"""Which of my edits broke the test? — answered by brute force, not by theory.

Born 2026-09-19 out of his encryption analogy: verification is cheaper than
generation, so the checker should be strong at checking and stupid at
everything else. A checker too dumb to have a theory cannot have a WRONG one —
which is the whole failure mode this replaces. At PASS 462/586 the answer was
"likely materials, tolerances, or node structure" (three plausible theories,
all wrong; the cause was one missing add_child).

This never guesses. It takes the last committed version of the file — the one
whose test passed — replays the working-tree changes one hunk at a time, and
runs the real yardstick after each. What comes back is a fact: these lines
flip it.

Her file is saved before anything is touched and restored in a finally block,
whatever happens, including a crash or a timeout.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

MAX_RUNS = 16          # a yardstick run costs 10-120 s on the old box
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _git(project: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(project), *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _split_hunks(diff: str) -> tuple[str, list[str]]:
    """(file header, [hunk text]) from a single-file unified diff."""
    lines = diff.split("\n")
    start = next((i for i, l in enumerate(lines) if l.startswith("@@")), None)
    if start is None:
        return "", []
    header = "\n".join(lines[:start]) + "\n"
    hunks, cur = [], []
    for l in lines[start:]:
        if l.startswith("@@") and cur:
            hunks.append("\n".join(cur) + "\n")
            cur = [l]
        else:
            cur.append(l)
    if cur:
        hunks.append("\n".join(cur).rstrip("\n") + "\n")
    return header, hunks


def _hunk_lines(hunk: str) -> str:
    """'lines 120-134' from a hunk header, for the report."""
    m = _HUNK.search(hunk)
    if not m:
        return "?"
    a = int(m.group(1))
    n = int(m.group(2) or "1")
    return f"lines {a}-{a + max(0, n - 1)}" if n > 1 else f"line {a}"


def _hunk_body(hunk: str, limit: int = 6) -> str:
    body = [l for l in hunk.split("\n")[1:] if l[:1] in "+-" and l[:3] not in ("+++", "---")]
    shown = body[:limit]
    more = f"\n      … +{len(body) - limit} more line(s)" if len(body) > limit else ""
    return "\n".join("      " + l for l in shown) + more


def bisect(project_dir: str, test: str, script_path: str = "",
           run_test=None, verbose: bool = False) -> str:
    """Find the working-tree hunks that make tests/<test>_test.gd fail.

    run_test(project, test) -> (passed: bool, pass_line: str); injected so the
    caller owns how the engine is reached (and so this is testable offline).
    """
    project = Path(project_dir)
    if not (project / ".git").exists():
        return f"{project} is not a git repository — nothing to compare against."
    if run_test is None:
        return "No test runner given."

    # Which files changed? Narrow to one if asked.
    rc, out = _git(project, "diff", "--name-only")
    changed = [f for f in out.split("\n") if f.strip().endswith(".gd")]
    if script_path:
        want = str(Path(script_path))
        changed = [f for f in changed if want.endswith(f) or f.endswith(Path(want).name)]
    if not changed:
        return ("Nothing to bisect: no uncommitted .gd changes"
                + (f" for {script_path}" if script_path else "")
                + ". If the test failed at the last commit too, the cause is older "
                  "than this working tree — say so rather than hunting here.")
    if len(changed) > 1:
        return ("More than one changed file: " + ", ".join(changed)
                + ". Bisect one at a time — pass script_path.")

    rel = changed[0]
    f = project / rel
    saved = f.read_text(encoding="utf-8")
    log: list[str] = []
    runs = 0

    def run(label: str) -> tuple[bool, str]:
        nonlocal runs
        runs += 1
        ok, line = run_test(str(project), test)
        log.append(f"  {label}: {line}")
        return ok, line

    try:
        rc, diff = _git(project, "diff", "-U0", "--", rel)   # no context: one edit = one hunk
        header, hunks = _split_hunks(diff)
        if not hunks:
            return f"No hunks in the diff for {rel}."

        # 1. Does it fail as it stands? If not, there is nothing to find.
        now_ok, now_line = run("working tree (your edits)")
        if now_ok:
            return (f"tests/{test}_test.gd PASSES with your current {rel}: {now_line}\n"
                    "Nothing to bisect.")

        # 2. Baseline: the last committed version.
        rc, msg = _git(project, "checkout", "--", rel)
        if rc != 0:
            return f"Could not check out the committed {rel}: {msg}"
        base_ok, base_line = run(f"{rel} at HEAD (your {len(hunks)} hunk(s) removed)")
        if not base_ok:
            return ("\n".join([
                f"It fails at HEAD too, with your changes to {rel} removed:",
                *log,
                "",
                "So this is NOT caused by your working-tree edits. The cause is in "
                "the last commit or in another file. Bisect that file, or read the "
                "failing cases of the HEAD run.",
            ]))

        # 3. Each hunk alone, on top of the clean baseline.
        if len(hunks) + 2 > MAX_RUNS:
            return ("\n".join([
                f"{len(hunks)} hunks would need {len(hunks) + 2} yardstick runs "
                f"(cap {MAX_RUNS}). Commit the parts you trust, or bisect a smaller "
                "change.", *log]))
        culprits: list[int] = []
        for i, h in enumerate(hunks):
            _git(project, "checkout", "--", rel)
            with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as t:
                t.write(header + h)
                patch = t.name
            rc, msg = _git(project, "apply", "--unidiff-zero", "--recount", patch)
            Path(patch).unlink(missing_ok=True)
            if rc != 0:
                log.append(f"  hunk {i + 1} ({_hunk_lines(h)}): could not be applied alone")
                continue
            ok, _ = run(f"hunk {i + 1} alone ({_hunk_lines(h)})")
            if not ok:
                culprits.append(i)

        out_lines = [f"BISECT tests/{test}_test.gd against {rel} — {runs} real run(s), no guessing:", *log, ""]
        if culprits:
            out_lines.append(f"THESE {len(culprits)} HUNK(S) BREAK IT ON THEIR OWN:")
            for i in culprits:
                out_lines.append(f"  hunk {i + 1}, {_hunk_lines(hunks[i])}:")
                out_lines.append(_hunk_body(hunks[i]))
            out_lines.append("")
            out_lines.append("Fix those lines. Every other hunk is innocent — leave them alone.")
        else:
            out_lines.append(
                "No single hunk breaks it alone, so it is an INTERACTION between two "
                "or more of them (or a change outside this file). Re-apply them in "
                "pairs, or commit the ones you trust and bisect the rest.")
        return "\n".join(out_lines)
    finally:
        f.write_text(saved, encoding="utf-8")     # her work back, always
