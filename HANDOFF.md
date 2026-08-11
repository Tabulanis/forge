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

## 2026-08-10 (session 901d2e42, Fable 5)

- **Power switch** (`forge/power.py`): `forge off` stops every
  forge-model-* unit and reports the VRAM that came back (verified live:
  23.3 → 4.0 GB); `forge on [big|vision|little|tiny]` starts one again.
  `/off` inside a chat = goodbye + shutdown. Dashboard has a Power card
  (GET/POST /api/power — POST is token- and kid-gated) with the same
  off/on buttons; the dash itself stays up as the wake button. Full
  round trip verified: off, wake via the API, 30B back and answering.
  `forge help power` explains it in kid terms.
- **`merge` is on the PATH**: ~/.local/bin/{merge,forge,forge-dash} →
  the venv entry points. The user starts her with `cd <project>; merge`.
- **`merge on` now waits with a real loading bar** — VRAM growth against
  the measured 19.3GB the 30B takes (power.EXPECTED_LOAD_MB), finishing
  when llama-server's /health goes 200 (power.is_ready — the difference
  between "systemd started it" and "she can talk"). CPU models pace on
  time instead. Already-up short-circuits; Ctrl-C leaves it loading;
  5-minute bail points at doctor. /api/power GET now returns ready[] next
  to running[], and the dashboard Power card shows "(loading…)" until
  ready. Measured: warm wake (weights still in page cache) is ~8s; cold
  wake after boot is the ~1min one.

