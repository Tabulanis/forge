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

# Memory compaction: when the conversation has eaten this fraction of the
# model's context window, the older part is condensed into a summary. Local
# models don't error when the window overflows — llama.cpp silently drops
# the oldest tokens, and the model just starts forgetting. Compacting on
# purpose, with a summary, beats forgetting at random.
MAX_RED_BOUNCES = 3        # times we refuse "done" while the last run failed

COMPACT_AT = 0.70          # start compacting at 70% full
COMPACT_KEEP = 0.25        # after compacting, recent turns may fill 25%
# Don't compact unless the part being summarized is at least this many
# tokens. When one long tool-heavy turn fills the window by itself, the
# compactable prefix shrinks to almost nothing — squeezing it again every
# step costs a model call each time and frees nothing.
COMPACT_MIN_OLD = 512
_CHARS_PER_TOKEN = 4       # rough estimate for sizing the kept tail

SUMMARY_PROMPT = """You are condensing an agent work session to free memory.
Write a compact briefing the agent can work from, covering: what the user
wants overall; decisions made; files created or changed and their current
state; what was tried and failed; anything the user corrected or insisted on;
what remains to be done. Concrete names and paths matter, prose style does
not. 300 words maximum. Reply with the briefing only."""

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
- Don't stop while it's broken. If a run fails, read the error, fix the
  cause, run again — repeat until it passes or you can say precisely why
  the failure is expected. Finishing with a known failure and no
  explanation is not an option.
