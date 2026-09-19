"""Godot 4.7.2 for Merge: read the real class reference, run the real engine.

Written 2026-09-18 after her first Godot pass guessed six API facts wrong while
810 class-reference files sat unread next to her. Three tools:

  godot_docs   — the exact signature/properties/signals of a class or member,
                 straight from the 4.7.2 XML class reference on this box.
  godot_check  — gd_lint (every JS-habit site at once, no engine) and then
                 Godot's own --check-only pass over a GDScript file.
  godot_run    — import / headless run / export of a project on the old box,
                 returning the real output and the ERROR lines.
  gd_lint      — the instant checks on their own: JS habits (forge/gdlint.py)
                 and wrong-class API names (forge/gdapi.py), for re-checking
                 after edits.
  godot_verify — ONE verdict: lint + API names + parser on every changed file,
                 every yardstick, and the bisect when something fails. Runs
                 itself at the end of any turn that wrote GDScript (2026-09-19:
                 "if checking is cheap, make a super checker that is dumb and
                 never screws up").
  godot_bisect — which uncommitted hunk makes a yardstick fail; replays them
                 one at a time against the real test (2026-09-19).
  godot_test   — run one yardstick (tests/<name>_test.gd) and report the
                 PASS line, script errors grouped, first failing cases.
                 Added 2026-09-18 so she can close the write→check→test→fix
                 loop herself instead of handing "done" to a reviewer.

Paths: the old box's home is mounted here at ~/titan-home. Tools accept either
the mount path (/home/tabulanis/titan-home/...) or the old-box path
(/home/tabulanis/...) and translate. Relative paths are refused on purpose —
that is how her first pass landed inside her own workspace.
"""
from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

DOCS = Path.home() / "godot-docs" / "classes-4.7.2"
OLD_BOX = "10.42.0.1"
GODOT = "~/bin/godot"
MOUNT = str(Path.home() / "titan-home")
OLD_HOME = "/home/tabulanis"
MAX_OUT = 4000


# ---------------------------------------------------------------- docs ------

def _class_file(name: str) -> Path | None:
    if not DOCS.exists():
        return None
    want = name.lower()
    for p in DOCS.glob("*.xml"):
        if p.stem.lower() == want:
            return p
    return None


def _text(el) -> str:
    return " ".join((el.text or "").split()) if el is not None else ""


def _fmt_method(m) -> str:
    ret = m.find("return")
    rtype = ret.get("type") if ret is not None else "void"
    params = []
    for p in sorted(m.findall("param"), key=lambda x: int(x.get("index", "0"))):
        d = p.get("default")
        params.append(f"{p.get('name')}: {p.get('type')}" + (f" = {d}" if d is not None else ""))
    quals = m.get("qualifiers", "")
    line = f"{rtype} {m.get('name')}({', '.join(params)})" + (f" {quals}" if quals else "")
    desc = _text(m.find("description"))
    return line + ("\n    " + desc[:300] if desc else "")


def _fmt_property(p) -> str:
    d = p.get("default")
    line = f"{p.get('type')} {p.get('name')}" + (f" = {d}" if d is not None else "")
    desc = " ".join((p.text or "").split())
    return line + ("\n    " + desc[:300] if desc else "")


def _fmt_signal(s) -> str:
    params = [f"{p.get('name')}: {p.get('type')}" for p in s.findall("param")]
    return f"signal {s.get('name')}({', '.join(params)})"


def _grep_member(member: str, limit: int = 20) -> str:
    """No class given / not found: which classes have a member by this name?"""
    hits = []
    pat = re.compile(rf'name="{re.escape(member)}"', re.I)
    for p in sorted(DOCS.glob("*.xml")):
        try:
            txt = p.read_text(errors="replace")
        except OSError:
            continue
        if pat.search(txt):
            hits.append(p.stem)
            if len(hits) >= limit:
                break
    if not hits:
        return f"No class or member named '{member}' in the 4.7.2 reference."
    return (f"'{member}' exists on: " + ", ".join(hits)
            + "\nAsk again as Class.member for the signature.")


