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
            seed: int | None = None, base: str = DEFAULT_URL, stock: str = "") -> str:
    """Repaint a video to a prompt while keeping its structure, then (optionally) lay a film stock
    over it. An empty prompt with a stock = the stock alone, no model pass (seconds, not minutes).
    Saves an mp4 to out_path; returns the path."""
    import subprocess, tempfile
    if not (prompt or "").strip():
        if not stock:
            raise ValueError("give a prompt, a stock, or both")
        src = str(Path(video).expanduser())
        if start or seconds:
            tmp = Path(tempfile.mkdtemp(prefix="restyle-")) / "cut.mp4"
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start), "-i", src]
            if seconds: cmd += ["-t", str(seconds)]
            subprocess.run(cmd + ["-c:v", "libx264", "-crf", "16", "-an", str(tmp)], check=True, timeout=600); src = str(tmp)
        return apply_stock(src, out_path, stock)
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
    joined = work / "joined.mp4"
    if len(outs) == 1:
        joined = outs[0]
    else:
        lst = work / "list.txt"; lst.write_text("".join(f"file '{o}'\n" for o in outs))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(joined)], check=True, timeout=600)
    if stock:
        return apply_stock(str(joined), str(out), stock)
    out.write_bytes(joined.read_bytes())
    return str(out)


# ---- film stock: the look, applied deterministically after (or instead of) the model ----------
# A distilled video model resolves toward clean output; grain, fade, softness and weave are
# better done by hand, identically every time. Each entry is an ffmpeg filter chain.
STOCKS = {
    "16mm": ("fps=18,"
             "eq=saturation=0.78:contrast=1.08:brightness=0.02,"
             "curves=preset=vintage,"
             "colorbalance=rs=0.06:gs=0.02:bs=-0.05:rm=0.04:bm=-0.04,"
             "gblur=sigma=0.6,"
             "noise=alls=22:allf=t+u,"
             "vignette=PI/4.5,"
             "crop=iw-8:ih-8:4+4*sin(n/7):4+3*cos(n/11),"     # gate weave
             "scale=trunc(iw/2)*2:trunc(ih/2)*2"),
    "super8": ("fps=16,eq=saturation=0.7:contrast=1.12,curves=preset=vintage,gblur=sigma=0.9,"
               "noise=alls=30:allf=t+u,vignette=PI/4,crop=iw-12:ih-12:6+6*sin(n/5):6+4*cos(n/9),scale=trunc(iw/2)*2:trunc(ih/2)*2"),
    "vhs":   ("scale=iw*0.6:ih*0.6,scale=iw/0.6:ih/0.6:flags=neighbor,eq=saturation=1.15:contrast=0.95,"
              "chromashift=cbh=3:crv=-3,noise=alls=12:allf=t,gblur=sigma=0.4"),
    "noir":  ("hue=s=0,eq=contrast=1.25:brightness=-0.03,curves=preset=strong_contrast,noise=alls=14:allf=t+u,vignette=PI/4"),
}


def apply_stock(video: str, out_path: str, stock: str) -> str:
    """Run a film-stock look over a video (CPU, seconds). Returns out_path."""
    import subprocess
    if stock not in STOCKS:
        raise ValueError(f"stock must be one of {sorted(STOCKS)}")
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(Path(video).expanduser()),
                    "-vf", STOCKS[stock], "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", "-an", str(out)],
                   check=True, timeout=1800)
    return str(out)


# ---- long form: small and long, then refined up, then smoothed ---------------------------------
# 1. Render at ~640x352 in chunks of 81 frames; every chunk starts from the last frame of the one
#    before (first-frame conditioning), which is what keeps the story continuous.
# 2. Refine: each chunk is scaled 2x, encoded back to latent, and re-sampled by the same model at
#    low denoise with a realism prompt. That puts real detail back (a resize can't) and, because
#    the pass is low-noise and the prompt is constant, keeps the look identical across chunks.
# 3. Join (dropping each chunk's duplicated first frame) and RIFE-interpolate 2x for smoothness.
FINISH_MAX_SIDE = 1792   # 1792x1024 finished fine on the 24GB card; bigger is untested
LONG = {"width": 640, "height": 352, "chunk_frames": 81, "refine_steps": 6, "refine_denoise": 0.28,
        "realism": ", photographic, natural skin texture, real fabric and surfaces, subtle film grain, no plastic sheen"}


