"""Tools a PROJECT owns, loaded only while she is working in that project.

Merge is the builder. MoneyLab, Storyweave, Hackbeat and the rest are what she
builds. A project's specialist tooling belongs to the project, not to her: it
should travel with that repo, appear when she opens it, and be absent
everywhere else.

Until 2026-09-08 that line did not exist. A whole market-trading suite —
regime testing, walk-forward, cross-asset mapping, a signal scanner, paper
trading, an x-files investigator and a crypto news feed — sat in her core and
rode on her belt in every project she ever opened. Nobody put it there on
purpose; it accumulated, and two separate audits swept past it because every
file looked like one of hers.

There was an earlier attempt at this. forge/plugins.py described a tools(ws)
contract on 2026-09-06, was never wired to anything, and was deleted on
2026-09-08 when this landed — its registry functions had no callers (the render
box's stats and progress are read from renderbox directly), and leaving two
half-plugin systems in one codebase is worse than either.

HOW A PROJECT SHIPS TOOLS
    <workspace>/merge-tools/*.py

Each file defines:

    def tools(ws) -> list[Tool]

`ws` is her Workspace. Return real Tool objects. Anything that raises on
import is skipped with a note rather than taking her belt down with it — a
project's broken tool file must never stop her working.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TOOLS_DIRNAME = "merge-tools"


def project_tool_files(ws_root: Path) -> list[Path]:
    d = Path(ws_root) / TOOLS_DIRNAME
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.py") if not p.name.startswith("_"))


def load(ws) -> tuple[list, list[str]]:
    """Return (tools, notes). Never raises."""
    found, notes = [], []
    root = getattr(ws, "root", None)
    if root is None:
        return found, notes
    for f in project_tool_files(Path(root)):
        mod_name = f"forge_project_tools.{Path(root).name}.{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, f)
            if spec is None or spec.loader is None:
                raise ImportError("no loader")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            fn = getattr(mod, "tools", None)
            if not callable(fn):
                notes.append(f"{f.name}: no tools(ws) function — skipped")
                continue
            got = list(fn(ws) or [])
            found.extend(got)
            notes.append(f"{f.name}: {len(got)} tool(s)")
        except Exception as e:
            # A project's broken tool file must never stop her working.
            notes.append(f"{f.name}: skipped — {type(e).__name__}: {e}")
    return found, notes
