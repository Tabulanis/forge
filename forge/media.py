"""
Eyes and ears — vision, speech-in, speech-out.

The point of this module is to get non-text information *into* the agent.
Everything a language model knows normally arrives as text, which means
anything that started as a picture or a sound has to be described by a human
first. That human is the bottleneck: they see a UI bug, translate it into
words, and the agent works from the translation instead of the thing. These
helpers remove that step.

Nothing here is required. Each capability reports whether it's available and
says plainly what's missing if it isn't, so Forge runs fine on a machine with
no vision model, no microphone and no speakers.

  see()        an image -> a description, via a local vision model
  screenshot() the screen -> a PNG on disk
  listen()     audio (file or microphone) -> text, via whisper.cpp
  speak()      text -> spoken aloud
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx


# ---------------------------------------------------------------- config

@dataclass
class MediaConfig:
    """Where the eyes and ears live. Overridable from config.yaml."""
    vision_url: str = "http://127.0.0.1:8090/v1"
    vision_model: str = "qwen2.5-vl"
    whisper_bin: str = str(Path.home() / "whisper.cpp/build/bin/whisper-cli")
    whisper_model: str = str(Path.home() / "whisper.cpp/models/ggml-small.en.bin")
    tts_command: str = "spd-say"          # speech-dispatcher; ships with most desktops
    record_seconds: int = 8


def load_media_config(cfg: dict) -> MediaConfig:
    m = (cfg or {}).get("media", {}) or {}
    clean = {k: v for k, v in m.items() if k in MediaConfig.__dataclass_fields__}
    # config files carry ~ paths; nothing downstream expands them
    for key in ("whisper_bin", "whisper_model"):
        if isinstance(clean.get(key), str):
            clean[key] = str(Path(clean[key]).expanduser())
    return MediaConfig(**clean)


# ---------------------------------------------------------------- vision

def vision_available(mc: MediaConfig) -> tuple[bool, str]:
    try:
        r = httpx.get(f"{mc.vision_url.rstrip('/')}/models", timeout=3.0)
        if r.status_code == 200:
            return True, "vision model is serving"
        return False, f"vision server answered HTTP {r.status_code}"
    except Exception:
        return False, (f"no vision server at {mc.vision_url} — "
                       f"start one with: forge/start-model.sh vision")


def see(image_path: str, question: str, mc: MediaConfig,
        max_tokens: int = 700) -> str:
    """
    Ask a local vision model about an image.

    Images ride as base64 data URLs, which is what llama.cpp's server expects
    when it's started with an mmproj (the vision half of the model).
    """
    p = Path(image_path).expanduser()
    if not p.exists():
        return f"Error: no such image: {p}"
    if p.stat().st_size > 20_000_000:
        return f"Error: image is very large ({p.stat().st_size} bytes)."

    suffix = p.suffix.lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
            "webp": "webp", "gif": "gif"}.get(suffix, "png")
    b64 = base64.b64encode(p.read_bytes()).decode()

    body = {
        "model": mc.vision_model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
            ],
        }],
    }
    try:
        r = httpx.post(f"{mc.vision_url.rstrip('/')}/chat/completions",
                       json=body, timeout=300.0)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except httpx.ConnectError:
        return (f"Error: no vision model is running at {mc.vision_url}. "
                f"Start one with: ~/forge/start-model.sh vision")
    except Exception as e:
        return f"Error asking the vision model: {type(e).__name__}: {e}"


# ------------------------------------------------------------ screenshot

def screenshot(out_path: str | None = None, region: str | None = None) -> str:
    """
    Capture the screen to a PNG and return its path.

    Tries several tools because desktops differ; whichever exists wins.
    `region` may be "x,y,w,h" to grab part of the screen.
    """
    out = Path(out_path).expanduser() if out_path else Path(
        tempfile.gettempdir()) / "forge-screenshot.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    # Pillow first: it's pure Python, needs no system package and no sudo,
    # and it grabs the whole desktop (all monitors) on X11. Only fall back to
    # external tools when it can't — mainly Wayland, which blocks direct
    # framebuffer access.
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab()
        im.save(out)
        if out.exists() and out.stat().st_size > 0:
            if region:
                _crop(out, region)
            return str(out)
    except Exception:
        pass

    wayland = (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland")
    candidates: list[list[str]] = []
    if wayland and shutil.which("grim"):
        candidates.append(["grim", str(out)])
    if shutil.which("import"):              # ImageMagick
        candidates.append(["import", "-window", "root", str(out)])
    if shutil.which("scrot"):
        candidates.append(["scrot", "-o", str(out)])
    if shutil.which("gnome-screenshot"):
        candidates.append(["gnome-screenshot", "-f", str(out)])
    if shutil.which("spectacle"):
        candidates.append(["spectacle", "-b", "-n", "-o", str(out)])
    if shutil.which("xwd") and shutil.which("convert"):
        candidates.append(["sh", "-c", f"xwd -root | convert xwd:- png:{out}"])

    if not candidates:
        return ("Error: no screenshot tool found. Install one:\n"
                "  sudo apt install imagemagick   (gives 'import')\n"
                "  sudo apt install scrot")

    errors = []
    for cmd in candidates:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                if region:
                    _crop(out, region)
                return str(out)
            errors.append(f"{cmd[0]}: {(r.stderr or 'failed').strip()[:80]}")
        except Exception as e:
            errors.append(f"{cmd[0]}: {type(e).__name__}")
    return "Error: every screenshot tool failed —\n  " + "\n  ".join(errors)


def _crop(path: Path, region: str) -> None:
    """Crop in place to 'x,y,w,h'. Silently skipped if Pillow isn't around."""
    try:
        from PIL import Image
        x, y, w, h = (int(v) for v in region.split(","))
        with Image.open(path) as im:
            im.crop((x, y, x + w, y + h)).save(path)
    except Exception:
        pass