def _refine_workflow(src_name: str, prompt: str, seed: int, width: int, height: int, steps: int, denoise: float) -> dict:
    p = PRESETS["fast"]
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": p["unet"], "weight_dtype": "default"}},
        "13": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": p["lora"], "strength_model": 0.6}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": p["clip_type"], "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["13", 0], "shift": p["shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": _NEGATIVE}},
        "v0": {"class_type": "LoadVideo", "inputs": {"file": src_name}},
        "v1": {"class_type": "GetVideoComponents", "inputs": {"video": ["v0", 0]}},
        "v2": {"class_type": "ImageScale", "inputs": {"image": ["v1", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "disabled"}},
        "7": {"class_type": "VAEEncode", "inputs": {"pixels": ["v2", 0], "vae": ["3", 0]}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": steps, "cfg": 1.0,
              "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": denoise,
              "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": p["fps"]}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/refine", "format": "auto", "codec": "auto"}},
    }


def _rife_workflow(src_name: str, multiplier: int, fps_out: float) -> dict:
    return {
        "1": {"class_type": "LoadVideo", "inputs": {"file": src_name}},
        "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
        "3": {"class_type": "RIFE VFI", "inputs": {"ckpt_name": "rife49.pth", "frames": ["2", 0], "clear_cache_after_n_frames": 10,
              "multiplier": multiplier, "fast_mode": True, "ensemble": True, "scale_factor": 1.0, "dtype": "float32", "torch_compile": False, "batch_size": 1}},
        "4": {"class_type": "CreateVideo", "inputs": {"images": ["3", 0], "fps": fps_out}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["4", 0], "filename_prefix": "merge/rife", "format": "mp4", "codec": "h264"}},
    }


def _run(base: str, wf: dict, label: str, out_file: Path, node: str = "11", timeout: int = 3600) -> Path:
    """Post a workflow, wait, download the named node's video output to out_file."""
    from . import renderbox
    cid = renderbox.new_client_id()
    pid = _post(base, "/prompt", {"prompt": wf, "client_id": cid}, 30)["prompt_id"]
    renderbox.start_watch(base, cid, pid, wf, label)
    t0 = time.time()
    try:
        while True:
            h = json.loads(_get(base, f"/history/{pid}")).get(pid)
            st = (h or {}).get("status", {})
            if st.get("completed"):
                break
            if st.get("status_str") == "error":
                msgs = [m[1].get("exception_message", "") for m in st.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError(f"{label}: " + ("; ".join(msgs) or json.dumps(st))[:400])
            if time.time() - t0 > timeout:
                raise TimeoutError(f"{label} took over {timeout} s")
            time.sleep(2.0)
    finally:
        renderbox.finish(pid)
    vid = None
    for key in ("images", "gifs", "videos"):
        if h["outputs"].get(node, {}).get(key):
            vid = h["outputs"][node][key][0]; break
    if not vid:
        raise RuntimeError(f"{label} finished but produced no file")
    q = urllib.parse.urlencode({"filename": vid["filename"], "subfolder": vid.get("subfolder", ""), "type": vid.get("type", "output")})
    out_file.write_bytes(_get(base, f"/view?{q}", 600))
    return out_file


def long_video(prompt: str, out_path: str, seconds: float = 12.0, image: str | None = None,
               width: int | None = None, height: int | None = None, refine: bool = True,
               interpolate: int = 2, fps_out: float | None = None, seed: int | None = None,
               base: str = DEFAULT_URL, on_progress=None) -> str:
    """Long clip: chained small chunks -> optional 2x refine -> optional RIFE. Returns out_path."""
    import subprocess, tempfile
    p = PRESETS["fast"]; L = LONG
    w, h = width or L["width"], height or L["height"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="longvid-"))
    total = max(5, int(round(seconds * p["fps"])))
    chunks: list[Path] = []
    start_img = Path(image).expanduser() if image else None
    done = 0
    k = 0
    while done < total:
        n = min(L["chunk_frames"], total - done)
        n = (n // 4) * 4 + 1 if n >= 5 else 5
        start_name = _upload(base, start_img) if start_img else None
        wf = _workflow(prompt, seed + k, w, h, n, start_name, "fast", None)
        wf["11"]["inputs"]["filename_prefix"] = "merge/longchunk"
        part = _run(base, wf, f"long clip: chunk {k + 1}", work / f"chunk{k:02d}.mp4")
        chunks.append(part)
        # the next chunk starts where this one ended
        last = work / f"last{k:02d}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-sseof", "-0.05", "-i", str(part), "-frames:v", "1", "-update", "1", str(last)], check=True, timeout=120)
        start_img = last
        done += (n - 1) if k > 0 else n
        k += 1
    if refine:
        refined = []
        for i, ch in enumerate(chunks):
            rw = _refine_workflow(_upload(base, ch), prompt + L["realism"], seed, w * 2, h * 2, L["refine_steps"], L["refine_denoise"])
            refined.append(_run(base, rw, f"long clip: refine {i + 1}/{len(chunks)}", work / f"ref{i:02d}.mp4"))
        chunks = refined
    # join, dropping the duplicated first frame of every chunk after the first
    parts = []
    for i, ch in enumerate(chunks):
        if i == 0:
            parts.append(ch); continue
        cut = work / f"cut{i:02d}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ch), "-vf", "select=gte(n\\,1),setpts=N/FRAME_RATE/TB", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(cut)], check=True, timeout=600)
        parts.append(cut)
    joined = work / "joined.mp4"
    if len(parts) == 1:
        joined = parts[0]
    else:
        lst = work / "list.txt"; lst.write_text("".join(f"file '{x}'\n" for x in parts))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(joined)], check=True, timeout=600)
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    if interpolate and interpolate > 1:
        fps = fps_out or p["fps"] * interpolate
        _run(base, _rife_workflow(_upload(base, joined), int(interpolate), float(fps)), "long clip: smoothing", out)
    else:
        out.write_bytes(joined.read_bytes())
    return str(out)


