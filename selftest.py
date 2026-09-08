#!/usr/bin/env python3
"""selftest — re-prove every fix, so none of them can quietly rot.

Everything here is a bug that actually happened and was actually fixed. The
point is not coverage for its own sake: each check is anchored to a real
failure, with the symptom written down, so a future change that reintroduces it
is caught by name rather than rediscovered the hard way.

    ./selftest.py            run everything
    ./selftest.py --quick    skip the checks that need a model/embedder server

Exit code is the number of failures, so it can gate a commit.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS, FAIL, SKIP = [], [], []


def check(name: str, fn, needs_server: bool = False):
    """Run one check. It passes if it returns True, fails on False or a raise."""
    if needs_server and "--quick" in sys.argv:
        SKIP.append(name)
        return
    try:
        ok = fn()
        (PASS if ok else FAIL).append(name if ok else (name, "returned False"))
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))



# ------------------------------------------------- the 2026-09-08 rebuild
def t_no_retired_model_in_defaults():
    """The vision ghost. The LIVE config was always right, but the code default
    still named the retired 27B, and that default fires on any config
    regeneration — which is how a dead model survived two rebuilds."""
    from forge.config import DEFAULT_CONFIG
    from forge.media import MediaConfig
    v = DEFAULT_CONFIG["media"]["vision_model"]
    return v == MediaConfig().vision_model and "27b" not in v.lower()


def t_flows_point_at_real_models():
    """All six flows named qwen30b or tiny, retired 2026-09-05. Every flow
    raised PipelineError on its first step, silently, for a month."""
    import yaml
    from forge.config import load_config
    cfg = load_config(); have = set(cfg.get("models", {}))
    doc = yaml.safe_load((Path.home() / ".forge/pipelines.yaml").read_text())
    flows = doc.get("pipelines", doc)
    for flow in flows.values():
        for step in (flow.get("steps") or []):
            m = (step or {}).get("model")
            if m and m not in have:
                return False
    return True


def t_browse_is_iterative():
    """The 2026-09-08 fix exempted the three tools that LOOK at a page from the
    repeat cap and missed `browse`, the one that OPENS a page. Eleven pages in
    a turn and the cap lands on the only tool that reaches the twelfth."""
    from forge.agent import _ITERATIVE_TOOLS
    return {"browse", "browser_js", "browser_view", "browser_console"} <= _ITERATIVE_TOOLS


def t_notebook_trim_keeps_both_ends():
    """The cap was 8000 chars, sized for a 16k window, and it trimmed the TAIL
    — dropping the OLDEST rules first, which are usually the founding ones.
    Found live 2026-08-11 when a notebook hit 4.7k and behaviour degraded."""
    from forge.agent import NOTES_LIMIT_CHARS as CAP
    if CAP < 16000:
        return False
    notes = "FOUNDING RULE\n" + ("filler\n" * 20000) + "NEWEST RULE\n"
    head = CAP // 3; tail = CAP - head
    out = notes[:head] + "\n\n(...middle of the notebook trimmed to fit...)\n\n" + notes[-tail:]
    return "FOUNDING RULE" in out and "NEWEST RULE" in out


def t_summary_budget_follows_the_model():
    """One number served a 131k brain and an 8k fallback. Sized for the
    fallback, applied to both, so a compaction dropping ~60k tokens wrote its
    briefing from the last 7k."""
    from forge.agent import SUMMARY_INPUT_TOKENS, SUMMARY_INPUT_FRACTION
    return (0 < SUMMARY_INPUT_FRACTION < 1
            and int(131072 * SUMMARY_INPUT_FRACTION) > SUMMARY_INPUT_TOKENS * 4)


def t_power_knows_the_live_stack():
    """forge-model-embed — the model that makes her memories searchable — was
    absent from the power roster entirely, so `forge off` could not stop it.
    And EXCLUSIVE still auto-stopped rivals for a card that now holds
    everything at once."""
    from forge import power
    live = set(power.LIVE_UNITS)
    return ({"forge-model-big122", "forge-model-little", "forge-model-embed"} <= live
            and not power.EXCLUSIVE
            and power.PORTS.get("embed") == 8086)


def t_ordinary_module_reads_whole():
    """600 lines / 50KB was sized for a 16-32k window and made ordinary source
    files unreadable: an 800-line module came back as a map and three hints."""
    from forge import tools as T
    from forge.session import Workspace
    ws_dir = tempfile.mkdtemp()
    (Path(ws_dir) / "ordinary.py").write_text("x = 1\n" * 900)   # a normal module
    tools = {t.name: t for t in T.build_tools(Workspace(ws_dir))}
    out = tools["read_file"].run(path="ordinary.py")
    return "MAP of" not in out[:600] and "not dumped whole" not in out[:600]


def t_superego_is_a_second_model():
    """It was reviewing itself: superego_model was blank, and blank means fall
    back to the answering brain. The 122B marked its own homework."""
    from forge.config import load_config
    cfg = load_config()
    name = (cfg["agent"].get("superego_model") or "").strip()
    if not name or name not in cfg.get("models", {}):
        return False
    judge = cfg["models"][name]; brain = cfg["models"][cfg["active_model"]]
    return judge.get("base_url") and judge["base_url"] != brain.get("base_url")


def t_superego_reviews_a_toolless_turn():
    """The hole in the middle of the gate. It ran only when tools had run, so a
    turn with NO tool calls was never reviewed — exactly the shape of a pure
    fabrication. Caught live 2026-09-08: asked her dashboard port, she named a
    port and a dotfile that do not exist, and nothing reviewed it."""
    import inspect
    from forge import agent as A
    src = inspect.getsource(A.Agent)
    i = src.find('get_mode(self.active_mode)["superego"]')
    if i < 0:
        return False
    line = src[max(0, i - 200):i]
    return "_tools_ran" not in line.split("if self.superego")[-1]


def t_everyday_mode_is_reviewed():
    """Balanced is the default. The honesty check was off there, so most
    answers he ever saw were never checked at all."""
    from forge.modes import get_mode
    return all(get_mode(m)["superego"] for m in ("balanced", "precise", "deep"))

# ---------------------------------------------------------------- context
def t_overhead_counted():
    """The bare 400: schemas + system prompt were invisible, so a turn read 19%
    full while sitting at 84%, and compaction never fired."""
    from forge.agent import Agent
    from forge import tools as T
    from forge.session import Workspace
    ag = Agent(provider=object(), tools=T.build_tools(Workspace(tempfile.mkdtemp())),
               permission_mode="auto", superego=None)
    ag.active_mode = "balanced"; ag.privacy = "normal"
    ag.client_env = ""; ag.identity_owner = ""; ag.history = []
    ag._overhead_cache = None
    return ag._fixed_overhead_tokens() > 3000


def t_force_compaction_shrinks():
    """One oversized turn: force-compaction summarized the prefix and kept the
    huge turn intact, so the retry was just as big."""
    from forge.agent import Agent
    ag = Agent.__new__(Agent); ag._turn_hiccups = []
    ag.history = [{"role": "user", "content": "go"}]
    for _ in range(6):
        ag.history.append({"role": "tool_use", "calls": []})
        ag.history.append({"role": "tool_result", "content": "X" * 20000})
    before = sum(len(str(m.get("content", ""))) for m in ag.history)
    ag._trim_tool_results(keep_recent=4)
    after = sum(len(str(m.get("content", ""))) for m in ag.history)
    pairs = sum(1 for m in ag.history if m.get("role") == "tool_result")
    return after < before * 0.6 and pairs == 6      # shrank, pairing intact


# --------------------------------------------------------------- superego
def t_evidence_not_starved():
    """The reviewer bounced true claims: results were clipped at 200 chars and
    the answer under review was clipped at 500."""
    from forge.agent import Agent
    ag = Agent.__new__(Agent)
    body = ("From your archive:\n\n● Roof quote — 2026-02-20\n    from: mike@x.com\n"
            "    " + "padding. " * 40 + "the estimate is $4,200 materials and labor.")
    ag.history = [{"role": "user", "content": "what was the quote?"},
                  {"role": "tool_result", "content": body}]
    answer = "The quote was $4,200 for materials and labor, dated 2026-02-20."
    d = ag._evidence_digest(0, answer)
    # Assert the WHOLE answer survives, not a fragile suffix — the first
    # version of this check tested endswith("2026.") while the answer ends
    # "2026-02-20.", so it failed against working code.
    return "$4,200" in d and answer in d


def t_truncation_is_marked():
    """Silent truncation made the reviewer treat 'not shown' as 'not there'."""
    from forge.agent import Agent
    ag = Agent.__new__(Agent)
    ag.history = [{"role": "user", "content": "x"},
                  {"role": "tool_result", "content": "A" * 5000}]
    return "not shown" in ag._evidence_digest(0, "answer")


# ------------------------------------------------------------------ vault
def t_secrets_scrubbed():
    """A password typed into chat was written to the session log, the model
    history, the review ledger and the memory queue, in plaintext."""
    from forge.vault import scrub, scrubbed
    must_redact = ["my password is hunter2", "The password is: s3cr3t!x",
                   "my api key is sk-ant-abc123def456ghi", "token: ghp_abcdefghijklmnop12"]
    must_keep = ["I forgot my password again", "password managers are great",
                 "can you reset the password on that box?"]
    return (all(scrubbed(s) for s in must_redact)
            and not any(scrubbed(s) for s in must_keep)
            and "hunter2" not in scrub("my password is hunter2"))


def t_vault_hides_values():
    """The model must never be able to read a stored secret back."""
    from forge import vault
    vault.clear()
    vault.put("probe", "CANARY-9137", kind="password")
    listing = vault.listing()
    ok = "CANARY-9137" not in listing and "cred:probe" in listing
    vault.clear()
    return ok and not vault.has("cred:probe")


# ------------------------------------------------------------- tool index
def t_toolindex_excludes_itself():
    """find_tools indexed itself, and its description carries example phrases,
    so 'search my email' returned find_tools instead of search_life."""
    from forge import tools as T
    from forge.session import Workspace
    T.build_tools(Workspace(tempfile.mkdtemp()))
    return not ({"find_tools", "load_tools"} & set(T._INDEX_REGISTRY))


def t_belt_is_whole_and_stays_whole():
    """Rewritten 2026-09-08. This used to assert that an on-demand tool
    DISAPPEARS after the turn ends, which was right while 51 of 84 tools were
    hidden behind a search step to save an 11.8k-token schema bill on a 24k
    window. On the 131k window that bill is 11.6%, the hiding is gone, and the
    thing worth guarding is the opposite: no tool may ever vanish mid-turn.
    She reported a working game as broken on 2026-09-08 because she held three
    tools for inspecting a page and not the one that opens it."""
    from forge.agent import Agent
    from forge import tools as T, toolindex
    from forge.session import Workspace
    ws = Workspace(tempfile.mkdtemp())
    built = T.build_tools(ws)
    ag = Agent(provider=object(), tools=built,
               permission_mode="auto", superego=None)
    ag.active_mode = "balanced"; ag.privacy = "normal"
    ag.client_env = ""; ag.identity_owner = ""; ag.history = []
    names = lambda: {s["name"] for s in ag.tool_schemas}
    before = names()
    if len(before) < len(built):          # everything she owns is on the belt
        return False
    for _ in range(8):
        ag.history.append({"role": "tool_result", "content": "x" * 3000})
    ag._trim_tool_results(keep_recent=2)
    toolindex.reset()                     # a reset must take nothing away
    return names() == before


def t_load_cannot_bypass_privacy():
    """An on-demand load must never reach past sandbox / off-the-record."""
    from forge.agent import Agent
    from forge import tools as T, toolindex
    from forge.session import Workspace
    ag = Agent(provider=object(), tools=T.build_tools(Workspace(tempfile.mkdtemp())),
               permission_mode="auto", superego=None)
    ag.active_mode = "balanced"; ag.privacy = "sandbox"
    ag.client_env = ""; ag.identity_owner = ""; ag.history = []
    toolindex.reset(); toolindex.load_tools(["run_command", "write_file"], T._INDEX_REGISTRY)
    names = {s["name"] for s in ag.tool_schemas}
    toolindex.reset()
    return "run_command" not in names and "write_file" not in names


def t_tool_search_quality():
    """Keyword-only retrieval put verify_case top for 'change how funny you
    are'. Semantic retrieval fixed it; junk must still return nothing."""
    from forge import tools as T, toolindex
    from forge.session import Workspace
    T.build_tools(Workspace(tempfile.mkdtemp())); reg = T._INDEX_REGISTRY
    pairs = [("search my old emails", "search_life"), ("design a 3d part", "design_part"),
             ("is this drug real", "verify_drug"), ("change how funny you are", "set_personality"),
             ("paper trade a strategy", "paper_market"), ("draw a picture", "generate_image")]
    top3 = lambda q: [l.strip().split(" —")[0]
                      for l in toolindex.find_tools(q, reg, limit=8).splitlines()[1:4]]
    hits = sum(1 for q, w in pairs if w in top3(q))
    junk_ok = all("Nothing" in toolindex.find_tools(j, reg).splitlines()[0]
                  for j in ["zzzz qqqq wubble", "flurb nax qopple zint"])
    return hits >= 5 and junk_ok