- **One brain, two doors** (user's ask: "make her act like Claude Code —
  terminal and web"). The CLI now runs on the same Session objects the web
  uses, saved to the same ~/.forge/sessions/ after every turn. `merge -c`
  continues the last chat in the current folder; `/sessions` lists saved
  chats (same list as the web drawer, newest first); `/resume <n>` swaps to
  one mid-REPL; every CLI event is mirrored into the session log so the web
  page shows terminal conversations verbatim. SessionStore.rescan() (called
  on /api/sessions, /api/chat, and stream-miss) picks up files the terminal
  wrote while the dash was running — never touching a busy session.
  cli.make_agent is GONE (Session builds the agent). Verified live:
  one-shot chat → fresh `merge -c` recalled the codeword; a chat created
  while the dash ran appeared in /api/sessions via rescan. Known edge, by
  design: the same session open in both doors at once = last save wins.

- **"Going off the rails" fix** (user's report; confirmed in ledger +
  session df51db2b5991: a chat about SCP lore ended with an uninvited
  48-line fanfic written to ~ and three rounds of arguing with the
  superego). Three-part cure, all verified live:
  1. CHAT/WORK modes in SYSTEM_PROMPT — conversational messages get words
     only, no tools, never create an unasked-for file; unsure = offer,
     don't do. Retest of the same SCP question: words only, zero tools,
     and she *offered* the project instead.
  2. BOUNCE_TAIL (agent.py) appended to all six harness/superego bounce
     injections: revise, then answer the USER as if the check never
     happened — no narrating verification, no arguing with the review.
     Superego injection also reworded ("if mistaken, let it go"). Retest:
     bounce fired, she fixed with tool calls, final answer user-facing.
  3. CLI home-folder guard: `merge` run in ~ redirects the chat to
     ~/Playground with a note (-w ~ overrides), same default the web has.
  The stray ~/SPC-lore.md was removed. SUPEREGO_PROMPT itself untouched —
  it stays sealed.

- **Mid-term memory** (2026-08-11, user's "use spare RAM for mid-term mem"
  idea, translated): forge/recall.py. Three tiers now: context (short),
  saved sessions (mid), notes+files (long). `recall` tool (tools.py, safe,
  no permission) does plain-code word search across all saved session
  transcripts + the librarian's index cards — no model in the loop, OS
  file cache makes it instant. System prompt tells her to recall instead
  of guessing, and that recall-during-CHAT is allowed. THE LIBRARIAN: the
  little 3B (now `systemctl --user enable --now forge-model-little`,
  starts at boot) distills each finished exchange into a one-line card in
  ~/.forge/memory-cards.jsonl. Flow: both doors drop finished turns into
  ~/.forge/card-queue/ (instant, one file per turn — a CLI thread died
  with the process and lost cards, hence the tray); the DASH works the
  tray on a daemon thread every 30s (serve() starts it). Little down =
  tray waits, search still finds verbatim. Verified live: she recalled
  the Bamboo Labs printer from a previous session's transcript on her own.
  Card quality from the 3B is unreviewed — eyeball memory-cards.jsonl
  after a few days.
- **32k context KEPT** (same day): start-model.sh big now runs
  `-c 32768 -fa on -ctk q8_0 -ctv q8_0` — 8-bit KV cache buys double the
  window in the same VRAM (23.4GB used, was 23.3 at 16k). Measured before
  keeping: needle-at-the-top of a 23,359-token prompt answered exactly
  (27s prompt eval ≈ 850 tok/s); short chat 4.8s; tool calls clean. The
  agent probes n_ctx from /props, so compaction adapted on its own.
  Revert = delete the EXTRA line in start-model.sh.

- **Live hand-editing** (2026-08-11, user building Storyweave with Merge:
  "I'll write new stuff in there... if I edit it, she has to go with the
  new edits"). Workspace.read_mtimes (tools.py) stamps mtime alongside
  every mark_read (renamed from reads.add — one helper, 6 call sites incl.
  both edit_file success paths, which previously forgot to re-stamp).
  Agent._stale_files() (agent.py) compares live disk mtime against that
  record; wired into _system() (so the model gets told on EVERY call
  within a turn, not just turn start — same "reread every turn" pattern
  as the notebook) and into a visible Event at turn start (so the user
  sees her notice). Passed through via Agent(read_mtimes=...) and
  session.py's _build_agent. Only tracks within one continuous process
  (REPL or a live dashboard Session) — Workspace state doesn't survive a
  CLI process exit, same pre-existing limit as the reads/edit-permission
  gate itself; not fixed, just noted.
  FOUND AND FIXED live via a real pty-driven test (script left in this
  session's scratchpad, drive_merge.py): a superego bounce flagged her
  correct, freshly-re-read answer as contradicting her own EARLIER
  answer — and she responded by editing the data file back to match her
  old claim, i.e. overwriting the user's real edit to stop looking
  inconsistent to her own reviewer. Fixed at the agent level (SYSTEM_PROMPT
  hard rule + reworded superego-bounce injection distinguishing "fix your
  WORK" from "fix your CLAIM by rereading, never by editing data") since
  the sealed superego prompt itself stays untouched by design. Re-tested
  after the fix: two more false bounces on the same run, she held the
  correct answer both times, never touched the file. This was a real risk
  for a canon-editing workflow and is exactly the kind of thing that
  needed catching before the user trusted her with real world files.

- **Canon-boundary + no-placeholder rules** (same session, continued
  stress-testing at user's request — "put the system through the paces").
  Found live in Storyweave: asked her to write an AI-originated idea into
  world/lore.md after "I like that, add it" — she filed it straight into
  CONFIRMED canon, declared it "confirmed" unprompted. The PROPOSED
  convention existed on paper (her own notebook, the file's own header)
  and she still crossed it under an enthusiastic-sounding prompt — a
  judgment failure, not a missing-information one. Fixed with a
  SYSTEM_PROMPT hard rule (agent.py: never write a placeholder instead
  of real content) plus, at the project level, a MECHANICAL (not
  principled) rule in Storyweave's FORGE-NOTES.md and world/lore.md's
  header: any AI idea goes below the PROPOSED heading, always, no matter
  how the author reacted — only "confirmed/canon/official" or the
  author's own edit promotes it. Retested clean on a second, unrelated
  idea — correct placement. A confound found along the way: a broken
  test (two `merge --auto` calls without `-c` between them) revealed she
  will confabulate an answer about a referent she has no memory of
  rather than saying so or using recall — real risk, not yet fixed,
  worth a dedicated test later. Also: quoting a heading's literal text
  in a file's own explanatory prose creates a duplicate-string collision
  that can spiral edit_file retries into triplicated content — not a
  Forge bug, a content-authoring trap; general enough that a note about
  it belongs here too for any future project.

- **Severe, still-open finding: creative continuity tasks fabricate**
  (same session, third round of stress-testing at user's request). Asked
  her to fill a real narrative gap in Storyweave's book — a scene the
  text references but never shows, with a real, checkable continuity
  trap (a character's name is learned much later than this scene, so
  using it early is a verifiable error). Two attempts, both bad:
  1st: never opened source-material/under-the-surface.txt at all —
  invented two entire fake "chapters," swapped both characters' genders,
  wrote a generic romance with zero connection to the real plot, voice,
  or setting, narrated with full confidence ("after some detective
  work, I found..."). Root-caused to a real gap: nothing in her
  notebook said where the actual book lived, so "the book" resolved to
  nothing findable and she filled the void.
  Added agent.py SYSTEM_PROMPT rule against confident fabrication
  (parallel to the existing no-placeholder rule — this is the same sin,
  dressed as diligence instead of laziness) + pointed Storyweave's
  FORGE-NOTES.md at the real file. Retested:
  2nd: DID open the real book this time (the fix reached her) — but
  searched imprecisely, landed on an unrelated later scene (a different
  character entirely), and built confidently on that wrong context
  anyway, fabricating three more scenes and one more false claim about
  the text's contents.
  Not fixed. This is a different, harder class of problem than
  everything else caught today (canon placement, stale files,
  destructive self-correction) — those were judgment/procedure bugs a
  clearer rule could close. This looks like a real research-discipline
  ceiling on the 30B: it can find A passage and mistake it for THE
  passage, then commit hard. Recommend for next time: don't hand her an
  open "find the gap and fill it" task solo — have her quote the exact
  before/after passages FIRST as a separate, checkable step the user
  confirms, before any drafting starts. Both bad attempts' output was
  deleted; nothing fabricated is in Storyweave's real files.

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