# ---------------------------------------------------------------- speech

def speech_available(mc: MediaConfig) -> tuple[bool, str]:
    if not Path(mc.whisper_bin).exists():
        return False, f"whisper not built at {mc.whisper_bin}"
    if not Path(mc.whisper_model).exists():
        return False, f"no speech model at {mc.whisper_model}"
    return True, "speech-to-text ready"


def listen(audio_path: str, mc: MediaConfig) -> str:
    """Transcribe an audio file to text with whisper.cpp."""
    ok, why = speech_available(mc)
    if not ok:
        return f"Error: {why}"
    src = Path(audio_path).expanduser()
    if not src.exists():
        return f"Error: no such audio file: {src}"

    # whisper.cpp wants 16kHz mono WAV; convert anything else if ffmpeg is here
    wav = src
    tmp = None
    if src.suffix.lower() != ".wav":
        if not shutil.which("ffmpeg"):
            return (f"Error: {src.suffix} needs converting to WAV first, and "
                    f"ffmpeg isn't installed (sudo apt install ffmpeg).")
        tmp = Path(tempfile.mkdtemp()) / "audio.wav"
        subprocess.run(["ffmpeg", "-y", "-i", str(src), "-ar", "16000",
                        "-ac", "1", str(tmp)], capture_output=True, timeout=120)
        wav = tmp

    try:
        r = subprocess.run(
            [mc.whisper_bin, "-m", mc.whisper_model, "-f", str(wav),
             "--output-txt", "--no-timestamps", "-of", str(wav)],
            capture_output=True, text=True, timeout=600,
        )
        txt = Path(str(wav) + ".txt")
        if txt.exists():
            return txt.read_text(encoding="utf-8").strip()
        return (r.stdout or r.stderr or "").strip() or "(nothing transcribed)"
    except subprocess.TimeoutExpired:
        return "Error: transcription timed out"
    except Exception as e:
        return f"Error transcribing: {type(e).__name__}: {e}"
    finally:
        if tmp:
            shutil.rmtree(tmp.parent, ignore_errors=True)


def record(seconds: int, out_path: str | None = None) -> str:
    """Record from the default microphone to a 16kHz mono WAV."""
    out = Path(out_path).expanduser() if out_path else Path(
        tempfile.gettempdir()) / "forge-recording.wav"
    if shutil.which("arecord"):
        cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
               "-d", str(seconds), str(out)]
    elif shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", str(seconds),
               "-ar", "16000", "-ac", "1", str(out)]
    else:
        return "Error: no recorder found (need arecord or ffmpeg)"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 30)
        if out.exists() and out.stat().st_size > 0:
            return str(out)
        return f"Error recording: {(r.stderr or '').strip()[:200]}"
    except Exception as e:
        return f"Error recording: {type(e).__name__}: {e}"


def speak(text: str, mc: MediaConfig) -> str:
    """Say something out loud. Best-effort — silence is not an error worth failing on."""
    if not shutil.which(mc.tts_command.split()[0]):
        return f"(no TTS: {mc.tts_command} not installed)"
    try:
        subprocess.run(mc.tts_command.split() + [text[:800]],
                       capture_output=True, timeout=60)
        return "spoken"
    except Exception as e:
        return f"(TTS failed: {type(e).__name__})"


def capabilities(mc: MediaConfig) -> dict:
    """What's actually usable right now — for the dashboard and /media."""
    v_ok, v_why = vision_available(mc)
    s_ok, s_why = speech_available(mc)
    # Don't infer "works" from "installed" — spectacle is present on this
    # kind of desktop and fails when actually invoked, which made the status
    # panel lie. Pillow is the one we can genuinely vouch for by import.
    try:
        from PIL import ImageGrab  # noqa: F401
        shot, shot_why = True, "ready (Pillow)"
    except Exception:
        shot = bool(shutil.which("import") or shutil.which("scrot")
                    or shutil.which("gnome-screenshot") or shutil.which("grim"))
        shot_why = "ready (external tool)" if shot else "no screenshot method available"
    return {
        "vision": {"ok": v_ok, "detail": v_why},
        "speech_in": {"ok": s_ok, "detail": s_why},
        "screenshot": {"ok": shot, "detail": shot_why},
        "speech_out": {"ok": bool(shutil.which(mc.tts_command.split()[0])),
                       "detail": mc.tts_command},
        "microphone": {"ok": bool(shutil.which("arecord") or shutil.which("ffmpeg")),
                       "detail": "arecord/ffmpeg"},
    }
