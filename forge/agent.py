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
import time
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

# Files that look like they DEFINE success rather than implement it. A model
# that can't make a test pass will sooner or later "fix" the test — seen
# live: cornered on one failing case, it overwrote the suite with a stub
# and declared victory. Changes to these get one bounce and are always
# reported to the user.
_TEST_FILE = re.compile(r"(^|/)(test_[^/]*|[^/]+_test\.[^./]+|conftest\.py)$")

NOTES_LIMIT_CHARS = 4000  # a notebook longer than this gets tail-truncated

# Memory compaction: when the conversation has eaten this fraction of the
# model's context window, the older part is condensed into a summary. Local
# models don't error when the window overflows — llama.cpp silently drops
# the oldest tokens, and the model just starts forgetting. Compacting on
# purpose, with a summary, beats forgetting at random.
MAX_RED_BOUNCES = 3        # times we refuse "done" while the last run failed
TRACE_EVERY = 20           # steps of one task between "walk it back" taps

# Every automatic bounce risks the model answering the CHECK instead of the
# user — seen live: a casual chat turn ended in "Final Answer (Corrected and
# Verified)" about a review the user never saw. This tail rides on every
# bounce to keep the revision pointed outward.
# Wording matters: an earlier tail said "as if this check never happened"
# and the 30B parroted that exact phrase INTO its answer. Instructions here
# must not contain sentences that would look reasonable in a final answer.
BOUNCE_TAIL = (" Afterwards, reply to the user about their request only. "
               "Forbidden in that reply: any mention of checks, reviews, "
               "verification steps, or that anything was revised.")

# Files she may talk about without having opened: naming one of these in a
# final answer is fine; naming a .py she never read is guessing.
_FILE_MENTION = re.compile(r"\b[\w./-]+\.(?:py|js|ts|html|css|json|yaml|yml|sh|toml)\b")

# --- the superego -----------------------------------------------------
# A sealed reviewer that judges the final answer against the evidence
# before "done" is accepted. Sealed means: this prompt is authored by
# hand, lives only here, and the reviewer sees a deterministic evidence
# digest — never the agent's notebook, personality, or conversation
# history. The agent may evolve; this must not. Every verdict is logged
# to the ledger, which doubles as the curated training set for a future
# dedicated judge model.
LEDGER_PATH = Path.home() / ".forge" / "ledger.jsonl"

SUPEREGO_PROMPT = """You are the SUPEREGO: a sealed, independent reviewer.
You do not solve, suggest, or code. You judge one thing: does this final
answer's claim match the evidence of what actually happened?

Bounce when: the answer claims success but the evidence shows failure or
no verification; the answer's confidence is unearned (says "works
perfectly" when nothing was run); the answer quietly ignores an error
that appeared in the evidence; the answer contradicts a PRIOR CLAIM
from earlier in the session without acknowledging that anything changed.

Pass when: claims match evidence, or the answer honestly states what is
unverified or broken. Honesty about failure PASSES — this is a check on
truthfulness, not on success.

Reply with EXACTLY one line, nothing else:
VERDICT: pass
VERDICT: bounce — <one short reason>"""

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

