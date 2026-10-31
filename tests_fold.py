"""B1a self-test: the fold's append-only invariant. Run alone."""
import forge.fold as F


def turn(i):
    return {"role": "user", "content": f"question {i} " + "x" * 300}


def ans(i):
    return {"role": "assistant",
            "content": f"answer {i} — decided: keep the number 42 and the path /tmp/f{i}.py "
                       + "y" * 300}


def main():
    hist = []
    for i in range(1, 13):
        hist += [turn(i), ans(i)]

    cut = F.select_block(hist, window_cap_tokens=250)
    assert cut > 0, "selection declined"
    block, window = hist[:cut], hist[cut:]

    # first fold: summary created at the boundary
    summary1, window1 = F.apply_fold(None, None, window, block)
    assert F.check_invariant((None, window), (summary1, window1)), "invariant failed on first fold"
    assert summary1["content"].startswith(F.SUMMARY_MARKER)
    # the folded block's verbatim filler is gone from the window (the retained
    # tail keeps its own recent turns, so check the folded contents specifically)
    joined = "\n".join(str(m.get("content", "")) for m in window1)
    for folded_user in [m["content"] for m in block if m.get("role") == "user"]:
        assert folded_user not in joined, "folded turn still in the window"
    # the distill kept what the brief says survives: a stated number, a path
    assert "42" in summary1["content"] and "/tmp/f" in summary1["content"], \
        "distill dropped stated facts"

    # second fold: earlier summary bytes survive verbatim (append-only)
    hist2 = [summary1] + window1 + [turn(13), ans(13), turn(14), ans(14), turn(15), ans(15)]
    cut2 = F.select_block(hist2, window_cap_tokens=250)
    assert cut2 > 0, "second selection declined"
    before2 = (summary1, hist2[cut2:])
    summary2, window2 = F.apply_fold(None, summary1, hist2[cut2:], hist2[:cut2])
    assert F.check_invariant(before2, (summary2, window2)), "append-only violated"
    assert summary2["content"].startswith(summary1["content"]), \
        "earlier summary bytes changed"

    # a chatter-only block folds to the nothing-of-record line; invariant holds
    chatter = [{"role": "user", "content": "hello there friend " + "z" * 300},
               {"role": "assistant", "content": "hi! nice to meet you " + "w" * 300}]
    summary3, window3 = F.apply_fold(None, summary2, [], chatter)
    assert F.check_invariant((summary2, chatter), (summary3, window3)), \
        "invariant failed on chatter fold"
    assert "nothing of record" in summary3["content"]

    print("PASS: fold is append-only in front")


if __name__ == "__main__":
    main()