SEEDVR2 = {"unet": "seedvr2_3b_fp16.safetensors", "vae": "seedvr2_ema_vae_fp16.safetensors",
           "tile": 512, "tile_overlap": 128, "temporal_size": 64, "temporal_overlap_vae": 8,
           "chunk_overlap": 4, "color": "lab"}


def _seedvr2_workflow(video_name: str, seed: int, multiplier: float, chunk_overlap: int, color: str,
                      frames_per_chunk: int | None = None) -> dict:
    """Comfy's own 'Video Upscale: SeedVR2' template, chunked mode on, as an API graph. One sampling step:
    the model is a one-step restorer, so steps=1, cfg=1, denoise=1 is the whole recipe."""
    S = SEEDVR2
    w = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": S["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "VAELoader", "inputs": {"vae_name": S["vae"]}},
        "3": {"class_type": "LoadVideo", "inputs": {"file": video_name}},
        "4": {"class_type": "GetVideoComponents", "inputs": {"video": ["3", 0]}},
        "5": {"class_type": "ResizeImageMaskNode", "inputs": {"input": ["4", 0], "resize_type": "scale by multiplier",
              "resize_type.multiplier": float(multiplier), "scale_method": "lanczos"}},
        "6": {"class_type": "SeedVR2Preprocess", "inputs": {"resized_images": ["5", 0]}},
        "7": {"class_type": "VAEEncodeTiled", "inputs": {"pixels": ["6", 0], "vae": ["2", 0], "tile_size": S["tile"], "overlap": S["tile_overlap"],
              "temporal_size": S["temporal_size"], "temporal_overlap": S["temporal_overlap_vae"]}},
        "8": {"class_type": "SeedVR2TemporalChunk", "inputs": {"latent": ["7", 0], "temporal_overlap": int(chunk_overlap), "chunking_mode": "auto"}},
        "9": {"class_type": "SeedVR2Conditioning", "inputs": {"model": ["1", 0], "vae_conditioning": ["8", 0]}},
        "10": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["9", 0], "negative": ["9", 1], "latent_image": ["8", 0],
               "seed": seed, "steps": 1, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "11": {"class_type": "SeedVR2TemporalMerge", "inputs": {"latents": ["10", 0], "temporal_overlap": ["8", 1]}},
        "12": {"class_type": "VAEDecodeTiled", "inputs": {"samples": ["11", 0], "vae": ["2", 0], "tile_size": S["tile"], "overlap": S["tile_overlap"],
               "temporal_size": S["temporal_size"], "temporal_overlap": S["temporal_overlap_vae"]}},
        "13": {"class_type": "SeedVR2PostProcessing", "inputs": {"images": ["12", 0], "original_resized_images": ["5", 0], "color_correction_method": color}},
        "14": {"class_type": "CreateVideo", "inputs": {"images": ["13", 0], "fps": ["4", 2]}},
        "15": {"class_type": "SaveVideo", "inputs": {"video": ["14", 0], "filename_prefix": "merge/seedvr2", "format": "auto", "codec": "auto"}},
    }
    if frames_per_chunk:
        w["8"]["inputs"]["chunking_mode"] = "manual"
        w["8"]["inputs"]["chunking_mode.frames_per_chunk"] = int(frames_per_chunk)
    return w


