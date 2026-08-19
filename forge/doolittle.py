"""The Deduction Pad — translation by ELIMINATION, not by reading minds.

You can't observe what an animal call MEANS. But you can play Clue: rule out
what it can't mean, one card at a time, and whatever's left standing is the
suspect. This never claims 'call X means danger'. It says 'for call X we ruled
out food, greeting, mating (they fire in contexts these don't fit); the alarm
family is still on the board; 70% of the deck is face-up.'

The pieces:
  * CALLS come from study_calls (the discrete signals — the cards).
  * OBSERVATIONS are the crime scenes: for each time a call fired, what was
    true in the world — an ethologist's log of context CUES (threat present?
    food present? did the animal flee after? juvenile calling?).
  * MEANINGS is a polymath library of candidate glosses, each tied to the cues
    it PREDICTS. The wider the net of guesses, the finer the elimination.

The engine cross-references: a meaning is RULED OUT for a call when the call
fires largely WITHOUT the context that meaning requires, and SURVIVES when the
call's context is both consistent with it AND more specific to it than chance.
Every elimination is kept on the pad — the negative space is the progress.

Honest to the bone: it corners meaning, it never decodes it. A surviving
hypothesis is a lead to field-test, not a translation.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

RENDERS = Path.home() / "forge" / "datasets" / "doolittle"
BG = (13, 16, 23); INK = (230, 236, 245); SOFT = (150, 162, 178)
CYAN = (59, 214, 228); GOLD = (245, 174, 61); GREEN = (111, 208, 140); RED = (200, 90, 74)

# The polymath meaning library. Each candidate gloss predicts which context
# CUES should hold when a call carrying it fires. Drawn from ethology across
# taxa — the breadth IS the method. `cues` = must tend to be present; `absent`
# = should tend to be ABSENT (helps eliminate look-alikes).
MEANINGS = [
    {"id": "alarm",     "label": "Alarm / predator",     "cues": ["threat", "then_flee_or_freeze"], "absent": ["calm"]},
    {"id": "food",      "label": "Food / foraging",      "cues": ["food", "then_approach"],          "absent": ["threat"]},
    {"id": "contact",   "label": "Contact / 'where are you'", "cues": ["conspecific_far", "then_regroup"], "absent": []},
    {"id": "affil",     "label": "Affiliation / greeting", "cues": ["conspecific_near", "calm"],      "absent": ["threat", "conflict"]},
    {"id": "mate",      "label": "Mating / courtship",   "cues": ["breeding", "opposite_sex_near"],   "absent": ["threat"]},
    {"id": "territory", "label": "Territory / boundary", "cues": ["intruder", "boundary"],            "absent": ["calm"]},
    {"id": "distress",  "label": "Distress / pain",      "cues": ["isolation_or_injury"],             "absent": ["calm"]},
    {"id": "dominance", "label": "Dominance / aggression", "cues": ["conflict", "competitor"],        "absent": ["calm"]},
    {"id": "play",      "label": "Play",                 "cues": ["juvenile", "calm"],                "absent": ["threat", "conflict"]},
    {"id": "id",        "label": "Location / self-ID",   "cues": ["movement"],                        "absent": []},
    {"id": "beg",       "label": "Begging",              "cues": ["juvenile", "parent_near", "food"], "absent": ["threat"]},
    {"id": "recruit",   "label": "Recruitment / rally",  "cues": ["resource_found", "then_group_move"], "absent": []},
]
RULE_OUT = 0.30      # support below this -> the call fires mostly without the context -> ruled out
STRONG = 0.60        # support above this AND specific -> a live lead


def _support(obs_for_call, meaning):
    """Mean fraction of the meaning's required cues present when this call
    fired, minus a penalty for its should-be-absent cues showing up."""
    if not obs_for_call:
        return 0.0
    req = meaning["cues"]
    presence = []
    for o in obs_for_call:
        cues = o.get("cues", {})
        pos = np.mean([1.0 if cues.get(c) else 0.0 for c in req]) if req else 0.0
        pen = np.mean([1.0 if cues.get(c) else 0.0 for c in meaning["absent"]]) if meaning["absent"] else 0.0
        presence.append(max(0.0, pos - 0.5 * pen))
    return float(np.mean(presence))


def _base_rate(all_obs, meaning):
    """How often the meaning's cues hold across ALL calls — the chance floor,
    so a call is only credited when it's MORE than baseline-specific."""
    req = meaning["cues"]
    if not all_obs or not req:
        return 0.0
    return float(np.mean([np.mean([1.0 if o.get("cues", {}).get(c) else 0.0 for c in req])
                          for o in all_obs]))