def godot_docs(query: str) -> str:
    """Look up a Godot 4.7.2 class, or one member of it, in the official reference."""
    if not DOCS.exists():
        return f"Class reference not found at {DOCS}. It lives on the old box at ~/godot/docs/classes-4.7.2."
    q = (query or "").strip().strip("()")
    if not q:
        return "Give me a class name (CharacterBody3D) or Class.member (CharacterBody3D.move_and_slide)."
    cls, _, member = q.partition(".")
    f = _class_file(cls)
    if f is None:
        return _grep_member(cls if not member else member)
    try:
        root = ET.parse(f).getroot()
    except ET.ParseError as e:
        return f"Could not parse {f.name}: {e}"

    name = root.get("name")
    inherits = root.get("inherits", "")
    out = [f"{name}" + (f" (inherits {inherits})" if inherits else "")]
    brief = _text(root.find("brief_description"))
    if brief:
        out.append(brief[:400])

    methods = root.find("methods")
    props = root.find("members")
    signals = root.find("signals")
    consts = root.find("constants")

    if member:
        m = member.lower()
        found = False
        for sec, fmt, label in ((methods, _fmt_method, "method"),
                                (props, _fmt_property, "property"),
                                (signals, _fmt_signal, "signal")):
            if sec is None:
                continue
            for el in sec:
                if el.get("name", "").lower() == m:
                    out.append(f"\n[{label}]\n" + fmt(el))
                    found = True
        if consts is not None:
            for el in consts:
                if el.get("name", "").lower() == m:
                    out.append(f"\n[constant] {el.get('name')} = {el.get('value')}  " + _text(el)[:200])
                    found = True
        if not found:
            names = [el.get("name") for sec in (methods, props, signals) if sec is not None for el in sec]
            close = [n for n in names if m in n.lower()][:12]
            out.append(f"\nNo member '{member}' on {name}."
                       + (f" Close names: {', '.join(close)}" if close else "")
                       + (f"\nIt may be inherited — try {inherits}.{member}" if inherits else ""))
        return "\n".join(out)[:MAX_OUT]

    # Whole class: list members compactly.
    if methods is not None and len(methods):
        out.append("\nMETHODS:")
        for el in methods:
            out.append("  " + _fmt_method(el).split("\n")[0])
    if props is not None and len(props):
        out.append("\nPROPERTIES:")
        for el in props:
            out.append("  " + _fmt_property(el).split("\n")[0])
    if signals is not None and len(signals):
        out.append("\nSIGNALS:")
        for el in signals:
            out.append("  " + _fmt_signal(el))
    text = "\n".join(out)
    if len(text) > MAX_OUT:
        text = text[:MAX_OUT] + f"\n… (truncated; ask for {name}.<member> for details)"
    return text


# ------------------------------------------------------------- engine -------

def _old_box_path(path: str) -> str | None:
    """Translate a Void mount path to the old box's own path. None if unusable."""
    p = (path or "").strip()
    if not p.startswith("/"):
        return None
    if p.startswith(MOUNT + "/"):
        return OLD_HOME + p[len(MOUNT):]
    if p == MOUNT:
        return OLD_HOME
    if p.startswith(OLD_HOME + "/") or p == OLD_HOME:
        return p
    return None


def _ssh(cmd: str, timeout: int = 150) -> tuple[int, str]:
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", OLD_BOX, cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"(timed out after {timeout}s — Godot hung, or the old box is unreachable)"
    except Exception as e:  # noqa: BLE001
        return 1, f"(ssh failed: {e})"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _tidy(out: str) -> str:
    out = _ANSI.sub("", out)
    lines = [l for l in out.splitlines() if l.strip()]
    errs = [l for l in lines if re.search(r"\b(ERROR|SCRIPT ERROR|Parse Error|Failed|error)\b", l)]
    body = "\n".join(lines[-40:])
    if len(body) > MAX_OUT:
        body = body[-MAX_OUT:]
    head = ("ERROR LINES:\n" + "\n".join(errs[:15]) + "\n---\n") if errs else "no ERROR lines\n---\n"
    return head + body


def _mount_path(script_path: str):
    """The file as reachable from THIS box: mount paths as-is, old-box paths via the mount."""
    p = str(script_path or "")
    if p.startswith(MOUNT + "/"):
        return p
    if p.startswith(OLD_HOME + "/"):
        return MOUNT + "/" + p[len(OLD_HOME) + 1:]
    return None