def seedvr2_upscale(video: str, out_path: str, longer_size: int | None = None, multiplier: float | None = None,
                    seed: int | None = None, base: str = DEFAULT_URL, chunk_overlap: int | None = None,
                    color: str | None = None, frames_per_chunk: int | None = None) -> str:
    """Real video super-resolution (SeedVR2 3B, Apache) on a draft: adds detail with temporal consistency,
    keeps what the draft shows. Give longer_size (pixels for the long edge) or a multiplier. Returns out_path."""
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    sw, sh, fps, dur = _probe(str(Path(video).expanduser()))
    if multiplier is None:
        target = int(longer_size or max(sw, sh) * 2)
        multiplier = target / max(sw, sh)
    wf = _seedvr2_workflow(_upload(base, Path(video).expanduser()), seed, multiplier,
                           SEEDVR2["chunk_overlap"] if chunk_overlap is None else chunk_overlap,
                           color or SEEDVR2["color"], frames_per_chunk)
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    _run(base, wf, f"seedvr2 x{multiplier:.2f}", out, node="15")   # SaveVideo is node 15 here
    return str(out)


def finish_video(video: str, prompt: str, out_path: str, interpolate: int = 2, fps_out: float | None = None,
                 seed: int | None = None, base: str = DEFAULT_URL, denoise: float | None = None,
                 upscaler: str = "seedvr2", longer_size: int | None = None) -> str:
    """The finishing pass for an approved draft. upscaler="seedvr2" (default): real video super-resolution
    straight to the full frame (long edge = longer_size, default FINISH_MAX_SIDE) — keeps the face and the
    motion, adds detail (measured 2026-09-07: 12 s draft 512x288 -> 1792 in 612 s; the VACE refine changed
    her face, SeedVR2 did not). upscaler="vace": the old 2x latent refine in <=81-frame pieces. Then RIFE
    frame doubling. Returns out_path."""
    import subprocess, tempfile
    p = PRESETS["fast"]; L = LONG
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="finish-"))
    sw, sh, fps, dur = _probe(str(Path(video).expanduser()))
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    if upscaler == "seedvr2":
        joined = Path(seedvr2_upscale(video, str(work / "up.mp4"), longer_size=longer_size or FINISH_MAX_SIDE, seed=seed, base=base))
        if interpolate and interpolate > 1:
            _run(base, _rife_workflow(_upload(base, joined), int(interpolate), float(fps_out or fps * interpolate)), "finish: smoothing", out)
        else:
            out.write_bytes(joined.read_bytes())
        return str(out)
    n_total = int(dur * fps + 0.5)
    # cut into pieces of <=81 frames (each piece overlaps nothing; the seams are refined with the same seed)
    pieces: list[Path] = []; i = 0
    while i < n_total:
        n = min(L["chunk_frames"], n_total - i)
        pc = work / f"piece{len(pieces):02d}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(Path(video).expanduser()),
                        "-vf", f"select=between(n\\,{i}\\,{i + n - 1}),setpts=N/FRAME_RATE/TB", "-frames:v", str(n), "-an",
                        "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(pc)], check=True, timeout=600)
        pieces.append(pc); i += n
    up = min(2.0, FINISH_MAX_SIDE / max(sw, sh))          # 2x, but never past the long edge the TITAN has proven
    w2, h2 = int(sw * up) // 16 * 16, int(sh * up) // 16 * 16
    dn = L["refine_denoise"] if denoise is None else float(denoise)
    refined = [_run(base, _refine_workflow(_upload(base, pc), prompt + L["realism"], seed, w2, h2, L["refine_steps"], dn),
                    f"finish: refine {k + 1}/{len(pieces)}", work / f"ref{k:02d}.mp4") for k, pc in enumerate(pieces)]
    joined = work / "joined.mp4"
    if len(refined) == 1:
        joined = refined[0]
    else:
        lst = work / "list.txt"; lst.write_text("".join(f"file '{x}'\n" for x in refined))
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(joined)], check=True, timeout=600)
    if interpolate and interpolate > 1:
        _run(base, _rife_workflow(_upload(base, joined), int(interpolate), float(fps_out or fps * interpolate)), "finish: smoothing", out)
    else:
        out.write_bytes(joined.read_bytes())
    return str(out)


