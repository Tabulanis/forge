"""
Conversations that outlive a single HTTP request.

The CLI keeps one Agent alive in a loop and asks permission by blocking on
input(). A browser can't do that — requests come and go, and the phone that
started a task might be in someone's pocket when the agent needs an answer.

So a session here owns:
  * an Agent (and therefore the conversation history)
  * the workspace it's working in
  * a queue of events the browser is streaming
  * a pending-permission slot the agent thread blocks on until the browser
    answers, or until it times out

The permission round-trip is the interesting part. The agent runs on its own
thread; when it wants to write a file it drops a request into `pending` and
waits on an Event. The browser sees a "permission" event in its stream, shows
buttons, and POSTs the answer, which sets the Event and unblocks the agent.
If nobody answers, it times out as a refusal — a phone left face-down should
never leave a half-finished edit hanging forever.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import park, recall
from .agent import Agent
from .config import active_model_config, load_config
from .media import load_media_config
from .modes import get_privacy
from .providers import ToolCall, build_provider
from .projecttools import load as _load_project_tools
from .tools import Workspace, build_media_tools, build_tools

# How long the agent waits for a human to tap allow/deny before giving up.
PERMISSION_TIMEOUT = 300

# Conversations live here and survive restarts. One JSON file per session,
# written after every completed turn; the newest KEEP_SESSIONS are kept.
SESS_DIR = Path.home() / ".forge" / "sessions"
KEEP_SESSIONS = 20


def _jsonable_blocks(blocks):
    """Provider-native assistant blocks → plain data.

    Anthropic's SDK objects (thinking blocks included) all model_dump() to
    dicts, and the API accepts those dicts back verbatim on replay — so a
    Claude session survives a restart with its thinking chain intact.
    Anything unserializable is dropped rather than poisoning the save; the
    provider rebuilds history from text + calls in that case, which is the
    same degradation as switching models mid-session.
    """
    if blocks is None:
        return None
    out = []
    for b in blocks:
        if isinstance(b, dict):
            out.append(b)
        elif hasattr(b, "model_dump"):
            try:
                out.append(b.model_dump())
            except Exception:
                return None
        else:
            return None
    return out


def _ser_entry(m: dict) -> dict:
    if m.get("role") == "tool_use":
        return {
            "role": "tool_use",
            "text": m.get("text") or "",
            "calls": [{"id": c.id, "name": c.name, "args": c.args}
                      for c in (m.get("calls") or [])],
            "assistant_blocks": _jsonable_blocks(m.get("assistant_blocks")),
        }
    return {k: v for k, v in m.items() if k != "_trimmed"} | (
        {"_trimmed": True} if m.get("_trimmed") else {})


def _de_entry(m: dict) -> dict:
    if m.get("role") == "tool_use":
        m = dict(m)
        m["calls"] = [ToolCall(**c) for c in (m.get("calls") or [])]
    return m


@dataclass
class PendingPermission:
    id: str
    tool: str
    args: dict
    summary: str
    answered: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False


# ---- the side lane's prompt front (2026-10-31) ------------------------------
SIDE_SUMMARY_CHARS = 4000     # config `side_summary_chars`
SIDE_EXCHANGES = 3            # recent user/assistant entries carried
SIDE_EXCHANGE_CHARS = 800     # chars kept of each
SIDE_MAX_TOKENS = 120         # the answer is a short paragraph


def side_prefix_msgs(history: list[dict], current: str = "") -> list[dict]:
    """Everything in the side prompt BEFORE his question: the tail of the
    running summary (history[0] when it carries fold.SUMMARY_MARKER), capped
    at side_summary_chars, then the last SIDE_EXCHANGES real user/assistant
    entries at SIDE_EXCHANGE_CHARS each, then `current` — the message the
    main turn is working on right now (it is not in history yet when the
    turn-start snapshot is taken; without it "how's it going?" was answered
    blind to the job). Tool entries are skipped on purpose (a bare tool
    message upsets the chat template). Built from one history snapshot, so
    two side questions in the same turn share the same bytes and the brain's
    side slot re-reads only the question (measured 2026-10-31: 7,396 tokens
    read from cold in 41 s; target ~2,000, mostly cached)."""
    from .fold import SUMMARY_MARKER
    try:
        cap = int((load_config() or {}).get("side_summary_chars", SIDE_SUMMARY_CHARS))
    except Exception:
        cap = SIDE_SUMMARY_CHARS
    hist = list(history)
    msgs: list[dict] = []
    if hist and hist[0].get("synthetic") and \
            str(hist[0].get("content", "")).startswith(SUMMARY_MARKER):
        body = str(hist[0]["content"])
        if len(body) > cap:
            # Keep the newest lines: the summary only ever grows by appended
            # lines, so the tail is the recent part.
            tail = body[-cap:]
            tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
            body = SUMMARY_MARKER + " (older part omitted)\n" + tail
        msgs.append({"role": "user", "content": body})
        hist = hist[1:]
    talk = [m for m in hist if m.get("role") in ("user", "assistant")
            and str(m.get("content") or "").strip()]
    for m in talk[-SIDE_EXCHANGES:]:
        msgs.append({"role": m["role"],
                     "content": str(m.get("content"))[:SIDE_EXCHANGE_CHARS]})
    if (current or "").strip():
        msgs.append({"role": "user", "content": "(What I am working on now) "
                     + str(current).strip()[:SIDE_EXCHANGE_CHARS]})
    return msgs


def _project_tools(ws, reserved=None) -> list:
    got, notes = _load_project_tools(ws, reserved=reserved)
    for n in notes:
        print(f"  project tools · {n}")
    return got


class Session:
    """One conversation, one workspace, one agent."""

    def __init__(self, session_id: str, workspace: Path, cfg: dict):
        self.id = session_id
        self.workspace = Path(workspace)
        self.created = time.time()
        self.last_used = time.time()
        # One append-only log, and a condition clients wait on. A client
        # tracks how much it has seen; there is no second copy anywhere for
        # an event to be delivered from twice.
        self.log: list[dict] = []
        self.log_base = 0          # how many were dropped off the front
        # A private/off-the-record chat: never written to disk, never listed,
        # gone when it ends. Set per-turn by the server from the privacy mode.
        self.ephemeral = False
        self.cond = threading.Condition()
        self.pending: PendingPermission | None = None
        self.busy = False
        # Messages typed while she's mid-turn wait here (she works one at a
        # time) and run in order when she frees up, instead of being dropped.
        self.queued: list[str] = []
        # 2026-10-27: queued messages only land after the turn; a stuck turn
        # needs a word between steps. A helper's nudge (POST /api/nudge) waits
        # here and the agent reads it before its next model call.
        self.nudges: list[str] = []
        # 2026-10-28 (kitchen B): side-lane Q/A — his quick questions answered
        # on the helpers slot while a turn runs (server._vip_side_answer).
        # Held here, then drained into her real history once the turn is over
        # (drain_side_exchanges) so she remembers what was said on the side.
        self.side_exchanges: list[tuple[str, str]] = []
        # 2026-10-31: the side prompt's frozen front — summary tail + recent
        # exchanges as they stood when the main turn started (side_prefix_msgs).
        # Byte-identical for every side question within one turn, so the
        # brain's "side" slot reuses its cache. None = build from live history.
        self.side_prefix: list[dict] | None = None
        # Set when a permission request times out unanswered: nobody is
        # watching this session right now, so further asks this message
        # auto-refuse instantly instead of stacking five-minute waits.
        self.unattended = False
        self.lock = threading.Lock()
        self._build_agent(cfg)

    def _build_agent(self, cfg: dict) -> None:
        provider = build_provider(active_model_config(cfg))
        ws = Workspace(self.workspace)
        mc = load_media_config(cfg)
        self.model_name = cfg.get("active_model", "?")
        # A model named by agent.summarizer_model takes over memory
        # compaction — a side-job a small CPU model can carry, keeping the
        # big model free. Misconfigured or missing = quietly no summarizer.
        summarizer = None
        s_name = (cfg["agent"].get("summarizer_model") or "").strip()
        if s_name and s_name in cfg.get("models", {}):
            try:
                summarizer = build_provider(cfg["models"][s_name])
            except Exception:
                summarizer = None
        # The sealed reviewer. A fresh provider instance for the active (or
        # designated) model — isolation comes from the sealed prompt and
        # evidence-only diet, not from separate weights.
        superego = None
        if cfg["agent"].get("superego", True):
            j_name = (cfg["agent"].get("superego_model") or "").strip()
            j_cfg = (cfg["models"].get(j_name)
                     if j_name in cfg.get("models", {})
                     else active_model_config(cfg))
            try:
                superego = build_provider(j_cfg)
            except Exception:
                superego = None
        # The devil's advocate (2026-09-28): a DIFFERENT model family from her
        # brain, so it does not share her blind spots. Unset = the judge does it.
        advocate = None
        a_name = (cfg["agent"].get("advocate_model") or "").strip()
        if a_name in cfg.get("models", {}):
            try:
                advocate = build_provider(cfg["models"][a_name])
            except Exception:
                advocate = None
        self.agent = Agent(
            provider=provider,
            # Her own tools, her eyes, and then whatever THIS project ships in
            # its merge-tools/ directory. A project's specialist tooling belongs
            # to the project and appears only while she is working in it.
            tools=build_tools(ws, fenced=bool(cfg.get("kid_mode")), session_id=self.id,
                            provider=provider, summarizer=summarizer)
                  + build_media_tools(ws, mc)
                  + _project_tools(ws, reserved={t.name for t in
                        build_tools(ws, fenced=bool(cfg.get("kid_mode")), session_id=self.id,
                                    provider=provider, summarizer=summarizer)}
                        | {t.name for t in build_media_tools(ws, mc)}),
            max_steps=int(cfg["agent"].get("max_steps", 40)),
            permission_mode=cfg["agent"].get("permission_mode", "ask"),
            notes_path=ws.root / "FORGE-NOTES.md",
            summarizer=summarizer,
            superego=superego,
            advocate=advocate,
            reads=ws.reads,
            read_mtimes=ws.read_mtimes,
        )
        self.agent.workspace_root = ws.root
        # so the flight recorder files a turn under the right session
        self.agent.session_id = self.id
        self.agent.nudge_source = self.take_nudges
        # The idle prime runs in _prime_and_park (after the side-lane drain,
        # right before park_save), not in the agent's own idle step.
        self.agent.prime_deferred = True

    def _prime_and_park(self, still) -> None:
        """After a turn, off its thread (2026-10-30): prime her chat slot to
        the chat as the next turn will render it (agent.prime_now) — only if
        this chat is still the slot's last user — then park it on disk.
        `still()` False (a new turn here began) skips both."""
        try:
            if park.holds(self.id) and self.agent._prime_on():
                self.agent.prime_now(still=still)
        except Exception:
            pass               # a failed prime must never cost the park
        if not self.ephemeral:
            park.park_save(self.id, self.ephemeral, still_mine=still)

    def nudge(self, text: str) -> str | None:
        """Leave a word for the RUNNING turn (read between steps). None if
        taken, else why not. Never starts a turn."""
        with self.lock:
            if not self.busy:
                return "not busy"
            if len(self.nudges) >= 3:
                return "3 nudges already pending"
            self.nudges.append(text[:400])
            return None

    def take_nudges(self) -> list[str]:
        """Everything nudged since the last step, emptied (the agent calls this)."""
        with self.lock:
            out, self.nudges = self.nudges, []
            return out

    def record_side_exchange(self, q: str, a: str) -> None:
        """One answered side-lane Q/A (2026-10-28, kitchen B), held until a
        drain folds it into history. The side thread calls this."""
        with self.lock:
            self.side_exchanges.append((q, a))

    def drain_side_exchanges(self) -> None:
        """Fold the side-lane Q/A into her REAL history (2026-10-28, kitchen
        B), as synthetic user/assistant turns — the superego bounce's pattern
        — so she remembers the exchange and the fold machinery treats it like
        anything else. Only called with no model call in flight: at the start
        of a turn (exchanges answered while ANOTHER session held the brain)
        and at a turn's end before `busy` clears (so no new turn can race the
        append). The whole drain holds the session lock."""
        with self.lock:
            for q, a in self.side_exchanges:
                self.agent.history.append({
                    "role": "user", "synthetic": True,
                    "content": "(Side lane, answered while the main task "
                               "ran) He said: " + q})
                self.agent.history.append({"role": "assistant", "content": a})
            self.side_exchanges = []

    def snapshot_side_prefix(self, current: str = "") -> None:
        """Freeze the side prompt's front for the coming/just-ended turn
        (2026-10-31). At a turn's start `current` is the message the turn is
        about to work on (the agent appends it to history only once it runs,
        so it would otherwise be missing). Never raises."""
        try:
            self.side_prefix = side_prefix_msgs(list(self.agent.history), current)
        except Exception:
            self.side_prefix = None

    def reload_model(self, cfg: dict) -> None:
        """Swap the model but keep the conversation."""
        history = self.agent.history
        self._build_agent(cfg)
        self.agent.history = history

    # -- the permission round-trip ------------------------------------

    def ask_permission(self, tool: str, args: dict, summary: str) -> bool:
        """
        Called on the agent's thread. Blocks until the browser answers.

        Returns False on timeout — an unanswered prompt is a refusal, never
        an approval. Silence must never be read as consent when the thing
        on the other end can edit files and run commands.
        """
        if self.unattended:
            self.emit("note", {"text": f"Nobody answered the last permission "
                                       f"request, so '{summary}' was refused "
                                       f"without waiting."})
            return False
        req = PendingPermission(id=uuid.uuid4().hex[:8], tool=tool,
                                args=args, summary=summary)
        self.pending = req
        self.emit("permission", {"id": req.id, "tool": tool,
                                 "summary": summary, "args": args})
        answered = req.answered.wait(timeout=PERMISSION_TIMEOUT)
        self.pending = None
        if not answered:
            self.unattended = True
            self.emit("note", {"text": "No answer in time — treating that as no."})
            return False
        return req.allowed

    # -- persistence --------------------------------------------------

    def save(self) -> None:
        """Write this conversation to disk. Failure to save must never
        break a working chat — worst case the restart loses one turn."""
        try:
            SESS_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "id": self.id,
                "workspace": str(self.workspace),
                "created": self.created,
                "last_used": self.last_used,
                "model": self.model_name,
                "log": self.log,
                "log_base": self.log_base,
                "history": [_ser_entry(m) for m in self.agent.history],
            }
            # Last stop before disk. A secret typed into chat lives in the
            # model HISTORY as well as the log, so scrub the serialized copy
            # here rather than in one of the several paths that feed them.
            try:
                from .vault import scrub

                def _deep(o):
                    if isinstance(o, str):
                        return scrub(o)
                    if isinstance(o, dict):
                        return {k: _deep(v) for k, v in o.items()}
                    if isinstance(o, list):
                        return [_deep(v) for v in o]
                    return o
                data = _deep(data)
            except Exception:
                pass
            tmp = SESS_DIR / f".{self.id}.tmp"
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(SESS_DIR / f"{self.id}.json")
            # retention: newest KEEP_SESSIONS stay, the rest go
            files = sorted(SESS_DIR.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[KEEP_SESSIONS:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    @classmethod
    def load(cls, path: Path, cfg: dict) -> "Session | None":
        """Rebuild a saved conversation. A corrupt file is set aside as
        .broken instead of taking the dashboard down with it."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            s = cls(data["id"], Path(data["workspace"]), cfg)
            s.created = data.get("created", time.time())
            s.last_used = data.get("last_used", time.time())
            s.log = data.get("log", [])
            s.log_base = data.get("log_base", 0)
            s.agent.history = [_de_entry(m) for m in data.get("history", [])]
            return s
        except Exception:
            try:
                path.replace(path.with_suffix(".broken"))
            except OSError:
                pass
            return None

    def stop(self) -> None:
        """The Stop button. Ends the turn at the next safe boundary; if the
        agent is blocked waiting on a permission card, that wait is answered
        'no' so the stop lands now instead of minutes from now."""
        self.agent.stop_requested = True
        with self.lock:
            self.queued.clear()   # Stop means stop — don't fire what was waiting.
            self.nudges.clear()
        req = self.pending
        if req:
            req.allowed = False
            req.answered.set()

    def answer_permission(self, request_id: str, allowed: bool) -> bool:
        req = self.pending
        if not req or req.id != request_id:
            return False
        req.allowed = allowed
        req.answered.set()
        return True

    # -- events -------------------------------------------------------

    def emit(self, kind: str, data: dict) -> None:
        # A secret typed straight into chat ("my password is hunter2") would
        # otherwise land here in plaintext, permanently. The vault only covers
        # what goes through the form; this covers what people actually do.
        try:
            from .vault import scrub
            if isinstance(data.get("text"), str):
                data = {**data, "text": scrub(data["text"])}
        except Exception:
            pass
        with self.cond:
            seq = self.log_base + len(self.log)
            self.log.append({"kind": kind, **data, "t": time.time(), "seq": seq})
            # keep memory bounded on a long-lived session
            if len(self.log) > 500:
                dropped = 250
                del self.log[:dropped]
                self.log_base += dropped
            self.cond.notify_all()

    def since(self, cursor: int, timeout: float = 20.0) -> tuple[list[dict], int]:
        """
        Everything after `cursor`, waiting up to `timeout` for something new.

        Returns (items, new_cursor). An empty list means nothing happened and
        the caller should send a keepalive and ask again.
        """
        with self.cond:
            start = max(cursor, self.log_base)
            if start >= self.log_base + len(self.log):
                self.cond.wait(timeout)
            start = max(cursor, self.log_base)
            items = self.log[start - self.log_base:]
            return list(items), self.log_base + len(self.log)

    # -- running a turn -----------------------------------------------

    def run_message(self, text: str) -> None:
        """Drive one user message to completion. Runs on its own thread."""
        with self.lock:
            if self.busy:
                self.queued.append(text)
                ahead = len(self.queued)
                self.emit("note", {"text": (
                    "⏳ She's still finishing your last message — she takes one "
                    "at a time. This one's queued"
                    + (f" (#{ahead} in line)" if ahead > 1 else "")
                    + " and runs the moment she's free.")})
                return
            self.busy = True
            # 2026-10-27: numbered, so a parked save still running from the last
            # turn knows to skip once this one has started (forge/park.py).
            self._turn_no = getattr(self, "_turn_no", 0) + 1
        self.unattended = False   # someone just typed — they're watching again
        self.last_used = time.time()
        # 2026-10-28 (kitchen B): side-lane answers given while the brain was
        # busy on ANOTHER session's turn land here, before this turn's
        # message, in the order he lived them.
        self.drain_side_exchanges()
        # 2026-10-31: freeze the side lane's prompt front for this turn — the
        # history as it stands plus the message this turn is about to work on.
        self.snapshot_side_prefix(text)
        final_text = ""
        # Stream the reply live: batch tokens into ~18-char pieces (so the log
        # doesn't fill with hundreds of one-token events) and emit them as
        # 'text_delta'. The authoritative 'text' event below finalizes each
        # segment, so any un-flushed tail is covered.
        delta_buf = [""]

        def on_delta(piece: str) -> None:
            delta_buf[0] += piece
            if len(delta_buf[0]) >= 18:
                self.emit("text_delta", {"text": delta_buf[0]})
                delta_buf[0] = ""

        # Her chat's memory, parked on disk after the last turn (2026-10-27):
        # if the brain's chat slot lost it, read it back in seconds instead of
        # re-reading the whole chat. A note, so All shows it and Chat hides it.
        back = park.begin_turn(self.id, self.ephemeral)
        if back:
            self.emit("note", {"text": f"restored this chat's memory from disk "
                                       f"({back['n']:,} tokens, {back['s']:.1f} s)"})
        done_seen = False
        try:
            for ev in self.agent.run(text, ask=self.ask_permission,
                                     on_delta=on_delta):
                if ev.kind == "done":
                    done_seen = True
                if ev.kind == "text" and ev.text.strip():
                    delta_buf[0] = ""   # final text supersedes buffered deltas
                    final_text = ev.text
                    self.emit("text", {"text": ev.text})
                elif ev.kind == "tool_request":
                    if not ev.will_ask:
                        self.emit("tool", {"tool": ev.tool, "summary": ev.summary})
                elif ev.kind == "tool_result":
                    self.emit("result", {"tool": ev.tool,
                                         "text": (ev.text or "")[:2000]})
                elif ev.kind == "note":
                    self.emit("note", {"text": ev.text})
                elif ev.kind == "progress":
                    self.emit("progress", {"text": ev.text, "summary": ev.summary, "tool": ev.tool})
                elif ev.kind == "error":
                    self.emit("error", {"text": ev.text})
                elif ev.kind == "done":
                    # done_check (2026-10-31): the project done-check's verdict
                    # for this turn, on the log entry — none/passed/failed.
                    self.emit("done", {"usage": ev.usage or {},
                                       "done_check": getattr(ev, "done_check", "") or "none"})
        except Exception as e:
            self.emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            # A turn that dies on a fatal error must STILL close the stream —
            # without a final 'done', every client waits forever (found live
            # 2026-08-18: a mid-turn HTTP 400 left the UI hanging).
            if not done_seen:
                self.emit("done", {"usage": {}})
            # 2026-10-28 (kitchen B): the turn is over, its idle fold has run
            # and no model call is in flight — drain the side-lane Q/A into
            # history NOW, before `busy` clears (no new turn can race the
            # append) and before save() and park_save read the history.
            self.drain_side_exchanges()
            self.snapshot_side_prefix()   # refreshed: the next turn's front
            self.busy = False
            self.nudges.clear()   # a nudge is for the turn it arrived in
            self.last_used = time.time()
            # Off the record: no session file, no memory card — nothing recorded.
            if not self.ephemeral:
                self.save()
                # the librarian files an index card in the background
                recall.remember_turn(text, final_text, str(self.workspace), self.id)
            # Park her chat slot on disk (2026-10-27, forge/park.py), off this
            # thread; skipped if a new turn here has started first. Primed
            # first (2026-10-30, agent.prime_now): the parked file then holds
            # the chat as the next turn renders it, so a restore is followed
            # by reading only the new message. Off the record: prime, no park.
            _no = self._turn_no
            threading.Thread(target=self._prime_and_park, daemon=True, args=(
                lambda: not self.busy and getattr(self, "_turn_no", 0) == _no,)).start()
            # Anything typed while she was busy runs now, in the order it came.
            nxt = None
            with self.lock:
                if self.queued:
                    nxt = self.queued.pop(0)
            if nxt is not None:
                threading.Thread(target=self.run_message, args=(nxt,),
                                 daemon=True).start()


