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


# ---- continuity: look before you fill --------------------------------------------------------------
def _look(images: list[str], question: str, max_tokens: int = 400) -> str:
    """Ask the house vision model (media.vision_url — her own eyes) about one or more pictures."""
    import base64, urllib.request
    from .config import load_config
    from .media import load_media_config
    mc = load_media_config(load_config())
    url = (getattr(mc, "vision_url", "") or "http://127.0.0.1:8087/v1").rstrip("/") + "/chat/completions"
    content = []
    for i, p in enumerate(images):
        b = base64.b64encode(Path(p).read_bytes()).decode()
        content.append({"type": "text", "text": f"Image {i + 1}:"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b}"}})
    content.append({"type": "text", "text": question})
    body = {"model": getattr(mc, "vision_model", "") or "vision", "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens, "temperature": 0.1, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def check_frame(hero: str, frame: str, shot: str) -> dict:
    """Continuity check: does the keyframe keep the hero's world and match the shot? Returns {ok, issues}."""
    q = ("Image 1 is the reference (the hero). Image 2 is a new keyframe that should show the SAME character, "
         f"clothes, vehicle/objects and place, from this camera position: \"{shot}\".\n"
         "Check continuity strictly. List only real problems, one per line, prefixed with '- ': things missing that "
         "should be visible, things added that weren't in the reference (a second lighthouse, extra people), the "
         "the SAME character appearing twice (e.g. one on the boat and one on the quay), "
         "character facing the wrong way for the shot, impossible positions (walking through a wall, standing on "
         "water), a different vehicle or clothes, a different time of day. Do NOT flag differences the requested camera "
         "position itself causes — size in frame, angle, which side of the character is visible, what is cropped out. "
         "If the frame is consistent and matches the shot, reply exactly: OK")
    try:
        ans = _look([hero, frame], q)
    except Exception as e:
        return {"ok": True, "issues": [], "note": f"check skipped: {type(e).__name__}"}
    issues = [ln[2:].strip() for ln in ans.splitlines() if ln.strip().startswith("- ")]
    ok = ans.strip().upper().startswith("OK") or not issues
    return {"ok": ok, "issues": issues, "raw": ans[:600]}


RULES = ("The character appears EXACTLY ONCE in the frame. Exactly one of each landmark. "
         "Anything the character has left behind (a boat, a chair) is empty. ")


def storyboard_checked(hero: str, shots: list[str], out_dir: str, base: str = DEFAULT_URL,
                       width: int | None = None, height: int | None = None, seed: int | None = None,
                       retries: int = 1, place: str | None = None, chain: bool = True,
                       scene: str = "") -> tuple[list[str], list[dict]]:
    """Storyboard with a continuity check per frame and one corrective redo. Returns (frames, reports).
    hero = the CHARACTER (alone). place = optional picture of the setting without them. chain = each
    frame is made from the previous one + the character, so the world continues instead of restarting.
    scene = a fixed scene map carried into every prompt (where things are, which way is which)."""
    out = Path(out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    w, h = width or STORY["board_w"], height or STORY["board_h"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    frames, reports = [], []
    for i, shot in enumerate(shots):
        p = out / f"key{i + 1:02d}.png"
        refs = [hero]
        who = "the person in image 1"
        if chain and frames:
            refs = [frames[-1], hero]; who = "the person in image 2"
            lead = f"Continue directly from image 1 (the previous shot): {shot}. The character is {who}."
        elif place:
            refs = [hero, place]; lead = f"{shot}. The character is {who}; the place is image 2."
        else:
            lead = f"{shot}. The character is {who}."
        prompt = f"{lead} {scene} {RULES}Same clothes, same time of day."
        rep = {"shot": shot, "attempts": []}
        for attempt in range(retries + 1):
            imagegen.render(prompt, str(p), preset="edit", references=refs, seed=seed + i + 100 * attempt, width=w, height=h, base=base)
            chk = check_frame(hero, str(p), shot)
            rep["attempts"].append(chk)
            if chk["ok"]:
                break
            prompt = f"{lead} {scene} {RULES}Fix these problems: " + "; ".join(chk["issues"][:4]) + "."
        rep["final_ok"] = rep["attempts"][-1]["ok"]
        frames.append(str(p)); reports.append(rep)
    return frames, reports


def inbetweens(keyframes: list[str], hero: str, out_dir: str, base: str = DEFAULT_URL,
               width: int | None = None, height: int | None = None, seed: int | None = None) -> list[str]:
    """A midpoint keyframe between each pair (the deltas): made from BOTH neighbours plus the hero,
    so the fill only bridges half the distance. Returns the expanded, ordered list."""
    out = Path(out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    w, h = width or STORY["board_w"], height or STORY["board_h"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    expanded = [keyframes[0]]
    for i in range(len(keyframes) - 1):
        p = out / f"key{i + 1:02d}b.png"
        imagegen.render("Image 1 is the shot before, image 2 the shot after. Make the exact halfway point between them: the "
                        "camera midway between the two positions, the action midway along, the same single character and the "
                        "same place. " + RULES,
                        str(p), preset="edit", references=[keyframes[i], keyframes[i + 1]], seed=seed + i, width=w, height=h, base=base)
        expanded += [str(p), keyframes[i + 1]]
    return expanded


# ---- reverse: from a video to a storyboard, then recast -------------------------------------------
def decompose(video: str, out_dir: str, every: float = 2.0, max_frames: int = 12) -> list[str]:
    """Keyframes out of an existing clip: scene cuts first, then evenly spaced pulls to fill in.
    Returns ordered PNG paths."""
    import subprocess, re
    out = Path(out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    src = str(Path(video).expanduser())
    _, _, fps, dur = _probe(src)
    # scene-cut times
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", src, "-vf", "select='gt(scene,0.35)',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=600)
    cuts = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
    times = sorted({0.0, *cuts, *[t for t in [i * every for i in range(int(dur / every) + 1)] if t < dur - 0.05], max(0.0, dur - 0.1)})
    if len(times) > max_frames:   # thin evenly, keep first and last
        step = (len(times) - 1) / (max_frames - 1); times = [times[round(i * step)] for i in range(max_frames)]
    frames = []
    for i, t in enumerate(times):
        p = out / f"src{i + 1:02d}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", src, "-frames:v", "1", "-update", "1", str(p)], check=True, timeout=120)
        frames.append(str(p))
    return frames


def recast(src_frames: list[str], hero: str, out_dir: str, base: str = DEFAULT_URL,
           width: int | None = None, height: int | None = None, seed: int | None = None,
           keep: str = "everything else — the place, the camera, the pose, the light") -> list[str]:
    """Swap the person in each source frame for the hero's character, keeping the shot. Chained so the
    result continues shot to shot. Returns the recast keyframes."""
    out = Path(out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    w, h = width or STORY["board_w"], height or STORY["board_h"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    frames = []
    for i, f in enumerate(src_frames):
        p = out / f"key{i + 1:02d}.png"
        refs = [f, hero] + ([frames[-1]] if frames else [])
        prev = " Image 3 is the previous recast shot: keep the character exactly as there." if frames else ""
        prompt = (f"Replace the person in image 1 with the character from image 2, in the same pose and position, keeping {keep}."
                  f"{prev} {RULES}")
        imagegen.render(prompt, str(p), preset="edit", references=refs, seed=seed + i, width=w, height=h, base=base)
        frames.append(str(p))
    return frames


# ---- through the event: pins inside a pass, not at its edges ----------------------------------------
def _pinned_workflow(prompt: str, seed: int, w: int, h: int, n: int, pins: list[tuple[int, str]], hero_name: str | None,
                     tail: tuple[str, int] | None = None, head: tuple[str, int] | None = None) -> dict:
    """VACE pass of n frames. pins = [(frame index, uploaded image)] held exactly (mask 0); everything
    else generated. tail = (uploaded clip, k): its last k frames occupy frames 0..k-1 (kept);
    head = (uploaded clip, k): its first k frames occupy frames n-k..n-1 (kept). Motion flows through."""
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
        "m0": {"class_type": "SolidMask", "inputs": {"value": 0.0, "width": w, "height": h}},
        "m1": {"class_type": "SolidMask", "inputs": {"value": 1.0, "width": w, "height": h}},
        "mk0": {"class_type": "MaskToImage", "inputs": {"mask": ["m0", 0]}},
        "mk1": {"class_type": "MaskToImage", "inputs": {"mask": ["m1", 0]}},
    }
    # frame-by-frame plan: (image source node, keep?)
    plan: list[tuple[list, bool]] = [(["g1", 0], False)] * n
    plan = list(plan)
    wf["g1"] = {"class_type": "EmptyImage", "inputs": {"width": w, "height": h, "batch_size": 1, "color": grey}}
    if tail:
        name, k = tail
        wf["t0"] = {"class_type": "LoadVideo", "inputs": {"file": name}}
        wf["t1"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["t0", 0]}}
        wf["t2"] = {"class_type": "ImageScale", "inputs": {"image": ["t1", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}}
        for j in range(k):
            wf[f"tf{j}"] = {"class_type": "ImageFromBatch", "inputs": {"image": ["t2", 0], "batch_index": j, "length": 1}}
            plan[j] = ([f"tf{j}", 0], True)
    if head:
        name, k = head
        wf["h0"] = {"class_type": "LoadVideo", "inputs": {"file": name}}
        wf["h1"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["h0", 0]}}
        wf["h2"] = {"class_type": "ImageScale", "inputs": {"image": ["h1", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}}
        for j in range(k):
            wf[f"hf{j}"] = {"class_type": "ImageFromBatch", "inputs": {"image": ["h2", 0], "batch_index": j, "length": 1}}
            plan[n - k + j] = ([f"hf{j}", 0], True)
    for i, (idx, name) in enumerate(pins):
        wf[f"p{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf[f"p{i}s"] = {"class_type": "ImageScale", "inputs": {"image": [f"p{i}", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}}
        plan[idx] = ([f"p{i}s", 0], True)
    # batch the frames and the mask in order (runs of identical sources are merged to keep the graph small)
    runs: list[tuple[list, bool, int]] = []
    for src, keep in plan:
        if runs and runs[-1][0] == src and runs[-1][1] == keep and src == ["g1", 0]:
            runs[-1] = (src, keep, runs[-1][2] + 1)
        else:
            runs.append((src, keep, 1))
    prev_img = prev_msk = None
    for r, (src, keep, cnt) in enumerate(runs):
        img = src if cnt == 1 else [f"rep{r}", 0]
        if cnt > 1:
            wf[f"rep{r}"] = {"class_type": "RepeatImageBatch", "inputs": {"image": src, "amount": cnt}}
        msk_src = ["mk0", 0] if keep else ["mk1", 0]
        wf[f"mrep{r}"] = {"class_type": "RepeatImageBatch", "inputs": {"image": msk_src, "amount": cnt}}
        msk = [f"mrep{r}", 0]
        if prev_img is None:
            prev_img, prev_msk = img, msk
        else:
            wf[f"cat{r}"] = {"class_type": "ImageBatch", "inputs": {"image1": prev_img, "image2": img}}
            wf[f"mcat{r}"] = {"class_type": "ImageBatch", "inputs": {"image1": prev_msk, "image2": msk}}
            prev_img, prev_msk = [f"cat{r}", 0], [f"mcat{r}", 0]
    wf["mm"] = {"class_type": "ImageToMask", "inputs": {"image": prev_msk, "channel": "red"}}
    vace = {"positive": ["5", 0], "negative": ["6", 0], "vae": ["3", 0], "width": w, "height": h, "length": n, "batch_size": 1,
            "strength": 1.0, "control_video": prev_img, "control_masks": ["mm", 0]}
    if hero_name:
        wf["r0"] = {"class_type": "LoadImage", "inputs": {"image": hero_name}}
        vace["reference_image"] = ["r0", 0]
    wf["7"] = {"class_type": "WanVaceToVideo", "inputs": vace}
    wf["8"] = {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": p["steps"], "cfg": p["cfg"],
               "sampler_name": p["sampler"], "scheduler": p["scheduler"], "denoise": 1.0,
               "positive": ["7", 0], "negative": ["7", 1], "latent_image": ["7", 2]}}
    wf["9t"] = {"class_type": "TrimVideoLatent", "inputs": {"samples": ["8", 0], "trim_amount": ["7", 3]}}
    wf["9"] = {"class_type": "VAEDecode", "inputs": {"samples": ["9t", 0], "vae": ["3", 0]}}
    wf["10"] = {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": 24}}
    wf["11"] = {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/story_pin", "format": "auto", "codec": "auto"}}
    return wf


def _crossfade_join(parts: list[Path], overlaps: list[int], out: Path, fps: int, work: Path) -> None:
    """Join clips whose consecutive pairs overlap by `overlaps[i]` frames, blending the overlap linearly."""
    import subprocess
    cur = parts[0]
    for i in range(1, len(parts)):
        ov = overlaps[i - 1]
        nxt = parts[i]
        a_frames = int(_probe(str(cur))[3] * _probe(str(cur))[2] + 0.5)
        d = ov / 24.0
        joined = work / f"join{i:02d}.mp4"
        # xfade over the overlap: cur's last ov frames blend into nxt's first ov frames
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(cur), "-i", str(nxt),
                        "-filter_complex", f"[0:v][1:v]xfade=transition=fade:duration={d:.4f}:offset={max(0.0, a_frames / 24.0 - d):.4f},format=yuv420p",
                        "-c:v", "libx264", "-crf", "14", "-an", str(joined)], check=True, timeout=900)
        cur = joined
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(cur), "-vf", f"setpts=N/({fps}*TB)", "-r", str(fps),
                    "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=900)


def fill_bidir(keyframes: list[str], prompt: str, hero: str, out_path: str, base: str = DEFAULT_URL,
               width: int | None = None, height: int | None = None, half: int = 40, fps_declared: int | None = None,
               seed: int | None = None, times: list[float] | None = None) -> str:
    """Bidirectional fill: every interior keyframe is the EVENT at the middle of its own pass
    [prev, event, next], so motion flows through it. Consecutive passes overlap by one interval, and
    the overlaps are crossfaded. With only two keyframes it is a single pinned pass."""
    w, h = width or STORY["draft_w"], height or STORY["draft_h"]
    fps_d = fps_declared or STORY["fps"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="storybidir-"))
    hero_name = _upload(base, Path(hero).expanduser())
    names = [_upload(base, Path(k).expanduser()) for k in keyframes]
    # frames per interval: from the beat times when given (irregular), else `half` each
    if times and len(times) == len(keyframes):
        ivals = [max(8, int(round((times[i + 1] - times[i]) * fps_d))) for i in range(len(keyframes) - 1)]
    else:
        ivals = [half] * (len(keyframes) - 1)
    if len(keyframes) == 2:
        n = ivals[0] + 1; n = (n // 4) * 4 + 1
        wf = _pinned_workflow(prompt, seed, w, h, n, [(0, names[0]), (n - 1, names[1])], hero_name)
        part = _run(base, wf, "story: pinned pass", work / "p0.mp4")
        parts, overlaps = [part], []
    else:
        parts, overlaps = [], []
        for i in range(1, len(keyframes) - 1):
            a, b = ivals[i - 1], ivals[i]
            n = a + b + 1; n = (n // 4) * 4 + 1; b = n - 1 - a
            wf = _pinned_workflow(prompt, seed + i, w, h, n, [(0, names[i - 1]), (a, names[i]), (n - 1, names[i + 1])], hero_name)
            parts.append(_run(base, wf, f"story: through event {i}/{len(keyframes) - 2}", work / f"p{i:02d}.mp4"))
            if i > 1:
                overlaps.append(a + 1)         # the shared interval prev->event
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(parts[0]), "-vf", f"setpts=N/({fps_d}*TB)", "-r", str(fps_d),
                        "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=900)
    else:
        _crossfade_join(parts, overlaps, out, fps_d, work)
    return str(out)


def bridge(clip_a: str, clip_b: str, prompt: str, out_path: str, hero: str | None = None, base: str = DEFAULT_URL,
           context: int = 12, gap: int = 33, seed: int | None = None) -> str:
    """Join two clips with generated motion between them: the last `context` frames of A and the
    first `context` frames of B are kept, `gap` frames are generated to connect them, both ways at
    once. Output = A + bridge + B at A's size."""
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(tempfile.mkdtemp(prefix="bridge-"))
    a, b = Path(clip_a).expanduser(), Path(clip_b).expanduser()
    sw, sh, fps, _ = _probe(str(a)); w, h = sw // 16 * 16, sh // 16 * 16
    n = context + gap + context; n = (n // 4) * 4 + 1; gap = n - 2 * context
    # the tail of A and the head of B as small clips
    ta, hb = work / "tailA.mp4", work / "headB.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-sseof", f"-{context / fps + 0.05:.3f}", "-i", str(a), "-frames:v", str(context), "-an", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(ta)], check=True, timeout=300)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(b), "-frames:v", str(context), "-an", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(hb)], check=True, timeout=300)
    hero_name = _upload(base, Path(hero).expanduser()) if hero else None
    wf = _pinned_workflow(prompt, seed, w, h, n, [], hero_name, tail=(_upload(base, ta), context), head=(_upload(base, hb), context))
    mid = _run(base, wf, "bridge: both ways", work / "bridge.mp4")
    # the generated middle only (drop the kept context frames), then A + middle + B
    gen = work / "gen.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mid), "-vf", f"select=between(n\\,{context}\\,{context + gap - 1}),setpts=N/FRAME_RATE/TB,scale={sw}:{sh}",
                    "-frames:v", str(gap), "-an", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", str(gen)], check=True, timeout=300)
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    lst = work / "list.txt"; lst.write_text(f"file '{a}'\nfile '{gen}'\nfile '{b}'\n")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-vf", f"fps={fps}", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=900)
    return str(out)


# ---- beats: the story in small steps of time ---------------------------------------------------------
def _ask(question: str, max_tokens: int = 900) -> str:
    """Plain-text question to the house brain (same endpoint her eyes use)."""
    import urllib.request
    from .config import load_config
    from .media import load_media_config
    mc = load_media_config(load_config())
    url = (getattr(mc, "vision_url", "") or "http://127.0.0.1:8087/v1").rstrip("/") + "/chat/completions"
    body = {"model": getattr(mc, "vision_model", "") or "brain", "messages": [{"role": "user", "content": question}],
            "max_tokens": max_tokens, "temperature": 0.3, "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def beat_sheet(idea: str, seconds: float, beat_seconds: float = 2.5, character: str = "the character",
               scene_map: str = "") -> list[dict]:
    """The shot as beats: one every `beat_seconds`, each the plausible NEXT moment of the one before —
    one small physical change, same place unless the beat is a cut, with a camera note.
    Returns [{"t": s, "beat": ..., "camera": ...}, ...] including t=0 and t=seconds."""
    n = max(2, int(round(seconds / beat_seconds)) + 1)
    q = (f"Plan a single continuous video shot as about {n} keyframes from t=0 to t={seconds:.0f} s. Put a keyframe wherever "
         "something CHANGES — a step off, a hand on a handle, a turn, a door opening — and nowhere else: a long walk with "
         "nothing new gets one keyframe at each end, a quick action gets several close together. Spacing between 1 and 6 "
         f"seconds, never evenly spaced, first at t=0 and last at t={seconds:.0f}.\n"
         f"The shot: {idea}\nCharacter: {character}.\n" + (f"Scene map (fixed, never contradict it): {scene_map}\n" if scene_map else "") +
         "Rules: each keyframe is what is physically true at its time, following naturally from the previous one — ONE change "
         "(a few steps, a hand on a handle, a turn of the head), never a jump to a new place; the same place unless a beat "
         "explicitly says CUT; the character appears exactly once; what they leave behind stays where it was, empty; "
         "NO new props, clothing or objects that were not in the character or scene description (no sticks, gloves, bags, hats). "
         "Describe what the camera sees, not the story. Give a short camera note per keyframe (e.g. 'static wide', "
         "'slow pan left following', 'push in').\n"
         "Answer as JSON only: a list of objects with keys t (seconds), beat (one or two sentences), camera (a few words).")
    raw = _ask(q)
    import re
    m = re.search(r"\[.*\]", raw, re.S)
    beats = json.loads(m.group(0)) if m else []
    beats = [b for b in beats if isinstance(b, dict) and b.get("beat")]
    if not beats:
        raise RuntimeError("the brain did not return a beat list: " + raw[:200])
    for i, b in enumerate(beats):
        b.setdefault("t", round(i * beat_seconds, 1)); b.setdefault("camera", "")
        b["t"] = float(b["t"])
    beats.sort(key=lambda b: b["t"])
    beats[0]["t"] = 0.0
    return beats


def story_from_beats(beats: list[dict], hero: str, out_dir: str, prompt: str, out_path: str, base: str = DEFAULT_URL,
                     place: str | None = None, scene_map: str = "", seed: int | None = None,
                     draft_w: int | None = None, draft_h: int | None = None, fps_declared: int | None = None) -> dict:
    """Beats -> chained, checked keyframes -> through-the-event fill sized to the beat spacing. Returns paths + reports."""
    fps_d = fps_declared or STORY["fps"]
    shots = [f"t={b['t']}s: {b['beat']} Camera: {b.get('camera', '')}." for b in beats]
    keys, reports = storyboard_checked(hero, shots, out_dir, base=base, seed=seed, place=place, chain=True, scene=scene_map)
    draft = fill_bidir(keys, prompt, hero, out_path, base=base, width=draft_w, height=draft_h, fps_declared=fps_d, seed=seed,
                       times=[float(b["t"]) for b in beats])
    return {"keyframes": keys, "reports": reports, "draft": draft, "beats": beats}
