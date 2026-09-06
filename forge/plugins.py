"""
House plugins — the registry.

A plugin is a module that owns one *capability* end to end: its tools, its
status readout for the dash, and (optionally) live progress the agent's
heartbeat can show. The point is that "the render box" or "the old machine"
is one thing you can list, ask about, or switch off — not tool code in one
file, URLs in another and stats nowhere.

Contract (all optional; missing = not offered):
    NAME, DESCRIPTION            what to call it
    stats() -> dict              machine/health readout (never raises)
    stats_line(stats=None) -> str  one human line of the above
    progress_line() -> str       live progress of the plugin's current job, '' when idle
    tools(ws) -> list[Tool]      tools it contributes (not yet used by tools.py — see NOTE)

NOTE 2026-09-06: generate_image / generate_video still register in tools.py;
moving them behind tools() is the next step of the plugin split, not done yet.
"""
from __future__ import annotations

from importlib import import_module

PLUGIN_MODULES = ("forge.renderbox",)


def plugins() -> list:
    out = []
    for name in PLUGIN_MODULES:
        try:
            out.append(import_module(name))
        except Exception:
            continue
    return out


def status_lines() -> list[str]:
    lines = []
    for p in plugins():
        fn = getattr(p, "stats_line", None)
        if fn:
            try:
                lines.append(fn())
            except Exception:
                lines.append(f"{getattr(p, 'NAME', p.__name__)}: error")
    return lines


def progress_line() -> str:
    for p in plugins():
        fn = getattr(p, "progress_line", None)
        if fn:
            try:
                line = fn()
                if line:
                    return line
            except Exception:
                pass
    return ""
