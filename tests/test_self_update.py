from pathlib import Path

import pytest

import claude_dingtalk_bridge.self_update as su


def test_repo_root_points_at_this_repo():
    root = su.repo_root()
    assert (root / "pyproject.toml").exists()
    assert (root / "Makefile").exists()


async def test_run_returns_code_and_merged_output(tmp_path):
    # stderr is merged into stdout so the combined text is what callers report.
    rc, out = await su._run(
        ["sh", "-c", "echo hello; echo oops 1>&2; exit 4"], tmp_path
    )
    assert rc == 4
    assert "hello" in out and "oops" in out


# --- fetch_and_compare -------------------------------------------------


async def test_fetch_and_compare_reports_behind(monkeypatch):
    async def fake_run(cmd, cwd):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return 0, "2\n"
        if cmd[:3] == ["git", "log", "--oneline"]:
            return 0, "bbbbbbb fix login\nccccccc add feature\n"
        raise AssertionError(cmd)

    monkeypatch.setattr(su, "_run", fake_run)
    res = await su.fetch_and_compare(Path("/repo"))
    assert res.behind == 2
    assert res.subjects == ["bbbbbbb fix login", "ccccccc add feature"]


async def test_fetch_and_compare_up_to_date(monkeypatch):
    async def fake_run(cmd, cwd):
        if "--abbrev-ref" in cmd:
            return 0, "main\n"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if "--count" in cmd:
            return 0, "0\n"
        raise AssertionError(cmd)

    monkeypatch.setattr(su, "_run", fake_run)
    res = await su.fetch_and_compare(Path("/repo"))
    assert res.behind == 0
    assert res.subjects == []


async def test_fetch_and_compare_parses_count_past_folded_warning(monkeypatch):
    # stderr is folded into stdout, so a git warning can ride alongside the
    # count; the lone digits line is still picked rather than int()-ing the blob.
    async def fake_run(cmd, cwd):
        if "--abbrev-ref" in cmd:
            return 0, "main\n"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if "--count" in cmd:
            return 0, "warning: ignoring broken ref\n2\n"
        if cmd[:3] == ["git", "log", "--oneline"]:
            return 0, "bbbbbbb fix\n"
        raise AssertionError(cmd)

    monkeypatch.setattr(su, "_run", fake_run)
    res = await su.fetch_and_compare(Path("/repo"))
    assert res.behind == 2


async def test_fetch_and_compare_raises_on_unparseable_count(monkeypatch):
    async def fake_run(cmd, cwd):
        if "--abbrev-ref" in cmd:
            return 0, "main\n"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if "--count" in cmd:
            return 0, "warning: something odd\n"
        raise AssertionError(cmd)

    monkeypatch.setattr(su, "_run", fake_run)
    with pytest.raises(su.SelfUpdateError) as exc:
        await su.fetch_and_compare(Path("/repo"))
    assert "commit count" in str(exc.value)


def test_parse_behind_empty_output_is_zero():
    # No commits ahead → git rev-list prints nothing; treat empty/whitespace
    # output as zero rather than raising on a blob with no digits line.
    assert su._parse_behind("") == 0
    assert su._parse_behind("   \n") == 0


async def test_fetch_and_compare_rejects_non_main_branch(monkeypatch):
    async def fake_run(cmd, cwd):
        if "--abbrev-ref" in cmd:
            return 0, "feature-x\n"
        raise AssertionError("must not fetch when off main")

    monkeypatch.setattr(su, "_run", fake_run)
    with pytest.raises(su.SelfUpdateError) as exc:
        await su.fetch_and_compare(Path("/repo"))
    assert "main" in str(exc.value)
    assert "feature-x" in str(exc.value)


async def test_current_branch_surfaces_failure(monkeypatch):
    async def fake_run(cmd, cwd):
        return 128, "fatal: not a git repository"

    monkeypatch.setattr(su, "_run", fake_run)
    with pytest.raises(su.SelfUpdateError) as exc:
        await su.current_branch(Path("/repo"))
    assert "not a git repository" in str(exc.value)


async def test_fetch_and_compare_surfaces_rev_list_failure(monkeypatch):
    async def fake_run(cmd, cwd):
        if "--abbrev-ref" in cmd:
            return 0, "main\n"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if "--count" in cmd:
            return 1, "boom"
        raise AssertionError(cmd)

    monkeypatch.setattr(su, "_run", fake_run)
    with pytest.raises(su.SelfUpdateError) as exc:
        await su.fetch_and_compare(Path("/repo"))
    assert "rev-list" in str(exc.value)


async def test_run_wraps_missing_binary_as_self_update_error(tmp_path):
    # A spawn failure (binary absent under launchd's minimal PATH) surfaces as
    # SelfUpdateError, not a bare OSError that callers don't catch.
    with pytest.raises(su.SelfUpdateError) as exc:
        await su._run(["definitely-not-a-real-binary-xyz"], tmp_path)
    assert "definitely-not-a-real-binary-xyz" in str(exc.value)


