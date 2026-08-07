"""
The terminal interface — what you actually talk to.

Deliberately close to Claude Code's feel: you're dropped into your project
folder, you type in plain language, tools run with your say-so, and slash
commands handle the meta stuff.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from .agent import Agent
from .config import (CONFIG_PATH, active_model_config, load_config,
                     load_pipelines, save_config)
from .pipeline import Pipeline
from .providers import build_provider
from .tools import Workspace, build_tools

console = Console()

BANNER = """[bold cyan]FORGE[/bold cyan] — your own coding agent
workspace: [dim]{ws}[/dim]
model: [bold]{model}[/bold] [dim]({provider})[/dim]   permissions: [bold]{perm}[/bold]

[dim]/help for commands · /model to switch · Ctrl-C to interrupt · Ctrl-D to quit[/dim]"""

HELP = """[bold]Commands[/bold]
  [cyan]/help[/cyan]            this list
  [cyan]/model[/cyan]           list models, or [cyan]/model <name>[/cyan] to switch
  [cyan]/perm[/cyan] <mode>     permission mode: ask · auto · deny
  [cyan]/clear[/cyan]           forget the conversation so far (fresh context)
  [cyan]/config[/cyan]          where the config file lives
  [cyan]/tools[/cyan]           what the agent can do
  [cyan]/flows[/cyan]           list orchestration recipes
  [cyan]/run[/cyan] <flow> <task>  run a recipe (multi-model loop)
  [cyan]/quit[/cyan]            exit