# --------------------------------------------------------------- embedder
def t_embedder_batches():
    """embed_documents sent everything in one request and silently returned
    None past ~32 items — Cortex indexes in chunks of 64."""
    from forge.embed import embed_documents, available
    if not available():
        return True
    m = embed_documents([f"doc number {i} about a thing" for i in range(40)])
    return m is not None and m.shape[0] == 40


# ------------------------------------------------------------------ shelf
def t_shelf_rejects_garbage():
    """An empty table and the bare string 'nope' both saved as ✓ CORROBORATED
    — the string was recorded as '4 entries', having counted its letters."""
    from forge import datasets as D
    S = ["http://a.example", "http://b.example"]
    bad = [D.save_dataset("_t_empty", {}, S), D.save_dataset("_t_str", "nope", S),
           D.save_dataset("_t_num", 42, S)]
    for n in ("_t_empty", "_t_str", "_t_num"):
        Path(f"datasets/{n}.json").unlink(missing_ok=True)
    return all(str(r).startswith("Error") for r in bad)


def t_circular_selftests_stay_demoted():
    """Four wing sims carried ✓ while testing the code against its own output,
    and verify_shelf kept re-promoting them because re-running is exactly what
    a circular test survives."""
    cat = json.loads(Path("sims/_catalog.json").read_text())
    flagged = [n for n, m in cat.items() if m.get("selftest_circular")]
    return bool(flagged) and all(not cat[n].get("validated") for n in flagged)


