"""Where am I, what can I reach, and what is out of bounds.

2026-09-19. Asked to summarise a project from a workspace that did not contain
it, she described a different project in confident detail rather than saying
she could not see it. Part of that was not knowing what she could see. This
answers that in facts: the folder she is standing in, the projects under it,
which machines and mounts are live, which of her tools reach outside this box,
and the things she cannot do at all.

Everything here is read at call time. Nothing is remembered between sessions —
which is itself worth her knowing, and the report says so.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

# Projects announce themselves by a marker file; the notes are what she should
# read before describing any of them.
MARKERS = {
    "project.godot": "Godot game",
    "package.json": "node project",
    "pyproject.toml": "python project",
    "Cargo.toml": "rust project",
    ".git": "git repo",
}
NOTES = ("FORGE-NOTES.md", "CLAUDE.md", "HANDOFF.md", "README.md")
MAX_PROJECTS = 30


def _du(path: Path) -> str:
    try:
        t = shutil.disk_usage(path)
        return f"{t.free // 2**30} GB free of {t.total // 2**30} GB"
    except Exception:
        return "?"


def _mounted(p: Path) -> bool:
    try:
        return p.is_dir() and os.path.ismount(p)
    except Exception:
        return False


def _reachable(host: str, timeout: float = 1.5) -> bool:
    try:
        socket.create_connection((host, 22), timeout=timeout).close()
        return True
    except Exception:
        return False


def whereami(workspace: str = "") -> str:
    ws = Path(workspace).expanduser() if workspace else Path.cwd()
    home = Path.home()
    out: list[str] = []

    out.append(f"WORKSPACE: {ws}" + ("" if ws.is_dir() else "  (does not exist)"))
    out.append(f"  your file tools work inside this folder only; paths outside it are refused.")
    out.append(f"  this machine: {socket.gethostname()}  ·  {_du(ws if ws.is_dir() else home)}")

    # --- projects under the workspace
    found: list[str] = []
    if ws.is_dir():
        seen: set[Path] = set()
        for marker in MARKERS:
            hits = []
            # 2026-09-19: this only ever looked BELOW the workspace, so standing
            # inside a project — the commonest case, and the one where she most
            # needs to know what she is in — reported "PROJECTS: none found".
            # The workspace itself is the first candidate.
            if (ws / marker).exists():
                hits.append(ws / marker)
            for depth in ("*", "*/*", "*/*/*"):
                hits += list(ws.glob(f"{depth}/{marker}"))[:MAX_PROJECTS]
            for hit in hits:
                d = hit.parent
                if d in seen or any(part.startswith(".") for part in d.relative_to(ws).parts):
                    continue
                seen.add(d)
                notes = [n for n in NOTES if (d / n).is_file()]
                kinds = [MARKERS[m] for m in MARKERS if (d / m).exists()]
                try:
                    age = d.stat().st_mtime
                except Exception:
                    age = 0
                rel = d.relative_to(ws)
                # "." tells her nothing; name the folder she is standing in.
                where = f"{d.name}/  ← YOU ARE IN THIS ONE" if str(rel) == "." else str(rel)
                found.append((age, f"  {where}  [{', '.join(kinds)}]"
                              + (f"  notes: {', '.join(notes)}" if notes else "  (no notes file)")))
        found.sort(reverse=True)            # most recently touched first: live work at the top
        found = [t for _, t in found]
    out.append("")
    if found:
        out.append(f"PROJECTS I CAN SEE ({len(found)}, most recently touched first):")
        out.extend(found[:MAX_PROJECTS])
        if len(found) > MAX_PROJECTS:
            out.append(f"  … and {len(found) - MAX_PROJECTS} older ones (list_dir to see them)")
        out.append("  Before describing any of these, READ its notes file. You have no memory")
        out.append("  of other sessions, so anything you 'recall' about a project is invented.")
    else:
        out.append("PROJECTS: none found under this workspace.")
        out.append("  If you are asked about a project you cannot see here, say exactly that.")

    # --- what was actually done, from the harness's own record
    try:
        from . import worklog
        out.append("")
        out.append(worklog.recent(limit=8))
    except Exception:
        pass

    # --- the other machine and the mounts
    out.append("")
    out.append("BEYOND THIS FOLDER:")
    old = _reachable("10.42.0.1")
    out.append(f"  old box (pop-os, 10.42.0.1): {'reachable over ssh' if old else 'NOT reachable'}"
               "  — Godot, Mote, ComfyUI, renders live there")
    for m, what in (("titan-home", "its home"), ("titan-big", "/srv/big"),
                    ("titan-mid", "/srv/mid"), ("titan-wd", "/srv/wd")):
        p = home / m
        out.append(f"  ~/{m}: {'mounted' if _mounted(p) else ('present but NOT mounted' if p.is_dir() else 'missing')}  ({what})")

    # --- what the tools actually reach
    out.append("")
    out.append("WHAT MY TOOLS REACH:")
    out.append("  read_file / write_file / edit_file / list_dir / search: inside the workspace only.")
    out.append("  run_command: this machine, as the owner, no sudo password — it can reach the")
    out.append("    old box with ssh 10.42.0.1 and can touch files outside the workspace.")
    out.append("  godot_* : the Godot project on the OLD box (paths are translated for you).")
    out.append("  machine_state: RAM, GPU, disk on both boxes.")

    out.append("")
    out.append("WHAT I CANNOT DO:")
    out.append("  remember anything from another chat (only this workspace's notes and my notebook),")
    out.append("  sudo anything (no passwordless root on either box — that is his to run),")
    out.append("  install my own checkers into the harness (I propose; a human installs),")
    out.append("  see his screen, or know what he did outside these chats.")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    print(whereami(sys.argv[1] if len(sys.argv) > 1 else str(Path.home())))
