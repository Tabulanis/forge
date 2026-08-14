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

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .codetools import syntax_check
from .dataops import data_ops, date_calc
from .mathtools import COMPUTE_DESCRIPTION, run_compute
from .physics import quantum_sim, relativity_sim
from .recall import search as recall_search
from .web import fetch_url, web_search
from .writing import ai_tells, name_check, text_stats

MAX_READ_BYTES = 400_000     # a huge file would blow the context window
MAX_OUTPUT_CHARS = 30_000    # same, for command output
DEFAULT_TIMEOUT = 120

# Commands refused even in auto mode. Deliberately a short list of
# machine-killers, not a nanny filter — deleting a project folder is the
# user's right; wiping the disk or the home directory is not a judgment
# call any model should get to make.
import re as _re
_MACHINE_KILLERS: list[tuple[str, _re.Pattern]] = [
    ("deletes / or your home directory",
     _re.compile(r"\brm\s+(?:-[a-zA-Z]+\s+)*(?:/|~|\$HOME)\*?(?:\s|$)")),
    ("formats a disk", _re.compile(r"\bmkfs(\.|\s|$)")),
    ("writes raw bytes over a disk device", _re.compile(r"\bdd\b.*\bof=/dev/")),
    ("overwrites a disk device", _re.compile(r">\s*/dev/(sd|nvme|mmcblk)")),
    ("fork bomb", _re.compile(r":\s*\(\s*\)\s*\{")),
]


def _machine_killer(command: str) -> str | None:
    for why, pat in _MACHINE_KILLERS:
        if pat.search(command):
            return why
    return None


# Paths a fenced command may still reference: system binaries and libraries,
# nothing personal. Everything else outside the workspace is refused.
_FENCE_ALLOWED = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/opt/",
                  "/dev/null", "/proc/self")


def _fence_violation(command: str, root: Path) -> str | None:
    """In kid mode, find the first thing in a command that reaches outside
    the workspace: home shortcuts, parent-hopping, absolute paths that
    aren't system locations. Deterministic and strict — a false refusal
    costs a reworded command; a false allow costs someone's files."""
    rootstr = str(root)
    for tok in _re.findall(r"[^\s;|&<>()'\"=]+", command):
        if tok.startswith("~") or "$HOME" in tok or "${HOME" in tok:
            return tok
        if ".." in tok.split("/"):
            return tok
        if tok.startswith("/"):
            if tok == rootstr or tok.startswith(rootstr + "/"):
                continue
            if not tok.startswith(_FENCE_ALLOWED):
                return tok
    return None


def _lenient_replace(text: str, old: str, new: str) -> str | None:
    """
    Whitespace-forgiving fallback for edit_file.

    Models — small ones constantly — reproduce the lines they want to change
    with the wrong indentation, then miss, re-read, and miss again forever.
    If the old text matches exactly one place in the file when comparing
    line-by-line with whitespace stripped, that's an unambiguous edit: take
    it, and shift the replacement to the file's real indentation.

    Returns the new file text, or None when there's no single clear match
    (zero or several) — ambiguity still refuses, same as the strict path.
    """
    hay = text.splitlines(keepends=True)
    old_lines = old.splitlines()
    while old_lines and not old_lines[0].strip():
        old_lines.pop(0)
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    if not old_lines:
        return None
    needle = [l.strip() for l in old_lines]
    n = len(needle)
    matches = [i for i in range(len(hay) - n + 1)
               if [hay[i + j].strip() for j in range(n)] == needle]
    if len(matches) != 1:
        return None
    i = matches[0]

    file_indent = len(hay[i]) - len(hay[i].lstrip())
    given_indent = len(old_lines[0]) - len(old_lines[0].lstrip())
    delta = file_indent - given_indent
    out = []
    for l in new.splitlines():
        if not l.strip():
            out.append("")
        elif delta >= 0:
            out.append(" " * delta + l)
        else:
            cur = len(l) - len(l.lstrip())
            out.append(l[min(-delta, cur):])
    matched = "".join(hay[i:i + n])
    trail = "\n" if matched.endswith("\n") else ""
    return "".join(hay[:i]) + "\n".join(out) + trail + "".join(hay[i + n:])


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
    # Ask even in auto mode. For actions whose effect lands somewhere the
    # user might not be — sound comes out of the server's speakers, and the
    # user may be on a tablet in another room.
    always_ask: bool = False


