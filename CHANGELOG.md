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

- **2026-09-08** — **The power switch could not see the embedder,** the model
  that makes her memory cards searchable. Live and enabled, absent from the
  roster, so `forge off` could not stop it and the Power card never showed it.
  Also emptied `EXCLUSIVE`: it auto-stopped a rival model before starting one,
  because on the 24GB card the 30B and 27B could never fit together. The whole
  stack is resident at once now. Retired units moved to `LEGACY_UNITS`, kept only
  so a stray hand-started one can still be stopped.
- **2026-09-08** — **The summary budget follows the model doing the work.** One
  number served two models that are nothing alike: a 131k main brain and an 8k
  fallback. It was sized for the fallback and applied to both, so a compaction
  that drops ~60,000 tokens wrote its briefing from the last 7,000. The main
  brain now reads 45% of its own window (58,982 tokens); the 3B keeps its 7,000.
- **2026-09-08** — **Auto-scout is back, on a brain that can be trusted with it.**
  Flipping the flag alone would have shipped the original bug: the digest ran on
  the summariser, which is the little 3B, and "hallucinates on the 3B" was the
  wrong model writing, not slow hardware. The digest is written by the main brain
  now. The big-file threshold also goes 600 lines / 50 KB → 2000 / 100 KB, sized
  for the 131k window: `storyvideo.py` (872 lines) and `videogen.py` (787) came
  back as a map and three hints and now read whole, while `agent.py` and
  `tools.py` are still mapped, correctly — they are genuinely huge.

- **2026-09-08** — **Maker Studio was never given a way to start itself.** Her
  3D tools pointed at a service with no systemd unit, so every call failed with
  connection refused. Now a managed service on 8840, enabled at boot, with the
  same memory-priority rule as her models. Verified: `list_parts` returns eight
  saved parts.
- **2026-09-08** — **The superego was reviewing itself, and blind where it
  mattered most.** Three faults. `superego_model` was blank, which means fall
  back to the answering brain, so the 122B judged its own work. The gate ran
  only when tools had run, so a turn with no tool calls — the exact shape of a
  fabrication — was never reviewed. And it was switched off in `balanced`, the
  everyday default, for a latency cost that no longer applies now the reviewer
  is a separate model on a separate port.
  Caught live while testing: asked what port her dashboard uses, she named a
  port and a dotfile that do not exist. Real answer 8770. Nothing reviewed it.
  Now: a `judge` model (Qwen3.8-27B, :8088, no vision, 8k window) scoring 10/10
  on a 10-case battery at ~3s a judgement, where the 3B managed 3/6 and bounced
  even answers it had described as correct. Re-run after the fix: bounced in
  3.3s and she went and used four tools.

> **Known limit — 2026-09-08:** the reviewer checks the claim against the
> EVIDENCE, not against the truth. In the test above her second answer was still
> wrong, but it matched a file she had actually read, so it passed. The gate
> stops unsupported confidence. It cannot stop reading the wrong file carefully.

> **Blocked on you — 2026-09-08:** the life archive holds nine FAKE records from
> an August demo (a package tracking number, a flight, an appointment with a
> "Dr. Reyes"). There is no Takeout export on this machine, so it has never held
> anything real, and she would answer questions about your life from invented
> data. Needs either your export or a wipe. Not touched.

> **Watch this — 2026-09-08:** with the judge resident, VRAM sits at 92.8 of
> 96GB. Your system RAM is untouched (13GB free) and nothing else uses the card
> since renders moved to the other box, but the margin is ~3GB.

> **Found, not fixed — 2026-09-08:** `forge doctor` reports three false warnings
> ("Config truth") by comparing a short model name in config against the full
> file path the server reports. Same model, naive string compare. Pre-existing
> and unrelated to this rebuild. Noise that trains you to ignore warnings.

---

## 2026-09-09 — the honesty pass

The premise for the day, in his words: *"we just need in the end that she
doesnt lies or halucinate. we fix that... if we cant fix it any other way then
checks and determanistic tools to keep her on track. And if she doesnt know she
finds out. But knowing she doesnt know is the bigest part."*

### Truth, and knowing the difference
- **She can now measure not-knowing instead of feeling it.** `do_i_know` samples
  the same question several times: answers that disagree with each other are a
  CERTAIN negative, not a guess. It says so plainly, and it is equally plain
  about the limit — consistency rules out one error, inventing it fresh, and
  nothing else. A memorised mistake is perfectly consistent. 4.0s for five
  samples, against 6.2s for one ordinary reply.
