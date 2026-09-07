"""Image generation through ComfyUI, on the GPU, with the model chosen by name.

Presets are the house's picture-making tiers (2026-09-05):
  sketch      Z-Image-Turbo — ~20 s, text only. The everyday one.
  reference   FLUX.2-klein 4B (Apache) — the workhorse: hero, keyframes, edits; takes reference images. ~7 s warm.
  masterpiece Qwen-Image — the flagship (Apache 2.0).
  (Licensing rule, 2026-09-05: only permissively licensed models — Apache/MIT.
   FLUX.2-dev and klein-9B are non-commercial-licensed and were removed.)

Where this runs is config (media.imagegen_url). Since 2026-09-06 it is the
render box — the old machine's TITAN, over the wire — because on the brain's
box every image model fought the 122B for memory (swap filled, and once the
GPU wedged). The brain's box does one thing now. Every render still ends with
/free so the render box stays clean between jobs.
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://10.42.0.1:8189"

PRESETS = {
    "sketch": {
        # text encoder as GGUF Q8 (4 GB) instead of bf16 (8 GB): the old card has no bf16 and
        # both encoder + model no longer fit on it together — measured 2026-09-06: prompt 33 s,
        # sampling 64 s (spilled) vs 8 s (fits).
        "unet": "z_image_turbo_bf16.safetensors", "clip": "qwen3-4b-Q8_0.gguf", "clip_loader": "CLIPLoaderGGUF",
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
    # FLUX.1 schnell (Apache 2.0) — the permissive FLUX. 4-step distilled, 12B; the GGUF
    # Q8 build so the old card doesn't convert bf16/fp8 on the way in. Two text
    # encoders (clip_l + T5-XXL, the T5 also GGUF Q8). No shift, no guidance.
    "schnell": {
        "unet": "flux1-schnell-Q8_0.gguf", "unet_loader": "UnetLoaderGGUF",
        "clip": "clip_l.safetensors", "clip2": "t5-v1_1-xxl-encoder-Q8_0.gguf",
        "clip_loader": "DualCLIPLoaderGGUF", "clip_type": "flux", "vae": "flux1-ae.safetensors",
        "steps": 4, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
        "latent": "EmptySD3LatentImage", "references": False,
    },
    # Stable Diffusion 3.5 Large (Stability Community License — owner-approved 2026-09-06 for the
    # bake-off; not Apache). Q8 GGUF; three text encoders (clip_l + clip_g + T5-XXL GGUF); the VAE
    # lifted out of Comfy's fp8 checkpoint. Stock recipe: 28 steps, cfg 4.5, dpmpp_2m/sgm_uniform, shift 3.
    "sd35": {
        "unet": "sd3.5_large-Q8_0.gguf", "unet_loader": "UnetLoaderGGUF",
        "clip": "clip_l.safetensors", "clip2": "clip_g.safetensors", "clip3": "t5-v1_1-xxl-encoder-Q8_0.gguf",
        "clip_loader": "TripleCLIPLoaderGGUF", "clip_type": "sd3", "vae": "sd3.5_vae.safetensors",
        "steps": 28, "cfg": 4.5, "sampler": "dpmpp_2m", "scheduler": "sgm_uniform", "shift": 3.0,
        "latent": "EmptySD3LatentImage", "references": False,
    },
    # Its Turbo distillation: 4 steps, no guidance.
    "sd35turbo": {
        "unet": "sd3.5_large_turbo-Q8_0.gguf", "unet_loader": "UnetLoaderGGUF",
        "clip": "clip_l.safetensors", "clip2": "clip_g.safetensors", "clip3": "t5-v1_1-xxl-encoder-Q8_0.gguf",
        "clip_loader": "TripleCLIPLoaderGGUF", "clip_type": "sd3", "vae": "sd3.5_vae.safetensors",
        "steps": 4, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "shift": 3.0,
        "latent": "EmptySD3LatentImage", "references": False,
    },
    # Qwen-Image-2512 (Apache 2.0) — the flagship retrained against the plastic look, GGUF Q6, its own
    # 8-step Lightning, plus a realism LoRA (Samsung_Qwen2512, Apache: "raw, unedited photo" texture).
    "real": {
        "unet": "qwen-image-2512-Q6_K.gguf", "unet_loader": "UnetLoaderGGUF",
        "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "clip_type": "qwen_image", "vae": "qwen_image_vae.safetensors",
        "loras": [("Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors", 1.0), ("samsung_qwen2512.safetensors", 0.8)],
        "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "shift": 3.1,
        "latent": "EmptySD3LatentImage", "references": False, "size": 1328,
    },
    # Qwen-Image-Edit-2511 (Apache 2.0) — reference-driven editing: give it 1-3 pictures and say
    # what to change/keep; it holds faces, characters and objects across poses and scenes. GGUF Q6
    # build + its own 8-step Lightning LoRA. Same text encoder + VAE as masterpiece.
    "edit": {
        "unet": "qwen-image-edit-2511-Q6_K.gguf", "unet_loader": "UnetLoaderGGUF",
        "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "clip_type": "qwen_image", "vae": "qwen_image_vae.safetensors",
        "lora": "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors",
        "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "shift": 3.0,
        "latent": "EmptySD3LatentImage", "references": True, "edit": True, "size": 1024,
    },
    # Qwen-Image (Apache 2.0) — the flagship. Comfy's recipe: fp8 model +
    # Lightning 8-step LoRA, shift 3.1, euler/simple, cfg 1, 1328².
    "masterpiece": {
        "unet": "qwen_image_fp8_e4m3fn.safetensors", "clip": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "clip_type": "qwen_image", "vae": "qwen_image_vae.safetensors",
        "lora": "Qwen-Image-Lightning-8steps-V1.0.safetensors",
        "steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple",
        "shift": 3.1, "latent": "EmptySD3LatentImage", "references": False,
        "size": 1328,
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


# Control (pose / depth / edges) rides on the Qwen-Image ControlNet-Union (InstantX, Apache 2.0):
# a picture goes through a preprocessor, the union net is told which kind it is, and the
# conditioning is steered by it. Only the Qwen-Image presets (masterpiece, edit) carry it.
CONTROL = {
    "pose":  {"pre": "DWPreprocessor", "pre_inputs": {"detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024}, "union": "openpose"},
    "depth": {"pre": "DepthAnythingV2Preprocessor", "pre_inputs": {"ckpt_name": "depth_anything_v2_vitb.pth", "resolution": 1024}, "union": "depth"},
    "edges": {"pre": "CannyEdgePreprocessor", "pre_inputs": {"low_threshold": 100, "high_threshold": 200, "resolution": 1024}, "union": "canny/lineart/anime_lineart/mlsd"},
}
CONTROLNET_FILE = "Qwen-Image-ControlNet-Union.safetensors"


def _workflow(p: dict, prompt: str, seed: int, width: int, height: int,
              steps: int | None, ref_names: list[str],
              control: dict | None = None) -> dict:
    w = {
        "1": ({"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": p["unet"]}}
              if p.get("unet_loader") == "UnetLoaderGGUF" else
              {"class_type": "UNETLoader", "inputs": {"unet_name": p["unet"], "weight_dtype": "default"}}),
        "2": ({"class_type": p["clip_loader"], "inputs": {"clip_name1": p["clip"], "clip_name2": p["clip2"], "clip_name3": p["clip3"]}}
              if p.get("clip3") else
              {"class_type": p["clip_loader"], "inputs": {"clip_name1": p["clip"], "clip_name2": p["clip2"], "type": p["clip_type"]}}
              if p.get("clip2") else
              {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": p["clip"], "type": p["clip_type"]}}
              if p.get("clip_loader") == "CLIPLoaderGGUF" else
              {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": p["clip_type"], "device": "default"}}),
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": ""}},
        "7": {"class_type": p["latent"], "inputs": {"width": width, "height": height, "batch_size": 1}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": "merge/img"}},
    }
    model = ["1", 0]
    loras = list(p.get("loras") or ([(p["lora"], 1.0)] if p.get("lora") else []))
    for i, (lname, lstr) in enumerate(loras):
        nid = "1l" if i == 0 else f"1l{i}"
        w[nid] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": model, "lora_name": lname, "strength_model": float(lstr)}}
        model = [nid, 0]
    if "shift" in p:
        _shift_node = "ModelSamplingSD3" if p.get("clip_type") == "sd3" else "ModelSamplingAuraFlow"
        w["4"] = {"class_type": _shift_node, "inputs": {"model": model, "shift": p["shift"]}}
        model = ["4", 0]
    positive = ["5", 0]
    negative = ["6", 0]
    if p.get("edit"):
        # Qwen-Image-Edit: the references go INTO the text encoder (up to 3), and the
        # first one, scaled to ~1 MP, becomes the starting latent so composition and
        # aspect are kept. Empty prompt on the same node = the negative.
        for i, name in enumerate(ref_names[:3]):
            w[f"e{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        imgs = {f"image{i + 1}": [f"e{i}", 0] for i in range(min(3, len(ref_names)))}
        w["5"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["2", 0], "prompt": prompt, "vae": ["3", 0], **imgs}}
        w["6"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {"clip": ["2", 0], "prompt": "", "vae": ["3", 0], **imgs}}
        if ref_names:
            # the starting latent = the first reference at the REQUESTED size (16:9 storyboard stills stay 16:9)
            w["es"] = {"class_type": "ImageScale", "inputs": {"image": ["e0", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
            w["7"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["es", 0], "vae": ["3", 0]}}
        ref_names = []          # consumed here, not by the FLUX-style reference chain below
    if control and control.get("image"):
        c = CONTROL[control.get("type", "pose")]
        w["c0"] = {"class_type": "LoadImage", "inputs": {"image": control["image"]}}
        w["c1"] = {"class_type": c["pre"], "inputs": {"image": ["c0", 0], **c["pre_inputs"]}}
        w["c2"] = {"class_type": "ControlNetLoader", "inputs": {"control_net_name": CONTROLNET_FILE}}
        w["c3"] = {"class_type": "SetUnionControlNetType", "inputs": {"control_net": ["c2", 0], "type": c["union"]}}
        w["c4"] = {"class_type": "ControlNetApplyAdvanced", "inputs": {"positive": positive, "negative": negative, "control_net": ["c3", 0],
                   "image": ["c1", 0], "vae": ["3", 0], "strength": float(control.get("strength", 0.8)), "start_percent": 0.0, "end_percent": 1.0}}
        positive, negative = ["c4", 0], ["c4", 1]
        w["c5"] = {"class_type": "SaveImage", "inputs": {"images": ["c1", 0], "filename_prefix": "merge/control"}}   # the map, for the record
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
            "positive": positive, "negative": negative, "latent_image": ["7", 0]}}
    return w


def render(prompt: str, out_path: str, preset: str = "sketch", references: list[str] | None = None,
           control_image: str | None = None, control_type: str = "pose", control_strength: float = 0.8,
           seed: int | None = None, steps: int | None = None, width: int = 1024, height: int = 1024,
           base: str = DEFAULT_URL, unload: bool = False) -> str:
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
    if p.get("size") and (width, height) == (1024, 1024):
        width = height = p["size"]           # the preset's native square, unless the caller chose a size
    names = [_upload(base, r) for r in refs]
    control = None
    if control_image:
        if control_type not in CONTROL:
            raise ValueError(f"control_type must be one of {sorted(CONTROL)}")
        control = {"image": _upload(base, Path(control_image).expanduser()), "type": control_type, "strength": control_strength}
    wf = _workflow(p, prompt, seed, width, height, steps, names, control)
    from . import renderbox
    cid = renderbox.new_client_id()
    pid = _post(base, "/prompt", {"prompt": wf, "client_id": cid}, 30)["prompt_id"]
    renderbox.start_watch(base, cid, pid, wf, f"image ({preset})")
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
        outs = h["outputs"]
        img = (outs.get("10", {}).get("images") or next(o for o in outs.values() if "images" in o)["images"])[0]
        q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")})
        out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_get(base, f"/view?{q}", 120))
        return str(out)
    finally:
        renderbox.finish(pid)
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


def pose_map(image: str, out_path: str, base: str = DEFAULT_URL) -> str:
    """Skeleton (DWPose) of the people in a picture, as a PNG on black — the thing `edit`
    takes as a pose reference. ~10 s on the render box."""
    name = _upload(base, Path(image).expanduser())
    wf = {"1": {"class_type": "LoadImage", "inputs": {"image": name}},
          "2": {"class_type": "DWPreprocessor", "inputs": {"image": ["1", 0], "detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024}},
          "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "merge/pose"}}}
    pid = _post(base, "/prompt", {"prompt": wf}, 30)["prompt_id"]
    t0 = time.time()
    while True:
        h = json.loads(_get(base, f"/history/{pid}")).get(pid)
        st = (h or {}).get("status", {})
        if st.get("completed"):
            break
        if st.get("status_str") == "error":
            msgs = [m[1].get("exception_message", "") for m in st.get("messages", []) if m[0] == "execution_error"]
            raise RuntimeError("pose extraction failed: " + ("; ".join(msgs) or json.dumps(st))[:300])
        if time.time() - t0 > 600:
            raise TimeoutError("pose extraction took over 10 minutes")
        time.sleep(1.0)
    im = h["outputs"]["3"]["images"][0]
    q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im.get("type", "output")})
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(_get(base, f"/view?{q}", 120))
    return str(out)
