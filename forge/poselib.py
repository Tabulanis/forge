"""
Pose library — the body as data, so the fill is directed rather than guessed.

Ported from Puppetmaster (aidojo/current/ChoreographerStudio/js: pose.js, bodies.js, pose-draw.js):
  * skeletons in OpenPose-18 order (what DWPose emits and what the video model reads),
  * interpolation between keyframe poses (linear / eased),
  * a procedural walk cycle for the stretches where someone just walks,
  * retargeting: keep every bone's direction (the performance), impose another body's bone lengths (the character),
  * camera drift: the skeleton's place and size in frame IS the framing,
  * an OpenPose-style renderer (the standard limb colours) to frames and an mp4.
No DOM, no THREE — plain lists of (x, y, confidence) in pixel space.
"""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path

# OpenPose-18 (COCO) joint order
NOSE, NECK, RSHO, RELB, RWRI, LSHO, LELB, LWRI, RHIP, RKNE, RANK, LHIP, LKNE, LANK, REYE, LEYE, REAR, LEAR = range(18)
NAMES = ["nose", "neck", "rsho", "relb", "rwri", "lsho", "lelb", "lwri", "rhip", "rkne", "rank", "lhip", "lkne", "lank", "reye", "leye", "rear", "lear"]
# standard OpenPose limb sequence + colours (what DWPose draws, what the model was trained on)
LIMBS = [(1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (8, 9), (9, 10), (1, 11), (11, 12), (12, 13), (1, 0), (0, 14), (14, 16), (0, 15), (15, 17)]
COLORS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170),
          (0, 255, 255), (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85)]
# bone tree for retargeting (parent -> child, key), anchored at mid-hip ('MH'); neck-to-shoulders via 'MS' = neck
BONE_TREE = [("MH", RHIP, "hipR"), ("MH", LHIP, "hipL"), ("MH", NECK, "spine"),
             (NECK, RSHO, "shoulderR"), (NECK, LSHO, "shoulderL"), (NECK, NOSE, "neck"),
             (RSHO, RELB, "upperArmR"), (RELB, RWRI, "forearmR"), (LSHO, LELB, "upperArmL"), (LELB, LWRI, "forearmL"),
             (RHIP, RKNE, "thighR"), (RKNE, RANK, "shinR"), (LHIP, LKNE, "thighL"), (LKNE, LANK, "shinL"),
             (NOSE, REYE, "eyeR"), (NOSE, LEYE, "eyeL"), (REYE, REAR, "earR"), (LEYE, LEAR, "earL")]

Pose = list  # 18 x [x, y, c]


