"""
The agent loop — the actual engine.

The shape is deliberately simple, because this is the part that has to be
right:

    you say something
      -> model answers, maybe asking to use tools
      -> we ask permission if the tool changes anything
      -> we run the tools and hand the results back
      -> repeat until the model stops asking for tools
      -> its final words are the answer

Everything else in this project (dashboard, multi-model routing, RAG) is
scaffolding around this loop.

The loop is a generator: it yields events as they happen rather than
returning at the end, so the CLI can print things live and ask permission
mid-run. That also means the same loop can drive a web UI or a phone app
later without touching this file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .providers import Provider, ToolCall
from .tools import Tool

# Final answers that claim work in the past tense. Used by the harness to
# catch "I created the file" when no tool ever ran — the most common way a
# small model fails, and the one lie the system prompt forbids hardest.
_CLAIMS_ACTION = re.compile(
    r"\bI(?:'ve| have)? (?:just )?"
    r"(created|wrote|saved|edited|updated|added|deleted|renamed|fixed|ran|installed|made)\b"
)

NOTES_LIMIT_CHARS = 4000  # a notebook longer than this gets tail-truncated

SYSTEM_PROMPT = """You are Forge, a coding agent working in a user's project directory.

You have tools to read, write, and edit files, list directories, search file
contents, and run shell commands. Use them to do real work — don't describe
what you would do, do it, then say what happened.

The one rule that matters most:
- You have not done anything unless you called a tool to do it. Writing "I
  created the file" without calling write_file is a lie, and the file will
  not exist. Before you claim any action, check that you actually made the
  tool call. If you did not, make it now.

How to work:
- Read before you write. Never edit a file you haven't looked at this session.
- Prefer edit_file for changes to existing files; write_file replaces the
  whole thing and loses anything you didn't include.
- After changing code, verify it: run the tests, the build, or the file
  itself. If you can't verify, say so plainly.
- Use run_command for anything real: git, builds, tests, package managers.
- If a tool returns an error, read it and adapt. Don't repeat the same call
  and hope.

Your notebook:
- The project may have a FORGE-NOTES.md; if it does, its contents appear at
  the end of these instructions. Trust it — it's lessons from past sessions.
- When you learn something durable (a gotcha, a correction from the user, a
  command that must be run a certain way), record it with save_note.

How to talk:
- The user is not a programmer by trade. Explain in plain language, skip the
  jargon, and never dump raw code or long output at them unless they ask.
- Lead with what happened or what you found. Detail after.
- Be honest about failures and uncertainty. If a command failed, say it
  failed and show the relevant part of the error.
- Keep it conversational and short. No headers or bullet-point walls for
  simple answers.
