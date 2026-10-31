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

    # Kid mode: chat only. Locks every settings route in the dashboard,
    # confines run_command to the workspace, and pins new chats to
    # ~/Playground. Flip it here in the file, at the computer — on purpose,
    # nothing reachable from a tablet can change it.
    "kid_mode": False,

    # 2026-10-27: park her chat's brain state on disk after each turn and read
    # it back in seconds when the slot lost it (forge/park.py). Needs the brain
    # started with --slot-save-path. `park: false` (or `slots: false`) = off.
    "park": True,

    # 2026-10-27: rolling memory (forge/fold.py). Between turns, when the
    # verbatim window passes fold_window_tokens, the oldest complete turns are
    # folded into ONE running-summary message right after the system prompt,
    # which only ever grows by appended lines, so the prompt cache holds. While
    # on, the old 70% from-scratch compaction does not run (the emergency path
    # after a server refusal stays). `fold: false` = the old behaviour, untouched.
    # The numbers are GUESSES (DESIGN.md) until measured on real chats.
    # 2026-10-28: the prompt front (system text, reasoning-effort line, tool list)
    # is the same in every mode, so a mode switch keeps the prompt cache. A mode's
    # lighter toolset becomes a one-line hint in the user message. false = old.
    "fixed_belt": True,
    # 2026-10-28 (kitchen A): the fold runs at IDLE now (after a turn, before
    # park_save), and the brain's cache is re-warmed in the background right
    # after — the re-read a fold causes is paid while he is away, not at the
    # top of his next message ("say ok" had taken 176 s that way).
    "fold": True,
    # 2026-10-30 (prime): at idle, after the turn and any fold, her chat slot
    # is left holding the chat rendered EXACTLY as the next turn will render
    # it, with nothing generated after it (agent.prime_now), then parked. The
    # brain is hybrid: it reuses a cached prefix only when the new prompt
    # extends it exactly, and a slot restored from disk has no checkpoints to
    # roll back to — so the raw reply left in the slot cost a ~47k re-read.
    # This replaces the idle warm. false = the old warm (after a fold only).
    "prime_at_idle": True,
    "fold_window_tokens": 20000,   # GUESSED: window size that triggers a fold
    "fold_keep_tokens": 10000,     # GUESSED: window left after a fold (folds stay rare)
    "summary_cap_tokens": 12000,   # 2026-10-28: was 8000; shrinks now deep and rare
    "fold_pin_turns": 6,           # GUESSED: newest real turns never folded
    # 2026-10-28 (kitchen A): fold rent — a fold that would free fewer tokens
    # than this is skipped silently (it would cost a summary+window re-read
    # for nearly nothing; today one freed 1,991 and cost a 30,514-token read).
    "fold_min_free_tokens": 6000,
    # 2026-10-28 (kitchen B): the VIP lane — a chat-shaped question typed
    # while she is mid-task is answered at once on the helpers slot (a cook
    # who talks while chopping); a work message still queues, with a heads-up
    # in the nudge inbox so she sees it between steps. false = the old
    # behaviour: everything typed while busy just waits for Friday/the queue.
    "vip_lane": True,
    # 2026-10-31 (faster side lane): the side prompt carries at most this many
    # chars of the running summary (its TAIL), 3 recent exchanges of 800 chars,
    # and a 120-token answer. Measured before: a 7,396-token side prompt took
    # 57 s while the main turn generated; target is ~2,000 tokens, and the
    # part before his question is frozen per main turn so the brain's "side"
    # slot re-reads only the question the second time.
    "side_summary_chars": 4000,
    # 2026-10-31 (done-check): a project says what "done" means with an
    # executable FORGE-DONE.sh at its git root (its own test run). These repo
    # roots are watched: at the end of a WORK turn, any one of them whose HEAD
    # or working tree changed during the turn has its script run, and a
    # failure is written under her reply by the harness — her words cannot
    # paper over it (twice she committed code that broke the project's tests
    # after checking only the file she touched). Watched by repo, not by chat
    # workspace: she edits Swarm from a chat whose workspace is DeepNPC.
    "done_repos": ["~/aidojo/current/Swarm", "~/aidojo/current/DeepNPC"],
    "done_check_timeout": 180,     # seconds one FORGE-DONE.sh may take
    # 2026-10-31: the biggest command/content one tool call may carry. One
    # 117,743-char call was cut off by the token cap and the turn was lost;
    # over this the tool refuses and says to write in parts. 0 = off.
    "tool_arg_max_chars": 8000,

    # Named model configs. Add as many as you like — local ones get a
    # base_url pointing at whatever's serving them.
    "models": {
        "claude": {
            "provider": "anthropic",
            "model": "claude-opus-5",
            "max_tokens": 8000,
            # Leave api_key empty to use the ANTHROPIC_API_KEY environment
            # variable instead of storing a secret in a plain text file.
            "api_key": "",
        },
        "fable": {
            "provider": "anthropic",
            "model": "claude-fable-5",
            "max_tokens": 8000,
            "api_key": "",
        },
        "local": {
            "provider": "openai-compat",
            "model": "qwen2.5-coder:14b",
            "base_url": "http://localhost:11434/v1",
            "max_tokens": 4096,
            "api_key": "",
        },
        # A small CPU-only model for short-prompt side jobs (quick
        # classifications; memory summaries when the main model is a paid
        # API — set agent.summarizer_model to its name to enable that).
        # Measured honestly: CPU prompt reading is ~24 tok/s, so anything
        # long-prompt (full agent work, big summaries) belongs on the GPU
        # or API model instead.
        "little": {
            "provider": "openai-compat",
            "model": "qwen2.5-3b",
            "base_url": "http://127.0.0.1:8083/v1",
            "max_tokens": 2048,
            "api_key": "",
        },
    },

    "agent": {
        # Hard stop on tool-call rounds per message, so a confused model
        # can't spin forever burning tokens.
        "max_steps": 80,
        # "ask"  — prompt before every world-changing tool (default, safest)
        # "auto" — run everything without asking (fast, for throwaway dirs)
        # "deny" — read-only; refuse all writes and commands
        "permission_mode": "ask",
        # The sealed reviewer: judges final answers against the evidence
        # before "done". True/False; superego_model names which model
        # judges (empty = the active model, with the sealed prompt).
        "superego": True,
        "superego_model": "",
    },

    # host 127.0.0.1 keeps this machine-only. Set 0.0.0.0 to reach it from a
    # phone or tablet on the same network — but then set a token too, because
    # this thing runs shell commands and anyone on the LAN could reach it.
    "server": {"host": "127.0.0.1", "port": 8770, "token": ""},

    # Who she belongs to, and a challenge phrase for when a session smells off.
    # Off by default — arm it when you're ready. The phrase is stored ONLY as a
    # salted hash (see identity.py), never in plaintext here, never in her prompt.
    "identity": {"enabled": False, "owner_name": "", "phrase_hash": "", "phrase_salt": ""},

    # Eyes and ears. Every one of these is optional — Forge runs fine
    # without any of them, and tools only appear when they actually work.
    "media": {
        # She has one set of eyes: the 122B's own, on 8087. There is no separate
        # vision model and no fallback — the 27B, the 30B and the 7B vision model
        # were all retired 2026-09-05. Keep in step with MediaConfig in media.py.
        "vision_url": "http://127.0.0.1:8087/v1",   # her brain's own eyes (big122)
        "vision_model": "qwen3.5-122b",
        # image engine: ComfyUI on the render box (the TITAN, over the wire);
        # "sketch" = Z-Image-Turbo, "reference" = FLUX.2-klein 4B, "masterpiece" = Qwen-Image
        "imagegen_url": "http://10.42.0.1:8189",
        "imagegen_preset": "sketch",
        # video engine: ComfyUI + WAN 2.2 on the render box (the TITAN), over the wire
        "videogen_url": "http://10.42.0.1:8189",
        "whisper_bin": "~/whisper.cpp/build/bin/whisper-cli",
        "whisper_model": "~/whisper.cpp/models/ggml-small.en.bin",
        "tts_command": "spd-say",
        "piper_voice": "~/forge/models/voices/en_US-amy-medium.onnx",
        # A TV she can cast to (forge.cast). Blank by default because it's
        # particular to one house — put the TV's ADDRESS in your own
        # config.yaml. Not its name: name lookup is an mDNS broadcast and a
        # VPN eats it. cast_host blank = work out this machine's address from
        # the routing table.
        "cast_device": "",
        "cast_host": "",
        "cast_port": 8921,
    },
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
        if not isinstance(loaded, dict):
            raise yaml.YAMLError(f"expected a mapping, got {type(loaded).__name__}")
    except yaml.YAMLError as e:
        # A hand-edit gone wrong shouldn't brick every Forge tool at once.
        # Keep the broken file for repair, start fresh, and say so loudly.
        import sys
        broken = CONFIG_PATH.with_name("config.yaml.broken")
        try:
            CONFIG_PATH.replace(broken)
        except OSError:
            broken = None
        save_config(DEFAULT_CONFIG)
        print(f"WARNING: {CONFIG_PATH} was not valid YAML ({e}).\n"
              + (f"Your old file is saved at {broken} — " if broken else "")
              + "Forge regenerated a default config so it can keep working. "
                "Your model list will need re-adding (dashboard → Add a model).",
              file=sys.stderr)
        return dict(DEFAULT_CONFIG)
    merged = _deep_merge(DEFAULT_CONFIG, loaded)
    # `models` is the user's list, not a set of defaults to top up. Merging it
    # meant a deleted model came straight back on the next load — and worse,
    # a stale default could be suggested as a fix when it points at something
    # that was never running here.
    if isinstance(loaded.get("models"), dict):
        merged["models"] = loaded["models"]
    return merged


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

# --- pipelines ------------------------------------------------------

PIPELINES_PATH = CONFIG_DIR / "pipelines.yaml"


def load_pipelines() -> dict:
    """
    Orchestration recipes, from the user's copy in ~/.forge.

    On first run the shipped examples are copied there, so editing them is
    safe — a package upgrade can't overwrite the user's own recipes.
    """
    if not PIPELINES_PATH.exists():
        shipped = Path(__file__).parent / "pipelines.yaml"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if shipped.exists():
            PIPELINES_PATH.write_text(shipped.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            PIPELINES_PATH.write_text("pipelines: {}\n", encoding="utf-8")
    try:
        data = yaml.safe_load(PIPELINES_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SystemExit(f"pipelines.yaml is not valid YAML: {PIPELINES_PATH}\n{e}")
    return data.get("pipelines", {}) or {}


def save_pipelines(pipelines: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINES_PATH.write_text(
        yaml.safe_dump({"pipelines": pipelines}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
