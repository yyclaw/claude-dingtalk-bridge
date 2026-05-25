import contextvars
import logging

from claude_dingtalk_bridge import log_context


def _in_fresh_context(fn):
    """Run a callable in a clean copy of the current context.

    log_context module state is global; tests would otherwise leak labels
    into each other depending on run order.
    """
    return contextvars.copy_context().run(fn)


def test_set_session_truncates_to_uuid_prefix():
    def go():
        log_context.set_session("9321bb41-8fa4-4635-b4f3-e8f71c313918")
        return log_context.session_label()

    assert _in_fresh_context(go) == "9321bb41"


def test_set_session_none_resets_to_dash():
    def go():
        log_context.set_session("abcdef01-…")
        log_context.set_session(None)
        return log_context.session_label()

    assert _in_fresh_context(go) == "-"


def test_set_turn_renders_int():
    def go():
        log_context.set_turn(3)
        return log_context.turn_label()

    assert _in_fresh_context(go) == "3"


def test_clear_resets_both():
    def go():
        log_context.set_session("abcdef01-x")
        log_context.set_turn(7)
        log_context.clear()
        return log_context.session_label(), log_context.turn_label()

    assert _in_fresh_context(go) == ("-", "-")


def test_default_labels_are_dash():
    # A fresh context starts with the dash sentinel — formatter never sees an
    # empty string, so the column stays aligned even outside of a turn.
    def go():
        return log_context.session_label(), log_context.turn_label()

    assert _in_fresh_context(go) == ("-", "-")


def test_set_cwd_round_trips():
    def go():
        log_context.set_cwd("/Users/foo/proj")
        return log_context.cwd_label()

    assert _in_fresh_context(go) == "/Users/foo/proj"


def test_cwd_default_is_empty_string():
    def go():
        return log_context.cwd_label()

    assert _in_fresh_context(go) == ""


def test_clear_resets_cwd_too():
    def go():
        log_context.set_cwd("/p")
        log_context.clear()
        return log_context.cwd_label()

    assert _in_fresh_context(go) == ""


def test_record_tool_use_lazy_inits_map_in_fresh_context():
    # The autouse conftest fixture pre-runs log_context.clear(), which sets
    # _tool_uses to {} — so copy_context() would inherit that. Use an empty
    # contextvars.Context() to start from the ContextVar's None default, so
    # the lazy-init path in _tool_use_map() actually fires.
    def go():
        log_context.record_tool_use("toolu_abc", "Read")
        name, elapsed = log_context.take_tool_use("toolu_abc")
        return name, elapsed

    name, elapsed = contextvars.Context().run(go)
    assert name == "Read"
    assert elapsed is not None and elapsed >= 0


def test_record_tool_use_ignores_empty_id():
    def go():
        log_context.record_tool_use("", "Read")
        # Empty id wasn't stored; take with empty id also no-ops.
        return log_context.take_tool_use("")

    assert _in_fresh_context(go) == (None, None)


def test_take_tool_use_returns_none_for_unknown_id():
    def go():
        return log_context.take_tool_use("toolu_never_recorded")

    assert _in_fresh_context(go) == (None, None)


def test_record_subagent_lazy_inits_map_in_fresh_context():
    # Same lazy-init coverage for the subagent map. Must start from a fully
    # empty Context() — see the tool_use lazy-init test for the reason.
    def go():
        log_context.record_subagent("toolu_x", "abcd1234efgh", "Explore")
        return log_context.lookup_subagent("toolu_x")

    sub_id, sub_type = contextvars.Context().run(go)
    assert sub_type == "Explore"
    assert sub_id == "abcd1234"


def test_record_subagent_skipped_without_type_or_id():
    def go():
        log_context.record_subagent("", "tid", "Explore")
        log_context.record_subagent("toolu_y", "tid", None)
        return (
            log_context.lookup_subagent(""),
            log_context.lookup_subagent("toolu_y"),
        )

    empty, missing_type = _in_fresh_context(go)
    assert empty == (None, None)
    assert missing_type == (None, None)


def test_forget_subagent_handles_missing_id():
    # Bare forget on an unknown id is a no-op (and the empty-id early return).
    def go():
        log_context.forget_subagent(None)
        log_context.forget_subagent("toolu_unknown")
        return log_context.lookup_subagent("toolu_unknown")

    assert _in_fresh_context(go) == (None, None)


class _PrefixFormatter(logging.Formatter):
    """Mirror of daemon._ShortNameFormatter's session/turn prefix logic, kept
    here so the test is independent of daemon's full wiring."""

    def format(self, record):
        session = log_context.session_label()
        turn = log_context.turn_label()
        if session == "-" and turn == "-":
            record.session_turn = ""
        else:
            record.session_turn = f"session={session} turn={turn} "
        return super().format(record)


def _format(msg: str) -> str:
    record = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )
    return _PrefixFormatter("%(session_turn)s%(message)s").format(record)


def test_prefix_present_when_turn_is_active():
    def go():
        log_context.set_session("9321bb41-8fa4-4635")
        log_context.set_turn(2)
        return _format("running tool Bash cmd=git status")

    assert (
        _in_fresh_context(go)
        == "session=9321bb41 turn=2 running tool Bash cmd=git status"
    )


def test_prefix_omitted_when_outside_any_turn():
    # daemon startup / websocket events have no turn — the prefix should
    # disappear entirely rather than show a pointless `session=- turn=-`.
    def go():
        return _format("startup")

    assert _in_fresh_context(go) == "startup"


def test_prefix_kept_when_only_session_unknown():
    # Between turn start and SDK init, session is still unknown but the line
    # is turn-scoped — keep the prefix so the turn id is greppable.
    def go():
        log_context.set_turn(1)
        return _format("running turn")

    assert _in_fresh_context(go) == "session=- turn=1 running turn"
