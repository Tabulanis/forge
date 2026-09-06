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

# Order matters only for display. big122 is her brain since 2026-09-05; the
# 27B (merge) and 30B (big) are retired but their units still exist.
MODEL_UNITS = ("forge-model-big122", "forge-model-big", "forge-model-merge",
               "forge-model-vision", "forge-model-little", "forge-model-tiny")

PORTS = {"big122": 8087, "big": 8084, "merge": 8085, "vision": 8090,
         "little": 8083, "tiny": 8081}

# How much GPU memory each model takes once loaded — measured. Only used to
# draw the loading bar; if a model ever changes, the bar just runs fast or slow.
EXPECTED_LOAD_MB = {"big122": 70000, "big": 17700, "merge": 17300}

# Models that shouldn't share the GPU at once. On the 24GB TITAN big and merge
# never fit together; on Void (64GB carve-out) the 122B plus either of them
# pushes into borrowed system RAM hard enough to matter. Starting one
# auto-stops its rivals first, instead of leaving that as a comment a human
# has to remember — a rule that isn't enforced gets crossed eventually.
EXCLUSIVE = {"big122": ("big", "merge"), "big": ("merge", "big122"),
             "merge": ("big", "big122")}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


def _amd_mb() -> tuple[int, int] | None:
    """(used, total) MiB from the amdgpu sysfs counters — the AMD box has no
    nvidia-smi. Picks the first card that exposes the counters."""
    import glob
    for d in glob.glob("/sys/class/drm/card*/device"):
        try:
            used = int(open(f"{d}/mem_info_vram_used").read())
            total = int(open(f"{d}/mem_info_vram_total").read())
            return used // 1048576, total // 1048576
        except Exception:
            continue
    return None


def vram() -> str:
    """'22.8 / 24.0 GB used' — NVIDIA or AMD; '' if neither answers."""
    try:
        r = _run("nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits")
        used, total = r.stdout.strip().splitlines()[0].split(",")
        return f"{int(used) / 1024:.1f} / {int(total) / 1024:.1f} GB used"
    except Exception:
        pass
    amd = _amd_mb()
    if amd:
        return f"{amd[0] / 1024:.1f} / {amd[1] / 1024:.1f} GB used"
    return ""


def vram_mb() -> int | None:
    try:
        r = _run("nvidia-smi", "--query-gpu=memory.used",
                 "--format=csv,noheader,nounits")
        return int(r.stdout.strip().splitlines()[0])
    except Exception:
        pass
    amd = _amd_mb()
    return amd[0] if amd else None


def is_ready(which: str) -> bool:
    """llama-server answers /health with 200 only once the weights are
    actually loaded — before that the port refuses or says 503. This is
    the difference between 'systemd started it' and 'she can talk'."""
    import urllib.request
    port = PORTS.get(which)
    if not port:
        return False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


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


def on(which: str = "big122") -> dict:
    """Start one model service. Loading a model takes a minute — this
    returns as soon as systemd accepts the job, it does not wait.
    Auto-stops whatever this model can't share the card with (see
    EXCLUSIVE) — VRAM math is not something to leave to a reminder."""
    unit = f"forge-model-{which}"
    if unit not in MODEL_UNITS:
        return {"started": [], "vram": vram(),
                "error": f"no model service named {which!r} — "
                         f"try: {', '.join(short(u) for u in MODEL_UNITS)}"}
    stopped = []
    # running() returns full unit names; compare like with like. (The old
    # check compared "merge" against "forge-model-merge" and never fired —
    # the auto-stop had been decorative since it was written.)
    live = {short(u) for u in running()}
    for rival in EXCLUSIVE.get(which, ()):
        if rival in live:
            _run("systemctl", "--user", "stop", f"forge-model-{rival}.service")
            stopped.append(rival)
    if stopped:
        time.sleep(1.5)   # let the driver actually release the memory
    r = _run("systemctl", "--user", "start", unit + ".service")
    err = r.stderr.strip()
    return {"started": [] if err else [which], "stopped_for_room": stopped,
            "vram": vram(), "error": err}
