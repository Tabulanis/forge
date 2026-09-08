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
_(entries land here as each cut is made)_

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
