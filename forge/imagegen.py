"""Image generation through ComfyUI, on the GPU, with the model chosen by name.

Presets are the house's picture-making tiers (2026-09-05):
  sketch      Z-Image-Turbo — ~20 s, text only. The everyday one.
  reference   FLUX.2-klein 9B — takes reference images: "make it look like this".
  masterpiece Qwen-Image — the flagship (Apache 2.0). ~28 GB, so it can't sit
              beside her brain: the caller sleeps the brain, renders, wakes it.
  (Licensing rule, 2026-09-05: only permissively licensed models — Apache/MIT.
   FLUX.2-dev and klein-9B are non-commercial-licensed and were removed.)

ComfyUI keeps nothing resident between jobs: every render ends with /free so
the brain keeps the memory. Cold render costs ~20 s more than warm. Measured:
the first time both stayed loaded, swap filled and the box strained.

Everything about WHERE this runs is config (media.imagegen_url) — ComfyUI on
this box today, could be any box tomorrow.
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8188"

PRESETS = {
    "sketch": {
        "unet": "z_image_turbo_bf16.safetensors", "clip": "qwen_3_4b.safetensors",
        "clip_type": "lumina2", "vae": "z_image_ae.safetensors",
        "steps": 8, "cfg": 1.0, "sampler": "res_multistep", "scheduler": "simple",
        "shift": 3.0, "latent": "EmptySD3LatentImage", "references": False,
    },
    # FLUX.2-klein 4B — Apache 2.0, takes reference images.
    "reference": {
        "unet": "flux-2-klein-4b.safetensors", "clip": "qwen_3_4b.safetensors",   # same file Z-Image uses (hash-identical)
        "clip_type": "flux2", "vae": "flux2-vae.safetensors",
        "steps": 4, "cfg": 1.0, "sampler": "euler", "scheduler": "flux2",
        "latent": "EmptyFlux2LatentImage", "references": True,
    },
    # Qwen-Image (Apache 2.0) — the flagship. Comfy's recipe: fp8 model +
    # Lightning 8-step LoRA, shift 3.1, euler/simple, cfg 1, 1328². ~28 GB, so
    # it does NOT fit beside her 122B brain: the tool puts the brain to sleep,
    # renders, and wakes it (see tools._generate_image). Minutes, not seconds.
    "masterpiece": {
        "unet": "qwen_image_fp8_e4m3fn.safetensors", "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "clip_type": "qwen_image", "vae": "qwen_image_vae.safetensors",
        "lora": "Qwen-Image-Lightning-8steps-V1.0.safetensors",
        "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
        "shift": 3.1, "latent": "EmptySD3LatentImage", "references": False,
        "size": 1328, "needs_brain_asleep": True,
    },
}


def _post(base: str, path: str, body: dict, timeout: float = 60) -> dict:
    req = urllib.request.Request(f"{base}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _get(base: str, path: str, timeout: float = 30):
    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as r:
        return r.read()


def available(base: str = DEFAULT_URL) -> tuple[bool, str]:
    try:
        d = json.loads(_get(base, "/system_stats", 5))
        dev = (d.get("devices") or [{}])[0]
        return True, f"ComfyUI up on {dev.get('name', '?')[:40]}"
    except Exception as e:
        return False, f"ComfyUI not answering at {base} ({type(e).__name__}) — systemctl --user start comfyui"


def _upload(base: str, path: Path) -> str:
    """Put a reference image where ComfyUI can LoadImage it; returns its name."""
    boundary = "----forge" + random.randbytes(8).hex()
    data = path.read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{base}/upload/image", body,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["name"]


def _workflow(p: dict, prompt: str, seed: int, width: int, height: int,
              steps: int | None, ref_names: list[str]) -> dict:
    w = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": p["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": p["clip_type"], "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}},
        "7": {"class_type": p["latent"], "inputs": {"width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "merge/img"}},
    }
    model = ["1", 0]
    if p.get("lora"):
        w["1l"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": p["lora"], "strength_model": 1.0}}
        model = ["1l", 0]
    if "shift" in p:
        w["4"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": p["shift"]}}
        model = ["4", 0]
    positive = ["5", 0]
    # References: each image is encoded by the VAE and chained onto the
    # conditioning — the FLUX.2 way of saying "like this one".
    for i, name in enumerate(ref_names):
        li, ei, ri = f"r{i}l", f"r{i}e", f"r{i}"
        w[li] = {"class_type": "LoadImage", "inputs": {"image": name}}
        w[ei] = {"class_type": "VAEEncode", "inputs": {"pixels": [li, 0], "vae": ["3", 0]}}
        w[ri] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": positive, "latent": [ei, 0]}}
        positive = [ri, 0]
    if p["scheduler"] == "flux2":
        # FLUX.2's own scheduler + guider + advanced sampler, per Comfy's template
        w["s1"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": p["sampler"]}}
        w["s2"] = {"class_type": "Flux2Scheduler", "inputs": {"steps": steps or p["steps"], "width": width, "height": height}}
        # BasicGuider = no classifier-free guidance, which is what the FLUX.2
        # templates use (cfg 1); the empty negative is simply unused here.
        w["s3"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": positive}}
        w["s4"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}}
        w["8"] = {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["s4", 0], "guider": ["s3", 0], "sampler": ["s1", 0], "sigmas": ["s2", 0], "latent_image": ["7", 0]}}
    else:
        w["8"] = {"class_type": "KSampler", "inputs": {
            "model": model, "seed": seed, "steps": steps or p["steps"], "cfg": p["cfg"],
            "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0,
            "positive": positive, "negative": ["6", 0], "latent_image": ["7", 0]}}
    return w


def render(prompt: str, out_path: str, preset: str = "sketch", references: list[str] | None = None,
           seed: int | None = None, steps: int | None = None, width: int = 1024, height: int = 1024,
           base: str = DEFAULT_URL, unload: bool = True) -> str:
    """Make one image; save it to out_path; return the path. Raises on failure."""
    p = PRESETS.get(preset)
    if not p:
        raise ValueError(f"no preset {preset!r} (have: {', '.join(PRESETS)})")
    refs = [Path(r).expanduser() for r in (references or [])]
    if refs and not p["references"]:
        raise ValueError(f"preset {preset!r} can't take reference images — use 'reference' or 'masterpiece'")
    for r in refs:
        if not r.is_file():
            raise FileNotFoundError(f"reference image not found: {r}")
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    if p.get("size") and width == 1024 and height == 1024:
        width = height = p["size"]           # the preset's native square
    names = [_upload(base, r) for r in refs]
    pid = _post(base, "/prompt", {"prompt": _workflow(p, prompt, seed, width, height, steps, names)}, 30)["prompt_id"]
    t0 = time.time()
    try:
        while True:
            h = json.loads(_get(base, f"/history/{pid}")).get(pid)
            st = (h or {}).get("status", {})
            if st.get("completed"):
                break
            if st.get("status_str") == "error":
                msgs = [m[1].get("exception_message", "") for m in st.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError("ComfyUI error: " + ("; ".join(msgs) or json.dumps(st))[:400])
            if time.time() - t0 > 1800:
                raise TimeoutError("render took over 30 minutes")
            time.sleep(1.0)
        img = next(o for o in h["outputs"].values() if "images" in o)["images"][0]
        q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")})
        out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_get(base, f"/view?{q}", 120))
        return str(out)
    finally:
        if unload:
            # Twice, a beat apart: measured 2026-09-05, one /free right after a
            # reference render left 28 GB borrowed; the second took it to 13.
            for _ in range(2):
                try:
                    _post(base, "/free", {"unload_models": True, "free_memory": True}, 20)
                except Exception:
                    pass
                time.sleep(2.0)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python -m forge.imagegen <prompt> <out.png> [preset] [ref.png ...]"); sys.exit(1)
    preset = sys.argv[3] if len(sys.argv) > 3 else "sketch"
    print("saved:", render(sys.argv[1], sys.argv[2], preset, sys.argv[4:]))
