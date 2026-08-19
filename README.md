# Forge

Your own coding agent — a terminal agent like Claude Code, pointed at whatever
model you want, with a web control panel for switching between them.

## Setup

    cd ~/forge
    ./.venv/bin/pip install -e .

Set an API key for Claude (only needed while using Claude):

    export ANTHROPIC_API_KEY=sk-ant-...

## Use it

**The terminal agent** — run it from whatever project you're working on:

    cd ~/some-project
    ~/forge/.venv/bin/forge

Then just talk to it. Type `/help` for commands.

**The control panel** — in another terminal:

    ~/forge/.venv/bin/forge-dash

Open http://127.0.0.1:8770 . Switch models, add local ones, change how much
the agent asks permission. Changes reach the terminal on your next message,
no restart needed.

## Using local models

Start whatever serves your model (Ollama, llama.cpp's server, LM Studio),
then in the control panel: **Add a model** → Local → point it at the server
→ hit **Find models on that server** → **Save model**. Click it in the list
to make it active.

Ollama serves at `http://localhost:11434/v1` by default; llama.cpp and
LM Studio usually at `http://localhost:8080/v1` or `:1234/v1`.

## Specialist tools

Beyond read/write/edit/search/run, this build carries a set of grounded,
verified-or-nothing domain tools. The through-line is honesty: each one says
"I don't know" out loud rather than making something up.

    recall                         associative memory across past sessions
    run_sim / build_sim            self-built simulations, null-tested off a validated shelf
    verify_case / find_regulation  US law, grounded in real sources — never a false VERIFIED
    verify_drug / find_condition   medical terms via NLM (information, not advice; 911 for emergencies)
    find_third_party / flag_xfile  the X-Files hunt: who drives two markets that move together
    design_part                    CAD parts for the Maker Studio (port 8840)
    see_sound / match_sound        turns a sound into a visual "sound portrait"
    study_calls                    finds STRUCTURE in animal calls (a system, never a meaning)
    language_scorecard             how language-like a call sequence is, vs random and vs human
    deduce_meaning                 the deduction pad — see below

### The deduction pad (`deduce_meaning`)

Translation-by-elimination, Clue-style. You can't read an animal's mind, so
instead of claiming what a call means, it rules out what the call *can't* mean.

You feed it a log of observations — each time a call fired, what was true in the
world (threat present? food? did it flee after?). It holds a polymath **blob**
of candidate meanings written as *phrases*, not words ("ground predator close —
get up / bolt", "food here — come eat"), coming at meaning from every angle:
predator-type, direction, urgency, who it's for, mood, resource, coordination,
identity — wild and long-shot guesses included, because crossing one off is
itself progress. Elimination is the chisel: it knocks away the phrases the call
fires *without*, and a coarse log stays an honest cluster ("a threat — kind
unknown") while a finer one collapses to a single phrase.

Before it will pin anything, it asks the prior question — **is this even a
signal, or just noise?** — by testing whether the call fires in a repeatable
context against a shuffle null. No signal, no meaning: a noisy call stays a blob
no matter how tidy its cues look. A real `study_calls` result can supply that
answer straight from the audio (`signal_scores={call: 0..1}`).

Hard limit: it *eliminates*, it does not translate. A surviving phrase is a lead
to field-test, never a claim about what the animal said.

## Layout

    forge/providers.py   talks to models (Claude, or anything OpenAI-compatible)
    forge/tools.py       what the agent can do — read, write, edit, search, run
    forge/agent.py       the loop: ask model, run tools, repeat
    forge/cli.py         the terminal interface
    forge/server.py      the control panel's backend
    forge/web/           the control panel page
    forge/doolittle.py   the deduction pad (deduce_meaning)
    ~/.forge/config.yaml your settings (shared by the terminal and the panel)
