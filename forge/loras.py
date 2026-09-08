"""
LoRAs: the ecosystem Merge can grow herself.

A LoRA is a small add-on to a base model that changes its look, its motion or what it knows how to
draw. The bases stay Apache/MIT (the house rule); every LoRA installed here is recorded in a
manifest with where it came from and what its author allows, so any render can be traced back.

Pieces:
  search(query, family, nsfw)   -> what Civitai has for one of our model families
  install(version_id)           -> download on Void (fast), ship to the render box, link it into
                                   ComfyUI, record it
  installed(family) / remove()  -> the manifest
  resolve(["name:0.7", ...])    -> (file, strength) pairs the render workflows chain in

Adult content is allowed (this is a private studio) with two hard limits that never move:
no real people, no minors. Civitai hides adult models without an account: put an API key in
config.yaml media.civitai_token (or CIVITAI_TOKEN in the environment).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MANIFEST = Path.home() / ".forge" / "loras.json"
STAGE = Path.home() / ".forge" / "lora-stage"          # Void downloads at ~20 MB/s; the render box at ~1
RENDER_HOST = "tabulanis@10.42.0.1"
RENDER_STORE = "comfy-models/loras"                     # on the render box, relative to its home
RENDER_COMFY = "ComfyUI/models/loras"                   # where ComfyUI looks (symlinks, subfolder per family)
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8", RENDER_HOST]
API = "https://civitai.com/api/v1"

# Which base a LoRA was trained for decides where it can go. Civitai's baseModel strings, verified 2026-09-07.
FAMILIES = {
    "wan14b": {"base_models": ("Wan Video 14B t2v", "Wan Video"),
               "use": "video drafts and long takes (our Wan 2.1 14B through VACE) — the main one",
               "default_strength": 0.8},
    "wan22":  {"base_models": ("Wan Video 2.2 T2V-A14B", "Wan Video 2.2 I2V-A14B"),
               "use": "Wan 2.2 14B LoRAs: same architecture as our 2.1 base, LOW-noise variants usually work — test each",
               "default_strength": 0.7},
    "klein":  {"base_models": ("Flux.2 Klein 4B",),
               "use": "stills (the reference preset, FLUX.2 klein 4B)",
               "default_strength": 0.8},
    "qwen":   {"base_models": ("Qwen Image", "Qwen-Image"),
               "use": "the slow Qwen stills (real / edit / masterpiece)",
               "default_strength": 0.8},
}

# What is already on the belt, so the manifest tells the whole truth. Not removable.
BUILTIN = [
    {"name": "lightx2v-distill", "family": "wan14b", "file": "Wan21_T2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
     "license": {"note": "Apache 2.0 (lightx2v)"}, "builtin": True, "always_on": True, "default_strength": 1.0,
     "note": "the 4-step distill every draft already uses; not something to add by hand"},
    {"name": "samsung-realism", "family": "qwen", "file": "samsung_qwen2512.safetensors", "default_strength": 0.8,
     "license": {"note": "see source"}, "builtin": True, "note": "the real preset's realism layer (Qwen-Image-2512)"},
    {"name": "flymy-realism", "family": "qwen", "file": "flymy_realism.safetensors", "default_strength": 0.8,
     "license": {"note": "see source"}, "builtin": True, "note": "alternative Qwen realism layer, not in a preset"},
]


# ---------------------------------------------------------------- manifest

def _load() -> list[dict]:
    try:
        data = json.loads(MANIFEST.read_text())
        items = data if isinstance(data, list) else data.get("loras", [])
    except Exception:
        items = []
    names = {i.get("name") for i in items}
    for b in BUILTIN:
        if b["name"] not in names:
            items.append(dict(b))
    return items


def _save(items: list[dict]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(items, indent=1))


def installed(family: str | None = None) -> list[dict]:
    items = _load()
    return [i for i in items if not family or i.get("family") == family]


def remove(name: str) -> str:
    items = _load()
    hit = next((i for i in items if i.get("name", "").lower() == name.lower()), None)
    if not hit:
        return f"no LoRA called {name!r} in the manifest"
    if hit.get("builtin"):
        return f"{name} is built in (part of a preset) — it stays"
    try:
        subprocess.run(SSH + [f"rm -f ~/{RENDER_COMFY}/{hit['file']} ~/{RENDER_STORE}/{hit['file']}"],
                       check=True, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return f"could not delete the file on the render box: {str(e)[:200]}"
    _save([i for i in items if i is not hit])
    return f"removed {name} ({hit['file']})"


def resolve(specs: list[str] | None, family: str) -> list[tuple[str, float]]:
    """['name', 'name:0.6', 'file.safetensors:1.0'] -> [(file, strength)] for one family. Raises on a miss."""
    out: list[tuple[str, float]] = []
    items = _load()
    for spec in specs or []:
        spec = str(spec).strip()
        if not spec:
            continue
        name, _, s = spec.rpartition(":") if re.search(r":\s*[0-9.]+\s*$", spec) else (spec, "", "")
        key = name.strip().lower()
        hit = next((i for i in items if i.get("name", "").lower() == key
                    or i.get("file", "").lower() in (key, f"{family}/{key}")
                    or Path(i.get("file", "")).stem.lower() == key), None)
        if not hit:
            known = ", ".join(i["name"] for i in items if i.get("family") == family) or "(none installed)"
            raise ValueError(f"no LoRA called {name!r}. Installed for {family}: {known}. lora_search finds more.")
        if hit.get("family") != family and not (hit.get("family") == "wan22" and family == "wan14b"):
            raise ValueError(f"{hit['name']} is a {hit.get('family')} LoRA; this render uses the {family} model")
        if hit.get("always_on"):
            continue                                   # already in the workflow
        strength = float(s) if s else float(hit.get("default_strength") or FAMILIES[family]["default_strength"])
        out.append((hit["file"], max(0.0, min(2.0, strength))))
    return out


# ---------------------------------------------------------------- civitai

def _token() -> str:
    tok = os.environ.get("CIVITAI_TOKEN", "")
    if not tok:
        try:
            from .config import load_config
            tok = str(((load_config().get("media") or {}).get("civitai_token")) or "")
        except Exception:
            tok = ""
    return tok.strip()


def _api(path: str, params: dict | None = None, timeout: int = 30) -> dict:
    url = f"{API}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "forge-merge/1.0"})
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _family_of(base_model: str) -> str | None:
    b = (base_model or "").strip()
    for fam, spec in FAMILIES.items():
        if b in spec["base_models"]:
            return fam
    for fam, spec in FAMILIES.items():                 # loose match ("Qwen Image 2512" etc.)
        if any(b.lower().startswith(x.lower()) for x in spec["base_models"]):
            return fam
    return None


def _license(m: dict) -> dict:
    return {"commercial": m.get("allowCommercialUse") or [], "credit_required": not m.get("allowNoCredit", True),
            "derivatives": bool(m.get("allowDerivatives", False)), "different_license": bool(m.get("allowDifferentLicense", False))}


def search(query: str, family: str = "wan14b", nsfw: bool | None = None, limit: int = 10,
           sort: str = "Most Downloaded") -> list[dict]:
    """Civitai LoRAs for one of our families. nsfw=None shows both; True only adult; False only safe.
    Adult results need media.civitai_token."""
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {sorted(FAMILIES)}")
    # Civitai's text search is fuzzy and its base-model filter is strict: "realism" + filter finds nothing,
    # "wan realism" + filter finds the Wan ones. So: try the family word in front, with the filter, then the
    # plain query filtered on our side. Results merge, newest attempt never displaces an earlier hit.
    hint = {"wan14b": "wan", "wan22": "wan 2.2", "klein": "klein", "qwen": "qwen"}[family]
    attempts = [(f"{hint} {query}", True), (query, True), (f"{hint} {query}", False), (query, False)]
    seen: set = set(); items: list[dict] = []
    for q, use_filter in attempts:
        params: dict = {"query": q, "types": "LORA", "limit": max(1, min(50, int(limit) * 3)), "sort": sort}
        if use_filter:
            params["baseModels"] = list(FAMILIES[family]["base_models"])
        if nsfw is not None:
            params["nsfw"] = "true" if nsfw else "false"
        try:
            data = _api("models", params)
        except Exception:
            continue
        for m in data.get("items", []):
            if m.get("id") in seen:
                continue
            seen.add(m.get("id")); items.append(m)
        if len(items) >= limit * 2:
            break
    out = []
    for m in items:
        for v in m.get("modelVersions", [])[:1]:
            fam = _family_of(v.get("baseModel", ""))
            if fam not in (family, "wan22" if family == "wan14b" else family):
                continue
            files = [f for f in v.get("files", []) if str(f.get("name", "")).endswith(".safetensors")] or v.get("files", [])
            f = next((x for x in files if x.get("primary")), files[0] if files else {})
            out.append({"model_id": m.get("id"), "version_id": v.get("id"), "name": m.get("name"), "family": fam,
                        "base_model": v.get("baseModel"), "nsfw": bool(m.get("nsfw")), "downloads": (m.get("stats") or {}).get("downloadCount"),
                        "rating": (m.get("stats") or {}).get("rating"), "triggers": v.get("trainedWords") or [],
                        "size_mb": int((f.get("sizeKB") or 0) / 1024), "license": _license(m),
                        "about": re.sub(r"<[^>]+>", " ", str(m.get("description") or ""))[:240].strip(),
                        "url": f"https://civitai.com/models/{m.get('id')}?modelVersionId={v.get('id')}"})
        if len(out) >= limit:
            break
    return out[:limit]


def install(version_id: int, family: str | None = None, name: str | None = None, strength: float | None = None,
            progress=None) -> dict:
    """Fetch one Civitai model version onto the render box and record it. Returns the manifest entry."""
    v = _api(f"model-versions/{int(version_id)}")
    fam = family or _family_of(v.get("baseModel", ""))
    if fam not in FAMILIES:
        raise ValueError(f"{v.get('model', {}).get('name')} is for base {v.get('baseModel')!r}, which none of our models use")
    files = [f for f in v.get("files", []) if str(f.get("name", "")).endswith(".safetensors")]
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f:
        raise ValueError("no .safetensors file in that version")
    mname = str(name or v.get("model", {}).get("name") or f["name"])
    slug = re.sub(r"[^a-z0-9]+", "-", mname.lower()).strip("-")[:48] or f"lora-{version_id}"
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", str(f["name"]))
    items = _load()
    if any(i.get("version_id") == v.get("id") for i in items):
        return next(i for i in items if i.get("version_id") == v.get("id"))
    # 1. download on Void
    STAGE.mkdir(parents=True, exist_ok=True)
    local = STAGE / fname
    url = f["downloadUrl"] or f"{API}/download/models/{version_id}"
    tok = _token()
    if tok:
        url += ("&" if "?" in url else "?") + "token=" + urllib.parse.quote(tok)
    req = urllib.request.Request(url, headers={"User-Agent": "forge-merge/1.0"})
    t0 = time.time(); got = 0
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError("Civitai wants a login for downloads" + (" (the key in config was refused)" if tok else "")
                               + ": make an API key at civitai.com/user/account and put it in config.yaml under media: civitai_token") from None
        raise
    with r, open(local, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk); got += len(chunk)
            if progress and total and got % (32 << 20) < (1 << 20):
                progress(f"lora {slug}: {got * 100 // total}%")
    if local.stat().st_size < 1_000_000:
        head = local.read_bytes()[:200]
        local.unlink(missing_ok=True)
        raise RuntimeError("download came back tiny — usually a login wall; set media.civitai_token. Server said: "
                           + head.decode("utf-8", "replace")[:160])
    # 2. ship to the render box, link into ComfyUI (subfolder per family)
    rel = f"{fam}/{fname}"
    subprocess.run(SSH + [f"mkdir -p ~/{RENDER_STORE}/{fam} ~/{RENDER_COMFY}/{fam}"], check=True, capture_output=True, text=True, timeout=30)
    subprocess.run(["rsync", "-a", "-e", " ".join(SSH[:-1]), str(local), f"{RENDER_HOST}:{RENDER_STORE}/{rel}"],
                   check=True, capture_output=True, text=True, timeout=1800)
    subprocess.run(SSH + [f"ln -sf ~/{RENDER_STORE}/{rel} ~/{RENDER_COMFY}/{rel}"], check=True, capture_output=True, text=True, timeout=30)
    local.unlink(missing_ok=True)
    entry = {"name": slug, "family": fam, "file": rel, "base_model": v.get("baseModel"),
             "model_id": v.get("modelId"), "version_id": v.get("id"), "source": f"https://civitai.com/models/{v.get('modelId')}?modelVersionId={v.get('id')}",
             "license": _license(v.get("model", {})) if "allowCommercialUse" in v.get("model", {}) else {"note": "see source"},
             "nsfw": bool(v.get("model", {}).get("nsfw")), "triggers": v.get("trainedWords") or [],
             "default_strength": float(strength) if strength else FAMILIES[fam]["default_strength"],
             "size_mb": int(got / 1048576), "installed": time.strftime("%Y-%m-%d"),
             "seconds": int(time.time() - t0)}
    items.append(entry); _save(items)
    return entry


def describe(entry: dict) -> str:
    lic = entry.get("license") or {}
    l = lic.get("note") or (f"commercial: {', '.join(lic.get('commercial') or []) or 'no'}; credit "
                            f"{'required' if lic.get('credit_required') else 'not required'}")
    trig = f"; triggers: {', '.join(entry['triggers'][:3])}" if entry.get("triggers") else ""
    return (f"{entry['name']} [{entry.get('family')}] {entry.get('file')} — strength {entry.get('default_strength', '?')}"
            f"{'; ADULT' if entry.get('nsfw') else ''}{'; built in' if entry.get('builtin') else ''}; {l}{trig}"
            + (f"; {entry['note']}" if entry.get("note") else ""))
