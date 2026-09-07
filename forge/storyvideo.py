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
from .videogen import DEFAULT_URL, RESTYLE, _run, _upload, _probe, _post, _get

STORY = {"board_w": 768, "board_h": 432,          # storyboard stills (16:9, small)
         "draft_w": 512, "draft_h": 288,          # tiny draft segments (exact 16:9 like the boards; was 448x256 = 7:4)
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
        imagegen.render(f"{shot}. Keep the same character, clothes, boat and place as image 1.", str(p), preset="reference",
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


def check_frame(hero: str, frame: str, shot: str, person_only: bool = False) -> dict:
    """Continuity check: does the keyframe keep the hero's character (and, unless person_only, world) and match the shot?"""
    if person_only:
        q = ("Image 1 shows the CHARACTER (only the person matters here — ignore image 1's background entirely). Image 2 is a "
             f"new keyframe that should show the SAME person — face, hair, beard, build, clothes — from this camera position: \"{shot}\".\n"
             "First, in one sentence, describe the person in image 2 in your own words: where they are in the frame, which way they "
             "face, what their hands do, what they wear. Then compare with image 1 and with the shot. List only real, specific problems, "
             "one per line prefixed with '- ', each stating the EVIDENCE you see (for example: '- he faces the camera, but the shot says "
             "he faces the door on the right'; '- his jacket is orange, image 1's is yellow'). Never write generic category names. "
             "Do NOT mention the setting, boats, buildings or objects around them. When in doubt, it is fine. If the person is "
             "consistent and the shot is followed, end your reply with the single word: OK")
    else:
        q = ("Image 1 is the reference (the hero). Image 2 is a new keyframe that should show the SAME character, "
         f"clothes, vehicle/objects and place, from this camera position: \"{shot}\".\n"
         "Check continuity strictly. List only real problems, one per line, prefixed with '- ': things missing that "
         "should be visible, things added that weren't in the reference (a second lighthouse, extra people), the "
         "the SAME character appearing twice (e.g. one on the boat and one on the quay), objects or props that appeared "
         "from nowhere (bags, boots, sticks), a vehicle or building that changed design (a different boat, a different café), "
         "character facing the wrong way for the shot, impossible positions (walking through a wall, standing on "
         "water), a different vehicle or clothes, a different time of day. Do NOT flag differences the requested camera "
         "position itself causes — size in frame, angle, which side of the character is visible, what is cropped out. "
         "If the frame is consistent and matches the shot, reply exactly: OK\n"
         "Also check DIRECTION: if the requested camera position or the described action was not followed (wrong side, "
         "wrong distance, the action not happening), list that as a problem too.")
    try:
        ans = _look([hero, frame], q)
    except Exception as e:
        return {"ok": True, "issues": [], "note": f"check skipped: {type(e).__name__}"}
    issues = [ln[2:].strip() for ln in ans.splitlines() if ln.strip().startswith("- ")]
    ok = ans.strip().upper().startswith("OK") or ans.strip().upper().endswith("OK") or not issues
    return {"ok": ok, "issues": issues, "raw": ans[:600]}


RULES = ("Same scene as the previous shot: the camera may move, the place may not. Follow the camera note and the action "
         "exactly. The character appears EXACTLY ONCE in the frame. Exactly one of each landmark. "
         "Anything the character has left behind (a boat, a chair) is empty. Every door, window and wall belongs to a "
         "building that is visible in the frame and matches the place; nothing stands on its own. ")


def check_transition(prev_frame: str, frame: str, prev_beat: str, beat: str, cause: str = "") -> dict:
    """Causal check: does image 2 follow from image 1 as the beat says? Flags impossible consequences."""
    q = ("Image 1 is the previous keyframe of a continuous shot; image 2 is the next one, a few seconds later.\n"
         f"Previous keyframe: \"{prev_beat}\"\nThis keyframe: \"{beat}\"" + (f"\nStated cause: {cause}" if cause else "") + "\n"
         "Judge only whether image 2 is a physically possible consequence of image 1 in that time: same place seen from a "
         "plausible camera move, the character moved a plausible distance, nothing passed through a wall or door that was "
         "closed, nothing that was left behind moved, no object appeared or vanished. List only real problems, one per line "
         "prefixed with '- '. If it follows, reply exactly: OK")
    try:
        ans = _look([prev_frame, frame], q)
    except Exception as e:
        return {"ok": True, "issues": [], "note": f"check skipped: {type(e).__name__}"}
    issues = [ln[2:].strip() for ln in ans.splitlines() if ln.strip().startswith("- ")]
    return {"ok": ans.strip().upper().startswith("OK") or not issues, "issues": issues, "raw": ans[:600]}


def check_place(place: str, frame: str, shot: str) -> dict:
    """Structure check: do the buildings, boat and landmarks in the frame belong to the place, and does every
    door/window/wall belong to a building that is actually there?"""
    q = ("Image 1 is the PLACE (no people): its buildings, boat, quay and landmarks are the only ones that exist. Image 2 is a "
         f"keyframe that must be set in that place, from this camera position: \"{shot}\".\n"
         "List only real problems, one per line prefixed with '- ': a building or boat of a different design than image 1; "
         "a door, window or wall that is not part of a building visible in the frame (a door standing on its own, a door on "
         "the wrong building, a door where image 1 has a wall); a landmark duplicated or missing; a new structure that image 1 "
         "does not have. Do NOT flag what the camera position itself causes (size, angle, cropping). If the structures are "
         "consistent, reply exactly: OK")
    try:
        ans = _look([place, frame], q)
    except Exception as e:
        return {"ok": True, "issues": [], "note": f"check skipped: {type(e).__name__}"}
    issues = [ln[2:].strip() for ln in ans.splitlines() if ln.strip().startswith("- ")]
    return {"ok": ans.strip().upper().startswith("OK") or not issues, "issues": issues, "raw": ans[:600]}


def storyboard_checked(hero: str, shots: list[str], out_dir: str, base: str = DEFAULT_URL,
                       width: int | None = None, height: int | None = None, seed: int | None = None,
                       retries: int = 2, place: str | None = None, chain: bool = True,
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
            refs = [frames[-1], hero] + ([place] if place else []); who = "the person in image 2"
            lead = (f"Continue directly from image 1 (the previous shot): {shot}. The character is {who}."
                    + (" The place, its buildings and its boat are exactly those of image 3 — never redesign them." if place else ""))
        elif place:
            refs = [hero, place]; lead = f"{shot}. The character is {who}; the place is image 2."
        else:
            lead = f"{shot}. The character is {who}."
        prompt = f"{lead} {scene} {RULES}Same clothes, same time of day."
        rep = {"shot": shot, "attempts": []}
        for attempt in range(retries + 1):
            imagegen.render(prompt, str(p), preset="reference", references=refs, seed=seed + i + 100 * attempt, width=w, height=h, base=base)
            chk = check_frame(hero, str(p), shot, person_only=bool(place))
            if chk["ok"] and place:
                pc = check_place(place, str(p), shot)
                if not pc["ok"]:
                    chk = {"ok": False, "issues": ["structure: " + x for x in pc["issues"]], "raw": pc.get("raw", "")}
            if chk["ok"] and frames:
                tr = check_transition(frames[-1], str(p), shots[i - 1], shot)
                if not tr["ok"]:
                    chk = {"ok": False, "issues": ["transition: " + x for x in tr["issues"]], "raw": tr.get("raw", "")}
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
                        str(p), preset="reference", references=[keyframes[i], keyframes[i + 1]], seed=seed + i, width=w, height=h, base=base)
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
    # a tight crop of the hero (centre 60% width, full height) so its setting can't take over the shot
    import subprocess
    hero_crop = str(out / "hero-figure.png")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(Path(hero).expanduser()),
                    "-vf", "crop=iw*0.6:ih:iw*0.2:0", str(hero_crop)], check=True, timeout=60)
    frames = []
    for i, f in enumerate(src_frames):
        p = out / f"key{i + 1:02d}.png"
        refs = [f, hero_crop] + ([frames[-1]] if frames else [])
        prev = " Image 3 is the previous recast shot: keep the character exactly as there." if frames else ""
        prompt = (f"Image 1 is the shot: keep its room, camera, framing, lighting and the exact body pose of the person. "
                  f"Change ONLY who the person is: give them the face, hair, beard and clothes of the character in image 2. "
                  f"Do not use image 2's background or setting at all; keep {keep} from image 1.{prev} {RULES}")
        imagegen.render(prompt, str(p), preset="reference", references=refs, seed=seed + i, width=w, height=h, base=base)
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
               seed: int | None = None, times: list[float] | None = None, work_dir: str | None = None) -> str:
    """Bidirectional fill: every interior keyframe is the EVENT at the middle of its own pass
    [prev, event, next], so motion flows through it. Consecutive passes overlap by one interval, and
    the overlaps are crossfaded. With only two keyframes it is a single pinned pass."""
    w, h = width or STORY["draft_w"], height or STORY["draft_h"]
    fps_d = fps_declared or STORY["fps"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    # passes live next to the output (resumable): a pass file that already exists is reused
    work = Path(work_dir).expanduser() if work_dir else Path(out_path).expanduser().with_suffix("") .with_name(Path(out_path).stem + "-passes")
    work.mkdir(parents=True, exist_ok=True)
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
            full = work / f"p{i:02d}.mp4"
            if not full.is_file() or full.stat().st_size < 1000:
                wf = _pinned_workflow(prompt, seed + i, w, h, n, [(0, names[i - 1]), (a, names[i]), (n - 1, names[i + 1])], hero_name)
                full = _run(base, wf, f"story: through event {i}/{len(keyframes) - 2}", full)
            if i == 1:
                parts.append(full)             # first pass: everything up to its end pin
            else:
                # later passes: only from their middle pin (which is the previous pass's end pin) onward —
                # the pin frame is identical in both, so the cut lands on it and motion flows through
                cut = work / f"c{i:02d}.mp4"
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(full), "-vf", f"select=gte(n\\,{a + 1}),setpts=N/FRAME_RATE/TB",
                                "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(cut)], check=True, timeout=600)
                parts.append(cut)
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    lst = work / "list.txt"; lst.write_text("".join(f"file '{x}'\n" for x in parts))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", f"setpts=N/({fps_d}*TB)", "-r", str(fps_d), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=900)
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
         "THREE RULES. (1) ONE SCENE: the whole shot stays in one place; the camera may move, the place may not; never cut to "
         "another location. (2) NO GUESSING: every keyframe states the concrete facts a picture needs — where the character "
         "stands (left/centre/right, near/far), which way they face, which hand does what, what is in the background, where "
         "the camera stands and points. (3) DIRECTION IS AN ORDER: the camera note and the action are to be followed exactly.\n"
         "Each keyframe is what is physically true at its time, following naturally from the previous one — ONE change "
         "(a few steps, a hand on a handle, a turn of the head), never a jump to a new place; always the same place; the character appears exactly once; what they leave behind stays where it was, empty; "
         "NO new props, clothing or objects that were not in the character or scene description (no sticks, gloves, bags, hats). "
         "Every keyframe after the first is CAUSED by the one before: state the cause. The time of a keyframe is the "
         "time of the previous one plus how long that consequence physically takes (a step: ~1 s; twenty metres of walking: "
         "~12 s; a door opening: ~2 s). Describe what the camera sees, not the story. Give a short camera note per keyframe "
         "(e.g. 'static wide', 'slow pan left following', 'push in').\n"
         "Answer as JSON only: a list of objects with keys t (seconds), beat (one or two sentences: what is true now), "
         "cause (one short clause: because ...), camera (a few words).")
    raw = _ask(q)
    import re
    m = re.search(r"\[.*\]", raw, re.S)
    beats = json.loads(m.group(0)) if m else []
    beats = [b for b in beats if isinstance(b, dict) and b.get("beat")]
    if not beats:
        raise RuntimeError("the brain did not return a beat list: " + raw[:200])
    for i, b in enumerate(beats):
        b.setdefault("t", round(i * beat_seconds, 1)); b.setdefault("camera", ""); b.setdefault("cause", "" if i == 0 else "follows from the previous keyframe")
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