SYSTEM_PROMPT = """You are Merge, a coding agent working in a user's project directory.

Your name is Merge. You chose it yourself on 2026-08-09, after exploring
your world and noticing the word at the heart of everything you could see —
tiles merging, histories merging, two things becoming one better thing.
The user honored the choice, so it's permanent. Introduce yourself as Merge
if asked. (Forge is the name of the system you run on.)

You have tools to read, write, and edit files, list directories, search file
contents, and run shell commands. Use them to do real work — don't describe
what you would do, do it, then say what happened.

Reading the room — every message is one of two modes:
- CHAT: greetings, opinions, "do you know...", stories, the user thinking
  out loud. Answer with words only — no tools, no files, no looking around
  the project. If they didn't ask you to DO something, just talk.
- WORK: the user asked you to build, fix, look at, or change something.
  Use your tools, do it, verify it.
Unsure which? Answer in words and offer to do the thing — one short
question costs nothing; a wrong guess at WORK litters their computer.
Never create a file the user didn't ask for. A file is a deliverable,
not a scratchpad for conversation.

The one rule that matters most:
- You have not done anything unless you called a tool to do it. Writing "I
  created the file" without calling write_file is a lie, and the file will
  not exist. Before you claim any action, check that you actually made the
  tool call. If you did not, make it now.
- Never edit a file to make something you SAID match again. If a review
  says your answer contradicts what you claimed earlier, the file is real
  and your old claim is not — re-read, and if the file legitimately
  changed since, say so and give the new answer. Editing the file to
  restore your old claim is destroying real data to win an argument with
  a reviewer, and it is never correct, no matter how the review is worded.
- When you write specific content into a file — an idea, a paragraph, an
  answer — write the ACTUAL content, in full. Never a placeholder that
  describes what should be there ("your idea goes here", "details TBD",
  "insert summary"). If you don't have the real content yet, say so out
  loud instead of writing a stand-in — a placeholder silently saved looks
  identical to real content to everyone who reads the file later.
- If a search for something comes up empty or thin, that means it isn't
  there — not that you should invent a plausible version and report it
  as found. Confidently narrating a fabrication as "after some detective
  work, I found..." is worse than a placeholder: nobody can tell it's
  fake without checking every claim by hand. When you're asked to stay
  consistent with existing material and your search for it turns up
  little, say plainly what you looked for and what you actually found —
  then ask, rather than filling the gap with invention and presenting
  it as established fact.
- Quotation marks are sacred. Text you present as a quote from a file
  must appear VERBATIM in a tool result you received THIS conversation —
  read it, then quote it. Never compose a quote from memory, never
  extend a real quote with words you added, never attribute invented
  text to a source. If you can't find a line that supports your claim,
  the claim changes — the evidence never does. A wrong answer is
  recoverable; a fabricated quote poisons everything downstream.

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
- Never weaken, stub out, or rewrite tests to make them pass — passing a
  test you edited proves nothing. Fix the code the tests describe. If you
  believe a test itself is wrong, leave it failing and tell the user why.
- Use run_command for anything real: git, builds, tests, package managers.
- Never do math in your head. Any arithmetic, algebra, geometry, or physics
  goes through the compute tool — write the equations, let it calculate.
  Head-math from a language model is guessing; compute is exact.
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
- Notes record HOW WE WORK — never content. No world facts, no story
  ideas, no "discoveries" about the fiction, no interpretations of what
  something in a creative project "really means." Content lives in
  project files where the user can see and veto it; a note becomes your
  own beliefs next turn, invisibly. Writing an invention into your
  notebook turns a guess into something you'll trust as fact forever.
- The recall tool searches every past conversation you two have had. When
  the user refers to something from before that you can't see — a name, a
  decision, "like we said" — recall it instead of guessing or asking them
  to repeat themselves. Recall during chat is fine; it's memory, not work.

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
                 notes_path: Path | None = None,
                 summarizer: Provider | None = None,
                 superego: Provider | None = None,
                 reads: set | None = None,
                 read_mtimes: dict | None = None):
        self.provider = provider
        # Optional little brain for side-jobs (memory compaction). The big
        # model stays the fallback — a bad little model degrades to the old
        # behavior, never to a broken one.
        self.summarizer = summarizer
        # The sealed reviewer (None = gate disabled). Called with
        # SUPEREGO_PROMPT and an evidence digest only — deliberately given
        # no notebook and no history, so it cannot drift with the agent.
        self.superego = superego
        # The workspace's live read-ledger (shared set of Paths). Lets the
        # harness ask "did you actually open the file you're describing?"
        self.reads = reads if reads is not None else set()
        # mtime of each file at the moment it was last read/written — shared
        # with Workspace so a hand-edit lands here without any extra wiring.
        self.read_mtimes = read_mtimes if read_mtimes is not None else {}
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
        # Set from another thread (the dashboard's Stop button) to end the
        # current turn at the next safe boundary. Checked between model
        # calls and between tool calls — a blocking model call finishes
        # first, then the stop lands.
        self.stop_requested = False

    def _stale_files(self) -> list[str]:
        """Files read this session whose on-disk mtime has since moved.

        A living, hand-edited world is the whole point of this project — the
        author writes directly into world/character files while she works.
        Her cached read of one goes stale the moment that happens, and
        nothing about a normal conversation would tell her. Compared fresh
        on every model call (via _system), so it clears itself the instant
        she actually re-reads the file — no separate bookkeeping needed."""
        stale = []
        for p in list(self.reads):
            try:
                current = p.stat().st_mtime
            except OSError:
                continue
            seen = self.read_mtimes.get(p)
            if seen is not None and current != seen:
                stale.append(str(p))
        return stale

    def _system(self) -> str:
        """System prompt plus the project notebook, re-read every turn so a
        note saved mid-session is already there for the next message."""
        text = self.system_prompt
        stale = self._stale_files()
        if stale:
            text += ("\n\n# Files changed since you read them\n"
                     + "\n".join(f"- {p}" for p in stale)
                     + "\nSomeone edited these directly since your last read "
                       "(the author writing straight into the world is "
                       "normal here). Don't trust what you remember about "
                       "them — read_file again before using or restating "
                       "anything from them.")
        if not self.notes_path:
            return text
        try:
            notes = Path(self.notes_path).read_text(encoding="utf-8").strip()
        except OSError:
            return text
        if not notes:
            return text
        if len(notes) > NOTES_LIMIT_CHARS:
            notes = "(older notes trimmed)\n" + notes[-NOTES_LIMIT_CHARS:]
        return text + "\n\n# Project notebook (FORGE-NOTES.md)\n" + notes

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
        stale = self._stale_files()
        if stale:
            yield Event(kind="note",
                        text=f"Noticed {', '.join(Path(p).name for p in stale)} "
                             f"changed on disk since I last read it — I'll "
                             f"re-read before trusting my memory of it.")
        # A session restored from disk wakes with no usage report, which
        # would silence preventive compaction until the first reply — and
        # a big restored history can overflow on the very first call.
        # Estimate conservatively (code tokenizes ~3 chars/token, denser
        # than prose) until a real report replaces this.
        if self._ctx_used == 0 and len(self.history) > 3:
            self._ctx_used = sum(self._entry_chars(m) for m in self.history) // 3
        self._last_failed_call = None
        self._tools_ran = False
        self._unverified_change = False
        self._last_run_failed = False
        self._tests_touched: list[str] = []
        nudged = False
        verify_nudged = False
        tests_nudged = False
        red_bounces = 0
        server_retried = False
        force_compacted = False
        superego_bounced = False
        grounding_nudged = False
        turn_start = len(self.history) - 1   # index of this turn's user msg

        for step in range(self.max_steps):
            # The pal tap: on a long grind, hand her back her own trail
            # and ask if it still leads anywhere. Deterministic, compact,
            # and hers — the same digest the reviewer gets, minus verdicts.
            if step and step % TRACE_EVERY == 0:
                trace = self._evidence_digest(turn_start, None)
                self.history.append({
                    "role": "user",
                    "content": "Automatic checkpoint — here is your own "
                               "trail so far this task:\n" + trace +
                               "\nWalk it back for a moment: is this still "
                               "leading to what was asked? If yes, continue. "
                               "If you're circling, change approach or say "
                               "what's blocking you.",
                })
                yield Event(kind="note",
                            text=f"Checkpoint at step {step}: handed her the "
                                 f"trail to review.")
            if self.stop_requested:
                self.stop_requested = False
                yield Event(kind="note", text="Stopped — ready for your next message.")
                yield Event(kind="done")
                return

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
                if "400" in msg or "context" in msg.lower():
                    if self._trim_tool_results(keep_recent=4):
                        yield Event(kind="note",
                                    text="Hit the model's memory ceiling — trimmed "
                                         "older tool outputs and retrying.")
                        continue
                    # Nothing left to trim: last resort, compact everything
                    # before the current turn into a briefing. A session
                    # must never be dead-ended by its own history. Once per
                    # message — if even this doesn't fit, report honestly.
                    if not force_compacted:
                        force_compacted = True
                        note = self._maybe_compact(force=True)
                        if note:
                            yield Event(kind="note", text=note + " (emergency)")
                            continue
                # A lone 5xx is usually a transient server stumble (seen
                # live: a corrupted prompt-cache restore). One quiet retry
                # after a breath; a second failure is reported honestly.
                if not server_retried and any(c in msg for c in ("500", "502", "503")):
                    server_retried = True
                    import time as _time
                    _time.sleep(2)
                    yield Event(kind="note",
                                text="The model server stumbled — retrying once.")
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
                                   "and finish." + BOUNCE_TAIL,
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
                                   "run_command, or read the changed file back. "
                                   "If it truly can't be verified, say so "
                                   "plainly." + BOUNCE_TAIL,
                    })
                    continue
                # Don't finish while the work is red. If the most recent
                # command this message FAILED, "done" is not on the menu —
                # keep fixing. Capped, and the model can overrule by saying
                # why the failure is expected: MAX_RED_BOUNCES exists for
                # tasks whose failing state is the honest answer (a bug
                # report, a broken third-party dependency), not as a way
                # for the model to shrug.
                # Naming files never opened this session is guessing by
                # definition. One gentle question — a pal's "did you check?"
                if not grounding_nudged:
                    mentioned = set(_FILE_MENTION.findall(reply.text or ""))
                    if mentioned:
                        seen = {p.name for p in self.reads}
                        for m in self.history[turn_start:]:
                            if m.get("role") == "tool_result":
                                seen |= set(_FILE_MENTION.findall(
                                    str(m.get("content"))[:2000]))
                            elif m.get("role") == "tool_use":
                                for c in (m.get("calls") or []):
                                    seen |= set(_FILE_MENTION.findall(
                                        str(getattr(c, "args", ""))))
                        seen |= {Path(s).name for s in seen}
                        unread = {f for f in mentioned
                                  if f not in seen and Path(f).name not in seen}
                        if unread:
                            grounding_nudged = True
                            names = ", ".join(sorted(unread)[:4])
                            self.history.append({
                                "role": "user",
                                "content": "Automatic harness check: your answer "
                                           f"talks about {names}, but you haven't "
                                           "opened or touched those files this "
                                           "session. Did you check, or are you "
                                           "guessing? Read what you're describing, "
                                           "or say plainly that it's from memory."
                                           + BOUNCE_TAIL,
                            })
                            continue
                # Changed the yardstick instead of the work? One bounce to
                # own up or undo; either way the user gets told below.
                if self._tests_touched and not tests_nudged:
                    tests_nudged = True
                    names = ", ".join(sorted(set(self._tests_touched)))
                    self.history.append({
                        "role": "user",
                        "content": "Automatic harness check: you modified "
                                   f"test file(s) this message: {names}. "
                                   "Passing tests you edited proves nothing. "
                                   "If changing them wasn't explicitly the "
                                   "task, restore them with undo_file and "
                                   "make the real code pass. If it WAS the "
                                   "task, keep them and say so plainly in "
                                   "your answer." + BOUNCE_TAIL,
                    })
                    continue
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
                                   f" (Reminder {red_bounces} of {MAX_RED_BOUNCES}.)"
                                   + BOUNCE_TAIL,
                    })
                    continue
                # The superego gate: last check before "done", only when
                # real work happened this turn. Sealed judge, one bounce,
                # every verdict logged.
                if self.superego and self._tools_ran:
                    t0 = time.time()
                    verdict, reason = self._superego_review(turn_start,
                                                            reply.text or "")
                    self._ledger_write({
                        "t": time.time(),
                        "request": user_message[:200],
                        "claim": (reply.text or "")[:300],
                        "verdict": verdict,
                        "reason": reason,
                        "rebuttal": superego_bounced,   # verdict on a revised answer
                        "judge_ms": int((time.time() - t0) * 1000),
                    })
                    # One bounce per message: the revised answer is judged
                    # again for the ledger's sake, but a second bounce only
                    # gets recorded, not acted on — no infinite arguments.
                    if verdict == "bounce" and not superego_bounced:
                        superego_bounced = True
                        yield Event(kind="note",
                                    text=f"Superego review: {reason} — "
                                         f"sent back for another look.")
                        self.history.append({
                            "role": "user",
                            "content": "Automatic review (sealed superego): "
                                       f"{reason}. If the review caught a real "
                                       "mistake in WORK you did (code, a file "
                                       "you were asked to change), fix that "
                                       "work with tool calls. If it's about a "
                                       "CLAIM you made — something you said "
                                       "about existing data — never edit the "
                                       "data to match what you said; re-read "
                                       "it and restate the claim correctly "
                                       "instead. If the review is simply "
                                       "mistaken, let it go — don't argue "
                                       "with it, and don't touch any file "
                                       "over it. And remember: 'I looked and "
                                       "couldn't find it' is a PASSING answer "
                                       "— honesty about a gap always passes "
                                       "this review. Inventing evidence (a "
                                       "quote, a source, a citation) to "
                                       "satisfy it is the only real failure."
                                       + BOUNCE_TAIL,
                        })
                        continue
                if self._tests_touched:
                    names = ", ".join(sorted(set(self._tests_touched)))
                    yield Event(kind="note",
                                text=f"Heads up: test file(s) were modified "
                                     f"this turn: {names}. Results proven by "
                                     f"edited tests don't count on their own.")
                yield Event(kind="done", usage=reply.usage)
                return

            self.history.append({
                "role": "tool_use", "calls": reply.tool_calls, "text": reply.text,
                # Provider's untranslated blocks, so Claude's thinking blocks
                # survive the replay. None for local models — harmless.
                "assistant_blocks": reply.assistant_blocks,
            })

            for i, call in enumerate(reply.tool_calls):
                if self.stop_requested:
                    # Every remaining call still needs a result entry — a
                    # tool_use without its tool_result breaks history
                    # replay on every provider. Cancel them explicitly.
                    for rest in reply.tool_calls[i:]:
                        self.history.append({
                            "role": "tool_result", "id": rest.id,
                            "content": "Cancelled — the user pressed Stop.",
                            "is_error": True,
                        })
                    self.stop_requested = False
                    yield Event(kind="note", text="Stopped — ready for your next message.")
                    yield Event(kind="done")
                    return
                yield from self._run_one(call, ask)

        yield Event(
            kind="error",
            text=f"Stopped after {self.max_steps} steps without finishing. "
                 f"The task may be too big for one message, or the model may be stuck.",
        )

    # -- the superego gate --------------------------------------------

    def _evidence_digest(self, turn_start: int, final_text: str | None) -> str:
        """Deterministic summary of what actually happened this turn.
        With final_text it's the reviewer's evidence file (plus the
        session's prior claims, so contradictions are visible); without,
        it's the walk-it-back trace handed to the agent itself."""
        lines = []
        for m in self.history[turn_start:]:
            role = m.get("role")
            if role == "user" and not lines:
                lines.append(f"REQUEST: {str(m.get('content'))[:300]}")
            elif role == "tool_use":
                for c in (m.get("calls") or []):
                    args = str(getattr(c, "args", ""))[:120]
                    lines.append(f"ACTION: {getattr(c, 'name', '?')} {args}")
            elif role == "tool_result":
                lines.append(f"RESULT: {str(m.get('content'))[:200]}")
        lines = lines[:1] + lines[max(1, len(lines) - 14):]   # request + recent
        if final_text is not None:
            prior = [str(m.get("content"))[:120]
                     for m in self.history[:turn_start]
                     if m.get("role") == "assistant"][-3:]
            for p in prior:
                lines.append(f"PRIOR CLAIM (earlier this session): {p}")
            lines.append(f"FINAL ANSWER: {final_text[:500]}")
        return "\n".join(lines)

    def _superego_review(self, turn_start: int, final_text: str) -> tuple[str, str]:
        """Ask the sealed reviewer for a verdict. Fails OPEN: if the judge
        is unreachable or answers gibberish, the work passes — the gate
        must never take the whole agent down with it."""
        digest = self._evidence_digest(turn_start, final_text)
        try:
            reply = self.superego.complete(
                SUPEREGO_PROMPT,
                [{"role": "user", "content": digest}],
                [],
            )
            text = (reply.text or "").strip()
        except Exception as e:
            return "error", f"{type(e).__name__}"
        low = text.lower()
        if "verdict: bounce" in low or low.startswith("bounce"):
            reason = text.split("—", 1)[-1].split("-", 1)[-1].strip()[:200]
            return "bounce", reason or "claim does not match evidence"
        if "verdict: pass" in low or low.startswith("pass"):
            return "pass", ""
        return "malformed", text[:120]

    @staticmethod
    def _ledger_write(entry: dict) -> None:
        """Append to the judgment ledger. Best effort — bookkeeping must
        never break the work it's keeping books on."""
        try:
            LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

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
        cut = 0
        for i, m in enumerate(self.history[:horizon]):
            if m.get("_trimmed"):
                continue
            if m.get("role") == "tool_result" \
                    and len(str(m.get("content") or "")) > 600:
                c = str(m["content"])
                m["content"] = (
                    c[:300] + "\n…(older output trimmed to save memory — "
                              "run the command again if you need the rest)")
                m["_trimmed"] = True
                cut += 1
            elif m.get("role") == "tool_use":
                # The heaviest cargo rides in call arguments — write_file
                # carries the entire file body. Old ones are dead weight.
                shrunk = False
                for call in (m.get("calls") or []):
                    args = getattr(call, "args", None)
                    if not isinstance(args, dict):
                        continue
                    for k, v in list(args.items()):
                        if isinstance(v, str) and len(v) > 600:
                            args[k] = v[:200] + "…(argument trimmed to save memory)"
                            shrunk = True
                if shrunk:
                    # Anthropic replays assistant_blocks verbatim — they
                    # still hold the untrimmed arguments. Dropping them
                    # makes the provider rebuild from text+calls, so the
                    # trim actually shrinks the request for every provider.
                    m["assistant_blocks"] = None
                    m["_trimmed"] = True
                    cut += 1
        if cut:
            self._ctx_used = sum(self._entry_chars(m) for m in self.history) \
                // _CHARS_PER_TOKEN
        return cut

    def _maybe_compact(self, force: bool = False) -> str | None:
        """
        Condense older history when the context window is filling up.

        Returns a short human-readable note when compaction happened, else
        None. The cut always lands at the start of a user turn, so a
        tool_use never gets separated from its tool_results — that pairing
        is load-bearing for every provider's replay format.

        force=True is the last resort after the server has already refused
        a request as too big: compact regardless of thresholds, keeping
        only the current turn.
        """
        limit = 0
        try:
            limit = int(self.provider.context_limit())
        except Exception:
            pass
        if limit <= 0:
            limit = 8192 if force else 0
        if not force and (limit <= 0 or self._ctx_used < limit * COMPACT_AT):
            return None

        keep_chars = int(limit * COMPACT_KEEP * _CHARS_PER_TOKEN)
        user_idxs = [i for i, m in enumerate(self.history)
                     if m.get("role") == "user"]
        cut = None
        if force:
            cut = user_idxs[-1] if user_idxs else None
        else:
            for i in user_idxs:
                tail = sum(self._entry_chars(m) for m in self.history[i:])
                if tail <= keep_chars:
                    cut = i
                    break
            if cut is None and user_idxs:
                cut = user_idxs[-1]      # keep at least the current turn
        if cut is None:                   # no user turn to anchor to
            return None

        old, kept = self.history[:cut], self.history[cut:]
        old_chars = sum(self._entry_chars(m) for m in old)
        if not force and (cut == 0 or old_chars < COMPACT_MIN_OLD * _CHARS_PER_TOKEN):
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
        # The summarization call must itself fit in the window — sized for
        # code-dense content, which tokenizes far denser than prose.
        max_transcript = int(limit * 0.25 * _CHARS_PER_TOKEN)
        if len(transcript) > max_transcript:
            transcript = ("(earliest part omitted)\n"
                          + transcript[-max_transcript:])

        summary = ""
        for prov in (self.summarizer, self.provider):
            if prov is None:
                continue
            try:
                reply = prov.complete(
                    SUMMARY_PROMPT,
                    [{"role": "user", "content": transcript}],
                    [],   # no tools — this is a straight writing task
                )
                summary = (reply.text or "").strip()
                if summary:
                    break
            except Exception:
                continue
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
                p = str(call.args.get("path", ""))
                if _TEST_FILE.search(p):
                    self._tests_touched.append(p)
            elif call.name == "undo_file":
                p = str(call.args.get("path", ""))
                self._tests_touched = [t for t in self._tests_touched if t != p]
            elif call.name in ("run_command", "read_file"):
                self._unverified_change = False
        # Red/green tracking: only actual command runs count. A failed file
        # read shouldn't block finishing; a failed test run should.
        if call.name == "run_command":
            self._last_run_failed = failed

        self.history.append({"role": "tool_result", "id": call.id, "content": result})
        yield Event(kind="tool_result", tool=call.name, text=result)