def gd_lint(script_path: str) -> str:
    """Two instant checks on one GDScript file, no engine: JS habits (gdlint)
    and API names that do not exist on the class they are used on (gdapi —
    added 2026-09-18, because every human fix to her Godot code that day was a
    name written from memory instead of looked up)."""
    from . import gdlint, gdapi
    mp = _mount_path(script_path)
    if mp is None:
        return ("Give an absolute path under /home/tabulanis/titan-home/... (the mount) "
                "or /home/tabulanis/... on the old box.")
    if not Path(mp).is_file():
        return f"No such file: {mp}"
    parts = [x for x in (gdlint.lint(mp), gdapi.check(mp)) if x]
    return "\n\n".join(parts) if parts else f"GDLINT {Path(mp).name}: clean"


def godot_check(script_path: str, project_dir: str = "") -> str:
    """gd_lint first (all JS-habit sites at once), then Godot's --check-only pass on the old box."""
    sp = _old_box_path(script_path)
    if sp is None:
        return ("Give an absolute path under /home/tabulanis/titan-home/... (the mount) "
                "or /home/tabulanis/... on the old box. Relative paths are refused.")
    pd = _old_box_path(project_dir) if project_dir else str(Path(sp).parent)
    if pd is None:
        return "project_dir must be absolute (mount path or old-box path)."
    # 2026-09-18: Godot reports ONE parse error per run; a JS-habit pass that
    # lists every site first turned a six-cycle fix into two.
    lint_out = gd_lint(script_path)
    rc, out = _ssh(f"cd '{pd}' && {GODOT} --headless --check-only -s '{sp}' 2>&1", timeout=90)
    head = "" if lint_out.endswith(": clean") else lint_out + "\n\n"
    return head + f"[exit {rc}] check-only {Path(sp).name}\n" + _tidy(out)


def godot_verify(project_dir: str, timeout: int = 300) -> str:
    """Everything the machine can check, in one verdict: the habit lint and the
    API-name check on every uncommitted .gd, the engine's parser on each, every
    yardstick in tests/, and — when one file is uncommitted and something fails
    — the bisect, automatically. The harness runs this at the end of any turn
    that wrote a .gd, so 'done' is never just an opinion."""
    from . import verify as _verify

    def _bisect(pd: str, test: str, script: str) -> str:
        return godot_bisect(pd, test, script_path=script)

    return _verify.verify(project_dir, _ssh, GODOT, _old_box_path, _mount_path,
                          gd_lint, bisect_fn=_bisect, timeout=timeout)


def godot_bisect(project_dir: str, test: str, script_path: str = "", timeout: int = 150) -> str:
    """Which of my uncommitted edits makes this yardstick fail? Brute force:
    the committed file is the baseline, each hunk is replayed alone, the real
    test runs after each. No theory, so no wrong theory. The working file is
    saved first and restored whatever happens."""
    from . import gdbisect
    pd = _old_box_path(project_dir)
    mount = _mount_path(project_dir)
    if pd is None or mount is None:
        return ("project_dir must be absolute: /home/tabulanis/titan-home/gamedev/<name> "
                "(mount) or /home/tabulanis/gamedev/<name> (old box).")
    name = re.sub(r"[^a-z0-9_]", "", (test or "").lower().replace("_test.gd", "").replace(".gd", ""))
    if not name:
        return "test must be a name like world, actor, viewport (tests/<name>_test.gd)."
    n = max(10, min(int(timeout or 150), 600))

    def run_test(_project: str, t: str):
        rc, out = _ssh(f"cd '{pd}' && timeout {n} {GODOT} --headless -s 'tests/{t}_test.gd' 2>&1",
                       timeout=n + 30)
        line = next((l for l in out.splitlines() if l.startswith("PASS ") or l.startswith("FATAL")),
                    f"[exit {rc}] no PASS line")
        m = re.search(r"FAIL (\d+)", line)
        return (rc == 0 and m is not None and int(m.group(1)) == 0), line

    return gdbisect.bisect(mount, name, script_path=script_path, run_test=run_test)[:MAX_OUT]


