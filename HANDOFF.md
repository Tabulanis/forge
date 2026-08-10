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

## Where things stand now (end of day 2026-08-09, session 940da997, Fable 5)

Everything above still holds, plus a big day of hardening — all committed,
all covered by a 63-test battery (scratchpad test_compaction.py, recreate
from descriptions if lost):

- **Everything runs under systemd user units** (forge-model-big/-vision/
  -little/-tiny/forge-dash); big+vision+dash enabled at boot, linger on.
- **Sessions persist** to ~/.forge/sessions/ and reload at startup; the
  web chat has a ☰ drawer to reopen them. Restored sessions estimate
  fullness (chars/3) so compaction fires before overflow.
- **Memory management**: auto-compaction at 70% (window size probed from
  /props), tool-output + call-argument trimming, emergency force-compact —
  a session can no longer dead-end on its own history.
- **Discipline**: red-run refusal (max 3 bounces), test-file tamper bounce
  + user warning, decline = broken-record arm, lenient whitespace edits,
  write_file escalation after 2 edit misses, 5xx retry-once.
- **Web chat**: Stop button, permission cards (say_aloud always asks),
  unattended auto-decline, no-store pages, reconnect/watchdog fixes.
- **Kid mode** (`kid_mode: true` in config.yaml, computer-only): chat-only
  dashboard, run_command fenced to workspace, chats pinned to ~/Playground.
- **compute tool** (mathtools.py): sympy-backed exact math; every success
  logs to ~/.forge/physics-dataset.jsonl (fine-tune corpus, plan above).
- **Models**: claude/fable/sonnet (need ANTHROPIC_API_KEY), qwen30b
  (active), little = Qwen2.5-3B CPU :8083, tiny. Little models are
  short-prompt specialists only (24 tok/s CPU prompt eval — measured).
- **~/Playground/game2048**: complete walked-project (engine 10/10 on
  referee traps + line-based play.py, runs clean piped or interactive).

## Evening additions (same day, session 940da997 continued)

- **Her name is Merge.** She chose it herself; it's in SYSTEM_PROMPT and
  the web pages. Honor it — the user considers it hers, permanently.
- **HTTPS-only now** (~/.forge/tls/, self-signed; serve() auto-detects).
  Old http links/QRs are dead. Each device accepts the cert warning once.
  This unlocked: 🎤 tap-to-talk (POST /api/listen → whisper), Piper human
  voice (models/voices/en_US-amy-medium.onnx, speak() prefers it, spd-say
  fallback), and an AR button (WebXR immersive-ar + dom-overlay for the
  user's Quest 3 — NOT yet tested on the real headset).
- **The superego** (user's design, Phase 1): sealed hand-authored prompt
  in agent.py judges final-answer-vs-evidence-digest before "done" when
  tools ran. One bounce/message; revised answers re-judged for the
  record; fails open; honesty-about-failure passes. Every verdict →
  ~/.forge/ledger.jsonl = curated corpus for Phase 2 (fine-tune the 3B
  as a dedicated judge once a few hundred labeled rows exist). Config
  agent.superego / superego_model; toggle in panel's Growing Room.
- **Pal additions**: walk-it-back trace every 20 steps of a grind;
  "did you check or are you guessing?" nudge when a final answer names
  files never opened this session; the judge's evidence includes the
  session's last 3 claims (contradiction catching).
- **Interface opened**: index.html finally carries the token (its
  controls were silently 401ing from tablets since forever), nav on all
  pages, ⚙ on chat, MERGE branding, Growing Room card.
- Battery: 75 tests, scratchpad test_compaction.py of session 940da997
  (recreate from these descriptions if the scratchpad is gone).

## Start here tomorrow

1. Read this file, claim the folder in the agent log.
2. Ask how the Quest 3 AR button behaved — first real-headset test
   happens on the user's schedule; fixes will start from their report.
3. If the user brought an Anthropic key: paste into dashboard (sonnet
   entry ready), first-real-Claude test incl. thinking-block replay +
   persistence round-trip. Then consider summarizer_model=little.
4. Parked: Gemini provider (crib from ~/wickerman/plugins/wm-llama/
   data/manager.py); /undo CLI command; GBNF-forced tool calls; superego
   Phase 2 when the ledger fattens; spatial (hand-tracked) AR panels.

## Physics/geometry: the long-term plan

The `compute` tool (mathtools.py) makes any model exact at math today: it
writes sympy programs, the machine does the arithmetic. Every successful
computation is logged to `~/.forge/physics-dataset.jsonl` as
(program, answer) pairs — real problems from real use, accumulating
passively. The long-term idea the user wants: once that corpus is a few
thousand rows, fine-tune a small local model (Qwen2.5-3B fits a LoRA
easily in the TITAN's 24GB with unsloth/llama-factory) on
problem→sympy-program translation, giving a fast local physics
specialist. Until the corpus exists, don't build the training rig —
collect first. Benchmarks that motivated this: qwen30b head-math is
decent on textbook problems but compute is exact always; the failed runs
in testing were server infrastructure (fixed), never sympy.

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