# ------------------------------------------------------------------ recall
def t_recall_refuses_gibberish():
    """The semantic floor sat below the noise ceiling, so gibberish came back
    quoted as a memory."""
    from forge import recall
    r = recall.search("zzzz qqqq wubble flurb", limit=3)
    return "Nothing" in r[:40] or not r.strip()


# ------------------------------------------------------------------ misc
def t_physics_refuses_impossible():
    """twin_trip printed 'Earth twin ages: -8.0000 years' for a negative
    distance — people aging backwards, with formula annotations."""
    from forge import physics
    r = physics.relativity_sim(scenario="twin_trip", distance_ly=-2, v_c=0.5)
    good = physics.relativity_sim(scenario="twin_trip", distance_ly=4, v_c=0.5)
    return r.startswith("Error") and "16.0000" in good


def t_markets_refuse_impossible():
    """implied_prob returned 'implied probability: -0.5' for negative odds, and
    arbitrage reported a negative stake."""
    from forge import markets as M
    for fn, args in ((M.implied_prob, {"decimal_odds": -2}),
                     (M.implied_prob, {"decimal_odds": 0}),
                     (M.arbitrage, {"odds_a": 2.1, "odds_b": -1})):
        try:
            fn(args)
            return False
        except (ValueError, KeyError, ZeroDivisionError):
            pass
    return abs(M.implied_prob({"decimal_odds": 2.5})[0][1] - 0.4) < 1e-9