async def test_fetch_and_compare_tolerates_log_failure(monkeypatch):
    # behind > 0 but `git log` fails — subjects degrade to empty rather than
    # aborting the whole check (the count is what matters).
    async def fake_run(cmd, cwd):
        if "--abbrev-ref" in cmd:
            return 0, "main\n"
        if cmd[:2] == ["git", "fetch"]:
            return 0, ""
        if "--count" in cmd:
            return 0, "1\n"
        if cmd[:3] == ["git", "log", "--oneline"]:
            return 1, "fatal: bad revision"
        raise AssertionError(cmd)

    monkeypatch.setattr(su, "_run", fake_run)
    res = await su.fetch_and_compare(Path("/repo"))
    assert res.behind == 1
    assert res.subjects == []


async def test_fetch_and_compare_surfaces_fetch_failure(monkeypatch):
    async def fake_run(cmd, cwd):
        if "--abbrev-ref" in cmd:
            return 0, "main\n"
        if cmd[:2] == ["git", "fetch"]:
            return 128, "fatal: could not read from remote repository"
        raise AssertionError(cmd)

    monkeypatch.setattr(su, "_run", fake_run)
    with pytest.raises(su.SelfUpdateError) as exc:
        await su.fetch_and_compare(Path("/repo"))
    assert "could not read from remote" in str(exc.value)


# --- pull --------------------------------------------------------------


async def test_pull_runs_ff_only(monkeypatch):
    seen = []

    async def fake_run(cmd, cwd):
        seen.append(cmd)
        return 0, "Updating aaaa..bbbb\nFast-forward\n"

    monkeypatch.setattr(su, "_run", fake_run)
    await su.pull(Path("/repo"))
    assert seen == [["git", "pull", "--ff-only", "origin", "main"]]


async def test_pull_raises_when_not_fast_forwardable(monkeypatch):
    async def fake_run(cmd, cwd):
        return 128, "fatal: Not possible to fast-forward, aborting."

    monkeypatch.setattr(su, "_run", fake_run)
    with pytest.raises(su.SelfUpdateError) as exc:
        await su.pull(Path("/repo"))
    assert "fast-forward" in str(exc.value)


# --- run_make ----------------------------------------------------------


async def test_run_make_returns_combined_output(monkeypatch):
    async def fake_run(cmd, cwd):
        assert cmd == ["make", "config"]
        return 0, "New keys in config.example.yaml not present in your config:\n  - geo.proxy_url"

    monkeypatch.setattr(su, "_run", fake_run)
    out = await su.run_make("config", Path("/repo"))
    assert "New keys" in out
    assert "geo.proxy_url" in out


async def test_run_make_raises_on_failure(monkeypatch):
    async def fake_run(cmd, cwd):
        return 2, "make: *** [setup] Error 1"

    monkeypatch.setattr(su, "_run", fake_run)
    with pytest.raises(su.SelfUpdateError) as exc:
        await su.run_make("setup", Path("/repo"))
    assert "setup" in str(exc.value)


# --- snapshot ----------------------------------------------------------


def test_snapshot_reads_pyproject_and_config_template(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = 'x'\n")
    (tmp_path / "config.example.yaml").write_bytes(b"a: 1\n")
    snap = su.snapshot(tmp_path)
    assert snap.pyproject == b"[project]\nname = 'x'\n"
    assert snap.config_template == b"a: 1\n"


def test_snapshot_raises_self_update_error_on_missing_file(tmp_path):
    # No pyproject.toml/config.example.yaml under tmp_path — the read failure
    # surfaces as SelfUpdateError so _cmd_update reports it instead of letting
    # a bare OSError escape unhandled.
    with pytest.raises(su.SelfUpdateError):
        su.snapshot(tmp_path)


def test_snapshot_detects_change(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes(b"v1")
    (tmp_path / "config.example.yaml").write_bytes(b"c1")
    before = su.snapshot(tmp_path)
    (tmp_path / "pyproject.toml").write_bytes(b"v2")
    after = su.snapshot(tmp_path)
    assert before.pyproject != after.pyproject
    assert before.config_template == after.config_template


# --- trigger_restart_detached ------------------------------------------


def test_trigger_restart_detached_spawns_detached_make(monkeypatch):
    calls = {}

    class FakePopen:
        def __init__(self, cmd, cwd, start_new_session, stdout, stderr):
            calls["cmd"] = cmd
            calls["cwd"] = cwd
            calls["start_new_session"] = start_new_session

    monkeypatch.setattr(su.subprocess, "Popen", FakePopen)
    su.trigger_restart_detached(Path("/repo"))
    assert calls["cmd"] == ["make", "daemon-restart"]
    assert calls["cwd"] == "/repo"
    # start_new_session detaches the restarter so launchctl kickstart -k
    # survives the daemon's own SIGTERM.
    assert calls["start_new_session"] is True
