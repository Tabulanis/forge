"""JS-habit linter for GDScript (Godot 4.x).

Born 2026-09-18 from the Penpal port: Merge drafted ~2,500 lines of GDScript
from JavaScript and every file carried the same dozen habits — `x == false`
for `!x`, `(a or "")` as a value, `dict()`, `.length`, `a ? b : c`, `await`,
`Rng.next() % n` — which Claude then fixed by script, ~150 sites, because
Godot reports ONE parse error per run and a 20-minute Merge turn per error
was the bottleneck. This is that script, made a tool: every finding is a
line-numbered "found → write this instead", the format she executes well.

lint(path) -> str: "" when clean, otherwise the findings grouped by rule.
"""
from __future__ import annotations

import re
from pathlib import Path

# (id, severity, regex, message). Applied per line with strings and comments
# blanked out (see _code_only) so a JS snippet inside a comment is not a hit.
RULES: list[tuple[str, str, re.Pattern, str]] = [
    ("eq-false", "error", re.compile(r"==\s*false\b(?!.*==\s*null)"),
     "JS `!x` is `not x`. `x == false` is FALSE when x is null/absent, so a missing key reads as 'true'."),
    ("or-value", "error", re.compile(r"\bor\s+(\"\"|''|\[\]|\{\}|0|null|[a-zA-Z_][\w.]*\))\s*[\)\.]|\(\s*[\w.\"()]+\s+or\s+[\w.\"()\[\]{}]+\s*\)\s*\."),
     "JS `a || b` as a VALUE is `a if a else b`. GDScript `or` yields a bool, so `(x or \"\").findn()` crashes."),
    ("dict-call", "error", re.compile(r"\bdict\("),
     "There is no dict(). `d.merged(other, true)` already returns a new Dictionary; `d.duplicate()` copies."),
    ("length-prop", "error", re.compile(r"\.length\b(?!\s*\()"),
     "`.length` is JS. Strings: `.length()`; arrays/dictionaries: `.size()`."),
    ("ternary", "error", re.compile(r"[^?]\?\s+[^:?]+?\s+:\s+"),
     "JS `a ? b : c` is `b if a else c`."),
    ("await", "warn", re.compile(r"\bawait\b"),
     "`await` makes the function a coroutine that returns before it finishes; the brain is called synchronously. Drop it unless you really wait on a signal."),
    ("rng-mod", "error", re.compile(r"Rng\.next\(\)\s*%"),
     "Rng.next() is a float in [0,1) like Math.random. Pick with `list[int(Rng.next() * list.size())]`, scale with `Rng.next() * n`."),
    ("match-glob", "warn", re.compile(r"\.match\(\s*[\"']"),
     "String.match() is a GLOB (* ?), not a regex. Use `RegEx.create_from_string(r\"...\")` and `.search()`."),
    ("replace-regex", "error", re.compile(r"\.replace\(\s*\"[^\"]*(\\\\[dDwWsSb.]|\[[^\]]+\]|\^|\$)[^\"]*\""),
     "String.replace() is literal text. A regex needs `RegEx.create_from_string(r\"...\").sub(s, repl, true)`."),
    ("js-op", "error", re.compile(r"===|!==|\|\||&&|=>|\bundefined\b|\.push\(|\.includes\(|\.forEach\(|\.some\(|\.flatMap\(|\.flat_map\(|\bMath\.[a-z]"),
     "JavaScript operator or method. == / != / or / and / func(): / null / .append() / .has() / for-in / any() / manual flatten / min() max() abs() floor()."),
    # only a lambda stored as a VALUE (dict entry or assignment); a signal callback `.connect(func(): x())` needs no return
    ("lambda-noreturn", "error", re.compile(r"(?:\"\s*:|=)\s*func\s*\([^)]*\)\s*:\s*(?!return\b|pass\b)[a-zA-Z_][\w.]*\("),
     "A one-line lambda needs `return`: `func(): return _T(...)`. Without it the lambda returns null."),
    ("dict-lambda-multiline", "error", re.compile(r"\"\s*:\s*func\s*\([^)]*\)\s*:\s*$"),
     "A multi-line lambda inside a dictionary literal does not parse. Make it a named method `func _thing_verb(g):` and store the name: `\"use\": _thing_verb`."),
    ("sort-cmp", "warn", re.compile(r"\.sort\(\s*func"),
     "Array.sort() takes no comparator; use `.sort_custom(func(a, b): return a.x > b.x)`. NOTE sort_custom is not stable — break ties on an index if order matters."),
    ("array-join", "error", re.compile(r"(\]|\))\.join\("),
     "Arrays have no join(). Write `\", \".join(array)`."),
    ("get-tree", "warn", re.compile(r"\bget_tree\(\)"),
     "Only Nodes have get_tree(). In a RefCounted brain script use `Engine.get_main_loop()`, or fire the callback directly in dry mode."),
    ("object-get-default", "warn", re.compile(r"\b(m|mystery)\.get\(\s*\"[^\"]+\"\s*,"),
     "Object.get(name) takes NO default — that is Dictionary.get. If this is a script instance (a mystery), use `m.get(k) if k in m else default`; if it is a Dictionary here, ignore."),
]