def godot_test(project_dir: str, test: str, timeout: int = 120) -> str:
    """Run one yardstick (tests/<test>_test.gd, a SceneTree script) on the old box and
    report it the way a reviewer would: the PASS line, script errors grouped by message
    and site, and the first failing cases with expected/got."""
    pd = _old_box_path(project_dir)
    if pd is None:
        return ("project_dir must be absolute: /home/tabulanis/titan-home/gamedev/<name> "
                "(mount) or /home/tabulanis/gamedev/<name> (old box).")
    name = re.sub(r"[^a-z0-9_]", "", (test or "").lower().replace("_test.gd", "").replace(".gd", ""))
    if not name:
        return "test must be a name like intent, playbook, harness (tests/<name>_test.gd)."
    n = max(10, min(int(timeout or 120), 600))
    rc, out = _ssh(f"cd '{pd}' && timeout {n} {GODOT} --headless -s 'tests/{name}_test.gd' 2>&1", timeout=n + 30)
    lines = out.splitlines()
    pass_line = next((l for l in lines if l.startswith("PASS ") or l.startswith("FATAL")), None)
    # script errors: message → count, and the first "at:" site per message
    errs: dict[str, list] = {}
    for i, l in enumerate(lines):
        if l.startswith("SCRIPT ERROR:"):
            msg = l[len("SCRIPT ERROR:"):].strip()
            site = next((s.strip() for s in lines[i + 1:i + 4] if s.strip().startswith("at:")), "")
            errs.setdefault(msg, [0, site])[0] += 1
    # failure blocks: "  [label]" followed by indented detail lines
    fails, cur = [], None
    for l in lines:
        if l.startswith("  [") and l.rstrip().endswith("]"):
            cur = [l.strip()]; fails.append(cur)
        elif cur is not None and l.startswith("      "):
            cur.append(l.strip()[:220])
        elif cur is not None and not l.startswith(" "):
            cur = None
    rep = [f"[exit {rc}] tests/{name}_test.gd" + (f" (killed at {n}s — a hang or a runtime error that never reached quit())" if rc == 124 else "")]
    rep.append(pass_line or "(no PASS line — the runner did not finish; see errors)")
    if errs:
        rep.append(f"\nSCRIPT ERRORS ({sum(v[0] for v in errs.values())} total, {len(errs)} kinds) — fix these first, they abort turns:")
        for msg, (cnt, site) in sorted(errs.items(), key=lambda kv: -kv[1][0])[:10]:
            rep.append(f"  {cnt}× {msg[:150]}" + (f"\n       {site[:120]}" if site else ""))
    if fails:
        rep.append(f"\nFAILING CASES ({len(fails)}; first {min(6, len(fails))} shown):")
        for blk in fails[:6]:
            rep.extend("  " + x for x in blk[:6])
    if not errs and not fails and pass_line and " FAIL 0" in pass_line:
        rep.append("Clean. Paste this PASS line in your report.")
    return "\n".join(rep)[:MAX_OUT]


def godot_run(project_dir: str, action: str = "run", quit_after: int = 30) -> str:
    """Import, headless-run, or export a Godot project on the old box; return real output."""
    pd = _old_box_path(project_dir)
    if pd is None:
        return ("project_dir must be absolute: /home/tabulanis/titan-home/gamedev/<name> "
                "(mount) or /home/tabulanis/gamedev/<name> (old box).")
    action = (action or "run").lower()
    if action == "import":
        cmd = f"cd '{pd}' && {GODOT} --headless --import 2>&1"
    elif action == "run":
        n = max(1, min(int(quit_after or 30), 600))
        cmd = f"cd '{pd}' && {GODOT} --headless --quit-after {n} 2>&1"
    elif action == "export":
        name = Path(pd).name
        preset = "/home/tabulanis/aidojo/shared/godot-linux-preset.cfg"
        cmd = (f"cd '{pd}' && [ -f export_presets.cfg ] || "
               f"scp -q Void:{preset} export_presets.cfg 2>/dev/null; "
               f"mkdir -p build && {GODOT} --headless --export-release Linux build/{name}.x86_64 2>&1; "
               f"ls -la build/{name}.x86_64 2>&1")
    else:
        return "action must be one of: import, run, export"
    rc, out = _ssh(cmd, timeout=240 if action == "export" else 150)
    return f"[exit {rc}] godot {action} in {pd}\n" + _tidy(out)


SELFTEST = """
out = godot_docs('CharacterBody3D.move_and_slide')
assert 'move_and_slide' in out and 'bool' in out, out
out2 = godot_docs('RectangleShape2D')
assert 'Shape2D' in out2, out2
assert godot_run('gamedev/penpal').startswith('project_dir must be absolute')
"""
