"""
Story video: a shot planned as stills, filled as motion, upscaled under guidance.

  hero          one full picture of the character/scene — the truth every stage is held to
  storyboard    N keyframes made with the edit model FROM the hero (same face, same boat), one
                per camera position; small, so the user approves the story as stills first
  fill          VACE fills each keyframe pair, pinned at both ends, hero as reference — tiny,
                fast, full motion, no drift across a seam because every seam is an approved frame
  guided_up     VACE again, doubling size: the draft is the structure, the hero is the identity,
                the prompt says what things are made of
  finish        videogen.finish_video / apply_stock as before

Everything renders on the render box (config media.*_url). Small first, always.
"""
from __future__ import annotations

import json
import random
import subprocess
import tempfile
import time
from pathlib import Path

from . import imagegen, videogen
from .videogen import DEFAULT_URL, RESTYLE, _run, _upload, _probe

STORY = {"board_w": 768, "board_h": 432,          # storyboard stills (16:9, small)
         "draft_w": 448, "draft_h": 256,          # tiny draft segments
         "seg_frames": 81, "fps": 16,             # per segment (4k+1), declared rate
         "up_strength": 0.75}                     # how hard the guided upscale follows the draft


def storyboard(hero: str, shots: list[str], out_dir: str, base: str = DEFAULT_URL,
               width: int | None = None, height: int | None = None, seed: int | None = None) -> list[str]:
    """One keyframe per shot description, each edited from the hero so identity holds."""
    out = Path(out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    w, h = width or STORY["board_w"], height or STORY["board_h"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    frames = []
    for i, shot in enumerate(shots):
        p = out / f"key{i + 1:02d}.png"
        imagegen.render(f"{shot}. Keep the same character, clothes, boat and place as image 1.", str(p), preset="edit",
                        references=[hero], seed=seed + i, width=w, height=h, base=base)
        frames.append(str(p))
    return frames


def _fill_workflow(prompt: str, seed: int, w: int, h: int, n: int, first_name: str, last_name: str, hero_name: str) -> dict:
    """VACE segment: keyframe A at frame 0, keyframe B at frame n-1 (mask 0 = keep), everything between generated."""
    p = RESTYLE
    grey = 8355711
    wf = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": p["unet"]}},
        "1l": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": p["lora"], "strength_model": 1.0}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1l", 0], "shift": p["shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "static, frozen, still image, subtitles, text, watermark, logo, extra fingers, deformed hands, duplicated person, morphing"}},
        "a0": {"class_type": "LoadImage", "inputs": {"image": first_name}},
        "a1": {"class_type": "ImageScale", "inputs": {"image": ["a0", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}},
        "b0": {"class_type": "LoadImage", "inputs": {"image": last_name}},
        "b1": {"class_type": "ImageScale", "inputs": {"image": ["b0", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}},
        "g0": {"class_type": "EmptyImage", "inputs": {"width": w, "height": h, "batch_size": n - 2, "color": grey}},
        "c0": {"class_type": "ImageBatch", "inputs": {"image1": ["a1", 0], "image2": ["g0", 0]}},
        "c1": {"class_type": "ImageBatch", "inputs": {"image1": ["c0", 0], "image2": ["b1", 0]}},
        "m0": {"class_type": "SolidMask", "inputs": {"value": 0.0, "width": w, "height": h}},
        "m1": {"class_type": "SolidMask", "inputs": {"value": 1.0, "width": w, "height": h}},
        "mk0": {"class_type": "MaskToImage", "inputs": {"mask": ["m0", 0]}},
        "mk1": {"class_type": "MaskToImage", "inputs": {"mask": ["m1", 0]}},
        "mb1": {"class_type": "RepeatImageBatch", "inputs": {"image": ["mk1", 0], "amount": n - 2}},
        "mc0": {"class_type": "ImageBatch", "inputs": {"image1": ["mk0", 0], "image2": ["mb1", 0]}},
        "mc1": {"class_type": "ImageBatch", "inputs": {"image1": ["mc0", 0], "image2": ["mk0", 0]}},
        "mm": {"class_type": "ImageToMask", "inputs": {"image": ["mc1", 0], "channel": "red"}},
        "r0": {"class_type": "LoadImage", "inputs": {"image": hero_name}},
        "7": {"class_type": "WanVaceToVideo", "inputs": {"positive": ["5", 0], "negative": ["6", 0], "vae": ["3", 0],
              "width": w, "height": h, "length": n, "batch_size": 1, "strength": 1.0,
              "control_video": ["c1", 0], "control_masks": ["mm", 0], "reference_image": ["r0", 0]}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": p["steps"], "cfg": p["cfg"],
              "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0,
              "positive": ["7", 0], "negative": ["7", 1], "latent_image": ["7", 2]}},
        "9t": {"class_type": "TrimVideoLatent", "inputs": {"samples": ["8", 0], "trim_amount": ["7", 3]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["9t", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": 24}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/story_seg", "format": "auto", "codec": "auto"}},
    }
    return wf


def fill(keyframes: list[str], prompt: str, hero: str, out_path: str, base: str = DEFAULT_URL,
         width: int | None = None, height: int | None = None, seg_frames: int | None = None,
         fps_declared: int | None = None, seed: int | None = None, camera: list[str] | None = None) -> str:
    """Tiny draft: one VACE segment per keyframe pair, pinned at both ends, hero as reference. Joined."""
    w, h = width or STORY["draft_w"], height or STORY["draft_h"]
    n = seg_frames or STORY["seg_frames"]; n = (n // 4) * 4 + 1
    fps_d = fps_declared or STORY["fps"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="storyfill-"))
    hero_name = _upload(base, Path(hero).expanduser())
    names = [_upload(base, Path(k).expanduser()) for k in keyframes]
    parts = []
    for i in range(len(keyframes) - 1):
        cam = f" Camera: {camera[i]}." if camera and i < len(camera) and camera[i] else ""
        wf = _fill_workflow(prompt + cam, seed + i, w, h, n, names[i], names[i + 1], hero_name)
        seg = _run(base, wf, f"story: segment {i + 1}/{len(keyframes) - 1}", work / f"seg{i:02d}.mp4")
        if i > 0:   # drop the duplicated keyframe at the seam
            cut = work / f"cut{i:02d}.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(seg), "-vf", "select=gte(n\\,1),setpts=N/FRAME_RATE/TB",
                            "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(cut)], check=True, timeout=600)
            seg = cut
        parts.append(seg)
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    lst = work / "list.txt"; lst.write_text("".join(f"file '{x}'\n" for x in parts))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", f"setpts=N/({fps_d}*TB)", "-r", str(fps_d), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=600)
    return str(out)


def _guided_workflow(src_name: str, prompt: str, seed: int, w: int, h: int, n: int, hero_name: str, strength: float) -> dict:
    """VACE repaint at a bigger size: the draft's frames are the control (structure), the hero the reference (identity)."""
    p = RESTYLE
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": p["unet"]}},
        "1l": {"class_type": "LoraLoaderModelOnly", "inputs": {"model": ["1", 0], "lora_name": p["lora"], "strength_model": 1.0}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": p["clip"], "type": "wan", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": p["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1l", 0], "shift": p["shift"]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": "static, frozen, subtitles, text, watermark, logo, extra fingers, deformed hands, duplicated person, morphing, blurry"}},
        "v0": {"class_type": "LoadVideo", "inputs": {"file": src_name}},
        "v1": {"class_type": "GetVideoComponents", "inputs": {"video": ["v0", 0]}},
        "v2": {"class_type": "ImageScale", "inputs": {"image": ["v1", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "disabled"}},
        "r0": {"class_type": "LoadImage", "inputs": {"image": hero_name}},
        "7": {"class_type": "WanVaceToVideo", "inputs": {"positive": ["5", 0], "negative": ["6", 0], "vae": ["3", 0],
              "width": w, "height": h, "length": n, "batch_size": 1, "strength": float(strength),
              "control_video": ["v2", 0], "reference_image": ["r0", 0]}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": p["steps"], "cfg": p["cfg"],
              "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0,
              "positive": ["7", 0], "negative": ["7", 1], "latent_image": ["7", 2]}},
        "9t": {"class_type": "TrimVideoLatent", "inputs": {"samples": ["8", 0], "trim_amount": ["7", 3]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["9t", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": 24}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/story_up", "format": "auto", "codec": "auto"}},
    }


def guided_up(draft: str, prompt: str, hero: str, out_path: str, factor: int = 2, base: str = DEFAULT_URL,
              strength: float | None = None, seed: int | None = None) -> str:
    """Guided upscale: doubles the draft, holding identity to the hero. Pieces of <=81 frames, joined."""
    strength = STORY["up_strength"] if strength is None else float(strength)
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="storyup-"))
    sw, sh, fps, dur = _probe(str(Path(draft).expanduser()))
    w, h = (sw * factor) // 16 * 16, (sh * factor) // 16 * 16
    n_total = int(dur * fps + 0.5); hero_name = _upload(base, Path(hero).expanduser())
    outs = []; i = 0; k = 0
    while i < n_total:
        n = min(STORY["seg_frames"], n_total - i); n = (n // 4) * 4 + 1 if n >= 5 else 5
        pc = work / f"piece{k:02d}.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(Path(draft).expanduser()),
                        "-vf", f"select=between(n\\,{i}\\,{i + n - 1}),setpts=N/FRAME_RATE/TB", "-frames:v", str(n), "-an",
                        "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(pc)], check=True, timeout=600)
        wf = _guided_workflow(_upload(base, pc), prompt, seed, w, h, n, hero_name, strength)
        outs.append(_run(base, wf, f"story: guided upscale {k + 1}", work / f"up{k:02d}.mp4")); i += n; k += 1
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    lst = work / "list.txt"; lst.write_text("".join(f"file '{x}'\n" for x in outs))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", f"setpts=N/({fps}*TB)", "-r", str(fps), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=600)
    return str(out)