class SessionStore:
    """All live conversations, keyed by id."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        # Restart? Reboot? Doesn't matter — saved conversations come back,
        # and a page holding its session id just reconnects and continues.
        if SESS_DIR.is_dir():
            cfg = load_config()
            for f in sorted(SESS_DIR.glob("*.json")):
                s = Session.load(f, cfg)
                if s:
                    self.sessions[s.id] = s

    def rescan(self) -> None:
        """
        Pick up what the terminal wrote while we were running.

        Both doors share ~/.forge/sessions/ as the one brain: the CLI saves
        there after every turn, but this store only read the folder at
        startup. Called before listing or opening sessions, this loads files
        we've never seen and re-loads ones the terminal has since updated —
        never touching a session that is busy right now (its in-memory state
        is newer than any file).
        """
        if not SESS_DIR.is_dir():
            return
        cfg = None
        with self.lock:
            for f in SESS_DIR.glob("*.json"):
                sid = f.stem
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                have = self.sessions.get(sid)
                if have and (have.busy or mtime <= have.last_used + 1):
                    continue
                cfg = cfg or load_config()
                s = Session.load(f, cfg)
                if s:
                    self.sessions[sid] = s

    def get_or_create(self, session_id: str | None, workspace: str | None,
                      privacy: str = "normal") -> Session:
        cfg = load_config()
        ephemeral = get_privacy(privacy)["ephemeral"]
        with self.lock:
            if session_id and session_id in self.sessions:
                s = self.sessions[session_id]
                s.ephemeral = ephemeral
                # a model switched in the dashboard should land here too
                if s.model_name != cfg.get("active_model"):
                    s.reload_model(cfg)
                return s
            # Unknown ids (a browser remembering a session from before a
            # restart) get a FRESH id, never the stale one back: the reply
            # carrying a new id is how the page knows to reconnect its
            # event stream.
            sid = uuid.uuid4().hex[:12]
            # No folder chosen means the Playground, not the whole home
            # directory. The agent's file tools are fenced to its workspace —
            # defaulting that fence to everything-you-own was fine for the
            # owner, and wrong for the ten-year-old borrowing the tablet.
            # Anyone can still pick any folder deliberately via New.
            # In kid mode there is no picking: everything lives in Playground.
            if cfg.get("kid_mode"):
                play = Path.home() / "Playground"
                w = Path(workspace).expanduser().resolve() if workspace else play
                if w != play and play not in w.parents:
                    workspace = None
            if workspace:
                ws = Path(workspace).expanduser()
                if not ws.is_dir():
                    ws = Path.home()
            else:
                # 2026-09-19, his call: the whole home. Asked about Penpal from
                # ~/Merge she could not see the project, and instead of saying
                # so she described a different one in confident detail. A fence
                # she cannot see past turns "I don't have that" into invention.
                ws = Path.home()
            if ws == Path.home() / "Merge":
                ws.mkdir(exist_ok=True)
            s = Session(sid, ws, cfg)
            s.ephemeral = ephemeral        # set BEFORE any save
            self.sessions[sid] = s
            if not s.ephemeral:            # a private chat is never written, even the shell
                s.save()
            return s

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def busy_count(self, exclude: str | None = None) -> int:
        """How many OTHER sessions are mid-turn right now. Used to warn a new
        request that it'll queue behind them on the single-lane model."""
        return sum(1 for sid, s in self.sessions.items()
                   if s.busy and sid != exclude)

    def listing(self) -> list[dict]:
        out = []
        for s in sorted(self.sessions.values(), key=lambda x: -x.last_used):
            if s.ephemeral:
                continue            # private chats never show in the drawer
            first = next((e.get("text", "") for e in s.log
                          if e.get("kind") == "user"), "")
            out.append({
                "id": s.id,
                "workspace": str(s.workspace),
                "model": s.model_name,
                "busy": s.busy,
                "messages": len(s.agent.history),
                "last_used": s.last_used,
                "title": first[:60] or "(empty chat)",
            })
        return out

    def drop(self, session_id: str) -> bool:
        with self.lock:
            gone = self.sessions.pop(session_id, None) is not None
        if gone:
            (SESS_DIR / f"{session_id}.json").unlink(missing_ok=True)
        return gone
