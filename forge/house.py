"""Environmental awareness: what hardware and software this house actually has.

Read-only. Facts come from ~/aidojo/shared/inventory (refreshed daily by a systemd
timer) plus a cheap live snapshot of RAM/GPU/disk on both boxes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SHARED = Path.home() / "aidojo" / "shared"
INV = SHARED / "inventory"
SCRIPT = SHARED / "inventory.sh"
OLD_BOX = "10.42.0.1"


def _run(cmd: list[str], timeout: int = 25) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except Exception as e:  # noqa: BLE001 - report, never raise into a turn
        return f"(failed: {e})"


def _live_local() -> str:
    if not SCRIPT.exists():
        return "(no inventory.sh on this box)"
    return _run(["bash", str(SCRIPT), "snapshot"])


def _live_old_box() -> str:
    if not SCRIPT.exists():
        return "(no inventory.sh to send)"
    try:
        with open(SCRIPT, "rb") as fh:
            p = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 OLD_BOX, "bash -s snapshot"],
                stdin=fh, capture_output=True, text=True, timeout=40)
        out = (p.stdout or "").strip()
        return out or f"(old box unreachable: {(p.stderr or '').strip()[:120]})"
    except subprocess.TimeoutExpired:
        return "(old box timed out)"
    except Exception as e:  # noqa: BLE001
        return f"(failed: {e})"


def machine_state(live: bool = True, box: str = "both") -> str:
    """Current state of the house: both boxes, live if asked."""
    parts: list[str] = []

    readme = INV / "README.md"
    if readme.exists():
        parts.append("WRITTEN FACTS (" + readme.as_posix() + ", refreshed daily):")
        parts.append(readme.read_text(errors="replace").strip())
    else:
        parts.append("No inventory yet. Run: bash ~/aidojo/shared/refresh-inventory.sh")

    if live:
        parts.append("\nLIVE NOW:")
        if box in ("both", "void", "local"):
            parts.append(_live_local())
        if box in ("both", "old", "titan", "pop-os"):
            parts.append(_live_old_box())

    parts.append(
        "\nFull reports: ~/aidojo/shared/inventory/void.md and titan.md. "
        "Old box mounted here at ~/titan-home, ~/titan-big, ~/titan-mid, ~/titan-wd."
    )
    return "\n".join(parts)


SELFTEST = """
out = machine_state(live=False)
assert isinstance(out, str) and len(out) > 50, out
out2 = machine_state(live=True, box='void')
assert 'LIVE NOW' in out2, out2
"""
