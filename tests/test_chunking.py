from claude_dingtalk_bridge.chunking import chunk_markdown, pad_code_tail


def _within(pieces, budget):
    return all(len(p.encode("utf-8")) <= budget for p in pieces)


def test_short_text_returned_unchanged():
    assert chunk_markdown("hello world", byte_budget=100) == ["hello world"]


def test_splits_at_blank_line_between_paragraphs():
    text = "AAAA\n\nBBBB"
    pieces = chunk_markdown(text, byte_budget=6)
    assert pieces == ["AAAA", "BBBB"]
    assert _within(pieces, 6)


def test_oversized_paragraph_splits_at_line_boundary():
    text = "L1\nL2\nL3\nL4"
    pieces = chunk_markdown(text, byte_budget=6)
    assert pieces == ["L1\nL2", "L3\nL4"]
    assert _within(pieces, 6)


def test_single_line_longer_than_budget_is_hard_cut():
    pieces = chunk_markdown("ABCDEFGHIJ", byte_budget=4)
    assert pieces == ["ABCD", "EFGH", "IJ"]
    assert _within(pieces, 4)


def test_hard_cut_does_not_split_multibyte_char():
    # Each Chinese char is 3 UTF-8 bytes; budget 4 fits exactly one per chunk.
    pieces = chunk_markdown("你好吗", byte_budget=4)
    assert pieces == ["你", "好", "吗"]
    assert _within(pieces, 4)


def test_cut_inside_code_fence_reopens_with_language():
    text = "```python\naaaa\nbbbb\ncccc\n```"
    pieces = chunk_markdown(text, byte_budget=20)
    assert pieces == [
        "```python\naaaa\n```",
        "```python\nbbbb\n```",
        "```python\ncccc\n```",
    ]
    assert _within(pieces, 20)
    for p in pieces:
        assert p.count("```") % 2 == 0  # balanced fences


def test_cut_inside_fence_without_language_reopens_bare():
    text = "```\np\nq\nr\n```"
    pieces = chunk_markdown(text, byte_budget=9)
    assert pieces == ["```\np\n```", "```\nq\n```", "```\nr\n```"]
    assert _within(pieces, 9)


def test_oversized_line_inside_fence_keeps_fence_wrapping():
    # A single code line longer than the budget must be hard-split into pieces
    # that each stay wrapped in the fence — never emitted as raw (live-markdown)
    # text, which would mangle code containing markdown metacharacters.
    long_line = "L" * 200
    text = f"```python\nshort1\n{long_line}\nshort2\n```"
    pieces = chunk_markdown(text, byte_budget=100)
    assert _within(pieces, 100)
    for p in pieces:
        assert p.split("\n")[0] == "```python"  # every piece opens the fence
        assert p.split("\n")[-1] == "```"  # and closes it
        assert p.count("```") % 2 == 0
    assert "".join(pieces).count("L") == 200  # no code dropped or leaked


def _line_count(piece):
    return piece.count("\n") + 1


def test_splits_by_line_count_even_when_bytes_fit():
    text = "\n".join(f"L{n}" for n in range(10))
    pieces = chunk_markdown(text, byte_budget=10_000, max_lines=3)
    assert all(_line_count(p) <= 3 for p in pieces)
    assert "\n".join(pieces).split("\n") == text.split("\n")  # content preserved


def test_line_budget_counts_reopened_fence_lines():
    # 6 code lines, max 4 lines/chunk. Each continuation chunk spends 2 of its
    # lines on the reopened ``` and the appended closing ```, leaving 2 for code.
    body = "\n".join(f"c{n}" for n in range(6))
    text = f"```python\n{body}\n```"
    pieces = chunk_markdown(text, byte_budget=10_000, max_lines=4)
    assert all(_line_count(p) <= 4 for p in pieces)
    for p in pieces:
        assert p.startswith("```python")
        assert p.endswith("```")
        assert p.count("```") == 2


def test_mixed_prose_and_code_stays_within_budget_with_balanced_fences():
    prose = "\n\n".join(f"Paragraph number {n} with some words." for n in range(6))
    code = "```python\n" + "\n".join(f"line_{n} = {n} * 2" for n in range(40)) + "\n```"
    text = f"{prose}\n\n{code}\n\n{prose}"
    pieces = chunk_markdown(text, byte_budget=120)
    assert len(pieces) > 1
    assert _within(pieces, 120)
    for p in pieces:
        assert p.count("```") % 2 == 0  # no chunk leaves a fence dangling


def _trailing_blanks(piece):
    lines = piece.split("\n")
    n = 0
    for ln in reversed(lines[:-1]):  # skip the closing ``` itself
        if ln == "":
            n += 1
        else:
            break
    return n


