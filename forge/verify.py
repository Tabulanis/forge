"""One verdict. Dumb, total, and not optional.

2026-09-19, his design: "if checking is cheap, we just make a super checker
that is dumb and never screws up."

The checkers built earlier that day were each sound and each OPTIONAL — and
the day's failures were not weak checks, they were skipped ones: a file
written without a lint, a failing yardstick answered with a theory instead of
its own printed failures. A tool she may forget is not a moat.

So: every changed .gd gets the habit lint, the API-name check and the engine's
parser; then EVERY yardstick in tests/ runs (the whole sweep costs about five
seconds, measured); and if something fails with uncommitted changes in one
file, the bisect runs itself and names the lines. One verdict comes back —
GREEN or RED — and the harness staples it to the end of any turn that touched
a .gd, so "done" is never just an opinion.

It decides nothing. It reports what the machine found.
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_OUT = 6000


def verify(project_dir: str, ssh, godot: str, old_box_path, mount_path,
           lint, bisect_fn=None, timeout: int = 300) -> str:
    """Everything, in order, as one verdict. `ssh(cmd, timeout)` and the path
    translators are injected so this stays testable and the caller owns how the
    engine is reached."""
    pd = old_box_path(project_dir)
    mount = mount_path(project_dir)
    if pd is None or mount is None:
        return ("project_dir must be absolute: /home/tabulanis/titan-home/gamedev/<name> "
                "(mount) or /home/tabulanis/gamedev/<name> (old box).")
    root = Path(mount)
    if not root.is_dir():
        return f"No such project: {mount}"

    # --- which .gd files are not committed (the ones this turn can have broken)
    import subprocess
    changed: list[str] = []
    if (root / ".git").is_dir():
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=60)
        for line in (r.stdout or "").split("\n"):
            f = line[3:].strip()
            if f.endswith(".gd"):
                changed.append(f)

    out: list[str] = []
    red: list[str] = []

    # --- 1. the instant checks, on every changed file
    lint_bad = 0
    for f in changed:
        res = lint(str(root / f))
        if res and not res.rstrip().endswith(": clean"):
            lint_bad += 1
            out.append(res)
    if changed:
        out.append(f"CHECKED {len(changed)} changed file(s): "
                   + ("lint clean" if lint_bad == 0 else f"{lint_bad} with findings"))
    else:
        out.append("No uncommitted .gd files.")

    # --- 2 and 3. the engine's parser on each, then every yardstick — one trip
    files = " ".join(f"'{f}'" for f in changed)
    script = (
        f"cd '{pd}' || exit 9; "
        + (f"for f in {files}; do "
           f"o=$({godot} --headless --check-only -s \"$f\" 2>&1 | grep -E 'SCRIPT ERROR|at: GDScript' | head -4); "
           f"[ -n \"$o\" ] && printf 'PARSE %s\\n%s\\n' \"$f\" \"$o\"; done; " if changed else "")
        + "for t in tests/*_test.gd; do "
          "n=$(basename \"$t\" _test.gd); "
          f"l=$(timeout 280 {godot} --headless -s \"$t\" 2>&1 | grep -E '^PASS|^FATAL' | head -1); "
          "printf 'TEST %s %s\\n' \"$n\" \"${l:-no PASS line}\"; done"
    )
    rc, raw = ssh(script, timeout=timeout)
    if rc == 9:
        return f"Cannot reach the project on the old box at {pd}."

    parses = [l for l in raw.split("\n") if l.startswith("PARSE ") or "SCRIPT ERROR" in l or "at: GDScript" in l]
    tests = [l for l in raw.split("\n") if l.startswith("TEST ")]
    if parses:
        red.append("the engine cannot parse a changed file")
        out.append("\nENGINE PARSE:")
        out.extend("  " + l for l in parses[:12])
    elif changed:
        out.append("ENGINE: all changed files parse.")

    failing: list[str] = []
    out.append("\nYARDSTICKS:")
    for l in tests:
        parts = l.split(" ", 2)
        name = parts[1] if len(parts) > 2 else "?"
        line = parts[2] if len(parts) > 2 else l
        m = re.search(r"FAIL (\d+)", line)
        bad = ("FATAL" in line) or (m is None) or (int(m.group(1)) > 0)
        out.append(f"  {'RED ' if bad else 'ok  '} {name:<10} {line}")
        if bad:
            failing.append(name)
    if failing:
        red.append(f"{len(failing)} yardstick(s) failing: {', '.join(failing)}")
    if not tests:
        red.append("no yardsticks ran")

    # --- 4. something failed and one file is uncommitted: find the lines, don't guess
    if failing and bisect_fn and len(changed) == 1:
        out.append("\nWHICH EDIT DID IT (bisect, not a theory):")
        out.append(bisect_fn(project_dir, failing[0], changed[0]))
    elif failing and len(changed) > 1:
        out.append(f"\n{len(changed)} files are uncommitted, so bisect cannot isolate one. "
                   f"Commit what you trust, then run godot_bisect on the rest.")

    verdict = ("RED — " + "; ".join(red)) if red else "GREEN — every changed file is clean and every yardstick passes."
    if lint_bad and not red:
        verdict = f"AMBER — every yardstick passes, but {lint_bad} file(s) have lint findings."
    head = (f"VERIFY {root.name}: {verdict}\n"
            f"(this ran by itself: it is the machine's verdict, not a summary — "
            f"quote it rather than restating it)\n")
    return (head + "\n".join(out))[:MAX_OUT]
