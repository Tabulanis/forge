"""
The agent's hands: read, write, edit, list, search, run.

Two rules shape everything here:

  1. Every path is resolved and checked against the workspace root before
     anything is read or written. An agent that can be talked into writing
     to /etc or reading ~/.ssh is a security hole, not a feature.
  2. Anything that changes the world (write, edit, run) declares itself
     as needing permission. The agent loop asks the user; tools never
     decide on their own that something is safe enough to skip the prompt.

Tools return plain strings — that's what goes back to the model. Errors are
returned as readable text rather than raised, so the model can read what
went wrong and correct itself instead of the run dying.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_READ_BYTES = 400_000     # a huge file would blow the context window
MAX_OUTPUT_CHARS = 30_000    # same, for command output
DEFAULT_TIMEOUT = 120


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict          # JSON Schema
    run: Callable[..., str]
    needs_permission: bool = False
    # A one-line human summary shown in the permission prompt, so the user
    # sees "write to config.py" rather than a wall of JSON.
    summarize: Callable[[dict], str] | None = None


class Workspace:
    """Everything the agent is allowed to touch, and nothing else."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, path: str) -> Path:
        """Resolve a user/model-supplied path, refusing anything outside root."""
        p = Path(path)
        full = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
        if full != self.root and self.root not in full.parents:
            raise PermissionError(
                f"Path is outside the workspace: {full}\n"
                f"The agent may only touch files under {self.root}"
            )
        return full

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)


def build_tools(ws: Workspace) -> list[Tool]:
    """Construct the toolset bound to one workspace."""

    def guard(fn):
        """Turn a workspace violation into readable text for the model.

        The sandbox itself raises — that's correct, it must be impossible to
        ignore. But a raise would abort the whole run, and the model would
        never learn why. Wrapping it here means the model reads 'that's
        outside the workspace' as a tool result and can adjust."""
        from functools import wraps

        @wraps(fn)
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except PermissionError as e:
                return f"Error: {e}"
        return inner

    def read_file(path: str, offset: int = 0, limit: int = 2000) -> str:
        f = ws.resolve(path)
        if not f.exists():
            return f"Error: no such file: {ws.rel(f)}"
        if f.is_dir():
            return f"Error: that's a directory, not a file: {ws.rel(f)}"
        if f.stat().st_size > MAX_READ_BYTES:
            return (f"Error: file is too large to read whole "
                    f"({f.stat().st_size} bytes). Use offset/limit to page through it.")
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            return f"Error reading {ws.rel(f)}: {e}"
        window = lines[offset:offset + limit]
        if not window:
            return f"(no lines in range; file has {len(lines)} lines)"
        # Line numbers help the model target edits precisely.
        body = "\n".join(f"{i + offset + 1:6d}\t{ln}" for i, ln in enumerate(window))
        more = ""
        if offset + limit < len(lines):
            more = f"\n... ({len(lines) - offset - limit} more lines; use offset={offset + limit})"
        return body + more

    def write_file(path: str, content: str) -> str:
        f = ws.resolve(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        existed = f.exists()
        f.write_text(content, encoding="utf-8")
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {ws.rel(f)} ({len(content.splitlines())} lines)"

    def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
        f = ws.resolve(path)
        if not f.exists():
            return f"Error: no such file: {ws.rel(f)}"
        text = f.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return (f"Error: that exact text isn't in {ws.rel(f)}. "
                    f"Read the file again — it may differ in whitespace or have changed.")
        if count > 1 and not replace_all:
            return (f"Error: found {count} matches in {ws.rel(f)}. "
                    f"Add more surrounding context to make it unique, or pass replace_all=true.")
        f.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1),
                     encoding="utf-8")
        return f"Edited {ws.rel(f)} ({count if replace_all else 1} replacement(s))"

    def list_dir(path: str = ".") -> str:
        d = ws.resolve(path)
        if not d.exists():
            return f"Error: no such directory: {ws.rel(d)}"
        if not d.is_dir():
            return f"Error: not a directory: {ws.rel(d)}"
        entries = []
        for item in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if item.name.startswith(".") and item.name not in (".env", ".gitignore"):
                continue
            entries.append(f"{item.name}/" if item.is_dir() else item.name)
        return "\n".join(entries) if entries else "(empty)"

    def search(pattern: str, path: str = ".", max_results: int = 60) -> str:
        """Content search. Uses ripgrep when present (fast), else Python."""
        d = ws.resolve(path)
        try:
            r = subprocess.run(
                ["rg", "--line-number", "--no-heading", "--color=never",
                 "--max-count=5", pattern, str(d)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode in (0, 1):
                hits = r.stdout.splitlines()[:max_results]
                return "\n".join(hits) if hits else "(no matches)"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # fall through to the pure-Python walk

        hits = []
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x != "node_modules"]
            for fn in files:
                fp = Path(root) / fn
                try:
                    for i, line in enumerate(fp.read_text(encoding="utf-8",
                                                          errors="ignore").splitlines(), 1):
                        if pattern in line:
                            hits.append(f"{ws.rel(fp)}:{i}:{line.strip()[:200]}")
                            if len(hits) >= max_results:
                                return "\n".join(hits)
                except Exception:
                    continue
        return "\n".join(hits) if hits else "(no matches)"

    def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        try:
            r = subprocess.run(
                command, shell=True, cwd=str(ws.root),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout}s: {command}"
        except Exception as e:
            return f"Error running command: {e}"
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        out = out.strip() or "(no output)"
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(out)} chars total)"
        return f"[exit {r.returncode}]\n{out}"

    return [
        Tool(
            name="read_file",
            description="Read a text file from the workspace. Returns numbered lines. "
                        "Use offset/limit to page through long files.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the workspace root"},
                    "offset": {"type": "integer", "description": "First line to show (0-based)"},
                    "limit": {"type": "integer", "description": "How many lines to show"},
                },
                "required": ["path"],
            },
            run=guard(read_file),
        ),
        Tool(
            name="write_file",
            description="Create a file, or completely replace an existing one. "
                        "For small changes to an existing file prefer edit_file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "The complete file contents"},
                },
                "required": ["path", "content"],
            },
            run=guard(write_file),
            needs_permission=True,
            summarize=lambda a: f"write {a.get('path')} "
                                f"({len(a.get('content', '').splitlines())} lines)",
        ),
        Tool(
            name="edit_file",
            description="Replace an exact snippet of text in a file. The 'old' text must "
                        "match exactly and be unique unless replace_all is true.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "Exact text to find"},
                    "new": {"type": "string", "description": "Text to replace it with"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old", "new"],
            },
            run=guard(edit_file),
            needs_permission=True,
            summarize=lambda a: f"edit {a.get('path')}",
        ),
        Tool(
            name="list_dir",
            description="List files and folders in a workspace directory.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
            run=guard(list_dir),
        ),
        Tool(
            name="search",
            description="Search file contents for a pattern across the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory to search in"},
                },
                "required": ["pattern"],
            },
            run=guard(search),
        ),
        Tool(
            name="run_command",
            description="Run a shell command in the workspace root. Use for builds, tests, "
                        "git, and any other real work. Returns exit code and output.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "Seconds before giving up"},
                },
                "required": ["command"],
            },
            run=run_command,
            needs_permission=True,
            summarize=lambda a: f"run: {a.get('command', '')[:120]}",
        ),
    ]