class Workspace:
    """Everything the agent is allowed to touch, and nothing else."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        # Paths agents may never WRITE, one glob per line in .forge-protect
        # at the workspace root (# for comments). Mechanical on purpose:
        # 2026-08-11 proved a written rule loses to a direct "fix it" from
        # the user — the model edited an author's read-only manuscript
        # minutes after reading a notebook rule saying never to. Reading
        # stays allowed; only writes are refused. (Known hole: run_command
        # can still shell its way in — the file tools are what models
        # actually use, and a fence there covers the real behavior.)
        self.protected: list[str] = []
        pf = self.root / ".forge-protect"
        if pf.is_file():
            try:
                self.protected = [ln.strip() for ln in
                                  pf.read_text(encoding="utf-8").splitlines()
                                  if ln.strip() and not ln.strip().startswith("#")]
            except OSError:
                pass
        # Files the model has actually seen this session. edit_file refuses
        # to touch a file that isn't in here — "read before you write" as a
        # hard rule instead of a polite request in the prompt.
        self.reads: set[Path] = set()
        # mtime at the moment each file in `reads` was last seen. The author
        # editing a world file by hand mid-session is a feature here, not an
        # edge case — this is how the agent notices its cached read is stale
        # (see Agent._stale_files, which compares this against disk).
        self.read_mtimes: dict[Path, float] = {}
        # Consecutive edit_file misses per file. Small models get stuck
        # re-guessing the exact text forever; after a couple of misses the
        # error starts telling them to rewrite the file instead.
        self.edit_misses: dict[Path, int] = {}

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

    def protection(self, f: Path) -> str | None:
        """The glob that write-protects f, or None. fnmatch's * crosses
        slashes, so 'source-material/*' covers nested files too."""
        import fnmatch
        rel = self.rel(f)
        for g in self.protected:
            if fnmatch.fnmatch(rel, g):
                return g
        return None

    def mark_read(self, f: Path) -> None:
        """Record that the model has seen f's CURRENT content — via read,
        write, or edit. Stamps the mtime so a later external edit (the
        author hand-writing into a world file) is detectable as staleness."""
        self.reads.add(f)
        try:
            self.read_mtimes[f] = f.stat().st_mtime
        except OSError:
            pass


def build_media_tools(ws: Workspace, mc) -> list[Tool]:
    """
    Eyes and ears, offered only when they actually work.

    A tool the model can see but that always fails is worse than no tool —
    it burns turns and teaches the model bad habits. So each of these is
    attached only if the underlying capability reports ready.
    """
    from . import media

    caps = media.capabilities(mc)
    tools: list[Tool] = []

    if caps["vision"]["ok"]:
        def look_at(image: str, question: str = "Describe this image in detail.") -> str:
            path = image
            try:
                path = str(ws.resolve(image))
            except PermissionError:
                # An absolute path outside the workspace is fine for *reading*
                # an image the user pointed at (a screenshot in /tmp, say) —
                # this tool never writes, so the sandbox isn't at risk.
                pass
            return media.see(path, question, mc)

        tools.append(Tool(
            name="look_at_image",
            description="Look at an image file and answer a question about it. "
                        "Use for screenshots, mockups, diagrams, photos of "
                        "whiteboards, or anything you need to SEE rather than read.",
            parameters={
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "Path to the image"},
                    "question": {"type": "string",
                                 "description": "What you want to know about it"},
                },
                "required": ["image"],
            },
            run=look_at,
        ))

    if caps["screenshot"]["ok"]:
        def grab(region: str = "") -> str:
            path = media.screenshot(region=region or None)
            if path.startswith("Error"):
                return path
            if caps["vision"]["ok"]:
                return f"Screenshot saved to {path} — use look_at_image to see it."
            return f"Screenshot saved to {path} (no vision model running to read it)."

        tools.append(Tool(
            name="take_screenshot",
            description="Capture the current screen to a PNG file. Pair with "
                        "look_at_image to actually see what's on screen.",
            parameters={
                "type": "object",
                "properties": {
                    "region": {"type": "string",
                               "description": "Optional 'x,y,w,h' to grab part of the screen"},
                },
            },
            run=grab,
            needs_permission=True,
            summarize=lambda a: "take a screenshot of your screen",
        ))

    if caps["speech_in"]["ok"]:
        tools.append(Tool(
            name="transcribe_audio",
            description="Turn a recording of speech into text. Use when the user "
                        "points at a voice memo or any audio file.",
            parameters={
                "type": "object",
                "properties": {"audio": {"type": "string", "description": "Path to the audio file"}},
                "required": ["audio"],
            },
            run=lambda audio: media.listen(audio, mc),
        ))

    if caps["speech_out"]["ok"]:
        tools.append(Tool(
            name="say_aloud",
            description="Speak a short message out loud — through the speakers "
                        "of the machine Forge runs on, which may not be where "
                        "the user is sitting. Only use it when the user "
                        "explicitly asked to be told out loud.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            run=lambda text: media.speak(text, mc),
            needs_permission=True,
            always_ask=True,
            summarize=lambda a: f"say out loud: {str(a.get('text',''))[:60]}",
        ))

    return tools


# Extensions we can parse-check after a write, so broken code can't be saved
# silently. Maps to the languages syntax_check knows.
_CHECK_EXT = {
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".py": "python", ".json": "json", ".sh": "bash", ".bash": "bash",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".pl": "perl", ".yaml": "yaml", ".yml": "yaml",
}


_JSTOOLS = Path.home() / "forge" / "jstools"
_ESLINT_BIN = _JSTOOLS / "node_modules" / ".bin" / "eslint"
_ESLINT_CFG = _JSTOOLS / "eslint.config.mjs"


def _eslint(f: Path) -> str:
    """Lint a JS file for real bugs beyond syntax (==, undefined names, const
    reassignment, ...). Advisory — returns a compact findings suffix or ''.
    Must run from the file's own dir (eslint v10 anchors its base path there)."""
    if not _ESLINT_BIN.exists() or f.suffix.lower() not in (".js", ".mjs", ".cjs"):
        return ""
    try:
        r = subprocess.run(
            [str(_ESLINT_BIN), "--config", str(_ESLINT_CFG), "--format", "json", str(f)],
            capture_output=True, text=True, timeout=30, cwd=str(f.parent))
        data = json.loads(r.stdout or "[]")
    except Exception:
        return ""
    msgs = [f"line {m.get('line')}: {m.get('message')} [{m.get('ruleId')}]"
            for entry in data for m in entry.get("messages", []) if m.get("ruleId")]
    if not msgs:
        return ""
    shown = "; ".join(msgs[:4])
    more = f" (+{len(msgs) - 4} more)" if len(msgs) > 4 else ""
    return (f"\n· lint flagged {len(msgs)} thing(s) — worth a look: {shown}{more}")