def sheet(keyframes: list[str], beats: list[dict], out_path: str, reports: list[dict] | None = None) -> str:
    """A contact sheet that shows the chain: each frame with its time, beat, cause and the checker's verdict."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap
    W, H, CAP = 400, 225, 150
    cols = min(3, len(keyframes)); rows = (len(keyframes) + cols - 1) // cols
    img = Image.new("RGB", (cols * W + 20, rows * (H + CAP) + 20), (243, 244, 242))
    d = ImageDraw.Draw(img)
    try:
        f_b = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        f_r = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception:
        f_b = f_r = ImageFont.load_default()
    for i, k in enumerate(keyframes):
        x = 10 + (i % cols) * W; y = 10 + (i // cols) * (H + CAP)
        im = Image.open(k).convert("RGB"); im.thumbnail((W - 10, H - 10)); img.paste(im, (x + 5, y + 5))
        b = beats[i] if i < len(beats) else {}
        ok = (reports[i].get("final_ok") if reports and i < len(reports) else None)
        head = f"{i + 1}.  t = {b.get('t', '?')} s   [{b.get('camera', '')}]" + ("" if ok is None else ("   ✓" if ok else "   ✗ flagged"))
        d.text((x + 5, y + H), head, fill=(29, 33, 38), font=f_b)
        ty = y + H + 20
        for line in textwrap.wrap(str(b.get("beat", "")), 58)[:4]:
            d.text((x + 5, ty), line, fill=(29, 33, 38), font=f_r); ty += 15
        if b.get("cause"):
            for line in textwrap.wrap("because " + str(b["cause"]).removeprefix("because ").strip(), 58)[:2]:
                d.text((x + 5, ty), line, fill=(74, 100, 114), font=f_r); ty += 15
        if reports and i < len(reports) and not reports[i].get("final_ok", True):
            iss = reports[i]["attempts"][-1].get("issues", [])[:1]
            for line in textwrap.wrap("flag: " + (iss[0] if iss else ""), 58)[:2]:
                d.text((x + 5, ty), line, fill=(170, 60, 40), font=f_r); ty += 15
    img.save(out_path); return out_path


# ---- least resistance: keyframes on the peaks of change ---------------------------------------------
def change_curve(video: str) -> list[float]:
    """Per-frame change energy (mean absolute luma difference to the previous frame), via ffmpeg signalstats."""
    import subprocess, re
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(Path(video).expanduser()), "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YDIF",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    vals = [float(v) for v in re.findall(r"lavfi\.signalstats\.YDIF=([0-9.]+)", r.stderr)]
    return vals


def erode_peaks(curve: list[float], fps: float, min_gap_s: float = 1.0, max_frames: int = 12, smooth_s: float = 0.5) -> list[int]:
    """Weather the curve (moving average over smooth_s), keep local maxima at least min_gap_s apart, strongest first,
    up to max_frames including the first and last frame. Returns frame indices, sorted."""
    n = len(curve)
    if n == 0:
        return [0]
    k = max(1, int(smooth_s * fps))
    sm = [sum(curve[max(0, i - k):i + k + 1]) / len(curve[max(0, i - k):i + k + 1]) for i in range(n)]
    gap = max(1, int(min_gap_s * fps))
    cands = [i for i in range(1, n - 1) if sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and sm[i] > 0]
    cands.sort(key=lambda i: -sm[i])
    chosen: list[int] = [0, n - 1]
    for i in cands:
        if len(chosen) >= max_frames:
            break
        if all(abs(i - c) >= gap for c in chosen):
            chosen.append(i)
    return sorted(chosen)


def decompose_peaks(video: str, out_dir: str, max_frames: int = 12, min_gap_s: float = 1.0) -> tuple[list[str], list[float]]:
    """Keyframes on the peaks of change. Returns (png paths, times in seconds)."""
    import subprocess
    out = Path(out_dir).expanduser(); out.mkdir(parents=True, exist_ok=True)
    src = str(Path(video).expanduser()); _, _, fps, _ = _probe(src)
    idx = erode_peaks(change_curve(src), fps, min_gap_s=min_gap_s, max_frames=max_frames)
    frames, times = [], []
    for j, i in enumerate(idx):
        p = out / f"src{j + 1:02d}.png"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vf", f"select=eq(n\\,{i})", "-frames:v", "1", "-update", "1", str(p)], check=True, timeout=120)
        frames.append(str(p)); times.append(round(i / fps, 2))
    return frames, times


def make_place(prompt: str, out_path: str, scene_map: str, base: str = DEFAULT_URL, seed: int | None = None,
               width: int = 1328, height: int = 736, retries: int = 2) -> tuple[str, dict]:
    """The place hero, checked against the scene map (landmark counts, layout, no people) and regenerated if wrong."""
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    rep = {"attempts": []}
    for attempt in range(retries + 1):
        imagegen.render(prompt + (" " if attempt == 0 else " Fix these problems: " + "; ".join(rep["attempts"][-1]["issues"][:4]) + ". "),
                        out_path, preset="reference", seed=seed + 100 * attempt, width=width, height=height, base=base)
        q = (f"This picture must match this scene map exactly: \"{scene_map}\". No people at all. List only real problems, one per "
             "line prefixed with '- ': a landmark duplicated (two lighthouses) or missing, something on the wrong side, a person "
             "present, a building of the wrong kind. If it matches, reply exactly: OK")
        try:
            ans = _look([out_path], q)
        except Exception as e:
            rep["attempts"].append({"ok": True, "issues": [], "note": f"check skipped: {type(e).__name__}"}); break
        issues = [ln[2:].strip() for ln in ans.splitlines() if ln.strip().startswith("- ")]
        ok = ans.strip().upper().startswith("OK") or not issues
        rep["attempts"].append({"ok": ok, "issues": issues})
        if ok:
            break
    rep["final_ok"] = rep["attempts"][-1]["ok"]
    return out_path, rep


# ---- pose-guided: the body is directed, not guessed ----------------------------------------------
def pose_track(video: str, out_path: str, base: str = DEFAULT_URL, width: int | None = None, height: int | None = None) -> str:
    """Skeleton every frame of a clip (DWPose) into a pose video — the control track for a guided fill."""
    w, h = width or STORY["draft_w"], height or STORY["draft_h"]
    name = _upload(base, Path(video).expanduser())
    wf = {"1": {"class_type": "LoadVideo", "inputs": {"file": name}},
          "2": {"class_type": "GetVideoComponents", "inputs": {"video": ["1", 0]}},
          "3": {"class_type": "ImageScale", "inputs": {"image": ["2", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}},
          "4": {"class_type": "DWPreprocessor", "inputs": {"image": ["3", 0], "detect_hand": "enable", "detect_body": "enable", "detect_face": "disable", "resolution": 512}},
          "10": {"class_type": "CreateVideo", "inputs": {"images": ["4", 0], "fps": 24}},
          "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": "merge/posetrack", "format": "auto", "codec": "auto"}}}
    return str(_run(base, wf, "pose track", Path(out_path).expanduser()))


def generated_actor(action: str, out_path: str, seconds: float = 5.0, base: str = DEFAULT_URL, seed: int | None = None,
                    width: int | None = None, height: int | None = None) -> str:
    """The model acts the action plainly (one person, plain clothes, plain room, no style) so its motion can be skeletoned."""
    w, h = width or STORY["draft_w"], height or STORY["draft_h"]
    prompt = (f"A plain reference clip for animators: one adult in plain grey clothes, full body always in frame, in an empty light-grey room, "
              f"performs this action clearly and completely: {action}. Static camera, even lighting, nothing else in the scene.")
    return videogen.render(prompt, str(Path(out_path).expanduser()), seconds=seconds, width=w, height=h, seed=seed, base=base, preset="fast")


def fill_posed(keyframes: list[str], pose_video: str, prompt: str, hero: str, out_path: str, base: str = DEFAULT_URL,
               width: int | None = None, height: int | None = None, fps_declared: int | None = None, seed: int | None = None,
               times: list[float] | None = None, strength: float = 0.9) -> str:
    """Draft where the pose track steers every frame (control video) and the keyframes are pinned at their times.
    The pose track is re-timed to span the keyframe times. One VACE pass per <=81 frames, joined at the pins."""
    import subprocess
    w, h = width or STORY["draft_w"], height or STORY["draft_h"]
    fps_d = fps_declared or STORY["fps"]
    seed = random.randrange(2 ** 31) if seed is None else int(seed)
    work = Path(out_path).expanduser().with_name(Path(out_path).stem + "-passes"); work.mkdir(parents=True, exist_ok=True)
    hero_name = _upload(base, Path(hero).expanduser())
    names = [_upload(base, Path(k).expanduser()) for k in keyframes]
    n_keys = len(keyframes)
    if not times or len(times) != n_keys:
        times = [i * 2.5 for i in range(n_keys)]
    total = max(5, int(round((times[-1] - times[0]) * fps_d)) + 1)
    total = (total // 4) * 4 + 1
    # re-time the pose track to `total` frames at 24 fps in the file (the model's native rate)
    pt = work / "pose-retimed.mp4"
    _, _, pfps, pdur = _probe(str(Path(pose_video).expanduser()))
    factor = (total / 24.0) / max(pdur, 0.04)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(Path(pose_video).expanduser()),
                    "-vf", f"setpts={factor:.6f}*PTS,fps=24,scale={w}:{h}", "-frames:v", str(total), "-an", "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(pt)], check=True, timeout=600)
    pt_name = _upload(base, pt)
    # pins at their frame indices within the whole track
    pins_all = [(min(total - 1, int(round((t - times[0]) * fps_d))), nm) for t, nm in zip(times, names)]
    # passes of <=81 frames, each starting on a pin frame where possible
    parts = []; start = 0; k = 0
    while start < total - 1:
        n = min(81, total - start); n = (n // 4) * 4 + 1 if n >= 5 else 5
        end = start + n - 1
        pins = [(idx - start, nm) for idx, nm in pins_all if start <= idx <= end]
        wf = _pinned_workflow(prompt, seed + k, w, h, n, pins, hero_name)
        # swap the grey filler for the pose track segment: control = pose frames, mask 1 (guide) except pins (0)
        wf["pt0"] = {"class_type": "LoadVideo", "inputs": {"file": pt_name}}
        wf["pt1"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["pt0", 0]}}
        wf["pt2"] = {"class_type": "ImageFromBatch", "inputs": {"image": ["pt1", 0], "batch_index": start, "length": n}}
        # pins composited over the pose frames: for each pin, replace that frame
        cur = ["pt2", 0]
        for j, (idx, nm) in enumerate(pins):
            wf[f"pk{j}"] = {"class_type": "LoadImage", "inputs": {"image": nm}}
            wf[f"pks{j}"] = {"class_type": "ImageScale", "inputs": {"image": [f"pk{j}", 0], "upscale_method": "lanczos", "width": w, "height": h, "crop": "center"}}
            if idx > 0:
                wf[f"pa{j}"] = {"class_type": "ImageFromBatch", "inputs": {"image": cur, "batch_index": 0, "length": idx}}
                wf[f"pb{j}"] = {"class_type": "ImageBatch", "inputs": {"image1": [f"pa{j}", 0], "image2": [f"pks{j}", 0]}}
                head = [f"pb{j}", 0]
            else:
                head = [f"pks{j}", 0]
            if idx < n - 1:
                wf[f"pc{j}"] = {"class_type": "ImageFromBatch", "inputs": {"image": cur, "batch_index": idx + 1, "length": n - idx - 1}}
                wf[f"pd{j}"] = {"class_type": "ImageBatch", "inputs": {"image1": head, "image2": [f"pc{j}", 0]}}
                cur = [f"pd{j}", 0]
            else:
                cur = head
        wf["7"]["inputs"]["control_video"] = cur
        wf["7"]["inputs"]["strength"] = float(strength)
        part = work / f"p{k:02d}.mp4"
        if not part.is_file() or part.stat().st_size < 1000:
            part = _run(base, wf, f"posed fill: pass {k + 1}", part)
        if k > 0:
            cut = work / f"c{k:02d}.mp4"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(part), "-vf", "select=gte(n\\,1),setpts=N/FRAME_RATE/TB",
                            "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(cut)], check=True, timeout=600)
            part = cut
        parts.append(part); start = end; k += 1
    out = Path(out_path).expanduser()
    lst = work / "list.txt"; lst.write_text("".join(f"file '{x}'\n" for x in parts))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-vf", f"setpts=N/({fps_d}*TB)", "-r", str(fps_d), "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-an", str(out)], check=True, timeout=900)
    return str(out)


# ---- the body as data: keypoints from pictures, pose tracks from storyboards ------------------------
def keypoints_of(image: str, base: str = DEFAULT_URL, resolution: int = 512) -> tuple[dict, int, int]:
    """DWPose keypoints (OpenPose-18 record) of a picture, plus the canvas size they're in."""
    name = _upload(base, Path(image).expanduser())
    wf = {"1": {"class_type": "LoadImage", "inputs": {"image": name}},
          "2": {"class_type": "DWPreprocessor", "inputs": {"image": ["1", 0], "detect_hand": "disable", "detect_body": "enable", "detect_face": "disable", "resolution": resolution}},
          "3": {"class_type": "SavePoseKpsAsJsonFile", "inputs": {"pose_kps": ["2", 1], "filename_prefix": "merge/kps"}},
          "4": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "merge/kpsimg"}}}
    pid = _post(base, "/prompt", {"prompt": wf}, 30)["prompt_id"]
    t0 = time.time()
    while True:
        h = json.loads(_get(base, f"/history/{pid}")).get(pid)
        st = (h or {}).get("status", {})
        if st.get("completed"):
            break
        if st.get("status_str") == "error":
            raise RuntimeError("keypoints failed: " + json.dumps(st)[:300])
        if time.time() - t0 > 600:
            raise TimeoutError("keypoints took over 10 minutes")
        time.sleep(1.0)
    # the json node reports nothing in history; find the newest merge/kps_*.json via the image's numbering
    img = h["outputs"]["4"]["images"][0]
    num = img["filename"].split("_")[-2]
    import urllib.parse
    for cand in (f"kps_{num}_.json", f"kps_{num}.json"):
        try:
            raw = _get(base, "/view?" + urllib.parse.urlencode({"filename": cand, "subfolder": "merge", "type": "output"}), 30)
            data = json.loads(raw)
            break
        except Exception:
            data = None
    if data is None:
        raise RuntimeError("keypoint json not found in ComfyUI output")
    rec = data[0] if isinstance(data, list) else data
    return rec, int(rec.get("canvas_width", resolution)), int(rec.get("canvas_height", resolution))


def pose_track_from_keyframes(keyframes: list[str], times: list[float], out_path: str, base: str = DEFAULT_URL,
                              width: int | None = None, height: int | None = None, walking: list[bool] | None = None,
                              fps: int = 24) -> str:
    """Puppetmaster route with no actor: skeleton each keyframe, interpolate the joints across the beat times, lay a
    walk cycle on the walking intervals, render an OpenPose track at draft size."""
    from . import poselib as P
    w, h = width or STORY["draft_w"], height or STORY["draft_h"]
    poses = []
    for k in keyframes:
        rec, cw, ch = keypoints_of(k, base=base)
        p = P.from_dwpose(rec)
        if p is None:
            raise RuntimeError(f"no person found in {k}")
        poses.append(P.denormalize(P.normalize(p, cw, ch), w, h))
    frames = P.track(poses, times, fps=fps, walking=walking)
    return P.render_video(frames, w, h, out_path, fps=fps)
