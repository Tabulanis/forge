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

## Layout

    forge/providers.py   talks to models (Claude, or anything OpenAI-compatible)
    forge/tools.py       what the agent can do — read, write, edit, search, run
    forge/agent.py       the loop: ask model, run tools, repeat
    forge/cli.py         the terminal interface
    forge/server.py      the control panel's backend
    forge/web/           the control panel page
    ~/.forge/config.yaml your settings (shared by the terminal and the panel)
