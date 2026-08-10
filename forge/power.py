"""
The power switch.

The local models are the only thing here that holds the GPU — the 30B alone
owns ~23 of the card's 24GB. Everything runs under systemd user units, so
"shut her down" is just stopping those units; this module is the one place
that knows their names. The dashboard is left running on purpose: it uses no
VRAM, and its Power card is how you wake her back up without a terminal.
"""

from __future__ import annotations

import subprocess
import time

# Order matters only for display. big is the workhorse; the others exist
# but are usually stopped anyway.
MODEL_UNITS = ("forge-model-big", "forge-model-vision",
               "forge-model-little", "forge-model-tiny")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def vram() -> str:
    """'22.8 / 24.0 GB used' — or '' on a machine with no NVIDIA GPU."""
    try:
        r = _run("nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits")
        used, total = r.stdout.strip().splitlines()[0].split(",")
        return f"{int(used) / 1024:.1f} / {int(total) / 1024:.1f} GB used"
    except Exception:
        return ""


def running() -> list[str]:
    out = []
    for unit in MODEL_UNITS:
        r = _run("systemctl", "--user", "is-active", unit + ".service")
        if r.stdout.strip() == "active":
            out.append(unit)
    return out


def short(unit: str) -> str:
    return unit.removeprefix("forge-model-")


def off() -> dict:
    """Stop every running model service and report the VRAM that came back."""
    stopped = running()
    for unit in stopped:
        _run("systemctl", "--user", "stop", unit + ".service")
    # systemctl stop waits for the process, but the driver takes a beat to
    # actually release the memory — without this the report shows the old
    # number and looks like nothing happened.
    if stopped:
        time.sleep(1.5)
    return {"stopped": [short(u) for u in stopped], "vram": vram()}


def on(which: str = "big") -> dict:
    """Start one model service. Loading the 30B takes a minute — this
    returns as soon as systemd accepts the job, it does not wait."""
    unit = f"forge-model-{which}"
    if unit not in MODEL_UNITS:
        return {"started": [], "vram": vram(),
                "error": f"no model service named {which!r} — "
                         f"try: {', '.join(short(u) for u in MODEL_UNITS)}"}
    r = _run("systemctl", "--user", "start", unit + ".service")
    err = r.stderr.strip()
    return {"started": [] if err else [which], "vram": vram(),
            "error": err}
