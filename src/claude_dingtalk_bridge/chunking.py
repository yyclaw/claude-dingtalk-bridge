from __future__ import annotations


def _bytelen(s: str) -> int:
    return len(s.encode("utf-8"))


def _last_blank(lines: list[str]) -> int | None:
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "":
            return i
    return None


_FENCE = "```"


def _fence_run(line: str) -> int:
    """Length of the line's leading run of backticks (0 if it isn't a fence).

    A fenced code block can use ``N >= 3`` backticks; nesting is achieved by
    wrapping with strictly more backticks than any fence inside. We track each
    opening fence's exact length so the matching close has the same count.

    Per CommonMark, a fence may be indented up to 3 spaces; 4+ spaces make the
    line indented code, not a fence — so an indented ``` inside an open block
    stays content instead of falsely closing it."""
    s = line.lstrip(" ")
    if len(line) - len(s) > 3:
        return 0
    n = 0
    while n < len(s) and s[n] == "`":
        n += 1
    return n if n >= 3 else 0


def _is_fence(line: str) -> bool:
    return _fence_run(line) > 0


def _is_closing_fence(line: str) -> bool:
    """A pure run of >=3 backticks closes a fence (info-string-only lines open)."""
    return _fence_run(line) > 0 and set(line.strip()) == {"`"}


def _hard_split(s: str, byte_budget: int) -> list[str]:
    """Cut a single oversized line on character boundaries so no piece
    exceeds the byte budget and no multibyte char is severed."""
    pieces: list[str] = []
    cur = ""
    for ch in s:
        if cur and _bytelen(cur + ch) > byte_budget:
            pieces.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:  # pragma: no branch - s is a non-empty over-budget line, so the
        pieces.append(cur)  # final piece always exists
    return pieces


# The tail-drop grows roughly with message length but slower than a 1:20 fit
# suggested at first sight (e.g. 145-line fence-ending pieces drop ~3-4, not 6).
# One blank line of padding per this many lines beyond the threshold keeps the
# estimate honest, so leftover padding stays ~margin instead of a visible surplus.
_TAIL_DROP_DIVISOR = 30


def pad_code_tail(piece: str, min_lines: int, margin: int) -> str:
    """Cushion DingTalk's tail-drop: a rendered message holding a long code
    block silently loses its last few lines (the closing fence included), and
    the loss grows with length. For a chunk past ``min_lines`` that contains a
    code block, insert sacrificial blank lines before the LAST closing fence
    so the drop eats those, not real code. Blank lines occupy real slots inside
    a fence yet stay invisible if the chunk renders whole. The closing fence
    isn't required to be the chunk's last line — trailing prose after the
    block doesn't save its tail from being chewed, so we still pad before
    whichever closing fence appears last. Chunks with no closing fence at all
    are left untouched."""
    lines = piece.split("\n")
    if len(lines) <= min_lines:
        return piece
    close_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if _is_closing_fence(lines[i]):
            close_idx = i
            break
    if close_idx is None:
        return piece
    drop_estimate = -(-(len(lines) - min_lines) // _TAIL_DROP_DIVISOR)
    pad = drop_estimate + margin
    return "\n".join(lines[:close_idx] + [""] * pad + lines[close_idx:])


def chunk_markdown(
    text: str, byte_budget: int, max_lines: int = 1_000_000
) -> list[str]:
    if _bytelen(text) <= byte_budget and text.count("\n") + 1 <= max_lines:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    lines = text.split("\n")
    inside = False  # are the lines in `current` lexically inside a fence?
    open_line = ""  # the fence-open token (e.g. "```python") to reopen/close

    def close_fence() -> str:
        # Match the opening fence's exact backtick count so nested fences (e.g.
        # ``` inside a ```` wrapper) don't collapse to an unmatched short close.
        return "`" * _fence_run(open_line) if open_line else _FENCE

    def emit() -> None:
        nonlocal current
        if not current or (inside and current == [open_line]):
            current = []
            return
        body = "\n".join(current)
        if inside:
            body += "\n" + close_fence()
        chunks.append(body)
        current = []

    i = 0
    while i < len(lines):
        if not current and inside:
            current = [open_line]  # reopen a fence carried from the prior chunk

        line = lines[i]
        if _is_fence(line):
            # Only a CLOSING fence (>=N matching backticks) toggles us out of an
            # open fence; a `` ```python `` inside a ```` block stays content.
            if inside and _is_closing_fence(line) and _fence_run(line) >= _fence_run(open_line):
                next_inside, next_open = False, ""
            elif not inside:
                next_inside, next_open = True, line.strip()
            else:
                next_inside, next_open = inside, open_line
        else:
            next_inside, next_open = inside, open_line

        reserve = len("\n" + close_fence()) if next_inside else 0
        size = _bytelen("\n".join(current + [line])) + reserve
        nlines = len(current) + 1 + (1 if next_inside else 0)
        minimal = (not current) or (inside and current == [open_line])

        if size <= byte_budget and nlines <= max_lines:
            current.append(line)
            inside, open_line = next_inside, next_open
            i += 1
            continue
        if minimal:
            # Doesn't fit even alone: hard-cut the single line on char bounds.
            emit()
            if inside and next_inside:
                # The line is content of an open fence — wrap every hard-split
                # piece back in the fence so code never leaks as live markdown.
                # Shrink the inner budget by the fence overhead so each wrapped
                # piece still fits; clamp to >=1 for a pathologically tiny budget.
                open_tok, close_tok = open_line, close_fence()
                overhead = _bytelen(open_tok) + _bytelen(close_tok) + 2
                inner = max(1, byte_budget - overhead)
                for piece in _hard_split(line, inner):
                    chunks.append(f"{open_tok}\n{piece}\n{close_tok}")
            else:
                chunks.extend(_hard_split(line, byte_budget))
            i += 1
            continue
        # Cut before this line (don't advance). Outside a fence, prefer to end
        # at a blank line; inside a fence blank lines are code, so cut at the
        # line boundary and let the next chunk reopen the fence.
        blank = None if inside else _last_blank(current)
        if blank is not None and blank > 0:
            chunks.append("\n".join(current[:blank]))
            current = current[blank + 1 :]
        else:
            emit()

    emit()
    return [c for c in chunks if c != ""]