# ---- long form v2: VACE continuation (motion memory across chunks) ---------------------------------
# Wan 2.1 VACE extends video: give it the tail of the previous chunk as the first frames of the
# control video, with a mask that is 0 over those frames (keep) and 1 over the rest (generate), and it
# continues the motion instead of restarting from a still. Chunks stay small; the finish stack
# (finish_video) brings size and frame rate back.
LONG2 = {"width": 512, "height": 288, "chunk_frames": 81, "overlap": 13, "fps": 16}   # exact 16:9 (was 448x256)


def _vace_extend_workflow(prompt: str, seed: int, width: int, height: int, frames: int,
                          tail_name: str | None, overlap: int, ref_name: str | None,
                          start_name: str | None = None, depth_name: str | None = None) -> dict:
    """One VACE pass. Control video = pinned frames (the previous tail, or the start still on pass 1; mask 0 =
    keep these pixels) followed by the new frames' guide (mask 1 = generate): the scene's depth map repeated,
    which holds the camera and the room in place, or flat grey = no guidance. ref_name = identity reference."""
    p = RESTYLE
    w = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": p["unet"]}},
        "1l": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": p["lora"], "strength_model": 1.0}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1l", 0], "shift": p["shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "static, frozen, still image, subtitles, text, watermark, logo, extra fingers, deformed hands, duplicated person"}},
        "7": {"class_type": "WanVaceToVideo", "inputs": {"positive": ["5", 0], "negative": ["6", 0], "vae": ["3", 0],
              "width": width, "height": height, "length": frames, "batch_size": 1, "strength": 1.0}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": p["steps"], "cfg": p["cfg"],
              "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0,
              "positive": ["7", 0], "negative": ["7", 1], "latent_image": ["7", 2]}},
        "9t": {"class_type": "TrimVideoLatent", "inputs": {"samples": ["8", 0], "trim_amount": ["7", 3]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["9t", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": 24}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/vace_ext", "format": "auto", "codec": "auto"}},
    }
    n_pin = overlap if tail_name else (1 if start_name else 0)
    n_new = max(1, frames - n_pin)
    pin = None
    if tail_name:
        w["t0"] = {"class_type": "LoadVideo", "inputs": {"file": tail_name}}
        w["t1"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["t0", 0]}}
        w["t2"] = {"class_type": "ImageScale", "inputs": {"image": ["t1", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
        pin = ["t2", 0]
    elif start_name:
        w["s0"] = {"class_type": "LoadImage", "inputs": {"image": start_name}}
        w["s1"] = {"class_type": "ImageScale", "inputs": {"image": ["s0", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
        pin = ["s1", 0]
    if depth_name:
        # the scene's depth, one copy per new frame: the camera and the room are held, the model animates within
        w["d0"] = {"class_type": "LoadImage", "inputs": {"image": depth_name}}
        w["d1"] = {"class_type": "ImageScale", "inputs": {"image": ["d0", 0], "upscale_method": "lanczos", "width": width, "height": height, "crop": "center"}}
        w["d2"] = {"class_type": "RepeatImageBatch", "inputs": {"image": ["d1", 0], "amount": n_new}}
        filler = ["d2", 0]
    else:
        w["g0"] = {"class_type": "EmptyImage", "inputs": {"width": width, "height": height, "batch_size": n_new, "color": 8355711}}   # 0x7F7F7F grey = no guidance
        filler = ["g0", 0]
    if pin or depth_name:
        w["m0"] = {"class_type": "SolidMask", "inputs": {"value": 0.0, "width": width, "height": height}}
        w["m1"] = {"class_type": "SolidMask", "inputs": {"value": 1.0, "width": width, "height": height}}
        w["mk0"] = {"class_type": "MaskToImage", "inputs": {"mask": ["m0", 0]}}
        w["mk1"] = {"class_type": "MaskToImage", "inputs": {"mask": ["m1", 0]}}
        w["mb1"] = {"class_type": "RepeatImageBatch", "inputs": {"image": ["mk1", 0], "amount": n_new}}
        if pin:
            w["c0"] = {"class_type": "ImageBatch", "inputs": {"image1": pin, "image2": filler}}
            w["mb0"] = {"class_type": "RepeatImageBatch", "inputs": {"image": ["mk0", 0], "amount": n_pin}}
            w["mc"] = {"class_type": "ImageBatch", "inputs": {"image1": ["mb0", 0], "image2": ["mb1", 0]}}
            control, masks = ["c0", 0], ["mc", 0]
        else:
            control, masks = filler, ["mb1", 0]
        w["mm"] = {"class_type": "ImageToMask", "inputs": {"image": masks, "channel": "red"}}
        w["7"]["inputs"]["control_video"] = control
        w["7"]["inputs"]["control_masks"] = ["mm", 0]
    if ref_name:
        w["r0"] = {"class_type": "LoadImage", "inputs": {"image": ref_name}}
        w["7"]["inputs"]["reference_image"] = ["r0", 0]
    return w


def depth_map(image: str, out_path: str, base: str = DEFAULT_URL, free_person: bool = False,
              width: int | None = None, height: int | None = None) -> str:
    """Depth map of a still (DepthAnything V2 on the render box), saved to out_path.
    free_person=True clean-plates the character: their box is filled from the depth around it, so a
    depth lock holds the room and the camera but leaves the person free to move."""
    import urllib.request, urllib.parse
    from PIL import Image
    name = _upload(base, Path(image).expanduser())
    wf = {"1": {"class_type": "LoadImage", "inputs": {"image": name}},
          "2": {"class_type": "DepthAnythingV2Preprocessor", "inputs": {"image": ["1", 0], "ckpt_name": "depth_anything_v2_vitb.pth", "resolution": 512}},
          "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "merge/depth"}}}
    pid = _post(base, "/prompt", {"prompt": wf}, 30)["prompt_id"]
    t0 = time.time()
    while True:
        h = json.loads(_get(base, f"/history/{pid}")).get(pid)
        st = (h or {}).get("status", {})
        if st.get("completed"):
            break
        if st.get("status_str") == "error":
            raise RuntimeError("depth failed: " + json.dumps(st)[:300])
        if time.time() - t0 > 300:
            raise TimeoutError("depth map took over 5 minutes")
        time.sleep(1.0)
    img = h["outputs"]["3"]["images"][0]
    qs = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img.get("subfolder", ""), "type": img.get("type", "output")})
    raw = urllib.request.urlopen(f"{base}/view?{qs}", timeout=60).read()
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    im = Image.open(out).convert("L")
    if width and height:
        im = im.resize((int(width), int(height)), Image.LANCZOS)
    if free_person:
        try:
            from .storyvideo import keypoints_of
            rec, cw, ch = keypoints_of(image, base=base)
            pts = []
            for person in rec.get("people", []):
                k = person.get("pose_keypoints_2d", [])
                pts += [(k[i] / cw, k[i + 1] / ch) for i in range(0, len(k), 3) if k[i + 2] > 0.3]
            if pts:
                W, H = im.size
                xs = [x * W for x, _ in pts]; ys = [y * H for _, y in pts]
                mx, my = 0.15 * W, 0.10 * H
                x0, x1 = max(0, int(min(xs) - mx)), min(W - 1, int(max(xs) + mx))
                y0, y1 = max(0, int(min(ys) - my)), min(H - 1, int(max(ys) + 2 * my))   # legs run below the last joint
                px = im.load()
                for y in range(y0, y1 + 1):
                    a, b = px[x0, y], px[x1, y]        # depth just outside the box on each side
                    span = max(1, x1 - x0)
                    for x in range(x0, x1 + 1):
                        px[x, y] = int(a + (b - a) * (x - x0) / span)
        except Exception:
            pass                                       # no person found: a plain depth map is still a lock
    im.save(out)
    return str(out)


def long_video2(prompt: str, out_path: str, seconds: float = 12.0, reference_image: str | None = None,
                width: int | None = None, height: int | None = None, fps_declared: int | None = None,
                seed: int | None = None, base: str = DEFAULT_URL, lock: str | None = "scene") -> str:
    """Long DRAFT with motion memory: VACE chunks, each continuing from the last `overlap` frames of
    the previous one. Output declared at fps_declared (default 16 — 1.5x slower than model motion;
    24 = true speed, 10 = long and slow). Returns out_path."""
    import subprocess, tempfile
    L = LONG2
    w, h = width or L["width"], height or L["height"]
    fps_d = fps_declared or L["fps"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="longvid2-"))
    total = max(5, int(round(seconds * fps_d)))          # frames of output at the declared rate
    # The opening frame: the still we were given, or one klein makes from the prompt (seconds). It is pinned
    # as frame 1, it is the identity reference for every pass, and its depth map is the lock that keeps the
    # camera and the room from creeping pass to pass (measured 2026-09-07: a 30 s café take zoomed itself
    # from a wide shot to a close-up and swapped the background twice without it).
    if reference_image:
        still = Path(reference_image).expanduser()
    else:
        from . import imagegen
        still = work / "start.png"
        sw, sh = imagegen.fit_size(None, None, None, 1024, default=(w, h)) if max(w, h) < 1024 else (w, h)
        imagegen.render(prompt, str(still), preset="reference", seed=seed, width=sw, height=sh, base=base)
    ref = _upload(base, still)
    depth_name = None
    if lock in ("frame", "scene"):
        depth_name = _upload(base, Path(depth_map(str(still), str(work / "depth.png"), base=base, free_person=(lock == "scene"), width=w, height=h)))
    chunks: list[Path] = []; tail: Path | None = None; got = 0; k = 0
    while got < total:
        n = min(L["chunk_frames"], total - got + (L["overlap"] if tail else 1))
        n = (n // 4) * 4 + 1 if n >= 5 else 5
        tail_name = _upload(base, tail) if tail else None
        wf = _vace_extend_workflow(prompt, seed + k, w, h, n, tail_name, L["overlap"], ref,
                                   start_name=(None if tail else ref), depth_name=depth_name)
        part = _run(base, wf, f"long clip: pass {k + 1}", work / f"chunk{k:02d}.mp4")
        # new frames only (drop the overlap that repeats the previous tail)
        keep = work / f"keep{k:02d}.mp4"
        skip = L["overlap"] if tail else 0          # pass 1 keeps its pinned opening frame
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(part), "-vf", f"select=gte(n\\,{skip}),setpts=N/FRAME_RATE/TB",
                        "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(keep)], check=True, timeout=600)
        chunks.append(keep); got += n - skip
        tail = work / f"tail{k:02d}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(part), "-vf", f"select=gte(n\\,{n - L['overlap']}),setpts=N/FRAME_RATE/TB",
                        "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(tail)], check=True, timeout=600)
        k += 1
    lst = work / "list.txt"; lst.write_text("".join(f"file '{c}'\n" for c in chunks))
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", f"setpts=N/({fps_d}*TB)", "-r", str(fps_d), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=600)
    return str(out)
