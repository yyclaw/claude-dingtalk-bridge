from __future__ import annotations


def question_label(question: dict) -> str:
    """Short label for a question — prefers `header` (chip-style tag) so it
    reads well as a `- {label}: {reply}` line. Falls back to the full question
    text, then empty."""
    return question.get("header") or question.get("question") or ""


def question_preview(question: dict) -> str:
    """Log/preview text for a question — prefers the full `question` so the
    reader sees what was actually asked, falling back to `header`."""
    return question.get("question") or question.get("header") or ""


def format_question(question: dict, index: int, total: int) -> str:
    """Render one AskUserQuestion entry as a phone message."""
    header = question.get("header") or ""
    text = question.get("question") or ""
    options = question.get("options") or []
    multi = bool(question.get("multiSelect"))

    counter = f" ({index + 1}/{total})" if total > 1 else ""
    lines = [f"❓ Claude is asking{counter}", ""]
    if header:
        lines.append(f"▌ {header}")
    if text:
        lines.append(text)
    lines.append("")
    for i, opt in enumerate(options, start=1):
        lines.append(f"{i}. {opt.get('label') or ''}")
        desc = opt.get("description") or ""
        if desc:
            lines.append(f"   {desc}")
    lines.append("")
    if multi:
        lines.append("Reply with numbers (e.g. 1,3), or type your own answer.")
    else:
        lines.append("Reply with a number, or type your own answer.")
    return "\n".join(lines)


def parse_answer(reply: str, options: list[dict]) -> tuple[str | None, bool]:
    """Map a phone reply to option labels.

    Returns (answer, valid):
    - a list of in-range option numbers -> (joined labels, True)
    - free-form text -> (raw text, True)
    - numeric but out of range -> (None, False), so the caller re-asks
    """
    stripped = reply.strip()
    tokens = [t.strip() for t in stripped.split(",") if t.strip()]
    if tokens and all(t.isdigit() for t in tokens):
        nums = [int(t) for t in tokens]
        if all(1 <= n <= len(options) for n in nums):
            labels = [options[n - 1].get("label") or "" for n in nums]
            return ", ".join(labels), True
        return None, False
    return stripped, True
