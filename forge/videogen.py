"""Video generation through a ComfyUI running WAN 2.2 (Apache 2.0).

One model, two doors: text → video, or image → video (a still becomes the
first frame). Video renders take minutes, so they live on the render box —
the old machine's TITAN — reached over the wire; where that is comes from
config (media.videogen_url), never from code.

Two recipes: "fast" (default) = Comfy's wan2.2 5B template + the FastWan distilled LoRA,
8 steps, cfg 1.0; "quality" = the stock 20 steps, cfg 5. Both uni_pc/simple, shift 8,
24 fps. Frames must be 4k+1 (WAN's rule); 121 frames = 5 s. Models stay resident on the
render box between clips (unload=False) — reloading cost ~60-90 s per clip when measured.
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .imagegen import _post, _get, _upload   # same ComfyUI plumbing

DEFAULT_URL = "http://10.42.0.1:8189"

# Comfy's stock negative for WAN — quality/artifact terms the model was trained against
_NEGATIVE = ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
             "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
             "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")

_BASE = {
    "unet": "wan2.2_ti2v_5B_fp16.safetensors", "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    "clip_type": "wan", "vae": "wan2.2_vae.safetensors",
    "sampler": "uni_pc", "scheduler": "simple", "shift": 8.0, "fps": 24,
}
PRESETS = {
    # Comfy's stock 5B recipe: 20 steps with guidance. Slow, the reference for quality.
    "quality": {**_BASE, "steps": 20, "cfg": 5.0, "lora": None, "lora_strength": 0.0},
    # FastWan distilled LoRA (Apache 2.0, from FastVideo via Kijai's ComfyUI repack): a handful of
    # steps, and guidance MUST be 1.0 or it distorts (Kijai/WanVideo_comfy discussion 61).
    "fast": {**_BASE, "steps": 8, "cfg": 1.0,
             "lora": "Wan2_2_5B_FastWanFullAttn_lora_rank_128_bf16.safetensors", "lora_strength": 1.0},
}
DEFAULT_PRESET = "fast"   # measured 2026-09-06: fast@8 ≈ quality@20 to the eye, ~4x quicker
PRESET = PRESETS[DEFAULT_PRESET]


def available(base: str = DEFAULT_URL) -> tuple[bool, str]:
    try:
        d = json.loads(_get(base, "/system_stats", 5))
        dev = (d.get("devices") or [{}])[0]
        return True, f"video engine up on {dev.get('name', '?')[:40]}"
    except Exception as e:
        return False, f"video engine not answering at {base} ({type(e).__name__})"


def _workflow(prompt: str, seed: int, width: int, height: int, frames: int,
              start_image: str | None, preset: str = DEFAULT_PRESET, steps: int | None = None) -> dict:
    p = PRESETS[preset]
    steps = int(steps or p["steps"])
    w = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": p["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": p["clip_type"], "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": p["shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": _NEGATIVE}},
        "7": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": width, "height": height, "length": frames, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": steps, "cfg": p["cfg"],
              "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0,
              "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": p["fps"]}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/vid", "format": "auto", "codec": "auto"}},
    }
    if p.get("lora"):
        # LoRA sits between the raw model and the shift node: 1 -> 13 (lora) -> 4 (shift) -> sampler
        w["13"] = {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": p["lora"],
                                                                     "strength_model": p["lora_strength"]}}
        w["4"]["inputs"]["model"] = ["13", 0]
    if start_image:
        w["12"] = {"class_type": "LoadImage", "inputs": {"image": start_image}}
        w["7"]["inputs"]["start_image"] = ["12", 0]
    return w


def render(prompt: str, out_path: str, image: str | None = None, seconds: float = 5.0,
           width: int = 1280, height: int = 704, seed: int | None = None,
           base: str = DEFAULT_URL, unload: bool = False,
           preset: str = DEFAULT_PRESET, steps: int | None = None) -> str:
    """Make one clip; save the mp4 to out_path; return the path. Raises on failure.
    preset: "quality" (stock 20-step) or "fast" (FastWan LoRA, ~4 steps). steps overrides the preset's count."""
    if preset not in PRESETS:
        raise ValueError(f"unknown video preset {preset!r}; choose from {sorted(PRESETS)}")
    frames = max(5, int(round(seconds * PRESETS[preset]["fps"])))
    frames = (frames // 4) * 4 + 1                       # WAN wants 4k+1
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    start = _upload(base, Path(image).expanduser()) if image else None
    wf = _workflow(prompt, seed, width, height, frames, start, preset, steps)
    from . import renderbox
    cid = renderbox.new_client_id()
    pid = _post(base, "/prompt", {"prompt": wf, "client_id": cid}, 30)["prompt_id"]
    renderbox.start_watch(base, cid, pid, wf, f"video {seconds:.0f}s ({preset})")
    t0 = time.time()
    try:
        while True:
            h = json.loads(_get(base, f"/history/{pid}")).get(pid)
            st = (h or {}).get("status", {})
            if st.get("completed"):
                break
            if st.get("status_str") == "error":
                msgs = [m[1].get("exception_message", "") for m in st.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError("video engine error: " + ("; ".join(msgs) or json.dumps(st))[:400])
            if time.time() - t0 > 3600:
                raise TimeoutError("video render took over an hour")
            time.sleep(2.0)
        outs = h["outputs"]
        vid = None
        for o in outs.values():
            for key in ("images", "gifs", "videos"):
                if o.get(key):
                    vid = o[key][0]; break
            if vid: break
        if not vid:
            raise RuntimeError("video engine finished but produced no file")
        q = urllib.parse.urlencode({"filename": vid["filename"], "subfolder": vid.get("subfolder", ""), "type": vid.get("type", "output")})
        out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_get(base, f"/view?{q}", 300))
        return str(out)
    finally:
        renderbox.finish(pid)
        if unload:
            for _ in range(2):
                try:
                    _post(base, "/free", {"unload_models": True, "free_memory": True}, 20)
                except Exception:
                    pass
                time.sleep(2.0)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python -m forge.videogen <prompt> <out.mp4> [start_image.png] [seconds]"); sys.exit(1)
    img = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    secs = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0
    print("saved:", render(sys.argv[1], sys.argv[2], img, secs))
