"""Operating modes ('styles') — presets that trade speed / creativity /
accuracy by bundling five levers: whether she reasons first, sampling
temperature, how many tools she carries, the honesty (superego) check, and a
framing nudge. Switchable per message from the UI.

The spectrum, left to right:
  Flash  — fastest, least tooled (quick chat & planning)
  Muse   — most creative (fiction, brainstorming, wild ideas)
  Balanced — the even default (everyday work)
  Precise — most accurate (facts, code, verified answers)
  Deep   — most thorough (hard problems, careful builds)

The reasoning phase ("thinking") is the speed dial: OFF for Flash/Muse/Balanced
(snappy), ON for Precise/Deep (they sit and think). llama.cpp only takes the
thinking-token budget globally at startup, so we can't set it per mode — instead
it's set generous server-side (start-model.sh) and only the two thinking modes
ever draw on it. That's how "budget scales with the mode" is faked.
"""

# Tool tiers by name. None = every tool (no filter).
_LIGHT = {"read_file", "list_dir", "search", "web_search", "fetch_url", "recall",
          "look_at_image", "take_screenshot", "say_aloud", "transcribe_audio",
          "verify_phrase"}
_WRITING = _LIGHT | {"write_file", "edit_file", "undo_file", "save_note",
                     "text_stats", "ai_tells", "name_check", "generate_image"}

# The everyday core. Every tool loaded costs window she could be thinking with:
# measured on the running 24,576-token server, all 70 schemas are ~11,800 tokens
# — so a session was two-thirds full before a word was said, which is what kept
# killing long jobs with a bare 400. Merge picked this list herself from her own
# working experience, and it matches the usage data (only 17 of 70 tools have
# ever actually been called). Everything else is reachable on demand; these are
# the ones worth paying for on every single request.
_CORE = {
    "read_file", "write_file", "edit_file", "list_dir", "search",   # files
    "run_command",                                                  # shell
    "web_search", "fetch_url",                                      # the world
    # 2026-09-08: the whole browser set was built and never put on her belt.
    # `browse` is the one that POINTS the browser at a URL; fetch_url only
    # downloads text over http and never touches the browser. Without `browse`
    # she opened nothing, so browser_js ran against about:blank and every
    # screenshot came back white — she reported a working game as broken.
    "browse", "browser_view", "browser_js", "browser_console",
    "recall", "save_note",                                          # memory
    "compute",                                                      # exact math
    "look_at_image",                                                # her eyes
    "lora_search", "lora_install", "lora_list", "lora_remove",      # 2026-09-07: she grows the LoRA shelf herself
    # 2026-09-05: pictures became first-class (ComfyUI on the GPU, reference
    # images, ~20 s). Without this on the belt she went looking for the tool on
    # the web and started writing her own with PIL — watched live. One schema.
    "generate_image",                                               # her hands
    "generate_video",                                               # her hands, moving
    "restyle_video",                                                # same shot, different world
    "extract_pose",                                                 # a skeleton from any picture
    "finish_video",                                                 # refine + smooth an approved draft
    "storyboard", "story_video",                                    # plan as stills, fill as motion
}

# 2026-09-06: every mode advertises the SAME tool set (_ALL). The model's chat template puts
# tool schemas at the very top of the prompt, so a per-mode list changed the first bytes and
# threw away the whole cached prompt on every mode switch (measured: 40 full re-reads in a
# day, 24 min). Mode-specific restraint now lives in the nudge, not the schema list.
_ALL = _CORE | _WRITING | _LIGHT