[bold]Anything else[/bold] you type is a message to the agent."""


def make_agent(cfg: dict, workspace: Path) -> Agent:
    mcfg = active_model_config(cfg)
    provider = build_provider(mcfg)
    ws = Workspace(workspace)
    return Agent(
        provider=provider,
        tools=build_tools(ws),
        max_steps=int(cfg["agent"].get("max_steps", 40)),
        permission_mode=cfg["agent"].get("permission_mode", "ask"),
    )


def ask_permission(tool_name: str, args: dict, summary: str) -> bool:
    # For a command, the box IS the description — repeating it in the header
    # just makes the same string appear twice on screen.
    if tool_name == "run_command":
        console.print("\n[yellow]▸ wants to run this command:[/yellow]")
        console.print(Panel(args.get("command", ""), border_style="yellow", expand=False))
    else:
        console.print(f"\n[yellow]▸ wants to {summary}[/yellow]")
    try:
        answer = console.input("[bold]allow?[/bold] [dim](y/n)[/dim] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes", "")


def print_events(agent: Agent, message: str) -> None:
    """Drive one turn and render it as it happens."""
    try:
        for ev in agent.run(message, ask=ask_permission):
            if ev.kind == "text" and ev.text.strip():
                console.print()
                console.print(Markdown(ev.text))
            elif ev.kind == "tool_request":
                # A permission prompt is about to describe this action in
                # full — no need to also whisper it here first.
                if not ev.will_ask:
                    console.print(f"[dim]  · {escape(ev.summary)}[/dim]")
            elif ev.kind == "tool_result":
                if ev.text == "declined":
                    console.print("[red]  · declined[/red]")
                    continue
                lines = (ev.text or "").strip().splitlines()
                # Show a couple of lines of real output, not just the first —
                # for a command, line one is "[exit 0]" and the interesting
                # part is what follows.
                for ln in lines[:3]:
                    console.print(f"[dim]    {escape(ln[:110])}[/dim]")
                if len(lines) > 3:
                    console.print(f"[dim]    … {len(lines) - 3} more line(s)[/dim]")
            elif ev.kind == "error":
                console.print(f"[red]{escape(ev.text)}[/red]")
            elif ev.kind == "done" and ev.usage:
                u = ev.usage
                if u.get("input_tokens") or u.get("output_tokens"):
                    console.print(
                        f"[dim]  ({u.get('input_tokens', 0)} in / "
                        f"{u.get('output_tokens', 0)} out)[/dim]"
                    )
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/yellow]")


def handle_command(line: str, cfg: dict, workspace: Path, agent: Agent) -> tuple[bool, Agent]:
    """Returns (should_continue, agent) — agent may be rebuilt on a model switch."""
    parts = line.strip().split()
    cmd, args = parts[0], parts[1:]

    if cmd in ("/quit", "/exit"):
        return False, agent

    if cmd == "/help":
        console.print(HELP)

    elif cmd == "/config":
        console.print(f"[dim]{CONFIG_PATH}[/dim]")

    elif cmd == "/tools":
        for t in agent.tools.values():
            lock = "[yellow]asks first[/yellow]" if t.needs_permission else "[dim]safe[/dim]"
            console.print(f"  [cyan]{t.name}[/cyan] — {t.description.splitlines()[0]} ({lock})")

    elif cmd == "/clear":
        agent.history.clear()
        console.print("[dim]context cleared[/dim]")

    elif cmd == "/model":
        if not args:
            for name, m in cfg["models"].items():
                mark = "[bold green]●[/bold green]" if name == cfg["active_model"] else " "
                console.print(f" {mark} [cyan]{name}[/cyan] — {m.get('model')} "
                              f"[dim]({m.get('provider')})[/dim]")
        elif args[0] not in cfg["models"]:
            console.print(f"[red]No model named {args[0]!r}. "
                          f"Try: {', '.join(cfg['models'])}[/red]")
        else:
            cfg["active_model"] = args[0]
            save_config(cfg)
            agent_new = make_agent(cfg, workspace)
            agent_new.history = agent.history        # keep the conversation
            console.print(f"[green]switched to {args[0]}[/green]")
            return True, agent_new

    elif cmd == "/perm":
        if not args or args[0] not in ("ask", "auto", "deny"):
            console.print(f"[dim]permission mode is [bold]{agent.permission_mode}[/bold] "
                          f"— use ask, auto, or deny[/dim]")
        else:
            agent.permission_mode = args[0]
            cfg["agent"]["permission_mode"] = args[0]
            save_config(cfg)
            console.print(f"[green]permissions: {args[0]}[/green]")

    elif cmd == "/flows":
        flows = load_pipelines()
        if not flows:
            console.print("[dim]no recipes defined[/dim]")
        for name, spec in flows.items():
            console.print(f"  [cyan]{name}[/cyan] — {spec.get('description', '')}")
            for st in spec.get("steps", []):
                k = st.get("kind", "agent")
                extra = (f" ×{st.get('max_rounds', 3)} until {st.get('until_ok') or st.get('until_text')}"
                         if k == "loop" else f" [{st.get('model', st.get('command', ''))[:40]}]")
                console.print(f"      [dim]{k}: {st.get('name')}{extra}[/dim]")

    elif cmd == "/run":
        flows = load_pipelines()
        if not args:
            console.print("[dim]usage: /run <recipe> <what you want done>[/dim]")
        elif args[0] not in flows:
            console.print(f"[red]No recipe {args[0]!r}. Try /flows.[/red]")
        elif len(args) < 2:
            console.print("[dim]tell it what to do: /run <recipe> <task>[/dim]")
        else:
            run_pipeline(args[0], flows[args[0]], " ".join(args[1:]), cfg, workspace)

    else:
        console.print(f"[red]Unknown command {cmd}. /help for the list.[/red]")

    return True, agent


def run_pipeline(name: str, spec: dict, task: str, cfg: dict, workspace: Path) -> None:
    """Run one orchestration recipe, printing progress as it goes."""
    spec = {**spec, "name": name}
    try:
        pipe = Pipeline(spec, cfg["models"], workspace,
                        permission_mode=cfg["agent"].get("permission_mode", "ask"))
    except Exception as e:
        console.print(f"[red]{escape(str(e))}[/red]")
        return

    console.print(f"\n[bold cyan]▶ {name}[/bold cyan] [dim]{spec.get('description','')}[/dim]")
    try:
        for ev in pipe.run(task, ask=ask_permission):
            if ev.kind == "step_start":
                console.print(f"\n[bold]· {ev.step}[/bold] [dim]({ev.text})[/dim]")
            elif ev.kind == "loop_round":
                console.print(f"\n[yellow]  ↻ {ev.step} — round {ev.round}[/yellow]")
            elif ev.kind == "step_done":
                mark = "[green]✓[/green]" if ev.ok else "[red]✗[/red]"
                first = (ev.text or "").strip().splitlines()
                console.print(f"  {mark} [dim]{escape(first[0][:100]) if first else ''}[/dim]")
            elif ev.kind == "error":
                console.print(f"[red]{escape(ev.text)}[/red]")
            elif ev.kind == "done":
                mark = "[green]finished[/green]" if ev.ok else "[yellow]finished with problems[/yellow]"
                console.print(f"\n{mark}")
                if ev.text.strip():
                    console.print(Markdown(ev.text[:1500]))
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted[/yellow]")


def main() -> None:
    ap = argparse.ArgumentParser(prog="forge", description="Your own coding agent.")
    ap.add_argument("message", nargs="*", help="Run one message and exit")
    ap.add_argument("-w", "--workspace", default=".", help="Project directory")
    ap.add_argument("-m", "--model", help="Model config to use for this run")
    ap.add_argument("--auto", action="store_true", help="Skip permission prompts")
    args = ap.parse_args()

    cfg = load_config()
    if args.model:
        if args.model not in cfg["models"]:
            console.print(f"[red]No model named {args.model!r}[/red]")
            sys.exit(1)
        cfg["active_model"] = args.model
    if args.auto:
        cfg["agent"]["permission_mode"] = "auto"

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        console.print(f"[red]Not a directory: {workspace}[/red]")
        sys.exit(1)

    mcfg = active_model_config(cfg)
    if mcfg.get("provider") == "anthropic" and not (mcfg.get("api_key")
                                                    or os.environ.get("ANTHROPIC_API_KEY")):
        console.print("[red]No Anthropic API key found.[/red]")
        console.print("Set one with:  [cyan]export ANTHROPIC_API_KEY=sk-ant-...[/cyan]")
        console.print(f"or put it in [dim]{CONFIG_PATH}[/dim] under models.claude.api_key")
        sys.exit(1)

    try:
        agent = make_agent(cfg, workspace)
    except Exception as e:
        console.print(f"[red]Couldn't start: {e}[/red]")
        sys.exit(1)

    # One-shot mode: `forge "fix the tests"` runs and exits.
    if args.message:
        print_events(agent, " ".join(args.message))
        return

    console.print(Panel(
        BANNER.format(ws=workspace, model=cfg["active_model"],
                      provider=mcfg.get("provider"),
                      perm=cfg["agent"].get("permission_mode")),
        border_style="cyan", expand=False,
    ))

    while True:
        try:
            line = console.input("\n[bold cyan]›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            break
        if not line:
            continue
        if line.startswith("/"):
            cont, agent = handle_command(line, cfg, workspace, agent)
            if not cont:
                console.print("[dim]bye[/dim]")
                break
            continue
        # Re-read config each turn so dashboard changes land without a restart.
        fresh = load_config()
        if fresh["active_model"] != cfg["active_model"]:
            cfg = fresh
            hist = agent.history
            agent = make_agent(cfg, workspace)
            agent.history = hist
            console.print(f"[dim](model changed to {cfg['active_model']} from the dashboard)[/dim]")
        print_events(agent, line)


if __name__ == "__main__":
    main()