def _autocheck_html(f: Path) -> str:
    """HTML files carry inline <script> that the plain extension check misses —
    exactly where a browser-game bug like an undefined function hides. Pull the
    inline JS out, parse-check and lint it, so 'checkCollisions is not defined'
    shows up at SAVE time instead of only in the browser console."""
    try:
        html = f.read_text(encoding="utf-8")
    except Exception:
        return ""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html,
                         re.S | re.I)
    js = "\n;\n".join(scripts).strip()
    if not js:
        return "  ✓ saved (no inline script to check)"
    # write the extracted JS to a sibling temp file so eslint/node see real paths
    tmp = f.parent / f".{f.stem}.inline.js"
    try:
        tmp.write_text(js, encoding="utf-8")
        parse = syntax_check("javascript", js)
        if not parse.startswith("SYNTAX OK"):
            first = " ".join(parse.split("\n")[:3])[:220]
            return (f"\n⚠ HEADS UP — the inline <script> DOES NOT PARSE: {first}\n"
                    f"Fix it before reporting done.")
        lint = _eslint(tmp)
        return "  ✓ inline script parses clean" + lint
    finally:
        tmp.unlink(missing_ok=True)


def _autocheck(f: Path) -> str:
    """After a write/edit, verify the file still parses (and for JS, lint it).
    Returns a short status suffix appended to the tool result — '' for files we
    can't check. The dummy-proofing: 'Created app.js' now also says whether
    app.js is valid, so she can't leave broken code on disk unaware."""
    if f.suffix.lower() in (".html", ".htm"):
        return _autocheck_html(f)
    lang = _CHECK_EXT.get(f.suffix.lower())
    if not lang:
        return ""
    try:
        result = syntax_check(lang, f.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if result.startswith("SYNTAX OK"):
        suffix = "  ✓ parses clean"
        if lang == "javascript":
            suffix += _eslint(f)      # add bug-lint on top of the parse check
        return suffix
    first = " ".join(result.split("\n")[:3])[:220]
    return (f"\n⚠ HEADS UP — this file DOES NOT PARSE: {first}\n"
            f"Fix it now before moving on; don't report it as done while broken.")


_PRETTIER_BIN = _JSTOOLS / "node_modules" / ".bin" / "prettier"
_PRETTIER_EXT = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json",
                 ".css", ".scss", ".html", ".md", ".yaml", ".yml"}