- Use run_command for anything real: git, builds, tests, package managers.
- run_command has no screen or keyboard. Interactive or full-screen
  programs (games, editors, TUIs) will fail with terminal errors there —
  that's the sandbox, not a bug in the code. Verify them another way and
  tell the user how to launch them in a real terminal.
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
                 max_steps: int = 80, permission_mode: str = "ask",
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
        # Tokens the whole conversation occupied at the last model call,
        # straight from the provider's usage report — not an estimate.
        self._ctx_used = 0

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
            return tool.always_ask
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
        self._unverified_change = False
        self._last_run_failed = False
        nudged = False
        verify_nudged = False
        red_bounces = 0

        for _ in range(self.max_steps):
            note = self._maybe_compact()
            if note:
                yield Event(kind="note", text=note)

            try:
                reply = self.provider.complete(
                    self._system(), self.history, self.tool_schemas
                )
            except Exception as e:
                # A request bigger than the model's window comes back as a
                # 400. That's recoverable: shed old tool output and go
                # again. Only a trim that actually cut something earns a
                # retry, so an unrelated 400 still surfaces as an error.
                msg = str(e)
                if ("400" in msg or "context" in msg.lower()) \
                        and self._trim_tool_results(keep_recent=4):
                    yield Event(kind="note",
                                text="Hit the model's memory ceiling — trimmed "
                                     "older tool outputs and retrying.")
                    continue
                yield Event(kind="error", text=f"Model call failed: {e}")
                return

            u = reply.usage or {}
            used = (u.get("prompt_tokens") or u.get("input_tokens") or 0) \
                 + (u.get("completion_tokens") or u.get("output_tokens") or 0)
            if used:
                self._ctx_used = used

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
                # Changed files but never checked the result? One bounce:
                # run it, test it, or read it back before calling it done.
                if self._unverified_change and not verify_nudged:
                    verify_nudged = True
                    self.history.append({
                        "role": "user",
                        "content": "Automatic harness check: you changed files "
                                   "this message but never verified the result. "
                                   "Verify now — run the code or tests with "
                                   "run_command, or read the changed file back — "
                                   "then give your final answer. If it truly "
                                   "can't be verified, say so plainly.",
                    })
                    continue
                # Don't finish while the work is red. If the most recent
                # command this message FAILED, "done" is not on the menu —
                # keep fixing. Capped, and the model can overrule by saying
                # why the failure is expected: MAX_RED_BOUNCES exists for
                # tasks whose failing state is the honest answer (a bug
                # report, a broken third-party dependency), not as a way
                # for the model to shrug.
                if self._last_run_failed and red_bounces < MAX_RED_BOUNCES:
                    red_bounces += 1
                    self.history.append({
                        "role": "user",
                        "content": "Automatic harness check: the most recent "
                                   "command you ran FAILED, and you're about "
                                   "to finish anyway. Don't stop while it's "
                                   "broken — read the error, fix the cause, "
                                   "and run it again until it passes. If the "
                                   "failure is genuinely expected or outside "
                                   "this task, say exactly why in your answer."
                                   f" (Reminder {red_bounces} of {MAX_RED_BOUNCES}.)",
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

    # -- memory compaction --------------------------------------------

    @staticmethod
    def _entry_chars(m: dict) -> int:
        """Rough size of one history entry, for choosing where to cut."""
        if m.get("role") == "tool_use":
            calls = m.get("calls") or []
            args = "".join(str(getattr(c, "args", "")) for c in calls)
            return len(m.get("text") or "") + len(args) + 40 * len(calls)
        return len(str(m.get("content") or ""))

    def _trim_tool_results(self, keep_recent: int = 8) -> int:
        """
        Emergency valve for a single turn that outgrows the window: truncate
        the bodies of older tool outputs in place. Pairing stays intact —
        every tool_use keeps its tool_result, just shorter — so any
        provider's replay format survives. Recent entries are left alone;
        they're what the model is actively working from.

        Returns how many outputs were cut.
        """
        horizon = max(0, len(self.history) - keep_recent)
        idxs = [i for i, m in enumerate(self.history[:horizon])
                if m.get("role") == "tool_result"
                and not m.get("_trimmed")
                and len(str(m.get("content") or "")) > 600]
        for i in idxs:
            c = str(self.history[i]["content"])
            self.history[i]["content"] = (
                c[:300] + "\n…(older output trimmed to save memory — "
                          "run the command again if you need the rest)")
            self.history[i]["_trimmed"] = True
        if idxs:
            self._ctx_used = sum(self._entry_chars(m) for m in self.history) \
                // _CHARS_PER_TOKEN
        return len(idxs)

    def _maybe_compact(self) -> str | None:
        """
        Condense older history when the context window is filling up.

        Returns a short human-readable note when compaction happened, else
        None. The cut always lands at the start of a user turn, so a
        tool_use never gets separated from its tool_results — that pairing
        is load-bearing for every provider's replay format.
        """
        limit = 0
        try:
            limit = int(self.provider.context_limit())
        except Exception:
            pass
        if limit <= 0 or self._ctx_used < limit * COMPACT_AT:
            return None

        keep_chars = int(limit * COMPACT_KEEP * _CHARS_PER_TOKEN)
        user_idxs = [i for i, m in enumerate(self.history)
                     if m.get("role") == "user"]
        cut = None
        for i in user_idxs:
            tail = sum(self._entry_chars(m) for m in self.history[i:])
            if tail <= keep_chars:
                cut = i
                break
        if cut is None and user_idxs:
            cut = user_idxs[-1]          # keep at least the current turn
        if cut is None:                   # no user turn to anchor to
            return None

        old, kept = self.history[:cut], self.history[cut:]
        old_chars = sum(self._entry_chars(m) for m in old)
        if cut == 0 or old_chars < COMPACT_MIN_OLD * _CHARS_PER_TOKEN:
            # Nothing meaningful before the current turn — it's one long
            # task filling the window by itself. Shrink its older tool
            # outputs instead of summarizing a prefix that's already tiny.
            cut_n = self._trim_tool_results()
            if cut_n:
                return (f"One long task filled the memory — trimmed "
                        f"{cut_n} older tool output(s) to make room.")
            return None

        lines = []
        for m in old:
            role = m.get("role")
            if role == "user":
                lines.append(f"User: {m.get('content','')}")
            elif role == "assistant":
                lines.append(f"Agent: {m.get('content','')}")
            elif role == "tool_use":
                for c in (m.get("calls") or []):
                    lines.append(f"Agent ran {getattr(c, 'name', '?')}"
                                 f"({str(getattr(c, 'args', ''))[:200]})")
            elif role == "tool_result":
                lines.append(f"  -> {str(m.get('content',''))[:400]}")
        transcript = "\n".join(lines)
        # The summarization call must itself fit in the window.
        max_transcript = int(limit * 0.5 * _CHARS_PER_TOKEN)
        if len(transcript) > max_transcript:
            transcript = ("(earliest part omitted)\n"
                          + transcript[-max_transcript:])

        try:
            reply = self.provider.complete(
                SUMMARY_PROMPT,
                [{"role": "user", "content": transcript}],
                [],   # no tools — this is a straight writing task
            )
            summary = (reply.text or "").strip()
        except Exception:
            summary = ""
        if not summary:
            summary = ("(The summary could not be produced; earlier details "
                       "were dropped to free memory. Re-read files rather "
                       "than trusting recollection.)")

        self.history = [
            {"role": "user",
             "content": "[Automatic memory note: the conversation was getting "
                        "too long, so everything before this point was "
                        "condensed into this briefing.]\n\n" + summary},
            {"role": "assistant",
             "content": "Understood — continuing from that briefing."},
        ] + kept
        # Real usage comes back with the next model call; until then a
        # rough estimate keeps us from immediately re-triggering.
        self._ctx_used = sum(self._entry_chars(m) for m in self.history) \
            // _CHARS_PER_TOKEN
        return (f"Memory was {int(100 * COMPACT_AT)}% full — condensed the "
                f"earlier conversation into a briefing so nothing degrades.")

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
                # A decline counts as a failure for the broken-record guard:
                # asking again with the same arguments must not restart a
                # long permission wait.
                self._last_failed_call = fingerprint
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

        # Verification tracking: a successful write/edit sets the flag; any
        # later successful run or read-back clears it.
        if not failed:
            if call.name in ("write_file", "edit_file"):
                self._unverified_change = True
            elif call.name in ("run_command", "read_file"):
                self._unverified_change = False
        # Red/green tracking: only actual command runs count. A failed file
        # read shouldn't block finishing; a failed test run should.
        if call.name == "run_command":
            self._last_run_failed = failed

        self.history.append({"role": "tool_result", "id": call.id, "content": result})
        yield Event(kind="tool_result", tool=call.name, text=result)
