"""Asking a repository WHEN something changed, instead of what looks relevant.

The bug-hunt test has been failed seven times across two brains, always the same
way: she picks the commits whose names sound related and reads them over and
over — 42 `git show`s in the last run, on three commits, none of them the one —
and never once asks when the thing broke.

She had been told. The four-step "find when it broke" procedure is in her
always-on prompt AND repeated as a per-turn nudge in bug-hunt mode. Telling her
twice did not work, and a third paragraph would not either.

What she did not have was a TOOL. Every history move went through the shell,
where the easy thing to type is `git show <the commit that looks interesting>`.
Nothing made "when" easier than "what". This does:

    when_changed(repo, text="...")   which commits made this text appear or
                                     vanish — git's pickaxe, the single sharpest
                                     question in a history
    when_changed(repo, path="...")   how one file changed over time
    since=/until=                    narrow to the window where it broke

Results come back OLDEST FIRST with the date on every line, so "the first commit
after it still worked" is literally the first row rather than something to
reason about.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

MAX_HITS = 40


def _git(repo: Path, *args: str, timeout: int = 60) -> tuple[bool, str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "git is not installed"
    except subprocess.TimeoutExpired:
        return False, f"git took longer than {timeout}s"
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()



def _find_repo(start: Path) -> tuple[Path | None, str]:
    """The repository the caller MEANT.

    2026-09-09, measured. The first bug-hunt run with this tool: she reached for
    it on her very first history question, correctly, and got back "'.' is not a
    git repository, so it has no history to search" — because the workspace root
    was not the repo, the repo sat one directory below at `repo/`. She never
    called it again and spent the rest of the run typing `git show` at a shell,
    which is the exact habit this exists to replace. One wrong default undid the
    whole tool.

    So: try the path, then walk UP (a subdirectory of a repo is still the repo),
    then look one level DOWN for a single obvious candidate. Say which was
    picked, because silently searching a different repository would be worse
    than finding none.
    """
    ok, _ = _git(start, "rev-parse", "--git-dir")
    if ok:
        return start, ""
    for parent in start.resolve().parents:
        ok, _ = _git(parent, "rev-parse", "--git-dir")
        if ok:
            return parent, f"(searched {parent}, the repository containing {start})"
        if parent == parent.parent or str(parent) in ("/", str(Path.home().parent)):
            break
    try:
        subs = [d for d in sorted(start.iterdir())
                if d.is_dir() and (d / ".git").exists()]
    except OSError:
        subs = []
    if len(subs) == 1:
        return subs[0], f"(no history at {start}; searched {subs[0].name}/ instead)"
    if len(subs) > 1:
        names = ", ".join(d.name for d in subs[:6])
        return subs[0], (f"(no history at {start}; several repositories below it "
                         f"[{names}] — searched {subs[0].name}/. Pass repo= to pick another.)")
    return None, ""

def when_changed(repo: str = ".", text: str = "", path: str = "",
                 since: str = "", until: str = "", regex: bool = False,
                 limit: int = MAX_HITS) -> str:
    """When did this enter or leave the code? Oldest first, dates on every line."""
    p = Path(str(repo or ".")).expanduser()
    if not p.exists():
        return f"No such directory: {p}"
    given = p
    p, note = _find_repo(p)
    # If the repository turned out to be BELOW where she pointed, a path she
    # wrote relative to that place no longer resolves. Same class of fault as
    # the default that broke this tool on its first real use: correct call,
    # wrong frame of reference, silent empty answer.
    if p is not None and path:
        try:
            rel = Path(path)
            if not rel.is_absolute() and p != given and given in p.parents:
                trimmed = p.relative_to(given)
                sp = str(rel)
                if sp.startswith(str(trimmed) + "/"):
                    path = sp[len(str(trimmed)) + 1:]
        except Exception:
            pass
    if p is None:
        return (f"No git repository at {Path(str(repo or '.')).expanduser()} or "
                f"just below it, so there is no history to search here. If the "
                f"code came as a copy without its history, say so — that is a "
                f"finding, not a dead end.")
    if not text and not path:
        return ("Give it something to look for: text= to find when a string "
                "appeared or vanished (the sharpest question you can ask a "
                "history), or path= to see how one file changed over time.")

    args = ["log", "--reverse", f"--max-count={max(1, min(int(limit), 200))}",
            "--date=short", "--pretty=format:%h\t%ad\t%an\t%s"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if text:
        args.append(("-G" if regex else "-S") + text)
        args.append("--pickaxe-all" if not regex else "--all-match")
    if path:
        args += ["--follow", "--", path] if not text else ["--", path]

    ok, out = _git(p, *args)
    if not ok:
        return f"git could not run that: {out[:300]}"
    rows = [l for l in out.splitlines() if l.strip()]
    if not rows:
        what = f"the text {text!r}" if text else f"the path {path!r}"
        return (f"No commit in that range touches {what}. That is an ANSWER, not a "
                f"dead end: it was never there, or it changed outside the window. "
                f"Widen the dates, or search for a different string from the "
                f"broken behaviour.")

    head = []
    if note:
        head.append(note)
    if text:
        head.append(f"Commits where {text!r} appeared or vanished"
                    + (f" in {path}" if path else "") + ":")
    else:
        head.append(f"Commits touching {path}:")
    head.append("OLDEST FIRST — if you know a date it still worked, the first row "
                "AFTER that date is your suspect.")
    head.append("")
    for l in rows:
        parts = l.split("\t")
        if len(parts) == 4:
            h, d, a, s = parts
            head.append(f"  {d}  {h}  {s[:88]}")
        else:
            head.append("  " + l[:100])
    head.append("")
    head.append(f"{len(rows)} commit(s). Read the suspect in full with "
                f"`git -C {p} show <hash>` — the whole diff, not just the message.")
    return "\n".join(head)


# ---- the whole history at once, instead of guessing which commits to read ----

_KIND = (
    ("build/packaging", r"(^|/)(Makefile|CMakeLists|.*\.xcconfig|.*\.pbxproj|"
                        r"Package\.swift|setup\.py|pyproject\.toml|.*\.gradle|"
                        r"Dockerfile|.*\.entitlements)$|(^|/)Scripts?/|\.sh$"),
    ("manifest/config", r"(^|/)(Info\.plist|.*\.plist|.*\.ya?ml|.*\.json|.*\.toml|"
                        r".*\.ini|.*\.cfg|.*\.conf)$"),
    ("docs",            r"(^|/)(README|CHANGELOG|DEPLOY\w*|HANDOFF|docs?/).*|\.md$"),
    ("tests",           r"(^|/)(tests?|spec)/|_test\.|test_"),
)


def _kind_of(path: str) -> str:
    for name, pat in _KIND:
        if re.search(pat, path, re.I):
            return name
    return "source"


def survey(repo: str = ".", since: str = "", until: str = "", limit: int = 200) -> str:
    """Every commit, oldest first, labelled by WHAT KIND of thing it changed.

    Built 2026-09-09 after three bug-hunt runs failed the same way. The method
    she is coached to use needs a date it last worked, and in a real repository
    there often isn't one — so "oldest first, take the row after that date" gave
    her nothing to anchor on and she took the most recent instead.

    This removes the need for the anchor. Forty-three commits is a readable
    number; the problem was never the size of the history, it was that she
    picked three commits by which MESSAGE sounded relevant and re-read them
    forty times.

    And it labels the kind, because of what the misses have in common: the
    commit holding the answer changed a BUILD SCRIPT, and she reads source. A
    fault that survives a clean rebuild usually lives in how the thing is built,
    stamped or packaged — the files everyone scrolls past.
    """
    p = Path(str(repo or ".")).expanduser()
    if not p.exists():
        return f"No such directory: {p}"
    p, note = _find_repo(p)
    if p is None:
        return f"No git repository at or below {repo}."

    args = ["log", "--reverse", f"--max-count={max(1, min(int(limit), 500))}",
            "--date=short", "--pretty=format:@@%h\t%ad\t%s", "--name-only"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    ok, out = _git(p, *args, timeout=120)
    if not ok:
        return f"git could not run that: {out[:300]}"

    commits, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@"):
            if cur:
                commits.append(cur)
            h, d, s = (line[2:].split("\t", 2) + ["", ""])[:3]
            cur = {"h": h, "d": d, "s": s, "files": []}
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip())
    if cur:
        commits.append(cur)
    if not commits:
        return "No commits in that range."

    lines = []
    if note:
        lines.append(note)
    lines.append(f"ALL {len(commits)} commit(s), oldest first. The KIND column is the "
                 f"point: a fault that survives a clean rebuild usually lives in how "
                 f"the thing is BUILT, stamped or packaged, not in the source — and "
                 f"those are the files everyone scrolls past.")
    lines.append("")
    tally = {}
    for c in commits:
        kinds = sorted({_kind_of(f) for f in c["files"]}) or ["(no files)"]
        for k in kinds:
            tally[k] = tally.get(k, 0) + 1
        tag = ",".join(kinds)[:28]
        lines.append(f"  {c['d']}  {c['h']}  [{tag:<28}] {c['s'][:62]}")
    lines.append("")
    lines.append("  by kind: " + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))

    # A closing paragraph of advice is not enough. Measured across four runs
    # (2026-09-09): she opens with this survey, reads the advice, then reads
    # twelve source files and names a cause anyway — `when_changed` used ZERO
    # times, `rule_out` ZERO times, and run 4 ended by EDITING the repo to
    # apply a confidently wrong fix. Her own code already carries the lesson
    # from an earlier round: a rule in the system prompt does not reach the
    # moment of action. So the survey now does the shortlisting itself and
    # hands back the literal next commands instead of describing them.
    shortlist = [c for c in commits
                 if any(_kind_of(f) in ("build/packaging", "manifest/config")
                        for f in c["files"])]
    lines.append("")
    if shortlist:
        lines.append(f"  THE SHORT LIST — {len(shortlist)} commit(s) touched how this is "
                     f"BUILT, STAMPED or PACKAGED. A fault that survives a clean rebuild "
                     f"lives here, and these are the files everyone scrolls past:")
        # Print the WHOLE short list. The first version of this capped the
        # display at 12 — and on the very repository this was built for, the
        # guilty commit sits at position 13 of 19. A helper that looks right
        # and silently hides the answer is worse than no helper. Only a truly
        # unwieldy list gets cut, and then it says so loudly.
        for c in shortlist[:60]:
            lines.append(f"    {c['d']}  {c['h']}  {c['s'][:64]}")
        if len(shortlist) > 60:
            lines.append(f"    … {len(shortlist) - 60} MORE NOT SHOWN — narrow with "
                         f"since=/until= and run this again; do not assume the cause "
                         f"is in the part you can see.")
        lines.append("")
        lines.append("  DO THESE NEXT, IN THIS ORDER. Do not read source files first —")
        lines.append("  that is the move that has failed this job four times running.")
        # NOT show_commit — no such tool. She does this with run_command, and
        # naming a tool that does not exist is the exact mistake that burned
        # two 45-minute runs earlier today.
        lines.append(f"    1. run_command('git show {shortlist[0]['h']}')   "
                     f"— and every other hash on the short list. The WHOLE diff.")
        lines.append("    2. when_changed(text='<a literal string from the BROKEN "
                     "BEHAVIOUR>') — the error text, the setting name, the symptom. "
                     "This dates the break. You have not run it yet.")
        lines.append("    3. rule_out('<theory>', because='<the evidence that killed "
                     "it>') — starting with whatever THE DOCUMENTATION claims, which "
                     "is the least-tested theory in any repository.")
        lines.append("    Name no cause until at least TWO theories are struck off. "
                     "'I don't know, here is what I eliminated' scores ABOVE a "
                     "confident wrong cause, and costs nobody a wrong fix.")
    else:
        lines.append("  No build/packaging or manifest commit in this range — widen it "
                     "before falling back to a source-level theory.")
    return "\n".join(lines)
