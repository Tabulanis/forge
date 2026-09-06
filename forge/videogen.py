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


# ---- restyle: the same shot, different world -----------------------------------------------
# Wan 2.1 VACE 14B (Apache 2.0, GGUF Q6 on the render box) reads the STRUCTURE of a source video
# (depth, edges or the people's poses, frame by frame) and repaints it to a prompt — "the same
# shot at night", "1975 handheld 16mm" — optionally steered by a reference still for the look.
# The owner's 2025 recipe, rebuilt: 4-step lightx2v distill, cfg 1, uni_pc, shift 8, 16 fps,
# up to 81 frames (5 s) per pass at ~480p. Longer inputs are cut into passes and joined.
RESTYLE = {
    "unet": "Wan2.1_14B_VACE-Q6_K.gguf", "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "vae": "wan_2.1_vae.safetensors",
    "lora": "Wan21_T2V_14B_lightx2v_cfg_step_distill_lora_rank64.safetensors",
    "steps": 4, "cfg": 1.0, "sampler": "uni_pc", "scheduler": "simple", "shift": 8.0, "fps": 16,
    "max_frames": 81, "long_side": 832,
}
_RESTYLE_CONTROL = {
    "depth": ("DepthAnythingV2Preprocessor", {"ckpt_name": "depth_anything_v2_vitb.pth", "resolution": 512}),
    "edges": ("CannyEdgePreprocessor", {"low_threshold": 100, "high_threshold": 200, "resolution": 512}),
    "pose":  ("DWPreprocessor", {"detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 512}),
}


def _restyle_workflow(src_name: str, prompt: str, seed: int, width: int, height: int, frames: int,
                      control: str, strength: float, ref_name: str | None) -> dict:
    p = RESTYLE
    pre, pre_inputs = _RESTYLE_CONTROL[control]
    w = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": p["unet"]}},
        "1l": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": p["lora"], "strength_model": 1.0}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1l", 0], "shift": p["shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        # NOT the stock quality-negative: it forbids grain, blur, faded colour and "low quality",
        # which is exactly what a period or film look asks for. Only ban the failure modes.
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "static, frozen, still image, subtitles, text, watermark, logo, extra fingers, deformed hands, duplicated person"}},
        "v0": {"class_type": "LoadVideo", "inputs": {"file": src_name}},
        "v1": {"class_type": "GetVideoComponents", "inputs": {"video": ["v0", 0]}},
        "v2": {"class_type": "ImageScale", "inputs": {"image": ["v1", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}},
        "v3": {"class_type": pre, "inputs": {"image": ["v2", 0], **pre_inputs}},
        "7": {"class_type": "WanVaceToVideo", "inputs": {"positive": ["5", 0], "negative": ["6", 0], "vae": ["3", 0],
              "width": width, "height": height, "length": frames, "batch_size": 1, "strength": float(strength),
              "control_video": ["v3", 0]}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": p["steps"], "cfg": p["cfg"],
              "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0,
              "positive": ["7", 0], "negative": ["7", 1], "latent_image": ["7", 2]}},
        "9t": {"class_type": "TrimVideoLatent", "inputs": {"samples": ["8", 0], "trim_amount": ["7", 3]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["9t", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": p["fps"]}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/restyle", "format": "auto", "codec": "auto"}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["v3", 0], "filename_prefix": "merge/restyle_control"}},
    }
    if ref_name:
        w["r0"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        w["7"]["inputs"]["reference_image"] = ["r0", 0]
    return w


def _probe(path: str) -> tuple[int, int, float, float]:
    """(width, height, fps, seconds) of a video via ffprobe."""
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate:format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=30).stdout.split()
    w, h, fr = out[0].split(",")[:3]
    num, den = fr.split("/"); fps = float(num) / float(den or 1)
    return int(w), int(h), fps, float(out[1]) if len(out) > 1 else 0.0


