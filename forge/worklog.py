"""What she actually did, written by the machine, readable from anywhere.

2026-09-19. He asked her what she had been working on. She had spent the
previous day writing several thousand lines of GDScript, and she said it had
been quiet — not dishonestly, but because nothing in reach could tell her
otherwise: her notebook holds lessons, her findings expire in a week and are
per-session, and a chat knows nothing of other chats. "That's just super
strange that I ask what she's working on and she doesn't know."

So the harness writes one line per working turn: when, which workspace, which
files, and the verdict the checker gave. She never writes it and cannot edit
it, which is the point — a worklog you can rewrite is a story, not a record.

Lives in ~/.forge/worklog.md, outside every workspace, so it is the same
record in every chat.
"""
from __future__ import annotations

import time
from pathlib import Path

LOG = Path.home() / ".forge" / "worklog.md"
HEADER = ("# Worklog\n\nOne line per working turn, written by the harness. "
          "Not editable by the assistant: it is the record, not the story.\n\n")
KEEP_DAYS = 120


def append(workspace: str, files: list[str], verdict: str = "", note: str = "") -> None:
    """One line for a turn that touched files. Never raises."""
    try:
        if not files and not note:
            return
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if not LOG.exists():
            LOG.write_text(HEADER, encoding="utf-8")
        ws = Path(workspace).name if workspace else "?"
        shown = ", ".join(Path(f).name for f in files[:6])
        if len(files) > 6:
            shown += f" (+{len(files) - 6} more)"
        bits = [time.strftime("%Y-%m-%d %H:%M"), ws]
        if shown:
            bits.append(shown)
        if verdict:
            bits.append(verdict.strip().split("\n")[0][:90])
        if note:
            bits.append(note.strip()[:120])
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("- " + "  ·  ".join(bits) + "\n")
    except Exception:
        pass            # a worklog that breaks a turn is worse than none


def recent(limit: int = 12, days: int = 14) -> str:
    """The last entries, newest last (the way a diary reads)."""
    try:
        if not LOG.exists():
            return ("No worklog yet — it starts recording the next time work is done. "
                    "So: you do not know what was done before now. Say that rather than guessing.")
        lines = [l.rstrip() for l in LOG.read_text(encoding="utf-8").split("\n")
                 if l.startswith("- ")]
        if not lines:
            return "The worklog is empty."
        cutoff = time.time() - days * 86400
        keep = []
        for l in lines:
            stamp = l[2:18]
            try:
                t = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M"))
            except Exception:
                t = cutoff + 1
            if t >= cutoff:
                keep.append(l)
        keep = keep[-limit:]
        if not keep:
            return f"Nothing in the worklog in the last {days} days."
        return ("WORKLOG (what was actually done, written by the harness):\n"
                + "\n".join("  " + l[2:] for l in keep)
                + "\n  Anything not in this list, you do not know that you did.")
    except Exception as e:
        return f"Could not read the worklog: {e}"


def prune() -> None:
    try:
        if not LOG.exists():
            return
        cutoff = time.time() - KEEP_DAYS * 86400
        out = []
        for l in LOG.read_text(encoding="utf-8").split("\n"):
            if not l.startswith("- "):
                out.append(l); continue
            try:
                t = time.mktime(time.strptime(l[2:18], "%Y-%m-%d %H:%M"))
            except Exception:
                t = cutoff + 1
            if t >= cutoff:
                out.append(l)
        LOG.write_text("\n".join(out), encoding="utf-8")
    except Exception:
        pass
