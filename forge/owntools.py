"""Tools Merge writes for herself — created, PROVEN, and usable the same turn.

Until 2026-09-08 she could not extend herself at all. There was no supported way
to create a tool, no way to load one without restarting her service, and nothing
that could prove a tool worked before she trusted it. Her only checker read code
for syntax errors and never ran it.

The pattern is not new — her sim shelf has done create → self-test → shelve for
weeks, and it works. This is that pattern applied to real tools:

    build_tool(name, code)   write it, prove it against its own SELFTEST,
                             install it, and put it on her belt NOW
    test_tool(name)          re-run one tool's SELFTEST
    list_my_tools()          what she has made, and which are proven
    remove_tool(name)        take one back off the shelf

A TOOL FILE looks like this and lives in ~/.forge/shelf/tools/<name>.py

    SELFTEST = [
        {"args": {"text": "hello"}, "expect": "HELLO"},
    ]

    def run(text: str) -> str:
        return text.upper()

    TOOL = {
        "name": "shout",
        "description": "Upper-case some text.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    }

Two rules, both learned the hard way elsewhere in this codebase:

  * NO SELFTEST, NO BELT. A tool that has never been run is a guess. The sim
    shelf keeps un-tested work as EXPERIMENTAL; a tool that could be called by
    mistake is worse than a number that might be wrong, so an untested tool is
    saved but never loaded.
  * VALIDATE IN A SUBPROCESS. Her own code, written mid-conversation, must not
    be able to take her down by looping, exiting, or blowing up on import.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from .paths import SHELF_DIR

TOOLS_DIR = SHELF_DIR / "tools"
_NAME_OK = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# A tool she wrote may not take a name her core already owns, for the same
# reason a project may not: which one answers would depend on ordering.
_RESERVED: set[str] = set()

# The RUNNING agent's tool dict. build_tool drops a proven tool straight in, so
# she can call it on the same turn she wrote it. Without this she would have to
# wait for a restart, which is exactly what made self-extension impossible.
_LIVE: dict = {}


def set_live(tools: dict) -> None:
    global _LIVE
    _LIVE = tools


def set_reserved(names) -> None:
    """The names her core and her projects already hold."""
    _RESERVED.clear()
    _RESERVED.update(names or ())


_CHECKER = r'''
import importlib.util, json, sys, traceback
spec = importlib.util.spec_from_file_location("candidate", sys.argv[1])
m = importlib.util.module_from_spec(spec)
out = {"ok": False, "cases": [], "error": ""}
try:
    spec.loader.exec_module(m)
    meta = getattr(m, "TOOL", None)
    run = getattr(m, "run", None)
    tests = getattr(m, "SELFTEST", None)
    if not isinstance(meta, dict) or not callable(run):
        out["error"] = "needs a TOOL dict and a run() function"
    elif not isinstance(tests, list) or not tests:
        out["error"] = "no SELFTEST"
    else:
        for i, case in enumerate(tests, 1):
            args = case.get("args", {}) if isinstance(case, dict) else {}
            want = case.get("expect") if isinstance(case, dict) else None
            try:
                got = run(**args)
                passed = (got == want)
                out["cases"].append({"n": i, "pass": bool(passed),
                                     "got": repr(got)[:200], "want": repr(want)[:200]})
            except Exception as e:
                out["cases"].append({"n": i, "pass": False,
                                     "got": f"{type(e).__name__}: {e}"[:200],
                                     "want": repr(want)[:200]})
        out["ok"] = all(c["pass"] for c in out["cases"])
        out["meta"] = {"name": meta.get("name"), "description": meta.get("description"),
                       "parameters": meta.get("parameters")}
except Exception:
    out["error"] = traceback.format_exc(limit=3)[-400:]
print(json.dumps(out))
'''


def _validate(path: Path, timeout: int = 25) -> dict:
    """Run the candidate's SELFTEST in a separate process. Never in ours."""
    try:
        r = subprocess.run([sys.executable, "-c", _CHECKER, str(path)],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"its self-test ran longer than {timeout}s "
                                      f"— a tool that hangs is a tool that hangs her"}
    line = (r.stdout or "").strip().splitlines()
    if not line:
        return {"ok": False, "error": (r.stderr or "no output")[-400:]}
    try:
        return json.loads(line[-1])
    except Exception:
        return {"ok": False, "error": (r.stdout or r.stderr)[-400:]}


