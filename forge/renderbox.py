"""
The render box, as a plugin: everything about the second machine in one place.

Two jobs:
  * PROGRESS — while ComfyUI grinds on an image or a clip, listen on its
    websocket and keep a live "sampling 12/20 · 61%" line that the agent's
    heartbeat can show the user. Silence was the stressful part.
  * STATS — GPU / VRAM / CPU / RAM / temperature of the render box, from the
    tiny reporter that runs there (renderbox-stats.service, port 8191), plus
    ComfyUI's own queue. The dash's Power card shows it next to Void's.

Where the box is comes from config (media.imagegen_url / videogen_url /
renderbox_stats_url), never from code. Every function here fails soft: a
render must never die because the progress feed or the stats hiccupped.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid

NAME = "renderbox"
DESCRIPTION = "Image + video rendering on the second box (ComfyUI over the wire)"

# What the user should read while each kind of node runs. Anything not listed
# shows as its node class, lower-cased.
_STAGES = {
    "UNETLoader": "loading model", "CLIPLoader": "loading text model", "VAELoader": "loading decoder",
    "LoraLoaderModelOnly": "loading speed LoRA", "CLIPTextEncode": "reading prompt",
    "KSampler": "sampling", "KSamplerAdvanced": "sampling", "VAEDecode": "decoding frames",
    "VAEDecodeTiled": "decoding frames", "CreateVideo": "assembling video", "SaveVideo": "saving",
    "SaveImage": "saving", "RIFE VFI": "in-betweening frames", "LoadImage": "loading reference",
    "Wan22ImageToVideoLatent": "preparing", "EmptySD3LatentImage": "preparing",
}

_lock = threading.Lock()
_jobs: dict[str, dict] = {}      # prompt_id -> live state


def new_client_id() -> str:
    return "forge-" + uuid.uuid4().hex[:12]


def start_watch(base: str, client_id: str, prompt_id: str, workflow: dict, label: str) -> None:
    """Listen to ComfyUI's websocket for this job, on a daemon thread. Never raises."""
    classes = {nid: n.get("class_type", "?") for nid, n in workflow.items()}
    with _lock:
        _jobs[prompt_id] = {"label": label, "t0": time.time(), "stage": "queued", "value": 0, "max": 0,
                            "pct": 0, "done": False, "steps_total": _count_steps(workflow)}

    def run():
        try:
            import websocket   # websocket-client (MIT); missing = no live bar, render still works
        except Exception:
            return
        ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
        try:
            ws = websocket.create_connection(ws_url, timeout=20)
        except Exception:
            return
        try:
            ws.settimeout(30)
            while True:
                try:
                    raw = ws.recv()
                except Exception as e:
                    # a quiet half-minute is normal (model loading sends nothing); keep
                    # listening while the job is still ours and unfinished
                    if type(e).__name__ in ("WebSocketTimeoutException", "timeout", "TimeoutError"):
                        with _lock:
                            j = _jobs.get(prompt_id)
                        if j and not j["done"] and time.time() - j["t0"] < 3600:
                            continue
                    break
                if not isinstance(raw, str):
                    continue
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                t, d = m.get("type"), m.get("data", {}) or {}
                if d.get("prompt_id") not in (None, prompt_id):
                    continue
                with _lock:
                    j = _jobs.get(prompt_id)
                    if not j:
                        break
                    if t == "executing":
                        node = d.get("node")
                        if node is None:
                            j["done"] = True; j["stage"] = "finished"; j["pct"] = 100
                            break
                        cls = classes.get(str(node), "?")
                        j["stage"] = _STAGES.get(cls, cls.lower()); j["value"] = 0; j["max"] = 0
                        j["stage_t0"] = time.time()
                    elif t == "progress":
                        j["value"] = int(d.get("value", 0)); j["max"] = int(d.get("max", 0)) or j["max"]
                        if j["max"]:
                            j["pct"] = int(100 * j["value"] / j["max"])
                    elif t in ("execution_success", "execution_error", "execution_interrupted"):
                        j["done"] = True; j["stage"] = "finished" if t == "execution_success" else t.split("_")[1]
                        j["pct"] = 100 if t == "execution_success" else j["pct"]
                        break
        finally:
            try:
                ws.close()
            except Exception:
                pass

    threading.Thread(target=run, name=f"renderbox-ws-{prompt_id[:8]}", daemon=True).start()


def finish(prompt_id: str) -> None:
    with _lock:
        _jobs.pop(prompt_id, None)