def _format_code(ws, path: str) -> str:
    f = ws.resolve(path)
    if not f.exists():
        return f"Error: no such file: {ws.rel(f)}"
    if f.suffix.lower() not in _PRETTIER_EXT:
        return f"Error: prettier doesn't format {f.suffix or 'this'} files."
    if not _PRETTIER_BIN.exists():
        return "Error: prettier isn't installed (expected in ~/forge/jstools)."
    try:
        r = subprocess.run([str(_PRETTIER_BIN), "--write", str(f)],
                           capture_output=True, text=True, timeout=30,
                           cwd=str(f.parent))
    except Exception as e:
        return f"Error running prettier: {type(e).__name__}: {e}"
    if r.returncode != 0:
        return f"Error formatting: {((r.stderr or r.stdout) or '')[-300:]}"
    ws.mark_read(f)      # content changed — treat as freshly known so edits re-match
    return f"Formatted {ws.rel(f)} with prettier."


_PHYS_RELATIVITY = ("time_dilation", "twin_trip", "doppler", "photon_clock")
_PHYS_QUANTUM = ("photon", "double_slit", "uncertainty")


def _physics_sim(scenario: str, params: dict | None = None) -> str:
    """One on-demand dispatcher for the physics sims — keeps a single lean tool
    in the schema instead of two fat ones, while covering every scenario."""
    p = params or {}
    if scenario in _PHYS_RELATIVITY:
        keys = ("v_c", "duration_years", "distance_ly")
        return relativity_sim(scenario, **{k: p[k] for k in keys if k in p})
    if scenario in _PHYS_QUANTUM:
        keys = ("wavelength_nm", "slit_separation_um", "screen_distance_m",
                "photons", "delta_x_nm", "seed")
        return quantum_sim(scenario, **{k: p[k] for k in keys if k in p})
    return ("Error: unknown scenario. Relativity: time_dilation, twin_trip, "
            "doppler, photon_clock. Quantum: photon, double_slit, uncertainty.")


