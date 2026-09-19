"""Godot API-name checker: is that property/method real, on THAT class?

Born 2026-09-18 from the Penpal port. Every fix a human had to make to Merge's
Godot code that day was the same species — a name written from memory:

    bounce.energy = 0                  # OmniLight3D has light_energy. This
                                       # assignment ABORTS the function it is in,
                                       # so the light was never even parented.
    mesh.set_surface_material(0, m)    # real name — on MeshInstance3D, not on a
                                       # BoxMesh. 13 sites; the file was dead.
    xf.xform(v)                        # Godot 3; in 4.x it is `xf * v`
    camera.update_projection_matrix()  # three.js, ported literally

A flat "is this name known anywhere" index gets two of those four wrong, because
`energy` and `set_surface_material` both exist on OTHER classes. So this is
per-type: it works out each local's class (`var c: Camera3D`, `= Camera3D.new()`),
walks that class's inheritance chain in the 4.7.2 reference, and reports names
that are not on it — with the near-misses from that same chain.

Deliberately silent where it cannot be sure: untyped locals, Dictionaries, the
project's own objects, and classes missing from the local reference (RegEx is,
as of 2026-09-18) are never flagged. A miss costs an engine error; a false
accusation costs trust.
"""
from __future__ import annotations

import difflib
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path

DOCS = Path.home() / "godot-docs" / "classes-4.7.2"

_CLASS = r"[A-Z][A-Za-z0-9_]*"
# var x: Camera3D   /   var x := Camera3D.new()   /   var x = Camera3D.new()
_TYPED = re.compile(rf"(?:^|\s)(?:@onready\s+)?var\s+([a-z_]\w*)\s*:\s*({_CLASS})\b")
_CTOR = re.compile(rf"(?:^|\s)var\s+([a-z_]\w*)\s*:?=\s*({_CLASS})\.new\(")
_ASSIGN = re.compile(rf"(?:^|\s)([a-z_]\w*)\s*=\s*({_CLASS})\.new\(")
_PARAM = re.compile(rf"\b([a-z_]\w*)\s*:\s*({_CLASS})\b")
_USE = re.compile(r"(?<![\w.])([a-z_]\w*)\.([a-zA-Z_]\w*)\b")

# A dot on these is a KEY, not an API name (Dictionary), or an index error the
# engine reports plainly (Array). Judging them here only produces noise; the
# missing-key crash is gdlint's rule.
_DATA_TYPES = {"Dictionary", "Array", "PackedStringArray", "PackedFloat32Array",
               "PackedFloat64Array", "PackedInt32Array", "PackedInt64Array",
               "PackedVector2Array", "PackedVector3Array", "PackedColorArray",
               "PackedByteArray", "Variant", "Signal", "Callable"}

# Declaring a project script as its engine base (`var world: Node3D` for a
# world.gd instance) hides every member the script adds. The reference cannot
# know those, so these types are never judged.
_GENERIC_BASES = {"Node", "Node3D", "Node2D", "Object", "RefCounted", "Resource",
                  "Script", "GDScript", "CanvasItem", "Control", "Variant"}


@lru_cache(maxsize=1)
def _index() -> dict[str, tuple[str, frozenset[str]]]:
    """class -> (inherits, own names)."""
    out: dict[str, tuple[str, frozenset[str]]] = {}
    if not DOCS.exists():
        return out
    for p in DOCS.glob("*.xml"):
        try:
            root = ET.parse(p).getroot()
        except Exception:
            continue
        cls = root.get("name") or p.stem
        names: set[str] = set()
        for tag in ("member", "method", "constant", "signal", "theme_item"):
            for el in root.iter(tag):
                n = el.get("name")
                if n:
                    names.add(n)
        out[cls] = (root.get("inherits") or "", frozenset(names))
    return out


@lru_cache(maxsize=512)
def names_of(cls: str) -> frozenset[str]:
    """Every name on a class and everything it inherits; empty when unknown."""
    idx = _index()
    names: set[str] = set()
    seen: set[str] = set()
    cur = cls
    while cur and cur in idx and cur not in seen:
        seen.add(cur)
        inherits, own = idx[cur]
        names |= own
        cur = inherits
    if not seen:
        return frozenset()
    # Object's methods are on everything; so are the ones every Node has.
    for extra in ("Object", "Node"):
        if extra in idx and extra not in seen and ("Node" in seen or extra == "Object"):
            names |= idx[extra][1]
    return frozenset(names)