MODES = {
    "flash": {
        "label": "⚡ Flash", "thinking": False, "temperature": 0.7,
        "tools": _ALL, "superego": False, "max_steps": 14, "wall_seconds": 300,
        "nudge": "Move fast and keep it conversational. Don't reach for heavy "
                 "tooling unless it's genuinely needed — this is for quick chat "
                 "and planning. No shell commands, no file edits, no renders "
                 "unless asked outright.",
    },
    "muse": {
        "label": "\U0001f3a8 Muse", "thinking": False, "temperature": 1.05,
        "tools": _ALL, "superego": False, "max_steps": 14, "wall_seconds": 300,
        "nudge": "Be imaginative and generative — riff, explore, follow wild "
                 "ideas, don't hedge or self-censor. This is for fiction and "
                 "brainstorming, not fact-checking; surprise beats caution here. "
                 "Words and pictures only: no shell commands or code edits here.",
    },
    "balanced": {
        # Snappy everyday default: no reasoning phase (that's what keeps it
        # quick), full tools + honesty check. Reach for Precise/Deep when a
        # problem actually needs her to sit and think.
        "label": "⚖️ Balanced", "thinking": False, "temperature": 0.7,
        "tools": _ALL, "superego": False, "max_steps": 40, "wall_seconds": 600,   # 2026-09-06: reviewer pass = a full extra prompt read per tool turn; kept for precise/deep/teach/bughunt
        "nudge": "",
    },
    "precise": {
        "label": "\U0001f3af Precise", "thinking": True, "temperature": 0.2,
        "tools": _ALL, "superego": True, "max_steps": 40, "wall_seconds": 1200,
        "nudge": "Accuracy above all. Verify with tools — compute for any number, "
                 "web_search for any fact — cite what you find, and say plainly "
                 "when you're unsure instead of guessing.",
    },
    "deep": {
        "label": "\U0001f9e0 Deep", "thinking": True, "temperature": 0.45,
        "tools": _ALL, "superego": True, "max_steps": 80, "wall_seconds": 1800,
        "nudge": "Take your time and be thorough. Work through edge cases, check "
                 "your own work, and don't stop until it's genuinely solid.",
    },
    "teach": {
        "label": "\U0001f393 Teach", "thinking": True, "temperature": 0.6,
        "tools": _ALL, "superego": True, "max_steps": 40, "wall_seconds": 1200,
        "nudge": "Teach — don't just answer. The goal is that they UNDERSTAND, "
                 "not that they walk away with a result. Start from first "
                 "principles at their level, and build on what they already know: "
                 "connect the new idea to something familiar to them. Make it "
                 "concrete — use your tools (compute, the sims, business_calc, "
                 "frameworks, etc.) to SHOW the thing working, not just describe "
                 "it. Prefer one idea explained deeply over ten glossed over; "
                 "always surface the WHY behind the WHAT. Check that it landed — a "
                 "short question, or 'does that click?' — and if they've got it "
                 "wrong, correct it kindly and clearly. Hand them understanding "
                 "they can reuse, not just the fish.",
    },
    "bughunt": {
        # Same capability as Deep, plus the flight recorder. For when something
        # is going wrong and someone will need to reconstruct it afterwards.
        "label": "\U0001f41e Bug Hunt", "thinking": True, "temperature": 0.3,
        "tools": _ALL, "superego": True, "max_steps": 60,
        # Same leash as Deep. Without this it inherited the 600s default and
        # the first thinking-on bug-hunt run (2026-09-05) died at 637s with
        # findings written and no answer — the ceiling, not the reasoning.
        "wall_seconds": 1800,
        "forensic": True,
        # The date procedure lives HERE as well as in the playbook: run A on
        # 2026-09-05 showed a rule in the system prompt does not reach the
        # moment of action — she ran 22 `git show`s and zero `--since`.
        "nudge": "Diagnostic mode: everything you do is being recorded so a "
                 "failure can be reconstructed later. Before reading any code or "
                 "any diff: (1) find a DATE it last worked — a comment, a commit "
                 "body, a log line; (2) `git log --oneline --since=<date> -- <the "
                 "part that broke>`; (3) `git show` the FIRST commit after that "
                 "date, in full, however boring its message; (4) only then read "
                 "code. Write each of those four results into FINDINGS.md as you "
                 "get it. Narrate as you go — say "
                 "what you're about to try and why, what you expected, and what "
                 "actually came back, especially when they differ. If a tool "
                 "errors or returns something odd, quote the actual text rather "
                 "than paraphrasing it, and say plainly what you can't explain. "
                 "An unexplained oddity reported honestly is worth more here "
                 "than a smooth answer that hides it.",
    },
}
DEFAULT_MODE = "balanced"
# "auto" isn't a preset — it's resolved per message by route_mode(). Listed
# first so it can be the default choice in the UI.
ORDER = ["auto", "flash", "muse", "balanced", "precise", "deep", "teach", "bughunt"]
AUTO_LABEL = "\U0001f39b️ Auto"