def _count_steps(workflow: dict) -> int:
    for n in workflow.values():
        if n.get("class_type", "").startswith("KSampler"):
            try:
                return int(n["inputs"].get("steps", 0))
            except Exception:
                return 0
    return 0


def _overall_pct(j: dict) -> int:
    """One bar for the whole job, so it never parks at 100% while frames decode.
    loading 0-5 · sampling 5-70 (real steps) · decoding 70-95 (by time) · saving 95-100."""
    stage, since = j["stage"], time.time() - j.get("stage_t0", j["t0"])
    if stage == "sampling":
        if j["max"]:
            return 5 + int(65 * j["value"] / j["max"])
        return min(5, int(since / 6))            # weights going onto the card
    if stage == "decoding frames":
        return 70 + min(25, int(since))         # ~1%/s; a 3 s clip decodes in ~28 s
    if stage in ("assembling video", "saving", "finished"):
        return 96 if stage != "finished" else 100
    if stage in ("queued", "reading prompt", "preparing", "loading reference") or stage.startswith("loading"):
        return min(5, int(since / 6))
    return j["pct"]


def current() -> dict | None:
    """The live job, if any: {label, stage, value, max, pct, elapsed, bar}. None when idle."""
    with _lock:
        live = [j for j in _jobs.values() if not j["done"]]
        if not live:
            return None
        j = dict(live[-1])
    j["elapsed"] = int(time.time() - j["t0"])
    j["pct"] = _overall_pct(j)
    j["bar"] = _bar(j["pct"])
    return j


def _bar(pct: int, width: int = 20) -> str:
    n = max(0, min(width, round(width * pct / 100)))
    return "▓" * n + "░" * (width - n)


def progress_line() -> str:
    """One human line for the heartbeat, '' when nothing is rendering."""
    j = current()
    if not j:
        return ""
    where = f"{j['stage']}"
    if j["max"]:
        where += f" {j['value']}/{j['max']}"
    elif j["stage"] == "sampling":
        where = "loading model into the card"     # sampling with no steps yet = weights staging
    return f"{j['label']} · {where} {j['bar']} {j['pct']}% · {j['elapsed']}s"


# ---- stats ----------------------------------------------------------------

def _get_json(url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def stats(stats_url: str = "http://10.42.0.1:8191/", comfy_url: str = "http://10.42.0.1:8189") -> dict:
    """Machine readout for the dash. Every field optional; 'up' says whether the box answered."""
    out: dict = {"up": False}
    s = _get_json(stats_url)
    if s:
        g = s.get("gpu") or {}
        out.update({"up": True, "cpu_pct": s.get("cpu_pct"), "ram_used_gb": round(s.get("ram_used", 0) / 2**30, 1),
                    "ram_total_gb": round(s.get("ram_total", 0) / 2**30), "load1": s.get("load1"),
                    "gpu_name": (g.get("name") or "").replace("NVIDIA ", ""), "gpu_pct": g.get("util_pct"),
                    "vram_used_gb": round(g.get("vram_used", 0) / 2**30, 1),
                    "vram_total_gb": round(g.get("vram_total", 0) / 2**30), "gpu_temp_c": g.get("temp_c"),
                    "gpu_power_w": g.get("power_w")})
    q = _get_json(comfy_url + "/queue")
    if q is not None:
        out["comfy_up"] = True
        out["queue_running"] = len(q.get("queue_running", []))
        out["queue_pending"] = len(q.get("queue_pending", []))
    else:
        out["comfy_up"] = False
    j = current()
    if j:
        out["job"] = {"label": j["label"], "stage": j["stage"], "pct": j["pct"], "elapsed": j["elapsed"]}
    return out


def stats_line(s: dict | None = None) -> str:
    """'render box: GPU 23% · VRAM 8.3/24 GB · RAM 16/63 GB · CPU 1% · 46°C' or a plain 'not answering'."""
    s = s or stats()
    if not s.get("up"):
        return "render box: not answering"
    parts = [f"GPU {s.get('gpu_pct', 0):.0f}%", f"VRAM {s['vram_used_gb']}/{s['vram_total_gb']} GB",
             f"RAM {s['ram_used_gb']}/{s['ram_total_gb']} GB", f"CPU {s.get('cpu_pct', 0):.0f}%"]
    if s.get("gpu_temp_c") is not None:
        parts.append(f"{s['gpu_temp_c']:.0f}°C")
    if s.get("job"):
        parts.append(f"rendering: {s['job']['label']} {s['job']['pct']}%")
    elif s.get("comfy_up") is False:
        parts.append("ComfyUI down")
    return "render box: " + " · ".join(parts)
