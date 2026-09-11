"""Casting a video to a TV — her hands on a screen in another room.

A Chromecast (or a TV with one built in) does not read files off a disk. It is
handed a URL and fetches the video itself, so casting is always three parts:
make the file playable, serve it, then tell the TV where to look. All three
have a sharp edge, and all three were learned the hard way on 2026-09-10.

**Find the TV by address, not by name.** Discovery is an mDNS broadcast, and a
VPN swallows it — `catt`-style name lookup reports "device not found" while the
TV sits there answering HTTP perfectly well. Connecting straight to its address
works whether the VPN is up or down, so that is what this does. The address
comes from config (media.cast_device), never from code.

**The receiver is fussier than any real player.** Its built-in decoder handles
H.264 video in an MP4 container with AAC audio, and little else. Hand it the
Dolby Digital Plus track that almost every TV rip carries, or an MKV, and it
either sits at IDLE forever or floods the log with mux errors and never shows a
frame. So `make_castable` probes first and only rebuilds what actually needs
rebuilding — the video is copied untouched when it is already H.264, which
makes the conversion a fast copy rather than a re-encode.

**The file server must speak HTTP Range, or seeking is broken.** Python's
stock `http.server` does not implement range requests. The TV asks for "the
bytes from 49 minutes in", the server can only answer with the whole file from
byte zero, and playback silently restarts from the beginning — twice in a row,
in front of an audience, which is how this was discovered. The little server
below answers 206 with a real byte range, so seek lands where it was asked to.

**Bitrate is a real constraint, not a detail.** The TV pulls over wifi. A
1080p rip at ~3800 kbps will stutter on a weak link no matter which machine
serves it, and the fix is to make the stream smaller, not to go hunting for a
different server. `fit_kbps` caps it.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_PORT = 8921
CAST_PORT = 8009          # the Chromecast control port; not configurable on the device

# The running share, if any: casting is one-at-a-time by nature — a TV shows one
# thing — so a single slot is the honest model.
_share: dict | None = None
_share_lock = threading.Lock()


# ---------------------------------------------------------------- config

def _cfg() -> tuple[str, str, int]:
    """(device address, host address to serve from, port). From config, with
    sane fallbacks so a missing key degrades instead of exploding."""
    device, host, port = "", "", DEFAULT_PORT
    try:
        from .config import load_config
        from .media import load_media_config
        mc = load_media_config(load_config())
        device = getattr(mc, "cast_device", "") or ""
        host = getattr(mc, "cast_host", "") or ""
        port = int(getattr(mc, "cast_port", 0) or DEFAULT_PORT)
    except Exception:
        pass
    return device, (host or _my_lan_ip()), port


def _my_lan_ip() -> str:
    """This machine's address on the network the TV is on. Asking the routing
    table beats guessing: a box with a VPN, a direct cable and wifi has several
    addresses and only one of them is reachable from the living room."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))      # no packet is sent; this just picks the route
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- the file

def probe(path: str | Path) -> dict:
    """What's actually inside the file: container, video codec, audio codec,
    bitrate, duration. Empty dict if ffprobe can't read it."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=60).stdout
        d = json.loads(out or "{}")
    except Exception:
        return {}
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = d.get("format", {})
    return {
        "container": (fmt.get("format_name") or ""),
        "video": v.get("codec_name", ""),
        "audio": a.get("codec_name", ""),
        "channels": a.get("channels", 0),
        "duration": float(fmt.get("duration") or 0),
        "kbps": int(float(fmt.get("bit_rate") or 0) / 1000),
    }


def castable(info: dict) -> tuple[bool, str]:
    """Can the TV's own decoder play this as-is? Returns (yes, why not)."""
    if not info:
        return False, "can't read the file"
    if info["video"] != "h264":
        return False, f"video is {info['video'] or 'unknown'}, the TV needs H.264"
    if info["audio"] not in ("aac", "mp3"):
        return False, f"audio is {info['audio'] or 'unknown'}, the TV needs AAC"
    if "mp4" not in info["container"] and "mov" not in info["container"]:
        return False, f"container is {info['container'] or 'unknown'}, the TV needs MP4"
    return True, ""


