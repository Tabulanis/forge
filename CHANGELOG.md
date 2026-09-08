# Changelog

The chain of changes: what changed, when, and — when something was abandoned —
that it was abandoned and why. A thing that gets dropped gets a line saying so.
It never just disappears.

**The rule:** one commit per change, one line here per commit, pushed after each.

Started 2026-09-08 at the beginning of the 2.0 rebuild. The full commit history
goes back to 2026-08-06 (`git log`); this file starts at the 2026-09-05 hardware
cutover, because that is the honest starting line for the code as it runs today.

---

## 2.0 rebuild — in progress, from 2026-09-08

The premise: every cap, hidden tool and switched-off feature in this codebase
was sized for a small model on a small card. That stopped being true on
2026-09-05. None of the limits moved with her. This is the pass that moves them.

Fallback point: `git checkout v1.9-preflight` returns the code to how it ran on
the morning of 2026-09-08, before any of this.

### Phase 0 — the floor
- **2026-09-08** — Tagged `v1.9-preflight`: the exact state of the code before
  the rebuild, so every cut after it is reversible.
- **2026-09-08** — `~/.forge` is now a git repo (settings only, no remote — it
  holds the dash token). Her config, persona and flows had no history at all
  until today. That gap is why a retired vision model kept coming back across
  two rebuilds: the live config was correct, but nothing recorded when or why,
  so every regeneration quietly restored the old default.
- **2026-09-08** — This file.

### Phase 1 — the strip
- **2026-09-08** — **All six flows were dead for a month.** Every step named
  `qwen30b` or `tiny`; neither has existed in config since the cutover, so every
  flow raised an error on its first step. Repointed at `big122` / `little`.
  Verified: all six resolve.
- **2026-09-08** — **`start-model.sh` with no argument launched the retired 30B.**
  The obvious command started a dead model. Default is `big122`.
- **2026-09-08** — **Killed the vision ghost at its source.** The live config was
  always right, but the code default still named `qwen3.6-27b`, and that default
  fires on any config regeneration. Nobody kept re-adding the dead model; the
  fallback path kept restoring it. That is why it survived two rebuilds.
- **2026-09-08** — **The health check was hunting retired models and could never
  pass.** It scanned five ports that serve nothing, told the owner to start a
  retired vision service, and demanded two disabled units be enabled.
- **2026-09-08** — **The help text was teaching the retired architecture to
  humans.** It described vision as a separate model competing for the graphics
  card, and offered three retired models as things you could start. Documentation
  that lies is worse than code that lies: it misleads everyone who reads it.
- **2026-09-08** — **Gave her back all 84 tools.** She owned 84 and could see 33.
  Hiding the rest saved ~9.6k tokens, which was 62% of the old 24k window and is
  11.6% of the 131k window today. The cost of that saving was a model that could
  not see what she owned. Privacy deny lists untouched and still bite.
- **2026-09-08** — **Finished the morning's browser fix.** It exempted the three
  tools that *look* at a page and missed `browse`, the one that *opens* a page.
- **2026-09-08** — **The notebook cap ate its own founding rules.** 8,000 chars
  sized for a 16k window, trimming the tail so the OLDEST rules died first.
  Raised to 32,000 and the trim now keeps both ends.

> **Not done, deliberately — 2026-09-08:** `AUTO_SCOUT_ENABLED` stays `False`.
> Its comment says "flip on with a fast brain", and there is one now, but the
> code tells a different story: the digest runs on `summarizer or provider`,
> which is the little 3B — and "hallucinates on the 3B" is exactly why it was
> shelved. The stated condition is not actually met. Enabling it would need the
> digest pointed at the big brain first, which is a behaviour change, not a lift.

> **Not done, deferred — 2026-09-08:** `SUMMARY_INPUT_TOKENS` stays 7,000. It is
> held down by the 3B fallback's real 8k window, not by old hardware. Raising it
> needs the budget to follow whichever model is actually summarizing.

> **Left alone — 2026-09-08:** `power.py`'s exclusive-model logic still knows the
> retired units. They exist but are disabled, and the power switch needs to be
> able to report on them. Intentional, not drift.

> **Found, not fixed — 2026-09-08:** `forge doctor` reports three false warnings
> ("Config truth") by comparing a short model name in config against the full
> file path the server reports. Same model, naive string compare. Pre-existing
> and unrelated to this rebuild. Noise that trains you to ignore warnings.

---

## Before 2.0

### 2026-09-08 — browser tools, and bulletproofing
Her browser tools were built and never wired onto her belt; wired them, then
found she still had no way to *open* a page and added `browse`. Stopped the
per-turn tool budget blindfolding her on iterative browser work. Then three
guards: a promise to act counts as a claim she has to back up, half a tool set
counts as a bug at startup, and notes get checked. A picture made from a
reference now inherits its size, not just its shape.

> **Known incomplete:** the iterative-tool fix exempted the three tools that
> *look* at a page and missed `browse`, the one that *opens* a page. The bug it
> was written to fix still returns after ten pages. Fixed in phase 1.

### 2026-09-07 — the window opens, and the story pipeline
Context went 32k → 64k → 131,072, sized for the new box rather than the old
24 GB card. Head diet: media descriptions and the playbook condensed. A holdover
sweep updated turn-wall accounting, the summary budget and TITAN-era comments.

> **Known incomplete:** that sweep missed the notebook cap (still sized for a
> 16k window) and the tool-curation subsystem (still hiding 51 of 86 tools to
> save a budget that no longer exists). Both fixed in phase 1.

Story video matured: pose-guided fill, a pose library in Python, anchored long
drafts, SeedVR2 as the upscale path, and a LoRA ecosystem she can grow herself.
Pictures moved to FLUX.2 klein 4B as the workhorse, Qwen-Image demoted to slow
flagship.

> **Abandoned 2026-09-07:** the VACE repaint as the upscale path. It invented
> tiles at every strength tested. Kept as a rescue, not the route. Replaced by
> SeedVR2.

### 2026-09-06 — everything that renders moves off her box
All image and video work moved to the render box so the brain's machine does one
thing. The prompt became append-only: one constant head, with per-session and
per-turn text riding inside the user messages, so the cache actually holds.
Large build-out of the story pipeline: beat sheets, causal beats, continuity and
transition checks with her own eyes, bidirectional fill, decompose and recast.

### 2026-09-05 — the hardware cutover
The 122B became her brain and her eyes, on port 8087. Wake buttons, power
switching and the start script repointed off the retired 27B and 30B. Timeouts
became silence-based rather than whole-call caps, which is what had been killing
her long bug-hunt runs. Images moved to ComfyUI on the GPU.

> **Removed 2026-09-05:** FLUX.2-dev and klein-9B. Gated, non-commercial
> weights. Permissive licences only from here.

> **Retired 2026-09-05:** the 27B, the 30B and the separate 7B vision model. Her
> eyes are the 122B's own. Their systemd units still exist but are disabled.

### Before 2026-09-05
Merge on the old box: a 27B and a 30B trading places on one 24 GB card, a
separate vision model, and a 16k–32k context. Almost every constraint the 2.0
rebuild is lifting was written during this period, and was correct at the time.
See `git log` before `2026-09-05` for the detail.