def deduce(observations, labels=None):
    """Run the pad. observations = list of {"call": <id/label>, "cues": {cue: bool}}.
    Returns per-call: ruled_out, standing (with support + lift), % of deck
    eliminated. Empty structure if there's nothing to work with."""
    if not observations:
        return {"calls": [], "note": "no observations — log some crime scenes first."}
    by_call = {}
    for o in observations:
        by_call.setdefault(o.get("call"), []).append(o)
    pad = {"calls": []}
    for call, obs in sorted(by_call.items(), key=lambda x: str(x[0])):
        ruled_out, standing = [], []
        for m in MEANINGS:
            sup = _support(obs, m)
            base = _base_rate(observations, m)
            lift = sup / base if base > 0.02 else (sup / 0.02)
            # a meaning resting on ONE common cue is too easy — weight by how
            # many cues it stakes its claim on (a 2-3 cue hypothesis that holds
            # is far more telling than a 1-cue coincidence)
            depth = min(1.0, 0.55 + 0.25 * len(m["cues"]))
            sup *= depth
            if sup < RULE_OUT or lift < 1.35:
                ruled_out.append({"id": m["id"], "label": m["label"],
                                  "support": round(sup, 2),
                                  "why": ("fires mostly without its context" if sup < RULE_OUT
                                          else "no more specific than chance")})
            else:
                standing.append({"id": m["id"], "label": m["label"],
                                 "support": round(sup, 2), "lift": round(lift, 1)})
        standing.sort(key=lambda x: -x["support"])
        elim_pct = round(100 * len(ruled_out) / len(MEANINGS))
        top_sup = standing[0]["support"] if standing else 0.0
        runner = standing[1]["support"] if len(standing) > 1 else 0.0
        # cornered only if the lead is strong AND clearly ahead — else the cues
        # just don't discriminate (honest 'inconclusive', not a lucky pin)
        conclusive = bool(standing) and top_sup >= STRONG and (top_sup - runner) >= 0.12
        pad["calls"].append({"call": call, "n_obs": len(obs),
                             "eliminated_pct": elim_pct, "conclusive": conclusive,
                             "ruled_out": ruled_out, "standing": standing})
    return pad


# ---- render the pad: calls × meanings grid (Clue sheet) -----------------
def _render(pad, title):
    calls = pad["calls"]
    if not calls:
        return None
    cols = MEANINGS
    cw, rh = 128, 40
    W = 210 + cw * 1  # legend width handled below; use a compact matrix
    W = 230 + 44 * len(cols)
    H = 90 + rh * len(calls) + 40
    img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
    d.text((14, 12), f"\U0001f5d2 the deduction pad — {title}", fill=CYAN)
    d.text((14, 30), "green = still standing (a lead) · red = ruled out · brighter = stronger",
           fill=SOFT)
    x0, y0 = 200, 60
    # column headers (meaning ids, rotated-ish: just short)
    for j, m in enumerate(cols):
        d.text((x0 + j * 44 + 4, y0 - 14), m["id"][:5], fill=SOFT)
    for i, c in enumerate(calls):
        y = y0 + i * rh
        d.text((14, y + 10), f"call {str(c['call'])[:14]}", fill=INK)
        d.text((150, y + 10), f"{c['eliminated_pct']}%", fill=GOLD)
        standing_ids = {s["id"]: s["support"] for s in c["standing"]}
        for j, m in enumerate(cols):
            x = x0 + j * 44
            if m["id"] in standing_ids:
                sup = standing_ids[m["id"]]
                g = int(80 + 175 * min(1, sup))
                d.rectangle([x, y + 4, x + 40, y + rh - 4], fill=(30, g, 60))
                d.text((x + 14, y + 12), "✓", fill=(230, 255, 235))
            else:
                d.rectangle([x, y + 4, x + 40, y + rh - 4], fill=(40, 22, 20), outline=(70, 40, 36))
                d.text((x + 15, y + 12), "✗", fill=(150, 90, 84))
    RENDERS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", title)[:40]
    out = RENDERS / f"pad_{safe}.png"
    img.save(out)
    return out


def deduce_meaning(observations, title: str = "case") -> str:
    """Play Clue with animal calls: given a log of OBSERVATIONS (each = a call
    plus the context cues true when it fired), rule out the meanings it CAN'T
    carry and report what's left standing. `observations` is a list of
    {"call": <name>, "cues": {"threat": true, "then_flee_or_freeze": true, ...}}.
    Known cues: threat, then_flee_or_freeze, food, then_approach,
    conspecific_near, conspecific_far, then_regroup, calm, breeding,
    opposite_sex_near, intruder, boundary, isolation_or_injury, conflict,
    competitor, juvenile, parent_near, movement, resource_found,
    then_group_move. Renders the pad. NEVER claims a call's meaning — it
    corners it by elimination; a surviving lead is for field-testing, not a
    translation."""
    if isinstance(observations, str):
        import json
        try:
            observations = json.loads(observations)
        except Exception as e:
            return f"observations wasn't valid JSON: {e}"
    pad = deduce(observations)
    if not pad["calls"]:
        return pad.get("note", "nothing to deduce.")
    out = _render(pad, title)
    lines = [f"THE DEDUCTION PAD — {title} ({len(observations)} observations):"]
    for c in pad["calls"]:
        lines.append(f"\n● call '{c['call']}' ({c['n_obs']} sightings) — "
                     f"{c['eliminated_pct']}% of the meaning-deck ruled out")
        if c["standing"]:
            lead = c["standing"][0]
            lines.append(f"    still standing: " + ", ".join(
                f"{s['label']} (support {s['support']})" for s in c["standing"][:4]))
            if c.get("conclusive"):
                lines.append(f"    → prime suspect: {lead['label']} — strong and clearly ahead; "
                             "a lead worth field-testing.")
            else:
                lines.append("    → INCONCLUSIVE — nothing is strong-and-separated enough to pin. "
                             "The cues logged don't discriminate yet; log more sightings.")
        else:
            lines.append("    nothing survived — the cues logged don't fit any candidate, "
                         "or the observations are too thin/noisy. Log more, or add candidates.")
        if c["ruled_out"]:
            lines.append(f"    ruled out: " + ", ".join(r["label"] for r in c["ruled_out"][:5])
                         + (" …" if len(c["ruled_out"]) > 5 else ""))
    if out:
        lines.append(f"\nRendered the pad to {out} — look_at_image it (the Clue sheet: "
                     "calls × meanings, green standing / red ruled out).")
    lines.append("HARD LIMIT: this ELIMINATES, it does not translate. A standing meaning is a "
                 "lead to test in the field, never a claim about what the animal said.")
    return "\n".join(lines)