_RETURN_TYPED = re.compile(r"^\s*(?:static\s+)?func\s+\w+\([^)]*\)\s*->\s*(String|int|float|bool|Array|Dictionary)\s*:")
_RETURN_NULL = re.compile(r"^\s*return\s*(null)?\s*(#.*)?$")
_INT_INIT = re.compile(r"^\s*var\s+(\w+)\s*:=\s*-?\d+\s*(#.*)?$")
_FLOAT_USE = re.compile(r"\b{name}\s*[+\-*/]=\s*[^#\n]*(\d\.\d|/\s)")
_INNER_CLASS = re.compile(r"^class\s+\w+\s*(\([^)]*\))?\s*:")
_NEW_FROM = re.compile(
    r"^\s*(?:var\s+)?(\w+)\s*(?::\s*\w+\s*)?:?=\s*(?:load|preload)\(\s*[\"'](res://[^\"']+\.gd)[\"']\s*\)\s*\.new\(")
_ADD_CHILD = re.compile(r"\badd_child\(\s*(\w+)\s*[,)]")
_NOT_A_NODE = {"RefCounted", "Object", "Resource"}


def _code_only(line: str) -> str:
    """Blank string literals and strip the comment so patterns see code only."""
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


def lint(path: str | Path) -> str:
    p = Path(path)
    try:
        src = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: cannot read {p}: {e}"
    lines = src.split("\n")
    code = [_code_only(l) for l in lines]
    found: dict[str, list[tuple[int, str]]] = {}
    msgs: dict[str, tuple[str, str]] = {}

    def hit(rid: str, sev: str, msg: str, ln: int, text: str) -> None:
        found.setdefault(rid, []).append((ln, text.strip()[:110]))
        msgs[rid] = (sev, msg)

    for i, c in enumerate(code, 1):
        for rid, sev, rx, msg in RULES:
            if rx.search(c):
                hit(rid, sev, msg, i, lines[i - 1])

    # -> String / -> Dictionary ... that `return null` (or bare return)
    cur_type, cur_indent = None, 0
    for i, c in enumerate(code, 1):
        m = _RETURN_TYPED.match(c)
        if m:
            cur_type, cur_indent = m.group(1), len(c) - len(c.lstrip("\t"))
            continue
        if cur_type and c.strip() and (len(c) - len(c.lstrip("\t"))) <= cur_indent and not c.startswith(" "):
            cur_type = None
        if cur_type and _RETURN_NULL.match(c):
            hit("typed-return-null", "error",
                f"Function is declared `-> {cur_type}` but returns null. Use `-> Variant` (or return a real {cur_type}).", i, lines[i - 1])

    # `var n := 0` (int) that later takes a fraction: `n += x * 0.5` truncates
    for i, c in enumerate(code, 1):
        m = _INT_INIT.match(c)
        if not m:
            continue
        name = m.group(1)
        rx = re.compile(_FLOAT_USE.pattern.format(name=re.escape(name)))
        for j in range(i, min(len(code), i + 60)):
            if rx.search(code[j]):
                hit("int-truncates", "error",
                    f"`var {name} := 0` is an int; `{name} += ... * 0.5` truncates to int. Initialise with `0.0`.", i, lines[i - 1])
                break

    # add_child(x) where x was built from a script that is NOT a Node.
    # 2026-09-19, chunk 5c-1: play.gd did `add_child(actor)`; game/actor.gd `extends
    # RefCounted`, so the engine refused it ("Required object rp_child is null"). The
    # checker can know this for certain: it reads the other script's `extends` line.
    # It never guesses — a script it cannot find or read is simply not flagged.
    root = p.resolve().parent
    for _ in range(6):
        if (root / "project.godot").is_file():
            break
        root = root.parent
    built: dict[str, str] = {}
    for raw, c in zip(lines, code):
        if ".new(" not in c:        # code-only blanks string bodies, so match the raw
            continue                # line — but only where real code calls .new()
        m = _NEW_FROM.match(raw)
        if m:
            built[m.group(1)] = m.group(2)
    for i, c in enumerate(code, 1):
        m = _ADD_CHILD.search(c)
        if not m or m.group(1) not in built:
            continue
        res = built[m.group(1)]
        target = root / res[len("res://"):]
        try:
            head = target.read_text(encoding="utf-8")
        except Exception:
            continue                      # cannot read it -> say nothing
        base = ""
        for l in head.split("\n")[:40]:
            if l.startswith("extends "):
                base = l.split(None, 1)[1].strip()
                break
        if base and base not in _NOT_A_NODE:
            continue                      # it IS a Node (or something derived) -> fine
        what = f"`extends {base}`" if base else "no `extends` line, so RefCounted"
        hit("add-child-not-a-node", "error",
            f"add_child() on an object that is not a Node. {res} has {what}. "
            "A RefCounted lives by reference alone — keep it in a `var`, do not put it in "
            "the scene tree. Check what a script extends BEFORE writing add_child().",
            i, lines[i - 1])

    # an inner `class X:` holding everything: the script itself should be the class
    top_funcs = sum(1 for c in code if re.match(r"^(static\s+)?func\s", c))
    for i, c in enumerate(code, 1):
        if _INNER_CLASS.match(c) and top_funcs <= 2:
            hit("inner-class", "warn",
                "The script IS the class: tests call `load(path).new(opts)`. Remove the `class X:` wrapper, dedent, use `_init(opts)`.", i, lines[i - 1])

    if not found:
        return ""
    order = [r[0] for r in RULES] + ["typed-return-null", "int-truncates",
                                     "add-child-not-a-node", "inner-class"]
    out = []
    total = sum(len(v) for v in found.values())
    errors = sum(len(v) for k, v in found.items() if msgs[k][0] == "error")
    out.append(f"GDLINT {p.name}: {total} finding(s) in {len(found)} rule(s), {errors} error(s). "
               "Fix these BEFORE godot_check — Godot shows one parse error per run, this shows them all.")
    for rid in order:
        if rid not in found:
            continue
        sev, msg = msgs[rid]
        sites = found[rid]
        out.append(f"\n[{rid}] ({sev}, {len(sites)}) {msg}")
        for ln, text in sites[:12]:
            out.append(f"  {p.name}:{ln}: {text}")
        if len(sites) > 12:
            out.append(f"  ... and {len(sites) - 12} more (same fix)")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    for a in sys.argv[1:]:
        r = lint(a)
        print(r if r else f"GDLINT {Path(a).name}: clean")