def _generate_image(ws_root: str, prompt: str, filename: str = "",
                    steps: int = 3, seed=None) -> str:
    """Run the CPU image generator as a subprocess (keeps the 2.5GB SD model
    out of the agent's own memory, and hides the GPU so Merge keeps it)."""
    root = Path(ws_root)
    if filename:
        name = filename if filename.lower().endswith((".png", ".jpg", ".jpeg")) \
            else filename + ".png"
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40] or "image"
        name = f"generated/{slug}.png"
    out = (root / name).resolve()
    if root != out and root not in out.parents:
        return f"Error: image path is outside the workspace: {out}"
    # Fast path: the keep-warm server (start-model.sh imagegen) holds the model
    # in RAM, so this skips the ~30s reload — only the draw remains. Falls
    # through to a one-off subprocess if that server isn't running.
    try:
        import urllib.request
        payload = {"prompt": prompt, "out_path": str(out), "steps": int(steps)}
        if seed is not None:
            payload["seed"] = int(seed)
        req = urllib.request.Request(
            "http://127.0.0.1:8771/generate", json.dumps(payload).encode(),
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            res = json.load(r)
        if res.get("path"):
            return (f"Image saved to {res['path']} (warm server). "
                    f"Use look_at_image on that path to see what you made.")
    except Exception:
        pass   # warm server down or errored -> subprocess fallback below
    forge_root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": forge_root, "CUDA_VISIBLE_DEVICES": ""}
    cmd = [sys.executable, "-m", "forge.imagegen", prompt, str(out), str(int(steps))]
    if seed is not None:
        cmd.append(str(int(seed)))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        return "Error: image generation timed out (over 5 minutes)."
    if r.returncode != 0:
        return f"Error generating image: {((r.stderr or r.stdout) or '')[-600:]}"
    return (f"Image saved to {out}. It's a concept/mood sketch (fast CPU model). "
            f"Use look_at_image on that path to see what you made.")


def build_tools(ws: Workspace, fenced: bool = False) -> list[Tool]:
    """Construct the toolset bound to one workspace.

    fenced=True (kid mode) additionally confines run_command to the
    workspace: the file tools were always fenced, but shell commands could
    reach anything. With the fence, a borrowed tablet can only make a mess
    inside the folder it was given."""

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
        ws.mark_read(f)
        window = lines[offset:offset + limit]
        if not window:
            return f"(no lines in range; file has {len(lines)} lines)"
        # Line numbers help the model target edits precisely. The BYTE cap
        # on the returned window exists because the line default (2000) is
        # sized for code: 1000 lines of a novel is ~25k tokens, which
        # detonates a 32k context in one call — seen live: two emergency
        # compactions and a dead turn. Cap loudly, never silently.
        body_lines, used = [], 0
        for i, ln in enumerate(window):
            entry = f"{i + offset + 1:6d}\t{ln}"
            if used + len(entry) > 20_000:
                next_off = offset + i
                body_lines.append(
                    f"... (output capped at 20KB to protect your working "
                    f"memory — {len(lines) - next_off} lines remain; "
                    f"continue with offset={next_off}, or search instead "
                    f"of reading big stretches)")
                break
            body_lines.append(entry)
            used += len(entry) + 1
        else:
            if offset + limit < len(lines):
                body_lines.append(f"... ({len(lines) - offset - limit} more "
                                  f"lines; use offset={offset + limit})")
        return "\n".join(body_lines)

    def _backup(f: Path) -> None:
        """Keep the previous version of a file about to change.

        One level deep, mirrored under .forge_backups/ — enough for 'undo
        that', without turning the workspace into a version-control system
        (that's what git is for).
        """
        if not f.exists() or not f.is_file():
            return
        bk = ws.root / ".forge_backups" / ws.rel(f)
        bk.parent.mkdir(parents=True, exist_ok=True)
        bk.write_bytes(f.read_bytes())

    def _refuse_protected(f: Path) -> str | None:
        g = ws.protection(f)
        if g:
            return (f"Error: {ws.rel(f)} is write-protected by .forge-protect "
                    f"({g}) — agents may read it, never change it. Tell the "
                    f"user exactly what to change and where (file and line); "
                    f"they make the change themselves.")
        return None

    def write_file(path: str, content: str) -> str:
        f = ws.resolve(path)
        if (refusal := _refuse_protected(f)):
            return refusal
        existed = f.exists()
        if existed and f not in ws.reads:
            return (f"Error: {ws.rel(f)} already exists and you haven't read it "
                    f"this session — overwriting it blind could destroy work. "
                    f"read_file it first, or pick a new filename.")
        f.parent.mkdir(parents=True, exist_ok=True)
        _backup(f)
        f.write_text(content, encoding="utf-8")
        # Writing the whole file counts as knowing its contents.
        ws.mark_read(f)
        verb = "Overwrote" if existed else "Created"
        return f"{verb} {ws.rel(f)} ({len(content.splitlines())} lines){_autocheck(f)}"

    def undo_file(path: str) -> str:
        f = ws.resolve(path)
        if (refusal := _refuse_protected(f)):
            return refusal
        bk = ws.root / ".forge_backups" / ws.rel(f)
        if not bk.exists():
            return (f"Error: no backup of {ws.rel(f)} — backups exist only for "
                    f"files write_file or edit_file changed this session or before.")
        current = f.read_bytes() if f.exists() else None
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(bk.read_bytes())
        # Swap, so undoing twice redoes — nothing is ever lost either way.
        if current is not None:
            bk.write_bytes(current)
        ws.mark_read(f)
        return f"Restored {ws.rel(f)} from backup (undo again to redo)."

    def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
        f = ws.resolve(path)
        if (refusal := _refuse_protected(f)):
            return refusal
        if not f.exists():
            return f"Error: no such file: {ws.rel(f)}"
        if f not in ws.reads:
            return (f"Error: you haven't read {ws.rel(f)} this session, so this "
                    f"edit was blocked. Use read_file on it first, then edit "
                    f"based on what's actually there.")
        text = f.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            fixed = _lenient_replace(text, old, new)
            if fixed is not None:
                _backup(f)
                f.write_text(fixed, encoding="utf-8")
                ws.edit_misses.pop(f, None)
                ws.mark_read(f)
                return (f"Edited {ws.rel(f)} (1 replacement — your text's "
                        f"whitespace didn't match the file exactly, but it "
                        f"matched one place clearly, so the edit was applied "
                        f"at the file's real indentation){_autocheck(f)}")
            misses = ws.edit_misses.get(f, 0) + 1
            ws.edit_misses[f] = misses
            msg = (f"Error: that exact text isn't in {ws.rel(f)}. "
                   f"Read the file again — it may differ in whitespace or have changed.")
            if misses >= 2:
                msg += (f" You have now missed {misses} times on this file. "
                        f"Stop trying to patch it: read it once more, then use "
                        f"write_file to replace the WHOLE file with the "
                        f"corrected version in one go.")
            return msg
        if count > 1 and not replace_all:
            return (f"Error: found {count} matches in {ws.rel(f)}. "
                    f"Add more surrounding context to make it unique, or pass replace_all=true.")
        _backup(f)
        ws.edit_misses.pop(f, None)
        f.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1),
                     encoding="utf-8")
        ws.mark_read(f)
        return (f"Edited {ws.rel(f)} ({count if replace_all else 1} "
                f"replacement(s)){_autocheck(f)}")

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

    def _canon(s: str) -> str:
        """Case and quote-style must never decide whether text is 'found'.
        A model that searches "pont neuf" and gets nothing concludes the
        bridge isn't in the book — a false negative that seeds a false
        answer. Prose also uses curly quotes (GQ’s) where a model types
        straight ones (GQ's); both spell the same word."""
        return (s.replace("’", "'").replace("‘", "'")
                 .replace("“", '"').replace("”", '"').casefold())

    def search(pattern: str, path: str = ".", max_results: int = 60) -> str:
        """Case-insensitive, quote-forgiving content search. One code path
        on purpose — a previous ripgrep fast-path was case-sensitive while
        this fallback wasn't, so the same query found different truths
        depending on what was installed."""
        d = ws.resolve(path)
        # A silent "(no matches)" for a directory that doesn't even exist
        # taught the model that entire real books "contain no mention" of
        # their own protagonist — it searched invented folders (src/,
        # novels/) and read the empty result as fact about the text.
        if not d.exists():
            return (f"Error: no such directory: {ws.rel(d)} — use list_dir "
                    f"to see what actually exists before searching in it.")
        # "foot|tall|height" searches all three at once — one wide net
        # beats five guesses. The height that hid from three separate
        # sessions was always one synonym away from the query used.
        needles = [_canon(p) for p in pattern.split("|") if _canon(p.strip())]
        if not needles:
            return "(empty pattern)"
        hits, total = [], 0
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x != "node_modules"]
            for fn in files:
                fp = Path(root) / fn
                try:
                    for i, line in enumerate(fp.read_text(encoding="utf-8",
                                                          errors="ignore").splitlines(), 1):
                        if any(n in _canon(line) for n in needles):
                            total += 1
                            if len(hits) < max_results:
                                hits.append(f"{ws.rel(fp)}:{i}:{line.strip()[:200]}")
                except Exception:
                    continue
        # Silent truncation is how "the name is never revealed" happens: 60
        # early-book hits with no hint that the reveal sits at hit 80. Keep
        # counting past the cap and SAY what was left unshown.
        if total > len(hits):
            hits.append(f"... {total - len(hits)} MORE match(es) not shown "
                        f"(last shown was {hits[-1].split(':')[0]}:"
                        f"{hits[-1].split(':')[1]}) — the answer may be in the "
                        f"later ones. Use a more specific pattern, or search "
                        f"again reading from higher line numbers.")
        return "\n".join(hits) if hits else "(no matches)"

    def save_note(note: str) -> str:
        """Append one lesson to the project notebook.

        Append-only and pinned to one file, so it doesn't need a permission
        prompt — the worst a confused model can do is write a bad note.
        """
        note = " ".join(note.split())
        if not note:
            return "Error: empty note."
        nb = ws.root / "FORGE-NOTES.md"
        header = "" if nb.exists() else (
            "# Project notebook\n\nLessons this project has taught its agents. "
            "Loaded at the start of every session.\n\n")
        with nb.open("a", encoding="utf-8") as f:
            f.write(header + f"- {note}\n")
        ws.mark_read(nb)
        return f"Noted in FORGE-NOTES.md: {note[:80]}"

    def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        blocked = _machine_killer(command)
        if blocked:
            return (f"Error: blocked — {blocked}. This command could damage the "
                    f"whole machine, so it's refused even in auto mode. If the "
                    f"user genuinely wants it, they can run it themselves.")
        if fenced:
            bad = _fence_violation(command, ws.root)
            if bad:
                return (f"Error: kid mode is on, and this command reaches "
                        f"outside the workspace ({bad!r}). Work only inside "
                        f"{ws.root} — rewrite the command with relative paths.")
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
            description="Search file contents across the workspace. "
                        "Case-insensitive and forgiving about quote style. "
                        "Separate alternatives with | to search several words "
                        "at once — for a height search 'foot|feet|tall|height', "
                        "for a name reveal search 'name|called|introduced'. "
                        "Cast a WIDE net of topic words; never search your "
                        "guessed answer ('six feet') — missing your own guess "
                        "proves nothing about what the text says. Short "
                        "distinctive words beat long phrases.",
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
            name="undo_file",
            description="Restore a file to how it was before the last write_file or "
                        "edit_file changed it. Use when a change was wrong or the "
                        "user says to undo. Calling it twice redoes the change.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            run=guard(undo_file),
            needs_permission=True,
            summarize=lambda a: f"restore {a.get('path')} from backup",
        ),
        Tool(
            name="save_note",
            description="Write one lesson to the project notebook (FORGE-NOTES.md), "
                        "which every future session reads at startup. Use it when you "
                        "learn something durable the hard way: a command that must be "
                        "run a particular way, a gotcha in this codebase, a correction "
                        "from the user. One short sentence per note. Don't record "
                        "things the code itself already says.",
            parameters={
                "type": "object",
                "properties": {"note": {"type": "string",
                                        "description": "One sentence worth remembering"}},
                "required": ["note"],
            },
            run=guard(save_note),
        ),
        Tool(
            name="run_command",
            description="Run a shell command in the workspace root. Use for builds, tests, "
                        "git, and any other real work. Returns exit code and output. "
                        "There is NO screen or keyboard attached: full-screen or "
                        "interactive programs (curses games, editors, anything that "
                        "draws a UI or waits for keypresses) cannot run here and fail "
                        "with terminal errors. Verify those differently — import "
                        "check, syntax check, unit-testable pieces — and tell the "
                        "user to launch the program in a real terminal themselves.",
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
        Tool(
            name="compute",
            description=COMPUTE_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string",
                             "description": "Python program; print() the result"},
                },
                "required": ["code"],
            },
            run=run_compute,
            needs_permission=True,
            summarize=lambda a: f"compute: {a.get('code', '')[:100]}",
        ),
        Tool(
            name="web_search",
            description="Search the web (current, live results — use this for anything "
                        "you don't know, anything recent, or to check a fact). Returns "
                        "titles, links, and snippets. Follow up with fetch_url to read a "
                        "page in full.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer",
                                    "description": "How many results (1-8, default 6)"},
                },
                "required": ["query"],
            },
            run=lambda query, max_results=6: web_search(query, max_results),
        ),
        Tool(
            name="fetch_url",
            description="Fetch a web page and read its text (scripts/menus stripped). "
                        "Use after web_search to read a result in full, or on a URL the "
                        "user gives you. Returns the page's readable text.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            run=fetch_url,
        ),
        Tool(
            name="physics_sim",
            description="Exact physics simulations, on demand — relativity and quantum. "
                        "scenario is one of: time_dilation / twin_trip / doppler / "
                        "photon_clock (relativity — params: v_c [fraction of light speed, "
                        "e.g. 0.9], duration_years, distance_ly) OR photon / double_slit / "
                        "uncertainty (quantum — params: wavelength_nm, slit_separation_um, "
                        "screen_distance_m, photons, delta_x_nm, seed). Pass params as an "
                        "object, e.g. {\"v_c\": 0.9}. double_slit returns an ASCII fringe "
                        "picture FOR THE USER — paste it into your reply, tool results aren't "
                        "shown to them automatically.",
            parameters={
                "type": "object",
                "properties": {
                    "scenario": {"type": "string",
                                 "enum": ["time_dilation", "twin_trip", "doppler",
                                          "photon_clock", "photon", "double_slit",
                                          "uncertainty"]},
                    "params": {"type": "object",
                               "description": "Scenario parameters as an object, "
                                              "e.g. {\"v_c\": 0.9} or "
                                              "{\"wavelength_nm\": 500, \"slit_separation_um\": 50}"},
                },
                "required": ["scenario"],
            },
            run=_physics_sim,
        ),
        Tool(
            name="data_ops",
            description="One-call versions of everyday data chores — use instead of "
                        "writing throwaway code or doing them in your head. Items go in "
                        "`text`, one per line. Operations: 'sort' (numeric if all numbers, "
                        "else alphabetical; flags: reverse, unique), 'dedupe' (order kept), "
                        "'count' (frequency table), 'diff' (`text` vs `second_text`), "
                        "'stats' (numbers -> count/sum/min/max/mean/median/stdev), "
                        "'case' (convert identifiers; style: snake/kebab/constant/camel/pascal), "
                        "'json_check' (validate + pretty-print, pinpoints errors), "
                        "'regex' (test `pattern` against `text`, shows matches). "
                        "For bespoke algorithms beyond these, use compute.",
            parameters={
                "type": "object",
                "properties": {
                    "operation": {"type": "string",
                                  "enum": ["sort", "dedupe", "count", "diff",
                                           "stats", "case", "json_check", "regex"]},
                    "text": {"type": "string",
                             "description": "The items/text to operate on, one item per line"},
                    "second_text": {"type": "string",
                                    "description": "Second list, for diff"},
                    "pattern": {"type": "string",
                                "description": "Regular expression, for regex"},
                    "style": {"type": "string",
                              "enum": ["snake", "kebab", "constant", "camel", "pascal"]},
                    "reverse": {"type": "boolean"},
                    "unique": {"type": "boolean"},
                },
                "required": ["operation"],
            },
            run=data_ops,
        ),
        Tool(
            name="date_calc",
            description="Exact calendar math — never work out dates in your head. "
                        "Operations: 'today' (current date + weekday), "
                        "'weekday' (of `date`), 'diff' (`date` to `second_date`, "
                        "or to today if omitted), 'add' (`date` + `days`, negative ok). "
                        "Dates are ISO format: 2026-08-12.",
            parameters={
                "type": "object",
                "properties": {
                    "operation": {"type": "string",
                                  "enum": ["today", "weekday", "diff", "add"]},
                    "date": {"type": "string"},
                    "second_date": {"type": "string"},
                    "days": {"type": "integer"},
                },
                "required": ["operation"],
            },
            run=date_calc,
        ),
        Tool(
            name="syntax_check",
            description="Verify that code PARSES before writing it to a file or "
                        "claiming it works — in any supported language, without "
                        "executing it. Pass the language and the code; returns "
                        "SYNTAX OK or the exact parse error with line numbers. "
                        "Supported here: python, javascript, bash, c, cpp, perl, "
                        "json, yaml. If a language isn't supported, it says so — "
                        "then be honest that the syntax is unverified.",
            parameters={
                "type": "object",
                "properties": {
                    "language": {"type": "string"},
                    "code": {"type": "string"},
                },
                "required": ["language", "code"],
            },
            run=syntax_check,
        ),
        Tool(
            name="text_stats",
            description="Exact prose statistics for a draft — use instead of eyeballing. "
                        "Pass the text in `text`; returns word/sentence/paragraph counts, "
                        "reading time, dialogue share, overused words (3+ repeats), and "
                        "the longest sentence. Read the file first if it's on disk.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "The draft text to analyze"},
                },
                "required": ["text"],
            },
            run=text_stats,
        ),
        Tool(
            name="ai_tells",
            description="Crossword-check for prose: measures how machine-flavored a "
                        "passage reads — stiff uniform sentence rhythm, cliche/AI-tell "
                        "phrases (delve, tapestry, moreover...), em-dash/semicolon "
                        "overuse, repeated openers. Returns a 0-100 machine-flavor score "
                        "and what to fix. Deterministic, not an online AI detector, but "
                        "measures the same fingerprints — so fixing what it flags is "
                        "editing for voice. Pass prose in `text`.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "The prose to check for machine flavor"},
                },
                "required": ["text"],
            },
            run=ai_tells,
        ),
        Tool(
            name="name_check",
            description="Canon guard: scan every .md/.txt file in the workspace for a "
                        "character/place name, reporting exact uses AND near-miss "
                        "spellings with file:line locations. A near-miss is usually a "
                        "typo about to fork into a second character — run this before "
                        "trusting that a name is spelled consistently.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The name to check, letters only"},
                    "max_distance": {"type": "integer",
                                     "description": "How many letters may differ (1-3, default 1)"},
                },
                "required": ["name"],
            },
            run=guard(lambda name, max_distance=1:
                      name_check(str(ws.root), name, max_distance)),
        ),
        Tool(
            name="format_code",
            description="Auto-format a file with prettier (js, jsx, ts, json, css, "
                        "html, md). Cleans up indentation, quotes, semicolons, and "
                        "spacing so the file is tidy and consistent. Rewrites the file "
                        "in place. Use after writing JS/CSS/JSON to make it neat.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            run=guard(lambda path: _format_code(ws, path)),
            needs_permission=True,
            summarize=lambda a: f"format {a.get('path', '')}",
        ),
        Tool(
            name="generate_image",
            description="Generate an image from a text prompt with a fast local model "
                        "(runs on CPU, so it never touches the GPU). Saves a PNG into the "
                        "workspace and returns the path — you can then look_at_image it to "
                        "see what you made. Quality is concept/mood/sketch tier and takes "
                        "~30s; good for visualizing an idea or a scene, not for final art. "
                        "Give a vivid, detailed prompt.",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string",
                               "description": "Vivid description of the image to make"},
                    "filename": {"type": "string",
                                 "description": "Optional output name; defaults to a slug"},
                    "steps": {"type": "integer",
                              "description": "Denoising steps, 1-4 (default 3); more = slower"},
                    "seed": {"type": "integer",
                             "description": "Optional seed for a repeatable image"},
                },
                "required": ["prompt"],
            },
            run=guard(lambda prompt, filename="", steps=3, seed=None:
                      _generate_image(str(ws.root), prompt, filename, steps, seed)),
            needs_permission=True,
            summarize=lambda a: f"generate image: {a.get('prompt', '')[:60]}",
        ),
        Tool(
            name="recall",
            description="Search every past conversation you and the user have "
                        "had — your mid-term memory. Use it when the user "
                        "mentions something said or decided before that you "
                        "can't see in the current conversation, BEFORE "
                        "guessing or asking them to repeat it. Returns "
                        "verbatim snippets with date, folder, and who said "
                        "it. Query with a few concrete words (names, "
                        "things, decisions), not full sentences.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string",
                                         "description": "A few concrete words to look for"}},
                "required": ["query"],
            },
            run=lambda query: recall_search(query, workspace=str(ws.root)),
        ),
    ]