def make_castable(src: str | Path, out: str | Path | None = None,
                  fit_kbps: int | None = None) -> tuple[Path, str]:
    """Produce a file the TV can actually play. Returns (path, what_changed).

    The video is stream-copied whenever it is already H.264, so this is usually
    a fast repackage rather than a re-encode. It only re-encodes the picture if
    asked to fit a smaller pipe, or if the codec is wrong to begin with.
    """
    src = Path(src)
    info = probe(src)
    ok, why = castable(info)
    shrinking = bool(fit_kbps and info.get("kbps", 0) > fit_kbps)
    if ok and not shrinking:
        return src, "already castable"

    out = Path(out) if out else src.with_name(f"cast-{src.stem}.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
    if info.get("video") == "h264" and not shrinking:
        cmd += ["-c:v", "copy"]
        changed = "audio rebuilt to AAC stereo, video untouched"
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast"]
        if fit_kbps:
            cmd += ["-b:v", f"{max(fit_kbps - 192, 300)}k", "-maxrate", f"{fit_kbps}k",
                    "-bufsize", f"{fit_kbps * 2}k"]
        changed = f"video re-encoded{f' to fit {fit_kbps} kbps' if fit_kbps else ''}, audio to AAC stereo"
    cmd += ["-c:a", "aac", "-ac", "2", "-b:a", "192k", "-movflags", "+faststart", str(out)]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not out.exists():
        tail = (r.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("conversion failed: " + " / ".join(tail))
    return out, changed


# ---------------------------------------------------------------- serving

class _RangeHandler(BaseHTTPRequestHandler):
    """Serves exactly one file, and answers byte-range requests properly.

    Range support is the whole point of writing this by hand — without it the
    TV's seek silently restarts playback from zero.
    """
    path_on_disk: Path = Path()
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):      # keep her logs clean
        pass

    def _headers(self, size: int, start: int, end: int, partial: bool):
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

    def do_HEAD(self):
        size = self.path_on_disk.stat().st_size
        self._headers(size, 0, size - 1, False)

    def do_GET(self):
        f = self.path_on_disk
        if not f.exists():
            self.send_error(404); return
        size = f.stat().st_size
        start, end, partial = 0, size - 1, False

        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            spec = rng.split("=", 1)[1].split(",")[0].strip()
            lo, _, hi = spec.partition("-")
            try:
                if lo:
                    start, partial = int(lo), True
                    if hi:
                        end = min(int(hi), size - 1)
                elif hi:                       # "bytes=-500" = the last 500 bytes
                    start, partial = max(size - int(hi), 0), True
            except ValueError:
                start, end, partial = 0, size - 1, False
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers(); return

        self._headers(size, start, end, partial)
        remaining = end - start + 1
        with open(f, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return            # the TV moved on; not an error
                remaining -= len(chunk)


def share(path: str | Path) -> str:
    """Serve one file on the LAN and return the URL the TV should fetch."""
    global _share
    path = Path(path).resolve()
    device, host, port = _cfg()
    with _share_lock:
        unshare()
        handler = type("H", (_RangeHandler,), {"path_on_disk": path})
        httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        url = f"http://{host}:{port}/{urllib.parse.quote(path.name)}"
        _share = {"httpd": httpd, "thread": t, "path": path, "url": url}
    return url


def unshare() -> None:
    """Stop serving. Safe to call when nothing is running."""
    global _share
    if _share:
        try:
            _share["httpd"].shutdown()
            _share["httpd"].server_close()
        except Exception:
            pass
        _share = None


# ---------------------------------------------------------------- the TV

def connect(address: str = "", timeout: int = 15):
    """Connect straight to the TV by address. Raises with a readable reason."""
    import pychromecast
    address = address or _cfg()[0]
    if not address:
        raise RuntimeError("no TV address set — put one in config media.cast_device")
    cast = pychromecast.get_chromecast_from_host((address, CAST_PORT, None, None, None))
    cast.wait(timeout=timeout)
    return cast


def play(path: str | Path, fit_kbps: int | None = None, address: str = "",
         root: str = "") -> str:
    """Convert if needed, serve it, and start it on the TV. Returns a summary.

    `root` is her workspace: casting puts a file on a screen in someone else's
    room, so it may only reach files she is allowed to touch.
    """
    src = Path(path)
    if root:
        base = Path(root).resolve()
        src = src.resolve() if src.is_absolute() else (base / src).resolve()
        if base != src and base not in src.parents:
            return f"Error: that file is outside the workspace: {src}"
    if not src.exists():
        return f"Error: no such file: {src}"
    ready, changed = make_castable(src, fit_kbps=fit_kbps)
    url = share(ready)
    try:
        cast = connect(address)
        mc = cast.media_controller
        mc.play_media(url, "video/mp4")
        mc.block_until_active(timeout=30)
        time.sleep(2)
        mc.update_status()
        state = mc.status.player_state
        cast.disconnect()
    except Exception as e:
        unshare()
        return f"Error: couldn't start it on the TV: {e}"
    mins = int(probe(ready).get("duration", 0) // 60)
    return (f"Playing {ready.name} on the TV ({state.lower()}), {mins} min. "
            f"{changed}. Serving from {url}")


def _control(action: str, seconds: float | None = None, address: str = "") -> str:
    try:
        cast = connect(address)
        mc = cast.media_controller
        mc.update_status(); time.sleep(1)
        before = mc.status.player_state
        if action == "pause" and before in ("PLAYING", "BUFFERING"):
            mc.pause()
        elif action == "resume" and before == "PAUSED":
            mc.play()
        elif action == "stop":
            mc.stop(); cast.quit_app()
        elif action == "seek" and seconds is not None:
            mc.seek(max(0.0, float(seconds)))
        time.sleep(2); mc.update_status()
        after, pos = mc.status.player_state, int(mc.status.current_time or 0)
        cast.disconnect()
    except Exception as e:
        return f"Error: {e}"
    if action == "stop":
        unshare()
        return "Stopped, and the TV is back to its own screen."
    return f"{action}: {before.lower()} → {after.lower()}, at {pos // 60}m{pos % 60:02d}s"


def pause(address: str = "") -> str:
    return _control("pause", address=address)


def resume(address: str = "") -> str:
    return _control("resume", address=address)


def stop(address: str = "") -> str:
    return _control("stop", address=address)


def seek(seconds: float, address: str = "") -> str:
    return _control("seek", seconds=seconds, address=address)


def status(address: str = "") -> str:
    try:
        cast = connect(address)
        mc = cast.media_controller
        mc.update_status(); time.sleep(1)
        s = mc.status
        name = (s.content_id or "").rsplit("/", 1)[-1] or "nothing"
        pos, dur = int(s.current_time or 0), int(s.duration or 0)
        state = s.player_state or "unknown"
        cast.disconnect()
    except Exception as e:
        return f"Error: {e}"
    if dur:
        return f"{state.lower()}: {urllib.parse.unquote(name)} at {pos // 60}m{pos % 60:02d}s of {dur // 60}m"
    return f"{state.lower()}: {urllib.parse.unquote(name)}"


def available(address: str = "") -> tuple[bool, str]:
    """Is there a TV listening at the configured address?"""
    address = address or _cfg()[0]
    if not address:
        return False, "no TV address set (config media.cast_device)"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((address, CAST_PORT))
        return True, ""
    except Exception:
        return False, f"no TV answering at {address}"
    finally:
        s.close()