def _load_one(path: Path, ws=None):
    """Turn a proven tool file into a real Tool. Returns (tool, note)."""
    from .tools import Tool
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(f"forge_own_tools.{path.stem}", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        meta, run = getattr(m, "TOOL", None), getattr(m, "run", None)
        if not isinstance(meta, dict) or not callable(run):
            return None, f"{path.name}: not a tool file"
        name = str(meta.get("name") or path.stem)
        if name in _RESERVED:
            return None, f"{path.name}: refused — '{name}' is already one of her own"
        return Tool(name=name,
                    description=str(meta.get("description") or "")[:800],
                    parameters=meta.get("parameters") or {"type": "object", "properties": {}},
                    run=run), f"{path.name}: {name}"
    except Exception as e:
        return None, f"{path.name}: skipped — {type(e).__name__}: {e}"


def load_own(ws=None) -> tuple[list, list[str]]:
    """Every tool she has made and proven. Never raises."""
    found, notes = [], []
    if not TOOLS_DIR.is_dir():
        return found, notes
    for f in sorted(TOOLS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        t, note = _load_one(f, ws)
        if t:
            found.append(t)
        notes.append(note)
    return found, notes


def build_tool(name: str, code: str, live_registry=None) -> str:
    """Write a tool, PROVE it against its own SELFTEST, and put it on her belt."""
    name = str(name or "").strip().lower()
    if not _NAME_OK.match(name):
        return ("Give it a short lower-case name: letters, digits and underscores, "
                "starting with a letter.")
    if name in _RESERVED:
        return (f"'{name}' is already one of her tools. Pick another name — two tools "
                f"with one name means whichever answers is down to ordering.")
    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"Won't save — syntax error line {e.lineno}: {e.msg}. Fix it and resend."

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    staged = TOOLS_DIR / f"_staged_{name}.py"
    staged.write_text(code, encoding="utf-8")
    res = _validate(staged)

    if not res.get("ok"):
        cases = res.get("cases") or []
        lines = [f"NOT installed — '{name}' did not prove itself."]
        if res.get("error"):
            lines.append(f"  {res['error']}")
        for c in cases:
            if not c["pass"]:
                lines.append(f"  case {c['n']} FAILED: got {c['got']} — expected {c['want']}")
        if not cases and not res.get("error"):
            lines.append("  it has no SELFTEST, so there is nothing to trust. A tool that "
                         "has never run is a guess.")
        lines.append(f"  The file is kept at {staged} so you can fix it and resend.")
        return "\n".join(lines)

    final = TOOLS_DIR / f"{name}.py"
    staged.replace(final)
    tool, note = _load_one(final)
    if tool is None:
        return f"Proved its self-test but could not be loaded: {note}"
    added = False
    live_registry = _LIVE if live_registry is None else live_registry
    if live_registry is not None:
        live_registry[tool.name] = tool          # usable THIS turn, no restart
        added = True
    n = len(res.get("cases") or [])
    return (f"'{name}' proved itself ({n}/{n} self-test case(s) passed) and is "
            f"{'on your belt now — call it in this same turn' if added else 'installed'}. "
            f"Saved to {final}.")


def test_tool(name: str) -> str:
    """Re-run one tool's SELFTEST. Its own claim, checked again."""
    f = TOOLS_DIR / f"{str(name).strip().lower()}.py"
    if not f.is_file():
        return f"No tool of yours called '{name}'. list_my_tools shows what you have."
    res = _validate(f)
    if res.get("ok"):
        n = len(res.get("cases") or [])
        return f"'{name}' still holds: {n}/{n} self-test case(s) pass."
    lines = [f"'{name}' FAILS its own self-test now."]
    if res.get("error"):
        lines.append(f"  {res['error']}")
    for c in (res.get("cases") or []):
        if not c["pass"]:
            lines.append(f"  case {c['n']}: got {c['got']} — expected {c['want']}")
    return "\n".join(lines)


def list_my_tools() -> str:
    if not TOOLS_DIR.is_dir() or not any(TOOLS_DIR.glob("*.py")):
        return ("You haven't made any tools yet. build_tool(name, code) writes one, "
                "proves it against its own SELFTEST, and puts it on your belt the "
                "same turn.")
    out = []
    for f in sorted(TOOLS_DIR.glob("*.py")):
        if f.name.startswith("_staged_"):
            out.append(f"  ⚠ {f.stem[8:]} — staged, did NOT pass its self-test")
        elif not f.name.startswith("_"):
            t, note = _load_one(f)
            out.append(f"  ✓ {t.name} — {t.description[:70]}" if t else f"  ✗ {note}")
    return "Tools you have made:\n" + "\n".join(out)


def remove_tool(name: str) -> str:
    n = str(name).strip().lower()
    gone = []
    for f in (TOOLS_DIR / f"{n}.py", TOOLS_DIR / f"_staged_{n}.py"):
        if f.is_file():
            f.unlink()
            gone.append(f.name)
    return f"Removed {', '.join(gone)}." if gone else f"No tool of yours called '{n}'."