# ---- in / out ---------------------------------------------------------------------------------
def from_dwpose(kp: dict) -> Pose | None:
    """OpenPose-18 pose from one DWPose keypoint record ({"people":[{"pose_keypoints_2d":[x,y,c]*18}], canvas_width/height}).
    Coordinates come back in pixels of the canvas."""
    people = kp.get("people") or []
    if not people:
        return None
    flat = people[0].get("pose_keypoints_2d") or []
    pose = [[flat[i * 3], flat[i * 3 + 1], flat[i * 3 + 2]] for i in range(min(18, len(flat) // 3))]
    while len(pose) < 18:
        pose.append([0.0, 0.0, 0.0])
    return pose


def normalize(pose: Pose, w: int, h: int) -> Pose:
    return [[x / w, y / h, c] for x, y, c in pose]


def denormalize(pose: Pose, w: int, h: int) -> Pose:
    return [[x * w, y * h, c] for x, y, c in pose]


# ---- geometry ----------------------------------------------------------------------------------
def _mid(a, b):
    return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, min(a[2], b[2])]


def joint(pose: Pose, key):
    if key == "MH":
        return _mid(pose[RHIP], pose[LHIP])
    return pose[key]


def bone_lengths(pose: Pose) -> dict:
    """A body profile: every bone's length as a fraction of torso length (mid-hip to neck), plus torso in px."""
    mh, nk = joint(pose, "MH"), pose[NECK]
    torso = max(1e-6, math.hypot(nk[0] - mh[0], nk[1] - mh[1]))
    prof = {"torso": torso, "bones": {}}
    for p, c, key in BONE_TREE:
        a, b = joint(pose, p), joint(pose, c)
        if a[2] > 0.05 and b[2] > 0.05:
            prof["bones"][key] = math.hypot(b[0] - a[0], b[1] - a[1]) / torso
    return prof


def body_profile(poses: list[Pose]) -> dict:
    """Median bone lengths over many frames (Puppetmaster: median over samples, normalized to torso)."""
    profs = [bone_lengths(p) for p in poses if p]
    keys = set(k for pr in profs for k in pr["bones"])
    out = {"torso": sorted(pr["torso"] for pr in profs)[len(profs) // 2], "bones": {}}
    for k in keys:
        vals = sorted(pr["bones"][k] for pr in profs if k in pr["bones"])
        out["bones"][k] = vals[len(vals) // 2]
    return out


def retarget(pose: Pose, dst: dict, size_scale: float = 1.0) -> Pose:
    """bodies.js retargetPose: keep each bone's DIRECTION from `pose`, impose `dst` bone lengths (× torso × size_scale)."""
    src = bone_lengths(pose)
    torso = src["torso"] * size_scale
    out = [list(j) for j in pose]
    place = {"MH": joint(pose, "MH")}
    for p, c, key in BONE_TREE:
        a, b = joint(pose, p), joint(pose, c)
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        ux, uy = (dx / L, dy / L) if L > 1e-9 else (0.0, -1.0)
        L_new = (dst["bones"].get(key, src["bones"].get(key, 0.0))) * torso
        base = place.get(p, a)
        np_ = [base[0] + ux * L_new, base[1] + uy * L_new, b[2]]
        place[c] = np_
        out[c][0], out[c][1] = np_[0], np_[1]
    return out


# ---- motion ------------------------------------------------------------------------------------
def ease(t: float, kind: str = "smooth") -> float:
    if kind == "linear":
        return t
    return t * t * (3 - 2 * t)          # smoothstep


def lerp_pose(a: Pose, b: Pose, t: float) -> Pose:
    return [[a[i][0] + (b[i][0] - a[i][0]) * t, a[i][1] + (b[i][1] - a[i][1]) * t, min(a[i][2], b[i][2])] for i in range(18)]


def walk_overlay(pose: Pose, phase: float, stride: float = 0.35, arm: float = 0.25, bob: float = 0.03) -> Pose:
    """A walk cycle laid over a pose: legs and arms swing in counter-phase, the body bobs. `phase` in cycles;
    amplitudes as fractions of torso length. Side-on or three-quarter walks read correctly; head stays put."""
    prof = bone_lengths(pose); T = prof["torso"]
    out = [list(j) for j in pose]
    s = math.sin(2 * math.pi * phase)
    # legs: knees/ankles swing forward-back (x) with the phase; right and left opposite
    for hip, knee, ank, sign in ((RHIP, RKNE, RANK, 1), (LHIP, LKNE, LANK, -1)):
        out[knee][0] += sign * s * stride * 0.6 * T
        out[ank][0] += sign * s * stride * T
        out[ank][1] -= max(0.0, sign * s) * stride * 0.35 * T       # the swinging foot lifts
    # arms counter-swing
    for sho, elb, wri, sign in ((RSHO, RELB, RWRI, -1), (LSHO, LELB, LWRI, 1)):
        out[elb][0] += sign * s * arm * 0.6 * T
        out[wri][0] += sign * s * arm * T
    # bob: twice per cycle
    dy = -abs(math.sin(2 * math.pi * phase)) * bob * T
    for i in range(18):
        if i not in (RANK, LANK):
            out[i][1] += dy
    return out


def track(keyposes: list[Pose], times: list[float], fps: int = 24, walking: list[bool] | None = None,
          cadence: float = 1.9, easing: str = "smooth") -> list[Pose]:
    """Frames of poses from keyframe poses at `times` (seconds): eased interpolation between neighbours, with a walk
    cycle overlaid on intervals marked walking (cadence in steps per second)."""
    frames = []
    total = int(round((times[-1] - times[0]) * fps)) + 1
    for f in range(total):
        t = times[0] + f / fps
        k = max(0, min(len(times) - 2, next((i for i in range(len(times) - 1) if times[i] <= t <= times[i + 1]), len(times) - 2)))
        span = max(1e-6, times[k + 1] - times[k]); u = (t - times[k]) / span
        p = lerp_pose(keyposes[k], keyposes[k + 1], ease(u, easing))
        if walking and k < len(walking) and walking[k]:
            p = walk_overlay(p, phase=(t - times[k]) * cadence / 2.0)
        frames.append(p)
    return frames


def drift(frames: list[Pose], w: int, h: int, start_scale: float = 1.0, end_scale: float = 1.0,
          start_shift: tuple[float, float] = (0, 0), end_shift: tuple[float, float] = (0, 0)) -> list[Pose]:
    """Camera in the track: scale/shift the skeleton across the frames (push-in = scale up; pan = shift)."""
    n = len(frames); out = []
    for i, p in enumerate(frames):
        u = i / max(1, n - 1); s = start_scale + (end_scale - start_scale) * u
        sx = start_shift[0] + (end_shift[0] - start_shift[0]) * u; sy = start_shift[1] + (end_shift[1] - start_shift[1]) * u
        cx, cy = w / 2, h / 2
        out.append([[cx + (x - cx) * s + sx * w, cy + (y - cy) * s + sy * h, c] for x, y, c in p])
    return out


# ---- render -------------------------------------------------------------------------------------
def draw(pose: Pose, w: int, h: int, thickness: int | None = None):
    """OpenPose-style skeleton on black (the standard limb colours), as a PIL image."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), (0, 0, 0)); d = ImageDraw.Draw(img)
    th = thickness or max(2, int(min(w, h) / 90))
    for (a, b), col in zip(LIMBS, COLORS):
        pa, pb = pose[a], pose[b]
        if pa[2] < 0.05 or pb[2] < 0.05:
            continue
        d.line([(pa[0], pa[1]), (pb[0], pb[1])], fill=col, width=th)
    for i, (x, y, c) in enumerate(pose):
        if c >= 0.05:
            r = th * 0.9; d.ellipse([x - r, y - r, x + r, y + r], fill=COLORS[i % len(COLORS)])
    return img


def render_video(frames: list[Pose], w: int, h: int, out_path: str, fps: int = 24) -> str:
    work = Path(tempfile.mkdtemp(prefix="posetrack-"))
    for i, p in enumerate(frames):
        draw(p, w, h).save(work / f"f{i:05d}.png")
    out = Path(out_path).expanduser(); out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps), "-i", str(work / "f%05d.png"),
                    "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p", str(out)], check=True, timeout=900)
    return str(out)


# ---- a standing pose to start from (front-facing, normalized 0-1) --------------------------------
def neutral(w: int, h: int, cx: float = 0.5, top: float = 0.12, height: float = 0.76) -> Pose:
    """A plain standing figure: nose at `top`, feet at top+height, centred at cx (fractions of the frame)."""
    H = height * h; y0 = top * h; x0 = cx * w
    u = H / 7.5     # head unit
    P = {NOSE: (0, 0), NECK: (0, 1.0), RSHO: (-0.9, 1.15), LSHO: (0.9, 1.15), RELB: (-1.05, 2.6), LELB: (1.05, 2.6), RWRI: (-1.1, 3.9), LWRI: (1.1, 3.9),
         RHIP: (-0.55, 3.8), LHIP: (0.55, 3.8), RKNE: (-0.6, 5.6), LKNE: (0.6, 5.6), RANK: (-0.65, 7.4), LANK: (0.65, 7.4),
         REYE: (-0.18, -0.15), LEYE: (0.18, -0.15), REAR: (-0.42, -0.05), LEAR: (0.42, -0.05)}
    return [[x0 + P[i][0] * u, y0 + P[i][1] * u, 1.0] for i in range(18)]