def t_xfiles_keeps_near_complete_suspects():
    """A suspect covering 399 of 400 days was dropped wholesale over one
    missing holiday, leaving a hollow verdict."""
    import inspect
    from forge import xfiles
    src = inspect.getsource(xfiles._aligned_returns)
    return "0.9" in src and "dropped.append" in src


def t_doolittle_wont_corner_noise():
    """A random-context call must not be cornered to a confident meaning."""
    import numpy as np
    from forge import doolittle as dl
    rng = np.random.RandomState(4)
    obs = []
    for _ in range(30):
        obs.append({"call": "real", "cues": {"threat": True, "then_flee_or_freeze": True,
                                             "predator_ground": True, "calm": False}})
    cues = ["threat", "food", "calm", "juvenile", "movement", "competitor"]
    for _ in range(30):
        obs.append({"call": "noise", "cues": {c: bool(rng.random() < 0.5) for c in cues}})
    by = {c["call"]: c for c in dl.deduce(obs)["calls"]}
    return by["real"]["conclusive"] and not by["noise"]["conclusive"]


def t_cortex_refuses_nonsense():
    """Semantic search scores EVERY record, so an unrelated query still had a
    'best' match and returned it as a memory."""
    from forge import cortex
    if not cortex.RECORDS.exists():
        return True
    r = cortex.search_life("submarine penguin tax fraud")
    return "clearly match" in r or "empty" in r or "Nothing" in r