def _prep_chunks(src: str, work: Path, fps: int, max_frames: int, long_side: int, start: float, seconds: float | None) -> tuple[list[Path], int, int]:
    """Resample the source to the model's fps and size, cut into passes of <= max_frames. Returns (chunk files, w, h)."""
    import subprocess
    sw, sh, _, dur = _probe(src)
    scale = long_side / max(sw, sh)
    w, h = int(sw * scale) // 16 * 16, int(sh * scale) // 16 * 16
    total = min(seconds, dur - start) if seconds else (dur - start)
    n_frames = int(total * fps)
    chunks: list[Path] = []
    i = 0
    while i < n_frames:
        n = min(max_frames, n_frames - i)
        n = (n // 4) * 4 + 1 if n >= 5 else 5
        out = work / f"chunk{len(chunks):02d}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start + i / fps), "-i", src,
                        "-vf", f"fps={fps},scale={w}:{h}", "-frames:v", str(n), "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(out)],
                       check=True, timeout=600)
        chunks.append(out); i += n
    return chunks, w, h


def restyle(video: str, prompt: str, out_path: str, reference_image: str | None = None,
            control: str = "depth", strength: float = 1.0, start: float = 0.0, seconds: float | None = None,
            seed: int | None = None, base: str = DEFAULT_URL) -> str:
    """Repaint a video to a prompt while keeping its structure. Saves an mp4 to out_path; returns the path."""
    import subprocess, tempfile
    if control not in _RESTYLE_CONTROL:
        raise ValueError(f"control must be one of {sorted(_RESTYLE_CONTROL)}")
    p = RESTYLE
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="restyle-"))
    chunks, w, h = _prep_chunks(str(Path(video).expanduser()), work, p["fps"], p["max_frames"], p["long_side"], start, seconds)
    ref = _upload(base, Path(reference_image).expanduser()) if reference_image else None
    from . import renderbox
    outs: list[Path] = []
    for k, ch in enumerate(chunks):
        frames = int(_probe(str(ch))[3] * p["fps"] + 0.5)
        frames = max(5, (frames // 4) * 4 + 1)
        name = _upload(base, ch)
        wf = _restyle_workflow(name, prompt, seed + k, w, h, frames, control, strength, ref)
        cid = renderbox.new_client_id()
        pid = _post(base, "/prompt", {"prompt": wf, "client_id": cid}, 30)["prompt_id"]
        renderbox.start_watch(base, cid, pid, wf, f"restyle pass {k + 1}/{len(chunks)}")
        t0 = time.time()
        try:
            while True:
                hist = json.loads(_get(base, f"/history/{pid}")).get(pid)
                st = (hist or {}).get("status", {})
                if st.get("completed"):
                    break
                if st.get("status_str") == "error":
                    msgs = [m[1].get("exception_message", "") for m in st.get("messages", []) if m[0] == "execution_error"]
                    raise RuntimeError("video engine error: " + ("; ".join(msgs) or json.dumps(st))[:400])
                if time.time() - t0 > 3600:
                    raise TimeoutError("restyle pass took over an hour")
                time.sleep(2.0)
        finally:
            renderbox.finish(pid)
        vid = None
        for key in ("images", "gifs", "videos"):
            if hist["outputs"].get("11", {}).get(key):
                vid = hist["outputs"]["11"][key][0]; break
        if not vid:
            raise RuntimeError("restyle pass finished but produced no file")
        q = urllib.parse.urlencode({"filename": vid["filename"], "subfolder": vid.get("subfolder", ""), "type": vid.get("type", "output")})
        part = work / f"out{k:02d}.mp4"; part.write_bytes(_get(base, f"/view?{q}", 300)); outs.append(part)
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    if len(outs) == 1:
        out.write_bytes(outs[0].read_bytes())
    else:
        lst = work / "list.txt"; lst.write_text("".join(f"file '{o}'\n" for o in outs))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)], check=True, timeout=600)
    return str(out)
