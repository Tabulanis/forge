"""
The power switch.

The local models are the only thing here that holds the GPU — on Void the
122B brain owns ~70 of the 96GB carve-out. Everything runs under systemd user units, so
"shut her down" is just stopping those units; this module is the one place
that knows their names. The dashboard is left running on purpose: it uses no
VRAM, and its Power card is how you wake her back up without a terminal.
"""

from __future__ import annotations

import subprocess
import time

# The stack as it actually runs since the 2026-09-05 cutover: her brain (which
# is also her eyes), the little model that distils memory cards, the embedder
# that makes those cards searchable, and the sealed reviewer that judges every
# answer. All four are enabled and up.
#
# The judge was added on 2026-09-08 and NOT added here, which is the same fault
# the embedder had until the day before: live, enabled, holding graphics memory,
# and invisible to the switch meant to stop it. Caught 2026-09-09 when `forge
# off` reported the GPU freed while the judge still held 21.4 of 96 GB. If you
# add a model service, add it here in the same commit.
LIVE_UNITS = ("forge-model-big122", "forge-model-little", "forge-model-embed",
              "forge-model-judge")

# Retired 2026-09-05, units still on disk but disabled. Kept in the roster for
# one reason only: `forge off` must still be able to stop one if somebody
# starts it by hand. Nothing here is part of the running stack.
LEGACY_UNITS = ("forge-model-big", "forge-model-merge",
                "forge-model-vision", "forge-model-tiny")

MODEL_UNITS = LIVE_UNITS + LEGACY_UNITS

PORTS = {"big122": 8087, "little": 8083, "embed": 8086, "judge": 8088,
         # retired
         "big": 8084, "merge": 8085, "vision": 8090, "tiny": 8081}

# How much GPU memory each model takes once loaded — measured. Only used to
# draw the loading bar; if a model ever changes, the bar just runs fast or slow.
EXPECTED_LOAD_MB = {"big122": 70000, "little": 2100, "embed": 700,
                    "big": 17700, "merge": 17300}

# Nothing competes for the card any more. This used to auto-stop a rival model
# before starting one, because on the 24GB TITAN the 30B and the 27B could
# never fit together. On Void the whole stack — 122B, 3B and embedder — is
# resident at once inside the 96GB carve-out with room to spare, and every
# render moved to the other box entirely. Emptied 2026-09-08: the rule now
# describes a scarcity that no longer exists, and enforcing it would stop a
# model that has every right to be running.
EXCLUSIVE: dict[str, tuple[str, ...]] = {}


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
