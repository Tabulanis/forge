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
from rich.panel import Panel

from .agent import Agent
from .config import CONFIG_PATH, active_model_config, load_config, save_config
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
    console.print(f"\n[yellow]▸ wants to {summary}[/yellow]")
    if tool_name == "run_command":
        console.print(Panel(args.get("command", ""), border_style="yellow", expand=False))
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
                console.print(f"[dim]  · {ev.summary}[/dim]")
            elif ev.kind == "tool_result":
                first = (ev.text or "").strip().splitlines()
                preview = first[0][:110] if first else ""
                if ev.text == "declined":
                    console.print("[red]  · declined[/red]")
                elif preview:
                    console.print(f"[dim]    {preview}[/dim]")
            elif ev.kind == "error":
                console.print(f"[red]{ev.text}[/red]")
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

    else:
        console.print(f"[red]Unknown command {cmd}. /help for the list.[/red]")

    return True, agent


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
