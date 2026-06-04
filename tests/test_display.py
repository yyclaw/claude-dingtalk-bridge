from pathlib import Path

from claude_dingtalk_bridge.display import (
    collapse_inline_paths,
    display_path,
    format_cost,
    format_relative_time,
    format_size,
    md_escape,
    short_model_name,
)


def test_relative_time_buckets():
    now = 1_000_000_000_000
    assert format_relative_time(now, now) == "just now"
    assert format_relative_time(now - 120_000, now) == "2m ago"
    assert format_relative_time(now - 7_200_000, now) == "2h ago"
    assert format_relative_time(now - 86_400_000, now) == "yesterday"
    assert format_relative_time(now - 3 * 86_400_000, now) == "3d ago"


def test_md_escape_neutralizes_special_chars():
    assert md_escape("plain text") == "plain text"
    assert md_escape("a_b*c") == "a&#95;b&#42;c"
    assert md_escape("/resume <n>") == "/resume &#60;n&#62;"
    # '&' is escaped first, so emitted entities are not double-escaped.
    assert md_escape("a<b") == "a&#60;b"
    assert "&#38;#" not in md_escape("x*y")


def test_md_escape_ampersand_escaped_exactly_once():
    # Proves the '&' entry exists in _MD_ENTITIES (a mutation removing it
    # would leave '&' literal) and that '&' is processed first so the '&#38;'
    # it emits is not re-escaped into '&#38;#38;' (double-escape bug).
    result = md_escape("a&b")
    assert result == "a&#38;b"          # '&' → '&#38;', no other change
    assert "&#38;#" not in result       # no double-escape of the emitted entity

    # Mixed: both '&' and another escapable char must each be escaped once.
    assert md_escape("a&b<c") == "a&#38;b&#60;c"


def test_format_size_buckets():
    assert format_size(0) == "0B"
    assert format_size(1023) == "1023B"
    assert format_size(38810) == "37.9KB"
    assert format_size(1258291) == "1.2MB"


def test_format_cost_renders_dollars():
    # Sub-cent amounts collapse to "<$0.01" — a turn that's effectively free
    # should read that way, not "$0.00" (which looks like a stale field).
    assert format_cost(0) == "<$0.01"
    assert format_cost(0.001) == "<$0.01"
    assert format_cost(0.009) == "<$0.01"
    assert format_cost(0.01) == "$0.01"
    assert format_cost(0.42) == "$0.42"
    assert format_cost(22.4) == "$22.40"
    assert format_cost(100) == "$100.00"


def test_display_path_collapses_home_to_tilde():
    inside = Path.home() / ".claude" / "projects" / "abc" / "x.jsonl"
    assert display_path(inside) == "~/.claude/projects/abc/x.jsonl"


def test_display_path_leaves_paths_outside_home_absolute():
    outside = Path("/tmp/elsewhere/file.jsonl")
    assert display_path(outside) == "/tmp/elsewhere/file.jsonl"


def test_display_path_project_relative_takes_precedence_over_home():
    # Project-relative is the more specific rewrite — when both could apply
    # (project root is under $HOME), the path should render relative to the
    # project, not as ~/proj/src/x.
    p = "/Users/dev/proj/src/x.py"
    assert display_path(p, cwd="/Users/dev/proj") == "src/x.py"


def test_display_path_returns_dot_when_path_equals_cwd():
    # Bare project root → "." rather than "" (which would render as an empty
    # field and lose all signal that a path was there).
    assert display_path("/Users/dev/proj", cwd="/Users/dev/proj") == "."


def test_display_path_outside_project_still_collapses_home():
    # Path inside $HOME but outside the project still gets the ~/ treatment.
    p = str(Path.home() / "notes" / "x.md")
    assert display_path(p, cwd="/Users/dev/proj") == "~/notes/x.md"


def test_collapse_inline_paths_rewrites_embedded_project_path():
    # Free-form string (Bash command) with an embedded absolute path inside
    # the project — the path collapses, the rest of the command is untouched.
    s = "grep -n foo /Users/dev/proj/src/a.py"
    assert collapse_inline_paths(s, cwd="/Users/dev/proj") == "grep -n foo src/a.py"


def test_collapse_inline_paths_falls_through_to_home_for_outside_paths():
    s = "cat " + str(Path.home() / "x.txt")
    assert collapse_inline_paths(s, cwd="/Users/dev/proj") == "cat ~/x.txt"


def test_collapse_inline_paths_noop_when_no_cwd_and_path_outside_home():
    assert collapse_inline_paths("ls /tmp/x", cwd="") == "ls /tmp/x"


def test_collapse_inline_paths_empty_string_unchanged():
    assert collapse_inline_paths("") == ""


def test_collapse_home_guards_against_empty_or_root_home(monkeypatch):
    # If $HOME ever resolves to "" (no pwd entry + no env var) the naive
    # `s.replace(_HOME + "/", "~/")` would replace EVERY `/` in the string
    # with `~/` — turning `/tmp/x` into `~/tmp~/x`. Same risk if HOME == "/"
    # (running as root with HOME=/): `_HOME + "/"` becomes `//` and would
    # collapse double-slashes. Guard at the helper, not just at config time.
    import claude_dingtalk_bridge.display as display_mod

    monkeypatch.setattr(display_mod, "_HOME", "")
    assert display_mod._collapse_home("/tmp/x") == "/tmp/x"

    monkeypatch.setattr(display_mod, "_HOME", "/")
    assert display_mod._collapse_home("/tmp/x") == "/tmp/x"


def test_short_model_name_strips_claude_prefix_and_datestamp():
    assert short_model_name("claude-opus-4-7") == "opus-4.7"
    assert short_model_name("claude-opus-4-7[1m]") == "opus-4.7[1m]"
    assert short_model_name("claude-haiku-4-5-20251001") == "haiku-4.5"
    assert short_model_name("claude-haiku-4-5-20251001[1m]") == "haiku-4.5[1m]"
    assert short_model_name("claude-sonnet-4-6") == "sonnet-4.6"
