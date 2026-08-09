# Forge — agent handoff notes

Read this first if you're an agent picking up work in `~/forge`.
Claim the folder in `~/aidojo/AGENT-LOG.md` before editing; release the
claim and log what you did when you stop.

## What Forge is

A terminal coding agent (like Claude Code) plus a web control panel, pointed
at whatever model you want. Layout is in README.md. Config lives at
`~/.forge/config.yaml` — shared by the CLI and the dashboard, re-read every
turn, so model switches take effect without restarts. The repo venv is
`~/forge/.venv`; run things as `~/forge/.venv/bin/python` (don't cd+activate
in agent shells). Own git repo, **no remote** — local commits only.

## Where things stand (end of day 2026-08-08, session eafbce98, Fable 5)

Everything below is committed; working tree is clean at `3646dd6`.

**Works, verified live:**
- Local models end-to-end with tool calls: qwen30b (llama.cpp :8080,
  currently the active model) and tiny (:8081). `start-model.sh` launches
  them; `forge doctor` names anything broken and its fix.
- Web dashboard :8770, token-protected, LAN-visible.
- Whisper speech-in, spd-say speech-out, screenshots. Vision model (:8090)
  is NOT running — `start-model.sh vision` if needed.

**Wired and confirmed to the doorstep, waiting on one thing:**
- The whole Claude path (current models: `claude` → claude-opus-5, `fable`
  → claude-fable-5, both in config). A deliberate fake-key test reached
  Anthropic's real API and got a proper 401 back, so transport, SDK, and
  request shape are all proven. **The machine has no ANTHROPIC_API_KEY.**
  First real test once the user provides one:
  `export ANTHROPIC_API_KEY=...` → `forge` → `/model claude` → say hi,
  then a small file task to exercise tool use + thinking-block replay.

**Hard-coded harness discipline (works with ANY model, all unit-tested):**
- edit_file refuses files not read this session; write_file refuses to
  overwrite files not read this session.
- Every write/edit backs up the prior version to `.forge_backups/<relpath>`;
  `undo_file` tool restores (calling it twice = redo).
- Identical repeat of a just-failed tool call is refused with advice.
- Final answers claiming actions when no tools ran get bounced back once.
- Final answers after file changes with no verification (no run_command /
  read_file after the change) get bounced back once, asking to verify.
- Machine-killer commands refused even in auto mode (root/home deletion,
  disk format/overwrite, fork bombs) — deliberately a short list.
- Per-project notebook: FORGE-NOTES.md injected into the system prompt
  every turn (tail-truncated at 4k chars); `save_note` tool appends to it.
- Provider errors name the exact fix; corrupt config.yaml self-heals
  (old file kept as config.yaml.broken).

## Start here tomorrow

1. Read this file, claim the folder in the agent log.
2. If the user brought an Anthropic key: run the first-real-Claude test
   above. Watch for tool-use loops specifically — the thinking-block replay
   (`assistant_blocks` in providers.py/agent.py) is stub-tested but has
   never run against the live API.
3. If the user brought Gemini keys: add a GeminiProvider in providers.py
   (follow the AnthropicProvider pattern; the old wickerman router at
   `~/wickerman/plugins/wm-llama/data/manager.py` has a working
   OpenAI→Gemini translation to crib from), plus a dashboard "Kind" option.
4. Otherwise, parked ideas in rough order of value:
   - big/little routing: tiny model (:8081) for cheap summaries/titles,
     qwen30b for the real thinking
   - GBNF grammar-forced tool calls for models clumsier than qwen
     (providers already accept a `grammar` arg; nothing passes one yet)
   - a `/undo` CLI command surfacing undo_file for the user directly

## Gotchas

- History is provider-neutral; Anthropic raw blocks ride in
  `assistant_blocks` and other providers must ignore them. Users can switch
  models mid-session — never assume history entries came from the current
  provider.
- `~/.forge/config.yaml` `models:` is the user's list, NOT topped up from
  DEFAULT_CONFIG (deliberate). Changing defaults doesn't touch existing
  installs; edit the live file too when that's intended.
- Workspace read-tracking (`ws.reads`) resets when the agent is rebuilt
  (model switch) — models just get asked to re-read; harmless.
- The keep-in-sync list of current Claude model names lives in two places:
  providers.py (NotFoundError message + SAFETY_FALLBACK_MODELS) and
  web/index.html (datalist).
- Test scripts and scratch runs live under the session scratchpad, not the
  repo — the guard test battery is at
  (scratchpad)/proof-test/test_guards.py if you want to re-run it; simplest
  is to re-create tests from the descriptions above.

## Session log

- 2026-08-08 · Fable 5 session eafbce98 · three commits:
  - `05951ed` current-Claude support: thinking-block replay, refusal
    handling + server-side fallbacks (Fable/Opus 5), current model names in
    config + dashboard, live config updated.
  - `a14e344` harness discipline: read-before-edit, broken-record detector,
    liar catcher, project notebook (save_note + FORGE-NOTES.md).
  - `3646dd6` dummy-proofing: blind-overwrite guard, backups + undo_file,
    machine-killer blocklist, verify-before-done, friendly provider errors,
    self-healing config.
  All verified: scripted deterministic test batteries per round + live
  qwen30b runs after each. User plans to bring Anthropic and Gemini keys.
