# Forge — agent handoff notes

Read this first if you're an agent picking up work in `~/forge`.
Claim the folder in `~/aidojo/AGENT-LOG.md` before editing.

## What Forge is

A terminal coding agent (like Claude Code) plus a web control panel, pointed
at whatever model you want. Layout is in README.md. Config lives at
`~/.forge/config.yaml` — shared by the CLI and the dashboard, re-read every
turn, so model switches take effect without restarts.

## Current state (2026-08-08, session eafbce98, Claude Fable 5)

**Working right now:**
- Local models via OpenAI-compatible endpoints: qwen30b on :8080 (active
  model), tinyllama on :8081. `start-model.sh` launches them.
- Web dashboard on :8770 (token-protected, LAN-visible).
- Doctor (`forge doctor` / `/doctor`) passes everything except vision
  (:8090 not running — start with `start-model.sh vision`).
- Whisper speech-in, spd-say speech-out, screenshots all report ready.

**Done this session — plug-and-play with current Claude models:**

1. `forge/providers.py` — AnthropicProvider now: replays its own raw
   assistant blocks (incl. thinking blocks) across tool loops via the new
   `Reply.assistant_blocks`; handles `stop_reason == "refusal"` with a
   readable message; opts into server-side fallbacks (`fallbacks="default"`
   + beta header) for Fable 5 / Opus 5 / Mythos 5; default model is now
   claude-opus-5.
2. `forge/agent.py` — `tool_use` history entries carry `assistant_blocks`.
3. `forge/config.py` + live `~/.forge/config.yaml` — `claude` entry points
   at claude-opus-5; new `fable` entry (claude-fable-5).
4. `forge/web/index.html` — picking "Claude" in Add-a-model now offers the
   current model IDs in a dropdown and prefills claude-opus-5.

**Verified:** end-to-end agent run with tool calls against local qwen30b
(:8080) passes; stub tests confirm the fallback request shape, verbatim
thinking-block replay, refusal handling, and that non-fallback models skip
the beta path.

**Still blocked on the user:** no ANTHROPIC_API_KEY on this machine — the
Claude path is wired and stub-tested but has not hit the real API. First
real test: `export ANTHROPIC_API_KEY=...` then `forge` → `/model claude`.
The user also plans to add Gemini + other providers later; the router
(`wm-llama` manager.py in `~/wickerman`) has a gemini translation layer that
could be cribbed, or add a GeminiProvider in providers.py.

**Also done this session — "Fable-izing" the harness** (discipline enforced
in code so any model benefits, esp. local ones):

- `tools.py`: Workspace tracks files read this session (`ws.reads`);
  edit_file refuses files not yet read, with a corrective error the model
  can follow. New `save_note` tool appends one-line lessons to
  `FORGE-NOTES.md` in the workspace (append-only, no permission prompt).
- `agent.py`: (a) system prompt now re-read each turn and carries the
  project notebook (tail-truncated at 4000 chars); (b) broken-record
  detector — an identical repeat of the immediately-preceding *failed* tool
  call is refused with advice instead of executed; (c) liar catcher — a
  final answer claiming past-tense actions when zero tools ran this message
  gets bounced back once ("do it now or say you meant earlier work");
  (d) notebook guidance added to SYSTEM_PROMPT.
- `cli.py` / `session.py`: pass `notes_path=<workspace>/FORGE-NOTES.md`.

Verified: 4 deterministic scripted-provider tests (block-then-recover edit,
repeat refusal, liar bounce, notebook injection) + live qwen30b run that
read → edited → ran-to-verify and honored a planted notebook preference.

**Dummy-proofing round (same session, later):**

- `tools.py`: write_file refuses to overwrite an existing file the model
  hasn't read; every write/edit backs up the prior version to
  `.forge_backups/<relpath>` (one level, swap-on-undo so undo twice =
  redo); new `undo_file` tool; `_machine_killer()` blocklist refuses
  rm -rf / (also ~, $HOME, /*), mkfs, dd-to-device, >/dev/sd*, fork bombs
  even in auto mode — deliberately short list, not a nanny filter.
- `agent.py`: verify-before-done — if write/edit succeeded this message and
  no later run_command/read_file happened, the final answer gets bounced
  once asking for verification. Tested that it does NOT nag when the model
  already verified.
- `providers.py`: connection/auth/model-name errors now raise RuntimeError
  with the exact fix in plain words (start-model.sh, export
  ANTHROPIC_API_KEY, current model names, /model to switch).
- `config.py`: corrupt YAML no longer kills everything — old file kept as
  config.yaml.broken, defaults regenerated, loud stderr warning.

Verified: 7-part guard test battery (blind-overwrite, undo/redo, blocklist
7-blocked/6-allowed, verify-nudge, no-false-nag, friendly provider errors,
config self-heal) + live qwen30b run passes untouched by the guards.
Notably: the fake-key test reached Anthropic's real API and got a proper
401 → whole Claude path confirmed wired; only a valid key is missing.

Ideas parked for next round: big/little model routing (tiny model for
summaries), GBNF grammar-forced tool calls for models worse than qwen at
tool syntax, Gemini provider when the user brings keys.

## Gotchas

- History is provider-neutral (see comment in providers.py); Anthropic raw
  blocks ride along in `assistant_blocks` and other providers must ignore
  them. Users can switch models mid-session — never assume history entries
  came from the current provider.
- `~/.forge/config.yaml` `models:` is the user's list, NOT topped up from
  defaults (deliberate — see config.py load_config). Changing DEFAULT_CONFIG
  does not touch existing installs; edit the live file too if that's wanted.
- The repo venv is `~/forge/.venv`; run things as
  `~/forge/.venv/bin/python` (don't `cd` + activate in agent shells).

## Log

- 2026-08-08 · Fable 5 session eafbce98 · started plug-and-play model work.
  Nothing committed yet beyond this file when first written; check
  `git -C ~/forge log --oneline` for what actually landed.
