"""
The dashboard's backend.

Small on purpose: it reads and writes the same config file the CLI reads, so
"switch the model in the browser" needs no messaging between processes. The
CLI notices on its next turn.

It also knows how to ask a local runner (Ollama, llama.cpp, LM Studio) what
models it has, so adding a local model is picking from a list rather than
typing a name exactly right.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import CONFIG_PATH, load_config, save_config

app = FastAPI(title="Forge Dashboard")
WEB_DIR = Path(__file__).parent / "web"


class ModelSpec(BaseModel):
    name: str
    provider: str
    model: str
    base_url: str | None = None
    api_key: str | None = None
    max_tokens: int = 4096


class ActiveSpec(BaseModel):
    name: str


class AgentSpec(BaseModel):
    max_steps: int | None = None
    permission_mode: str | None = None


@app.get("/api/config")
def get_config():
    cfg = load_config()
    # Never ship secrets to the browser — just whether one is set.
    safe = {**cfg, "models": {
        name: {**m, "api_key": "***" if m.get("api_key") else ""}
        for name, m in cfg["models"].items()
    }}
    safe["config_path"] = str(CONFIG_PATH)
    return safe


@app.post("/api/active")
def set_active(spec: ActiveSpec):
    cfg = load_config()
    if spec.name not in cfg["models"]:
        raise HTTPException(404, f"No model named {spec.name!r}")
    cfg["active_model"] = spec.name
    save_config(cfg)
    return {"ok": True, "active_model": spec.name}


@app.post("/api/models")
def upsert_model(spec: ModelSpec):
    cfg = load_config()
    entry = {
        "provider": spec.provider,
        "model": spec.model,
        "max_tokens": spec.max_tokens,
    }
    if spec.base_url:
        entry["base_url"] = spec.base_url
    # An empty/masked key means "leave whatever's there alone" — so saving
    # from the browser can't wipe a key the browser was never shown.
    existing = cfg["models"].get(spec.name, {})
    entry["api_key"] = (existing.get("api_key", "")
                        if spec.api_key in (None, "", "***") else spec.api_key)
    cfg["models"][spec.name] = entry
    save_config(cfg)
    return {"ok": True, "models": list(cfg["models"])}


@app.delete("/api/models/{name}")
def delete_model(name: str):
    cfg = load_config()
    if name not in cfg["models"]:
        raise HTTPException(404, f"No model named {name!r}")
    if cfg["active_model"] == name:
        raise HTTPException(400, "That's the active model — switch to another first.")
    del cfg["models"][name]
    save_config(cfg)
    return {"ok": True}


@app.post("/api/agent")
def set_agent(spec: AgentSpec):
    cfg = load_config()
    if spec.max_steps is not None:
        cfg["agent"]["max_steps"] = spec.max_steps
    if spec.permission_mode is not None:
        if spec.permission_mode not in ("ask", "auto", "deny"):
            raise HTTPException(400, "permission_mode must be ask, auto, or deny")
        cfg["agent"]["permission_mode"] = spec.permission_mode
    save_config(cfg)
    return {"ok": True, "agent": cfg["agent"]}


@app.get("/api/discover")
def discover(base_url: str = "http://localhost:11434"):
    """
    Ask a local runner what models it has.

    Tries Ollama's native endpoint first, then the OpenAI-compatible one that
    llama.cpp/LM Studio/vLLM serve, so one button covers every common setup.
    """
    base = base_url.rstrip("/")
    found: list[str] = []
    errors: list[str] = []

    for url, extract in (
        (f"{base}/api/tags", lambda d: [m["name"] for m in d.get("models", [])]),
        (f"{base}/v1/models", lambda d: [m["id"] for m in d.get("data", [])]),
        (f"{base}/models", lambda d: [m["id"] for m in d.get("data", [])]),
    ):
        try:
            r = httpx.get(url, timeout=4.0)
            if r.status_code == 200:
                names = extract(r.json())
                if names:
                    found = names
                    break
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}")

    return {"models": found, "checked": base,
            "hint": "" if found else "Nothing answered there. Is the model server running?"}


@app.get("/api/health")
def health():
    """Is the active model actually reachable right now?"""
    cfg = load_config()
    name = cfg["active_model"]
    m = cfg["models"].get(name, {})
    if m.get("provider") == "anthropic":
        import os
        has_key = bool(m.get("api_key") or os.environ.get("ANTHROPIC_API_KEY"))
        return {"model": name, "reachable": has_key,
                "detail": "API key present" if has_key else "No API key set"}
    base = (m.get("base_url") or "").rstrip("/")
    if not base:
        return {"model": name, "reachable": False, "detail": "No base_url configured"}
    try:
        r = httpx.get(f"{base}/models", timeout=4.0)
        ok = r.status_code == 200
        return {"model": name, "reachable": ok,
                "detail": "Server responded" if ok else f"HTTP {r.status_code}"}
    except Exception as e:
        return {"model": name, "reachable": False, "detail": f"{type(e).__name__}"}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def serve() -> None:
    import uvicorn

    cfg = load_config()
    host = cfg["server"].get("host", "127.0.0.1")
    port = int(cfg["server"].get("port", 8770))
    print(f"\n  Forge dashboard → http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve()