def test_pad_code_tail_inserts_blanks_before_close_for_long_chunk():
    code = "\n".join(f"c{n:02d}" for n in range(40))
    piece = f"```python\n{code}\n```"  # 42 lines
    padded = pad_code_tail(piece, min_lines=35, margin=2)
    assert padded.split("\n")[-1] == "```"  # still closed
    assert padded.split("\n")[0] == "```python"
    assert "c39" in padded                   # real code preserved
    assert _trailing_blanks(padded) >= 3      # estimate(1) + margin(2)


def test_pad_code_tail_scales_with_chunk_length():
    short = f"```\n" + "\n".join(f"c{n}" for n in range(40)) + "\n```"
    long = f"```\n" + "\n".join(f"c{n}" for n in range(140)) + "\n```"
    assert _trailing_blanks(pad_code_tail(long, min_lines=35, margin=2)) > (
        _trailing_blanks(pad_code_tail(short, min_lines=35, margin=2))
    )


def test_pad_code_tail_leaves_short_code_chunk_untouched():
    piece = "```python\nc0\nc1\nc2\n```"
    assert pad_code_tail(piece, min_lines=35, margin=2) == piece


def test_pad_code_tail_leaves_prose_chunk_untouched():
    piece = "\n".join(f"prose line {n}" for n in range(50))  # long but no fence
    assert pad_code_tail(piece, min_lines=35, margin=2) == piece


def test_pad_code_tail_recognizes_four_backtick_close():
    # Content using 4-backtick fence to wrap markdown that itself has ``` inside.
    code = "\n".join(f"c{n}" for n in range(40))
    piece = f"````markdown\n{code}\n````"
    padded = pad_code_tail(piece, min_lines=35, margin=2)
    assert padded != piece                               # padding applied
    assert padded.split("\n")[-1] == "````"              # 4-backtick close preserved
    assert _trailing_blanks(padded) >= 3                 # blanks inserted before close


def test_nested_fence_inside_outer_fence_stays_content():
    # A 4-backtick block wraps markdown that itself contains a 3-backtick code
    # block. While inside the outer fence, the inner ```python / ``` lines are
    # content, not toggles (the close is too short to match the opener), so the
    # chunker keeps every chunk wrapped in the 4-backtick outer fence.
    inner = "```python\n" + "\n".join(f"c{n}" for n in range(8)) + "\n```"
    text = f"````markdown\nintro\n{inner}\noutro\n````"
    pieces = chunk_markdown(text, byte_budget=10_000, max_lines=5)
    assert len(pieces) > 1
    for p in pieces:
        assert p.split("\n")[0] == "````markdown"
        assert p.split("\n")[-1] == "````"
    rejoined = "\n".join(pieces)
    assert "```python" in rejoined  # inner fence survived as content
    assert "c7" in rejoined         # inner code body preserved


def test_indented_backtick_line_inside_fence_stays_content():
    # A bare ``` indented 4+ spaces inside an open block is indented code, not a
    # closing fence — it must not toggle the chunker out of the block (which
    # would leak the rest as live markdown). Force a split so fence tracking
    # matters across chunks.
    body = "\n".join(f"    line{n}" if n != 4 else "    ```" for n in range(12))
    text = f"```python\n{body}\n```"
    pieces = chunk_markdown(text, byte_budget=10_000, max_lines=5)
    assert len(pieces) > 1
    for p in pieces:
        assert p.split("\n")[0] == "```python"  # still inside the python fence
        assert p.split("\n")[-1] == "```"
    rejoined = "\n".join(pieces)
    assert "    ```" in rejoined  # the indented fence survived as content


def test_pad_code_tail_pads_when_prose_follows_close():
    # send_markdown can emit a chunk whose code block closes mid-way with
    # trailing prose after it (e.g. "以上是 ... 完整源码"). Without padding
    # the in-block close, DingTalk's tail-drop still chews the code block's
    # last lines — the trailing prose doesn't shield it.
    code = "\n".join(f"c{n:02d}" for n in range(40))
    piece = f"```python\n{code}\n```\n\nThis was the daemon.py source code."
    padded = pad_code_tail(piece, min_lines=35, margin=2)
    assert padded.endswith("This was the daemon.py source code.")
    assert "c39" in padded
    lines = padded.split("\n")
    close_idx = max(i for i, line in enumerate(lines) if line == "```")
    blanks_before_close = 0
    for line in reversed(lines[:close_idx]):
        if line == "":
            blanks_before_close += 1
        else:
            break
    assert blanks_before_close >= 3  # estimate(1) + margin(2)


def test_chunker_matches_opening_fence_length_when_reopening():
    # A 4-backtick fenced block forced to split: each chunk must reopen and
    # close with the SAME 4-backtick count, not collapse to 3.
    body = "\n".join(f"c{n}" for n in range(8))
    text = f"````markdown\n{body}\n````"
    pieces = chunk_markdown(text, byte_budget=10_000, max_lines=5)
    assert len(pieces) > 1
    for p in pieces:
        assert p.split("\n")[0] == "````markdown"
        assert p.split("\n")[-1] == "````"
