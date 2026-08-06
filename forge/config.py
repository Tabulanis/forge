"""
Config: one YAML file the CLI reads and the dashboard writes.

That shared file is the whole trick behind "change the model in the browser,
the terminal picks it up." No sockets, no message bus — the CLI re-reads
config at the start of every turn, so a change saved in the dashboard takes
effect on your next message.

Lives at ~/.forge/config.yaml so it's per-user, not per-project.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(os.environ.get("FORGE_HOME", Path.home() / ".forge"))
CONFIG_PATH = CONFIG_DIR / "config.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    # Which entry in `models` the agent talks to right now. The dashboard
    # changes this; the CLI honors it on the next turn.
    "active_model": "claude",

    # Named model configs. Add as many as you like — local ones get a
    # base_url pointing at whatever's serving them.
    "models": {
        "claude": {
            "provider": "anthropic",
            "model": "claude-sonnet-4-5",
            "max_tokens": 8000,
            # Leave api_key empty to use the ANTHROPIC_API_KEY environment
            # variable instead of storing a secret in a plain text file.
            "api_key": "",
        },
        "local": {
            "provider": "openai-compat",
            "model": "qwen2.5-coder:14b",
            "base_url": "http://localhost:11434/v1",
            "max_tokens": 4096,
            "api_key": "",
        },
    },

    "agent": {
        # Hard stop on tool-call rounds per message, so a confused model
        # can't spin forever burning tokens.
        "max_steps": 40,
        # "ask"  — prompt before every world-changing tool (default, safest)
        # "auto" — run everything without asking (fast, for throwaway dirs)
        # "deny" — read-only; refuse all writes and commands
        "permission_mode": "ask",
    },

    "server": {"host": "127.0.0.1", "port": 8770},
}


def _deep_merge(base: dict, over: dict) -> dict:
    """Fill in anything the user's config file leaves out."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(base[k], v) if (k in base and isinstance(base[k], dict)
                                             and isinstance(v, dict)) else v
    return out


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"Config file is not valid YAML: {CONFIG_PATH}\n{e}")
    return _deep_merge(DEFAULT_CONFIG, loaded)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def active_model_config(cfg: dict) -> dict:
    """The config block for whichever model is currently selected."""
    name = cfg.get("active_model", "claude")
    models = cfg.get("models", {})
    if name not in models:
        raise SystemExit(
            f"active_model is {name!r} but there's no such entry under models. "
            f"Available: {', '.join(models) or '(none)'}"
        )
    return models[name]