def known_class(cls: str) -> bool:
    return cls in _index()


def _code_only(line: str) -> str:
    """Blank string literals and strip the comment (same rule as gdlint)."""
    out, i, n, q = [], 0, len(line), None
    while i < n:
        c = line[i]
        if q:
            if c == "\\" and i + 1 < n:
                out.append("  "); i += 2; continue
            if c == q:
                q = None; out.append(c)
            else:
                out.append(" ")
            i += 1; continue
        if c in "\"'":
            q = c; out.append(c); i += 1; continue
        if c == "#":
            break
        out.append(c); i += 1
    return "".join(out)


def _types_in(lines: list[str]) -> dict[str, str]:
    """local name -> Godot class, for the locals we can be sure about.
    A name assigned two different classes is dropped: too easy to be wrong."""
    types: dict[str, str] = {}
    dropped: set[str] = set()
    for line in lines:
        for rx in (_TYPED, _CTOR, _ASSIGN, _PARAM):
            for name, cls in rx.findall(line):
                if not known_class(cls) or cls in _DATA_TYPES or cls in _GENERIC_BASES:
                    continue
                if name in types and types[name] != cls:
                    dropped.add(name)
                types[name] = cls
    for d in dropped:
        types.pop(d, None)
    return types


def check(path: str | Path, _project: str | Path | None = None) -> str:
    """'' when every name is real on its class; otherwise the findings."""
    p = Path(path)
    try:
        src = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: cannot read {p}: {e}"
    if not _index():
        return (f"Error: the 4.7.2 class reference is not at {DOCS} — "
                "cannot check API names.")
    raw = src.split("\n")
    lines = [_code_only(l) for l in raw]
    # Types are per FUNCTION: the same short name (`st`, `t`, `n`) is a
    # different thing two functions down, and judging it across the file
    # invents errors.
    bounds: list[int] = [i for i, l in enumerate(lines) if re.match(r"^(static\s+)?func\s", l)]
    bounds.append(len(lines))
    scopes: list[tuple[int, int, dict]] = []
    head_end = bounds[0] if bounds else len(lines)
    members = _types_in(lines[:head_end])            # script members: visible everywhere
    for b in range(len(bounds) - 1):
        a0, a1 = bounds[b], bounds[b + 1]
        local = dict(members)
        local.update(_types_in(lines[a0:a1]))
        scopes.append((a0, a1, local))
    if not scopes:
        scopes = [(0, len(lines), members)]
    def _types_at(idx: int) -> dict:
        for a0, a1, t in scopes:
            if a0 <= idx < a1:
                return t
        return members
    hits: dict[str, list] = {}
    for i, line in enumerate(lines, 1):
        at = _types_at(i - 1)
        for recv, name in _USE.findall(line):
            cls = at.get(recv)
            if cls is None:
                continue
            known = names_of(cls)
            if not known or name in known:
                continue
            hits.setdefault(f"{recv}.{name}", [cls, []])[1].append(i)
    if not hits:
        return ""
    out = [f"GDAPI {p.name}: {len(hits)} name(s) that do not exist on the class "
           f"they are used on (Godot 4.7.2). An invented property assignment "
           f"ABORTS the function it is in — the rest of that function never runs."]
    for use, (cls, ls) in sorted(hits.items(), key=lambda kv: kv[1][1][0]):
        name = use.split(".", 1)[1]
        near = difflib.get_close_matches(name, names_of(cls), n=3, cutoff=0.6)
        where = ", ".join(f"{p.name}:{l}" for l in ls[:6])
        more = f" (+{len(ls) - 6} more)" if len(ls) > 6 else ""
        hint = f"\n      {cls} has: {', '.join(near)}" if near else \
               f"\n      not on {cls} or anything it inherits — godot_docs(\"{cls}\") lists what is"
        out.append(f"  {use}  [{cls}]  ({where}{more}){hint}")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        if Path(a).is_dir():
            continue
        r = check(a)
        print(r if r else f"GDAPI {Path(a).name}: clean")
