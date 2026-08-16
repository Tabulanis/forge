"""Her dataset shelf — cited real-world reference data on disk.

The counterpart to the sim shelf, and its fuel. Sims are validated deterministic
COMPUTATION; datasets are cited stable real-world FACTS she looks up. A sim is
only as good as what you feed it — clean equations on guessed inputs is confident
garbage — so the datasets are the grounded values: material properties, physical
constants, empirical figures, each carrying WHERE it came from and WHEN.

Trust here is PROVENANCE, not validation. Real-world data changes and can't be
reproduced against a fixed answer the way a sim's SELFTEST can, so every dataset
is required to cite a source, and it's stamped with the date it was saved. That's
what makes it re-checkable instead of just asserted.

Sims pull from a dataset directly: inside a sim's run(), call dataset("name") to
get its data, so the computation runs on grounded real values, not made-up ones.
Kept on disk forever (cheap); only a query's result enters her context (scarce).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

DATASETS_DIR = Path.home() / "forge" / "datasets"
_CATALOG = DATASETS_DIR / "_catalog.json"


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")[:60]


def _load_catalog() -> dict:
    try:
        return json.loads(_CATALOG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_catalog(cat: dict) -> None:
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CATALOG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cat, indent=1), encoding="utf-8")
    tmp.replace(_CATALOG)


def save_dataset(name: str, data, source: str, description: str = "") -> str:
    name = _safe_name(name)
    if not name:
        return "Error: give the dataset a name (letters/numbers/underscores)."
    if not source or not str(source).strip():
        return ("Error: a dataset MUST cite its source — where did this data come from? "
                "Real-world data can't be validated like a sim; the citation IS its trust.")
    try:
        json.dumps(data)
    except Exception as e:
        return f"Error: data isn't JSON-serializable ({e}). Pass a plain object/list of values."
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"data": data, "source": str(source).strip(),
           "description": str(description or "").strip(), "saved": time.time()}
    tmp = DATASETS_DIR / f"{name}.json.tmp"
    tmp.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    tmp.replace(DATASETS_DIR / f"{name}.json")
    n = len(data) if hasattr(data, "__len__") else None
    cat = _load_catalog()
    cat[name] = {"description": rec["description"], "source": rec["source"],
                 "saved": rec["saved"], "n": n,
                 "keys": (list(data.keys())[:40] if isinstance(data, dict) else None)}
    _save_catalog(cat)
    return (f"Saved dataset '{name}' ({n if n is not None else '?'} entries) — "
            f"source: {rec['source']}. Sims can pull it with dataset('{name}').")


def query_dataset(name: str, key: str | None = None) -> str:
    name = _safe_name(name)
    path = DATASETS_DIR / f"{name}.json"
    if not path.exists():
        return f"No dataset '{name}'. Use list_datasets, or build_dataset to make it."
    rec = json.loads(path.read_text(encoding="utf-8"))
    data = rec.get("data")
    cite = f"[source: {rec.get('source', '?')}]"
    if key is not None and isinstance(data, dict):
        if key in data:
            return f"{name}[{key!r}] = {json.dumps(data[key], default=str)}  {cite}"
        hits = {k: v for k, v in data.items() if str(key).lower() in k.lower()}
        if hits:
            return f"{json.dumps(hits, default=str)[:1500]}  {cite}"
        return f"'{key}' not found in {name}. Keys: {list(data.keys())[:40]}"
    return f"{json.dumps(data, default=str)[:2500]}  {cite}"


def list_datasets() -> str:
    cat = _load_catalog()
    if not cat:
        return "No datasets yet. Use build_dataset to save cited real-world data."
    lines = [f"{len(cat)} dataset(s):"]
    for n, m in sorted(cat.items()):
        lines.append(f"  {n} — {m.get('description', '')} "
                     f"({m.get('n', '?')} entries) [source: {m.get('source', '?')}]")
    return "\n".join(lines)


def load_data(name: str):
    """For sims (via the injected dataset() helper): just the data, {} if missing."""
    path = DATASETS_DIR / f"{_safe_name(name)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("data", {})
    except Exception:
        return {}
