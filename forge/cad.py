"""CAD authoring for Merge — talk to the Maker Studio (tiny SolidWorks).

A part is plain JSON: named params + a feature tree of primitives combined by
CSG (add / cut / keep). Merge writes that JSON; the Studio renders it in 3D.
These tools let her save a part and get it back with a URL to look at.

The Studio server (aidojo/current/MakerStudio, port 8840) does the geometry;
this module just validates a part and hands it over, so a malformed tree comes
back as a plain error instead of silent garbage.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

STUDIO = "http://127.0.0.1:8840"
STUDIO_DIR = Path.home() / "aidojo" / "current" / "MakerStudio"
RENDERS = STUDIO_DIR / "renders"
SHAPES = {"box", "cylinder", "sphere", "cone"}
OPS = {None, "add", "cut", "keep"}


def _render(name: str) -> str | None:
    """Screenshot a saved part so the user SEES it. Returns the PNG path, or
    None if rendering isn't available — the caller still hands back the link."""
    try:
        RENDERS.mkdir(exist_ok=True)
        out = RENDERS / f"{name}.png"
        r = subprocess.run(
            [sys.executable, str(STUDIO_DIR / "render.py"), name, str(out)],
            capture_output=True, text=True, timeout=60)
        return str(out) if (r.returncode == 0 and out.is_file()) else None
    except Exception:
        return None


def _validate(part: dict) -> str | None:
    if not isinstance(part, dict):
        return "part must be an object with 'params' and 'features'"
    feats = part.get("features")
    if not isinstance(feats, list) or not feats:
        return "part needs a non-empty 'features' list"
    for i, f in enumerate(feats):
        if not isinstance(f, dict):
            return f"feature {i} must be an object"
        if f.get("shape") not in SHAPES:
            return f"feature {i}: shape must be one of {sorted(SHAPES)}, got {f.get('shape')!r}"
        if f.get("op") not in OPS:
            return f"feature {i}: op must be add/cut/keep, got {f.get('op')!r}"
    if feats[0].get("op") in ("cut", "keep"):
        return "feature 0 can't be a cut/keep — the first feature is the base to build on"
    return None


def design_part(part) -> str:
    """Save a CAD part to the Maker Studio and get a link to see it.

    part: a dict (or JSON string) shaped like
      {"name": "bracket",
       "params": {"w": 40, "h": 20, "hole": 5},
       "features": [
         {"shape": "box", "w": "w", "h": "h", "d": 8},
         {"shape": "cylinder", "op": "cut", "r": "hole/2", "h": 10}]}
    op is add (default) / cut / keep; number fields may be params or
    expressions over params like "w/2". Optional per-feature at:[x,y,z],
    rotate:[x,y,z] degrees, scale:[x,y,z].
    """
    if isinstance(part, str):
        try:
            part = json.loads(part)
        except Exception as e:
            return f"That wasn't valid JSON: {e}"
    err = _validate(part)
    if err:
        return f"Part rejected: {err}. Nothing was saved — fix the tree and resend."
    try:
        r = httpx.post(f"{STUDIO}/api/model", json=part, timeout=15)
        data = r.json()
    except Exception:
        return ("Couldn't reach the Maker Studio (is it running on :8840?). The "
                "part looks valid; start the studio and resend to see it.")
    if not data.get("ok"):
        return f"Studio rejected it: {data.get('error')}"
    name = data["name"]
    png = _render(name)
    msg = f"Saved part '{name}' ({data['features']} features)."
    if png:
        # The path in this line makes the picture appear in the user's chat,
        # and lets you look_at_image your own design to check and refine it.
        msg += f"\nRendered to {png}"
    msg += f"\nSpin it live in 3D: {STUDIO}/?load={name}"
    return msg


def list_parts() -> str:
    """List the CAD parts saved in the Maker Studio."""
    try:
        names = httpx.get(f"{STUDIO}/api/models", timeout=10).json()
    except Exception:
        return "Couldn't reach the Maker Studio (:8840)."
    return "Saved parts: " + (", ".join(names) if names else "(none yet)")


def get_part(name: str) -> str:
    """Fetch a saved part's JSON so you can read or modify its feature tree."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:60]
    try:
        r = httpx.get(f"{STUDIO}/api/model/{safe}", timeout=10)
        if r.status_code != 200:
            return f"No part named '{name}'."
        return json.dumps(r.json(), indent=1)
    except Exception:
        return "Couldn't reach the Maker Studio (:8840)."