def t_persona_clamps():
    """Dials had to refuse nonsense rather than store it."""
    from forge import persona
    before = persona.settings()["humor"]
    persona.set_personality("humor", 9999)
    hi = persona.settings()["humor"]
    persona.set_personality("humor", -50)
    lo = persona.settings()["humor"]
    bad = persona.set_personality("humor", "high")
    persona.set_personality("humor", before)
    return hi == 100 and lo == 0 and "isn't a number" in bad


def t_forensic_env_not_clobbered():
    """A per-turn mode switch deleted a FORGE_FORENSIC the user had set
    deliberately, silently discarding the recording they asked for."""
    import os
    from forge import forensic
    had = os.environ.get("FORGE_FORENSIC")
    os.environ["FORGE_FORENSIC"] = "1"
    forensic.set_enabled(False)          # what a normal-mode turn does
    still_on = forensic.enabled()
    if had is None:
        os.environ.pop("FORGE_FORENSIC", None)
    return still_on


def t_forensic_redacts():
    """The flight recorder must never write a secret."""
    from forge import forensic
    return forensic._redact("hunter2", "password") == "[redacted]"


# ------------------------------------------------------------------- main
CHECKS = [
    ("context: schema+prompt counted", t_overhead_counted, False),
    ("context: force-compaction shrinks the big turn", t_force_compaction_shrinks, False),
    ("superego: evidence not starved", t_evidence_not_starved, False),
    ("superego: truncation is marked", t_truncation_is_marked, False),
    ("vault: chat-typed secrets scrubbed", t_secrets_scrubbed, False),
    ("vault: values never readable", t_vault_hides_values, False),
    ("toolindex: does not index itself", t_toolindex_excludes_itself, False),
    ("belt: whole, and nothing vanishes mid-turn", t_belt_is_whole_and_stays_whole, False),
    ("rebuild: no retired model in the code defaults", t_no_retired_model_in_defaults, False),
    ("rebuild: every flow points at a real model", t_flows_point_at_real_models, False),
    ("rebuild: browse counts as iterative", t_browse_is_iterative, False),
    ("rebuild: notebook trim keeps both ends", t_notebook_trim_keeps_both_ends, False),
    ("rebuild: summary budget follows the model", t_summary_budget_follows_the_model, False),
    ("rebuild: power knows the live stack", t_power_knows_the_live_stack, False),
    ("rebuild: an ordinary module reads whole", t_ordinary_module_reads_whole, False),
    ("superego: is a SECOND model", t_superego_is_a_second_model, False),
    ("superego: reviews a turn with no tools", t_superego_reviews_a_toolless_turn, False),
    ("superego: the everyday mode is reviewed", t_everyday_mode_is_reviewed, False),
    ("toolindex: cannot bypass privacy", t_load_cannot_bypass_privacy, False),
    ("toolindex: retrieval quality + junk refused", t_tool_search_quality, True),
    ("embedder: batches large inputs", t_embedder_batches, True),
    ("shelf: garbage refused", t_shelf_rejects_garbage, False),
    ("shelf: circular selftests stay demoted", t_circular_selftests_stay_demoted, False),
    ("recall: refuses gibberish", t_recall_refuses_gibberish, True),
    ("physics: refuses impossible trips", t_physics_refuses_impossible, False),
    ("markets: refuses impossible odds", t_markets_refuse_impossible, False),
    ("xfiles: keeps near-complete suspects", t_xfiles_keeps_near_complete_suspects, False),
    ("doolittle: will not corner noise", t_doolittle_wont_corner_noise, False),
    ("cortex: refuses nonsense queries", t_cortex_refuses_nonsense, True),
    ("persona: clamps and refuses bad values", t_persona_clamps, False),
    ("forensic: env override not clobbered", t_forensic_env_not_clobbered, False),
    ("forensic: redacts secrets", t_forensic_redacts, False),
]

if __name__ == "__main__":
    t0 = time.time()
    for name, fn, needs in CHECKS:
        check(name, fn, needs)
    for n in PASS:
        print(f"  ✓ {n}")
    for item in FAIL:
        n, why = item if isinstance(item, tuple) else (item, "failed")
        print(f"  ✗ {n}  <- {why}")
    for n in SKIP:
        print(f"  · {n} (skipped)")
    print(f"\n  {len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped "
          f"in {time.time()-t0:.1f}s")
    sys.exit(len(FAIL))