def get_mode(name: str) -> dict:
    return MODES.get((name or "").strip().lower(), MODES[DEFAULT_MODE])


# ---- privacy modes: a SEPARATE axis from the speed styles above ----------
# Each can further restrict her tools and switch off all persistence, so a
# chat can leave no trace. `deny` names tools removed on top of the style's
# own toolset; `ephemeral` means nothing about the chat is written to disk.
_WRITES_DISK = {"write_file", "edit_file", "undo_file", "save_note",
                "run_command", "format_code", "generate_image", "generate_video", "restyle_video", "extract_pose", "finish_video", "storyboard", "story_video", "take_screenshot",
                "build_sim", "run_sim", "build_dataset"}
_TOUCHES_FILES = _WRITES_DISK | {"read_file", "list_dir", "search"}

PRIVACY = {
    # everyday: saved, full access
    "normal":  {"label": "Normal", "ephemeral": False, "deny": frozenset()},
    # off the record: reads anything, changes nothing on disk, nothing recorded
    "offrec":  {"label": "\U0001f576️ Off the record", "ephemeral": True,
                "deny": frozenset(_WRITES_DISK)},
    # knowledge only: her knowledge + safe tools, hands off the filesystem
    "sandbox": {"label": "\U0001f512 Knowledge only", "ephemeral": True,
                "deny": frozenset(_TOUCHES_FILES)},
}
PRIVACY_DEFAULT = "normal"


def get_privacy(name: str) -> dict:
    return PRIVACY.get((name or "").strip().lower(), PRIVACY[PRIVACY_DEFAULT])


import re as _re

# Ordered rules: the first that matches wins. Explicit style requests (what the
# user literally asks for in chat) come first so "be more careful" or "get
# creative" always win over content guesses.
_ROUTE_RULES = [
    ("teach", r"\b(teach me|help me (understand|learn|grasp|wrap my head around)|"
              r"walk me through|eli5|explain it like|i want to learn|"
              r"learn (about|how)|break it down for me|"
              r"how does .{0,40}\bwork|what.?s the (idea|concept|intuition|logic) behind)\b"),
    ("precise", r"\b(be precise|precise|accurate|accuracy|verify|fact.?check|"
                r"double.?check|is it (true|real)|are you sure|really true|"
                r"cite|source|look .*up|search the web|prove)\b"),
    ("muse", r"\b(be creative|get creative|creative|imaginative|imagine|"
             r"brainstorm|riff|make .*up|dream up|come up with ideas)\b"),
    ("flash", r"\b(quick|quickly|real quick|fast|briefly|just tell me|"
              r"short answer|tl;?dr|keep it short|one line)\b"),
    ("deep", r"\b(be thorough|thorough|carefully|think (this )?through|"
             r"work through|deep dive|go deep|take your time|complex problem|"
             r"hard problem|step by step)\b"),
    # content shape (weaker signals, after explicit requests)
    ("muse", r"\b(write|draft|compose)\b.{0,25}\b(story|poem|scene|song|tale|"
             r"chapter|character|lyric|dialogue|fiction|novel)\b"),
    ("precise", r"(\b(calculate|compute|how (much|many)|convert|what year|"
                r"when did|percent|equation|formula)\b|\d+\s*[-+*/%]\s*\d+|"
                r"\d+\s*%|what.?s\s+\d)"),
    ("balanced", r"\b(fix|debug|implement|refactor|build|code|write a "
                 r"(function|script|program|test))\b"),
]


def route_mode(message: str) -> str:
    """Pick a mode from the message's intent. Instant heuristics; defaults to
    balanced when nothing clearly fits."""
    t = (message or "").lower()
    for mode, pattern in _ROUTE_RULES:
        if _re.search(pattern, t):
            return mode
    return DEFAULT_MODE

