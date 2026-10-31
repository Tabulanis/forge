#!/usr/bin/env python3
"""Rolling memory, Patch B3 (2026-10-27): measure the fold against the judging
numbers written down BEFORE it was built (merge-rolling-memory/DESIGN.md).

  1. Size      100-turn synthetic chat -> live prompt stays under 45k after the
               first fold.
  2. Fold cost each fold re-reads AT MOST ONCE, no bigger than the window cap.
  3. Recall    20 facts stated early, looked up after 60 more turns -> right/20.

TWO MODES

  Offline (no model needed; nothing touches the live brain):
      ~/forge/.venv/bin/python bench/fold_harness.py [--turns 100] [--seed 7] [--json out.json]
    Drives the REAL Agent.run() -> fold path with a fake brain. HOME is pointed
    at a throwaway folder before forge is imported, so her config, sessions and
    recall tray are never touched. Prompt tokens come from the agent's own
    accounting (_entry_chars // 4 + _fixed_overhead_tokens). Re-reads are
    MODELLED: a single-slot prefix cache like llama-server's (what the next
    prompt shares with the previous prompt + the reply the model just wrote is
    free; the rest is re-read). That model is the design's claim, not proof —
    the live log below is the proof.
    Judging #3 uses recall.search() — the verbatim transcript path always (no
    server), and the card path only when the embed organ (:8086) is up; else it
    says "embed down, skipped". Cards are filed from the fold's own tray
    entries with the fold's extractive lines as the gist (a stand-in: the real
    librarian distils with the little model, which this harness does not call).

  Live re-read count (judging #2 on the real server), AFTER a real chat with
  `fold: true` — the fold's notes ("folded N turns ...") tell you when:
      ~/forge/.venv/bin/python bench/fold_harness.py --log-since "2026-10-28 09:00" \
          [--log-until "2026-10-28 12:00"] [--threshold 15000] [--unit forge-model-big122.service]
    Reads the brain's journal (journalctl --user, read-only) for llama-server's
    per-task prompt lines and lists every task that processed more than
    --threshold tokens. Pass: re-reads over the threshold <= number of folds in
    that window, each no bigger than window cap + summary. (Over-threshold
    reads that are not fold turns are the old tax, and are worth reading too.)

Numbers it reports are measurements of THIS synthetic chat; tokens-per-turn in
a real chat is still GUESSED (DESIGN.md) until a live run says otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ------------------------------------------------------------ live log reader
_PP = re.compile(r"task (\d+) \| prompt processing, n_tokens =\s*(\d+)")
_PE = re.compile(r"task (\d+) \| prompt eval time =\s*[\d.]+ ms /\s*(\d+) tokens")


def reread_events(since: str = "", until: str = "", threshold: int = 15000,
                  unit: str = "forge-model-big122.service",
                  text: str | None = None) -> list[dict]:
    """llama-server prompt reads bigger than `threshold` tokens, one per task.

    Reads `journalctl --user -u <unit>` between since/until (any journalctl
    time spec) unless `text` (log lines) is given. A task's size is the
    largest n_tokens on its "prompt processing" progress lines or its
    "prompt eval time ... / N tokens" line — the tokens actually processed,
    i.e. NOT served from the prompt cache. Read-only; never calls the server."""
    if text is None:
        cmd = ["journalctl", "--user", "-u", unit, "-o", "short-iso", "--no-pager"]
        if since:
            cmd += ["--since", since]
        if until:
            cmd += ["--until", until]
        text = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120).stdout
    tasks: dict[str, dict] = {}
    for line in text.splitlines():
        m = _PP.search(line) or _PE.search(line)
        if not m:
            continue
        tid, n = m.group(1), int(m.group(2))
        pid = re.search(r"\[(\d+)\]:", line)        # task ids restart with the server
        key = (pid.group(1) if pid else "", tid)
        t = tasks.setdefault(key, {"task": int(tid), "time": line.split(" ", 1)[0],
                                   "tokens": 0})
        t["tokens"] = max(t["tokens"], n)
    return [t for t in tasks.values() if t["tokens"] > threshold]


# ------------------------------------------------------------ synthetic chat
_WORDS = ("garden lantern quiet river window paper morning orange velvet ladder "
          "pocket cloud marble kettle harbor meadow pencil thunder blanket violin "
          "basket island mirror cotton candle forest copper rabbit saddle canvas "
          "feather breeze ribbon pebble walnut tunnel shadow honey compass otter "
          "maybe probably honestly roughly gently slowly simply truly kind of sort of "
          "thinking wondering noticing feeling hoping guessing looking talking").split()

FACTS = [  # (what is asked about, the answer that must come back) — half carry a number
    ("the courier for the Ostrava shipment", "Pemberton"),
    ("the gate code for the north shed", "4471"),
    ("the name of the grey heron that visits the pond", "Bartholomew"),
    ("the torque for the trailer wheel nuts", "118"),
    ("the colour we chose for the studio door", "vermilion"),
    ("the frequency of the workshop radio", "1405"),
    ("the surname of the plumber who fixed the boiler", "Okonkwo"),
    ("the shelf number of the spare fuses", "37"),
    ("the codename of the winter story arc", "Larkspur"),
    ("the bus that goes past the library", "512"),
    ("the variety of tomato in the greenhouse", "Brandywine"),
    ("the serial ending of the old projector", "8826"),
    ("the street where the framing shop is", "Wexcombe"),
    ("the pressure for the airbrush compressor", "29"),
    ("the dog that lives next door", "Marzipan"),
    ("the locker that holds the tripods", "263"),
    ("the font the poster uses", "Garamondo"),
    ("the weight limit of the loft hatch", "145"),
    ("the village the cider comes from", "Dunsterby"),
    ("the year on the brass plaque in the hall", "1887"),
]


class Gen:
    """Seeded chat text. Chatter never matches the fold's keep-pattern (so the
    extractive distiller drops it, as it would drop real small talk); work
    sentences and number-facts do match; name-facts do NOT (the distiller only
    keeps them if they carry a number/path/decision word — recall must)."""

    def __init__(self, seed: int):
        import forge.fold as F
        self.r = random.Random(seed)
        self.keep = F._FOLD_KEEP
        # A wide made-up vocabulary: the agent's garbled-reply guard
        # (_looks_incoherent) rightly bounces low-variety word salad, which
        # would swap every scripted answer for a short retry.
        syl = ["ba", "ke", "lo", "mi", "nu", "ra", "se", "ti", "vo", "wa", "zu",
               "gel", "dor", "fen", "hal", "mur", "pel", "sor", "tan", "vin"]
        vocab = set(_WORDS)
        while len(vocab) < 4000:
            vocab.add("".join(self.r.choice(syl) for _ in range(self.r.randint(2, 4))))
        self.vocab = sorted(vocab)

    def sentence(self) -> str:
        while True:
            s = " ".join(self.r.choice(self.vocab) for _ in range(self.r.randint(8, 18)))
            s = s[0].upper() + s[1:] + "."
            if not self.keep.search(s):
                return s

    def chatter(self, lo: int, hi: int) -> str:
        return " ".join(self.sentence() for _ in range(self.r.randint(lo, hi)))

    def work(self, i: int) -> str:
        n = self.r.randint(2, 900)
        return self.r.choice([
            f"We decided to set the retry limit in /srv/app/conf{i}.yaml to {n}.",
            f"Next step: rename the batch {n} files under ~/renders/t{i}/ before Friday.",
            f"Still unverified: whether job {n} finished cleanly last night.",
            f"Agreed that the queue size stays at {n} for now.",
        ])


def _said(subj: str, ans: str) -> str:
    """What counts as RIGHT: the fact as stated, subject and answer together
    (a bare number like 37 could match anything)."""
    return f"{subj} is {ans}".lower()


def _fact_line(k: int) -> str:
    subj, ans = FACTS[k]
    return f"Please keep this in mind: {subj} is {ans}."


# ------------------------------------------------------------ the harness
def run_offline(args) -> dict:
    home = Path(tempfile.mkdtemp(prefix="fold-harness-"))
    os.environ["HOME"] = str(home)          # BEFORE forge import: her files untouched
    os.environ.pop("FORGE_HOME", None)
    sys.path.insert(0, str(ROOT))
    import forge.agent as A
    import forge.fold as F
    from forge import recall, embed
    from forge.providers import Provider, Reply, ToolCall
    from forge.tools import Tool

    g = Gen(args.seed)
    agent_ref: dict = {}

    def _render(system, messages, tools) -> list[str]:
        out = ["S" + system + json.dumps(tools, sort_keys=True, default=str)]
        for m in messages:
            if m.get("role") == "tool_use":
                out.append("T" + (m.get("text") or "") + json.dumps(
                    [[c.name, c.args] for c in (m.get("calls") or [])], default=str))
            else:
                out.append(m.get("role", "?")[0] + str(m.get("content", "")))
        return out

    class FakeBrain(Provider):
        name = "fake"

        def __init__(self):
            self.script: list = []
            self.prev: list[str] | None = None
            self.calls: list[dict] = []

        def context_limit(self):
            return args.ctx

        def complete(self, system, messages, tools, grammar=None,
                     extra_body=None, on_delta=None, **kw):
            ag = agent_ref["agent"]
            prompt = (sum(ag._entry_chars(m) for m in messages) // A._CHARS_PER_TOKEN
                      + ag._fixed_overhead_tokens())
            cur = _render(system, messages, tools)
            cached = 0
            if self.prev is not None:
                k = 0
                while k < min(len(cur), len(self.prev)) and cur[k] == self.prev[k]:
                    cached += len(cur[k])
                    k += 1
                if k < min(len(cur), len(self.prev)):
                    a, b = cur[k], self.prev[k]
                    j = 0
                    while j < min(len(a), len(b)) and a[j] == b[j]:
                        j += 1
                    cached += j
                elif k == len(self.prev) and k < len(cur) and cur[k][:1] in ("a", "T"):
                    cached += len(cur[k])        # the reply it just wrote is in cache
            total = sum(len(x) for x in cur)
            self.prev = cur
            reread = (total - cached) // A._CHARS_PER_TOKEN
            self.calls.append({"prompt": prompt, "reread": reread,
                               "request": total // A._CHARS_PER_TOKEN})
            return self.script.pop(0) if self.script else Reply(text="Noted.")

    brain = FakeBrain()
    lookup = Tool(name="lookup", description="Look something up in the notes.",
                  run=lambda topic="": (f"Notes on {topic}: " + g.chatter(15, 30)),
                  parameters={"type": "object",
                              "properties": {"topic": {"type": "string"}}})
    # Her REAL toolset (constructed only; the fake brain calls just `lookup`),
    # so the system prompt and tool schemas weigh what they weigh live.
    (home / "ws").mkdir(parents=True, exist_ok=True)
    tools = [lookup]
    if not args.no_real_tools:
        from forge.tools import Workspace, build_tools
        tools = build_tools(Workspace(home / "ws"), session_id="fold-harness") + tools
    ag = A.Agent(brain, tools, permission_mode="auto")
    agent_ref["agent"] = ag
    st = ag._fold_settings()
    if st is None:
        raise SystemExit("fold is off in this config — nothing to measure")
    for k_cfg, k_st in (("window", "window"), ("keep", "keep"),
                        ("summary_cap", "summary_cap"), ("pins", "pins")):
        v = getattr(args, k_cfg)
        if v:
            st[k_st] = v
    ag._fold_settings = lambda: dict(st)
    sid = "fold-harness"
    ag.session_id, ag.workspace_root = sid, home / "ws"
    log: list[dict] = []
    rows: list[dict] = []
    fact_turn = {}
    recall_at: dict = {}

    def _save_session():
        recall.SESS_DIR.mkdir(parents=True, exist_ok=True)
        (recall.SESS_DIR / f"{sid}.json").write_text(json.dumps(
            {"id": sid, "workspace": str(ag.workspace_root), "log": log}),
            encoding="utf-8")

    def _recall_check(at_turn: int) -> dict:
        _save_session()
        res = {"turn": at_turn}
        real_q = embed.embed_query
        embed.embed_query = lambda q: None            # verbatim path only
        try:
            right = [ans for subj, ans in FACTS
                     if _said(subj, ans) in recall.search(f"what was {subj}",
                                                          str(ag.workspace_root)).lower()]
        finally:
            embed.embed_query = real_q
        res["verbatim_right"] = len(right)
        summ = ag.history[0].get("content", "") if ag.history and \
            str(ag.history[0].get("content", "")).startswith(F.SUMMARY_MARKER) else ""
        res["in_live_summary"] = sum(1 for subj, ans in FACTS if _said(subj, ans) in summ.lower())
        if embed.embed_query("probe") is None:
            res["card_path"] = "embed down, skipped"
        else:
            filed = 0
            for q in sorted(recall.QUEUE_DIR.glob("*.json")) if recall.QUEUE_DIR.is_dir() else []:
                e = json.loads(q.read_text(encoding="utf-8"))
                recall._append_card_and_vec({
                    "t": e.get("t"), "workspace": e.get("workspace", ""),
                    "session": e.get("session", ""),
                    "gist": " ".join(str(e.get("answer", "")).split())[:1200],
                    "turn_range": e.get("turn_range")})
                q.unlink()
                filed += 1
            right_c = [ans for subj, ans in FACTS
                       if _said(subj, ans) in recall.search(f"what was {subj}",
                                                            str(ag.workspace_root)).lower()]
            res["card_path"] = {"cards_filed": filed, "right_with_cards": len(right_c)}
        return res

    for t in range(1, args.turns + 1):
        parts = []
        if t <= len(FACTS) and t <= args.facts:
            parts.append(_fact_line(t - 1))
            fact_turn[t - 1] = t
        if g.r.random() < 0.3:
            parts.append(g.work(t))
        parts.append(g.chatter(1, 4))
        user = " ".join(parts)
        answer = g.chatter(args.ans_lo, args.ans_hi)
        if g.r.random() < 0.3:
            answer = g.work(t) + " " + answer
        script = []
        if t % 3 == 0:
            script.append(Reply(tool_calls=[ToolCall(id=f"c{t}", name="lookup",
                                                     args={"topic": f"topic {t}"})]))
        script.append(Reply(text=answer))
        brain.script = script
        n_calls = len(brain.calls)
        win_before = sum(ag._entry_chars(m) for m in ag._fold_split()[1]) // A._CHARS_PER_TOKEN
        events = list(ag.run(user))
        log.append({"kind": "user", "text": user, "t": time.time()})
        for e in events:
            if e.kind == "text":
                log.append({"kind": "text", "text": e.text, "t": time.time()})
        mine = brain.calls[n_calls:]
        notes = [e.text for e in events if e.kind == "note"]
        folds = [n for n in notes if n.startswith("folded ")]
        summ, window = ag._fold_split()
        rows.append({
            "turn": t,
            "prompt_tokens": mine[0]["prompt"] if mine else 0,
            "prompt_tokens_max": max((c["prompt"] for c in mine), default=0),
            "request_tokens_max": max((c["request"] for c in mine), default=0),
            "reread_first_call": mine[0]["reread"] if mine else 0,
            "reread_max": max((c["reread"] for c in mine), default=0),
            "window_before": win_before,
            "window_after_turn": sum(ag._entry_chars(m) for m in window) // A._CHARS_PER_TOKEN,
            "summary_tokens": F.summary_tokens(summ, A._CHARS_PER_TOKEN),
            "fold_notes": folds + [n for n in notes if n.startswith("summary folded")],
            "turn_tokens": (len(user) + len(answer)) // A._CHARS_PER_TOKEN,
        })
        if t == len(FACTS) + args.after:
            recall_at = _recall_check(t)
    final_recall = _recall_check(args.turns)

    fold_rows = [r for r in rows if r["fold_notes"]]
    first_fold = fold_rows[0]["turn"] if fold_rows else None
    after = [r for r in rows if first_fold and r["turn"] >= first_fold]
    non_fold = [r for r in rows if not r["fold_notes"] and r["turn"] > 1]
    out = {
        "settings": st,
        "overhead_tokens": ag._fixed_overhead_tokens(),
        "n_tools": len(tools),
        "turns": args.turns,
        "mean_turn_tokens": round(sum(r["turn_tokens"] for r in rows) / len(rows)),
        "folds": len([n for r in rows for n in r["fold_notes"] if n.startswith("folded ")]),
        "summary_condensed": len([n for r in rows for n in r["fold_notes"]
                                  if n.startswith("summary folded")]),
        "first_fold_turn": first_fold,
        "judging_1_max_prompt_after_first_fold": max((r["prompt_tokens_max"] for r in after), default=None),
        "judging_1_pass": bool(after) and max(r["prompt_tokens_max"] for r in after) < 45000,
        "judging_1_max_request_after_first_fold": max((r["request_tokens_max"] for r in after), default=None),
        "judging_2_per_fold": [{"turn": r["turn"], "note": r["fold_notes"],
                                "reread_tokens": r["reread_first_call"],
                                "window_after_fold": r["window_after_turn"]}
                               for r in fold_rows],
        "judging_2_cap": st["window"],
        "judging_2_max_fold_reread": max((r["reread_first_call"] for r in fold_rows), default=0),
        "judging_2_max_nonfold_reread": max((r["reread_first_call"] for r in non_fold), default=0),
        "judging_3_at_turn": recall_at,
        "judging_3_final": final_recall,
        "rows": rows,
        "home": str(home),
    }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--turns", type=int, default=100)
    ap.add_argument("--facts", type=int, default=20)
    ap.add_argument("--after", type=int, default=60, help="turns after the last fact before the recall check")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ctx", type=int, default=131072, help="fake brain window (the live brain is 128k)")
    ap.add_argument("--no-real-tools", action="store_true",
                    help="carry only the fake `lookup` tool (default: her real toolset too)")
    ap.add_argument("--ans-lo", type=int, default=25, help="answer length, sentences (low)")
    ap.add_argument("--ans-hi", type=int, default=90, help="answer length, sentences (high)")
    ap.add_argument("--window", type=int, default=0)
    ap.add_argument("--keep", type=int, default=0)
    ap.add_argument("--summary-cap", type=int, default=0)
    ap.add_argument("--pins", type=int, default=0)
    ap.add_argument("--json", default="")
    ap.add_argument("--rows", action="store_true", help="print the per-turn table")
    ap.add_argument("--log-since", default="")
    ap.add_argument("--log-until", default="")
    ap.add_argument("--threshold", type=int, default=15000)
    ap.add_argument("--unit", default="forge-model-big122.service")
    args = ap.parse_args()

    if args.log_since:
        ev = reread_events(args.log_since, args.log_until, args.threshold, args.unit)
        print(f"{len(ev)} prompt read(s) over {args.threshold:,} tokens "
              f"in {args.unit} since {args.log_since}" + (f" until {args.log_until}" if args.log_until else ""))
        for e in ev:
            print(f"  {e['time']}  task {e['task']}  {e['tokens']:,} tokens")
        return

    out = run_offline(args)
    if args.rows:
        print("turn  prompt request  reread  window  summary  notes")
        for r in out["rows"]:
            print(f"{r['turn']:>4} {r['prompt_tokens_max']:>7,} {r['request_tokens_max']:>7,} {r['reread_first_call']:>7,} "
                  f"{r['window_after_turn']:>7,} {r['summary_tokens']:>8,}  {' | '.join(r['fold_notes'])}")
    s = out
    print(f"\nsettings {s['settings']}  overhead {s['overhead_tokens']:,} tok  "
          f"mean turn {s['mean_turn_tokens']:,} tok  turns {s['turns']}")
    print(f"folds {s['folds']} (first at turn {s['first_fold_turn']}), summary condensed {s['summary_condensed']}x")
    print(f"#1 size: max live prompt after first fold = {s['judging_1_max_prompt_after_first_fold']:,} "
          f"tokens by the agent's own accounting -> {'PASS' if s['judging_1_pass'] else 'FAIL'} (< 45,000)")
    print(f"   (the request as actually sent, chars/4: max {s['judging_1_max_request_after_first_fold']:,} "
          f"after the first fold; the agent's accounting counts every tool's FULL description, "
          f"the request carries the mode's tools at one sentence each)")
    print(f"#2 fold cost (MODELLED prefix cache): max re-read on a fold turn = "
          f"{s['judging_2_max_fold_reread']:,} (window cap {s['judging_2_cap']:,}); "
          f"max on a non-fold turn = {s['judging_2_max_nonfold_reread']:,}")
    for f in s["judging_2_per_fold"]:
        print(f"     turn {f['turn']:>3}: re-read {f['reread_tokens']:>6,}  window after {f['window_after_fold']:>6,}  {' | '.join(f['note'])}")
    for label, rc in (("after", s["judging_3_at_turn"]), ("final", s["judging_3_final"])):
        if rc:
            print(f"#3 recall at turn {rc['turn']} ({label}): verbatim path {rc['verbatim_right']}/20; "
                  f"still in the live summary {rc['in_live_summary']}/20; card path: {rc['card_path']}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