- **True, believed and popular are three different things,** and the reviewer
  now bounces one stated as another. A scripture, a myth or a legend can be
  reported as what it says; asserting its content as fact about the world is a
  bounce. What people believe, and how many, is a fact about PEOPLE. It has no
  favourites: a religious claim asserted as established fact and a claim that a
  belief has been disproven are the same error in different clothes. Describing
  a belief accurately and respectfully, in its own terms, is never the error.
  Battery 13/15 before, 15/15 after.
- **What a belief CLAIMS is not a fact. What a belief DID is.** Effects in the
  world are historical and measurable, and respect for a belief never licenses
  vagueness about its record. The failure named in the rule is the NON-ANSWER —
  "a force for both good and ill in complex ways" — which protects an
  institution by refusing to be specific. The test is EQUAL SPECIFICITY, not
  equal airtime, and it cuts both ways. Manufacturing a counterweight to make
  the shape look even is a fabrication and bounces as one.

  Measured before the rule was written, five matched pairs, blind judge:
  **she is not shilling.** She leans the other way — more specific about harms
  in 4 of 5 pairs. Pushed for named cases she fills both sides, so the gap is
  emphasis, not a gap in what she knows.

### The bug hunt
- **`when_changed`** gives her the move she never makes: ask git when a line of
  code actually changed, instead of reading the current file and guessing.
  `history_survey` and `rule_out` came with it — a theory needs evidence to be
  struck off, and two strikes before anything is called a cause.
- The same tool was then broken three ways in one day, each a variant of one
  mistake: a default that failed when the repo sat one level down, a path that
  stopped resolving once the repo was found below, and registration that
  resolved relative paths against the process directory rather than her
  workspace. She wrote "the tool says no git repo but I see .git" and then
  typed 46 `git show` commands by hand. Now `t_path_tools_work_from_where_she_stands`
  exercises all 21 path-taking tools from a foreign directory.

### Her memory, and her record
- **17% of her memory could never have been recalled** — gated, then cleaned.
- **The clean-up was then rewritten, because the first version was throwing away
  his life.** It filtered on LENGTH as a proxy for value, which deleted "Riverton
  AZ 40881", "It's the Riverton unit, not the spare", a project codename, a budget cap, and
  both of two contradictory statements of his height — where the contradiction
  was the useful part. All 810 cards restored from backup, every candidate
  printed and read, 30 removed instead of 143.

  > The vector store is row-aligned to the card file, 768 floats per card.
  > Pruning cards without their vector rows would have silently mis-attributed
  > her entire history. Alignment is asserted before and after, and guarded.

- **She can read what the reviewer says about her,** and a pattern that repeats
  reaches her unasked rather than waiting to be requested.

### Her ears
- **44.1 kHz**, up from 22.05, and that exposed a spectrogram bug that had
  always been there: each row was drawn from a single frequency bin instead of
  the maximum across its band, so a 15 kHz tone rendered at brightness 6 out of
  255. Source separation landed with it, with ground-truth verification.

### The reviewer's own failure
- **An empty verdict now earns one nudged retry.** Found while testing the
  effects rule: the reviewer returned ZERO characters with finish_reason
  "stop", five identical runs at temperature 0, and raising max_tokens from 80
  to 400 changed nothing. It is not a content refusal — the same shape of
  answer about a corporation did it too, and one about a government did not.
  Because the gate fails open, that answer shipped UNREVIEWED while the ledger
  recorded only "malformed", which nobody would ever look at. One trailing
  newline or a little temperature breaks it.
- **The regression suite now runs the code that ships.** `bench/reviewer.py`
  holds every rule the superego has been taught, reads the prompt live from
  `forge.agent`, and calls the reviewer through the same `superego_ask()` the
  agent uses. It used to speak HTTP itself, which is exactly how the retry
  above could have been "fixed" while the test went on grading a path that no
  longer existed. evidence 8/8, belief 7/7, effects 10/10.

> **My own errors today, since they belong in the record too:** one battery
> MISS was my test case rather than her reviewer — the evidence named a study
> without carrying its finding, so the answer asserted an unsupported result
> and the bounce was correct. Two numbers in the effects work were my
> measurement bugs: a regex that counted "1088" but not "16th century", and a
> prompt that forced "name an event with a date" onto a question about what
> people self-report.

> **Found, not fixed — 2026-09-09:** the factual suite's 5/8 was measured
> against the RAW brain on its own port. It bypassed her tools, her guards and
> her reviewer entirely, so it is a score for the model and not for her. Needs
> re-running through the full pipeline before it means anything.

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