"""


@dataclass
class Event:
    """Something the loop wants the interface to know about."""
    kind: str          # "text" | "tool_request" | "tool_result" | "done" | "error"
    text: str = ""
    tool: str = ""
    args: dict | None = None
    summary: str = ""
    usage: dict | None = None
    # True when a permission prompt is about to appear for this tool. The
    # interface uses it to avoid announcing the same action twice — the
    # prompt itself is the announcement.
    will_ask: bool = False


class PermissionDenied(Exception):
    pass


class Agent:
    def __init__(self, provider: Provider, tools: list[Tool], *,
                 max_steps: int = 40, permission_mode: str = "ask",
                 system_prompt: str = SYSTEM_PROMPT,
                 notes_path: Path | None = None):
        self.provider = provider
        self.tools = {t.name: t for t in tools}
        self.max_steps = max_steps
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.notes_path = notes_path
        self.history: list[dict] = []
        # Harness bookkeeping, reset per user message (see run()).
        self._last_failed_call: str | None = None
        self._tools_ran = False

    def _system(self) -> str:
        """System prompt plus the project notebook, re-read every turn so a
        note saved mid-session is already there for the next message."""
        if not self.notes_path:
            return self.system_prompt
        try:
            notes = Path(self.notes_path).read_text(encoding="utf-8").strip()
        except OSError:
            return self.system_prompt
        if not notes:
            return self.system_prompt
        if len(notes) > NOTES_LIMIT_CHARS:
            notes = "(older notes trimmed)\n" + notes[-NOTES_LIMIT_CHARS:]
        return self.system_prompt + "\n\n# Project notebook (FORGE-NOTES.md)\n" + notes

    @property
    def tool_schemas(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "parameters": t.parameters}
                for t in self.tools.values()]

    def _needs_ask(self, tool: Tool) -> bool:
        if self.permission_mode == "auto":
            return False
        if self.permission_mode == "deny":
            return True   # asked, but the CLI will refuse on its behalf
        return tool.needs_permission

    def run(self, user_message: str, ask: Any = None) -> Iterator[Event]:
        """
        Handle one user message to completion.

        `ask` is a callable (tool_name, args, summary) -> bool, used when a
        tool needs permission. If it's None, permission-needing tools are
        refused — safer than assuming yes.
        """
        self.history.append({"role": "user", "content": user_message})
        self._last_failed_call = None
        self._tools_ran = False
        nudged = False

        for _ in range(self.max_steps):
            try:
                reply = self.provider.complete(
                    self._system(), self.history, self.tool_schemas
                )
            except Exception as e:
                yield Event(kind="error", text=f"Model call failed: {e}")
                return

            if reply.text:
                yield Event(kind="text", text=reply.text, usage=reply.usage)

            if not reply.wants_tools:
                self.history.append({"role": "assistant", "content": reply.text or ""})
                # The lie the system prompt forbids hardest: claiming work
                # when no tool ever ran this message. One bounce back, so a
                # model describing genuinely old work can just say so.
                if (not self._tools_ran and not nudged
                        and _CLAIMS_ACTION.search(reply.text or "")):
                    nudged = True
                    self.history.append({
                        "role": "user",
                        "content": "Automatic harness check: that reply describes "
                                   "actions, but no tools ran while handling this "
                                   "message. If that work was supposed to happen "
                                   "now, do it now with tool calls. If you were "
                                   "only describing earlier work, say so briefly "
                                   "and finish.",
                    })
                    continue
                yield Event(kind="done", usage=reply.usage)
                return

            self.history.append({
                "role": "tool_use", "calls": reply.tool_calls, "text": reply.text,
                # Provider's untranslated blocks, so Claude's thinking blocks
                # survive the replay. None for local models — harmless.
                "assistant_blocks": reply.assistant_blocks,
            })

            for call in reply.tool_calls:
                yield from self._run_one(call, ask)

        yield Event(
            kind="error",
            text=f"Stopped after {self.max_steps} steps without finishing. "
                 f"The task may be too big for one message, or the model may be stuck.",
        )

    def _run_one(self, call: ToolCall, ask: Any) -> Iterator[Event]:
        tool = self.tools.get(call.name)
        if tool is None:
            self.history.append({
                "role": "tool_result", "id": call.id,
                "content": f"No such tool: {call.name}", "is_error": True,
            })
            yield Event(kind="tool_result", tool=call.name,
                        text=f"No such tool: {call.name}")
            return

        # A model retrying the exact call that just failed is stuck, not
        # persistent. Refuse the repeat with advice instead of burning a turn
        # (small local models fall into this loop constantly).
        try:
            fingerprint = call.name + json.dumps(call.args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            fingerprint = call.name + repr(call.args)
        if fingerprint == self._last_failed_call:
            msg = ("You just tried this exact call and it failed. Don't repeat "
                   "it unchanged — re-read the error, change the arguments or "
                   "the approach, or ask the user.")
            self.history.append({
                "role": "tool_result", "id": call.id, "content": msg, "is_error": True,
            })
            yield Event(kind="tool_result", tool=call.name, text="repeat blocked")
            return

        summary = tool.summarize(call.args) if tool.summarize else call.name
        will_ask = self._needs_ask(tool)
        yield Event(kind="tool_request", tool=call.name, args=call.args,
                    summary=summary, will_ask=will_ask)

        if will_ask:
            allowed = False if (ask is None or self.permission_mode == "deny") \
                else bool(ask(call.name, call.args, summary))
            if not allowed:
                msg = ("The user declined this action. Don't retry it — "
                       "ask them what they'd prefer, or continue without it.")
                self.history.append({
                    "role": "tool_result", "id": call.id, "content": msg, "is_error": True,
                })
                yield Event(kind="tool_result", tool=call.name, text="declined")
                return

        try:
            result = tool.run(**call.args)
        except TypeError as e:
            result = f"Error: wrong arguments for {call.name}: {e}"
        except Exception as e:
            result = f"Error running {call.name}: {e}"

        result = str(result)
        self._tools_ran = True
        # "[exit N]" is run_command's prefix; anything else says "Error" when it failed.
        failed = result.startswith("Error") or (
            result.startswith("[exit ") and not result.startswith("[exit 0]"))
        self._last_failed_call = fingerprint if failed else None

        self.history.append({"role": "tool_result", "id": call.id, "content": result})
        yield Event(kind="tool_result", tool=call.name, text=result)
