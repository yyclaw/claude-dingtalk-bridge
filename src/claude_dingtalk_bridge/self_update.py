"""Self-update of the daemon program itself (the `claude-dingtalk-bridge` repo).

These helpers drive git/make against the daemon's *own* checkout — unrelated to
the user's configured `projects`. The orchestrator's `/update` command and the
daemon's daily auto-check both build on `fetch_and_compare`.

Note on the runtime environment: the daemon is launched by launchd with a
minimal `PATH` and no SSH agent (`SSH_AUTH_SOCK` absent). `git`/`make` resolve
fine (`/usr/bin/git`, `/usr/bin/make` are on the minimal PATH), but a `git
fetch`/`pull` against an SSH remote may fail auth. Such failures raise
`SelfUpdateError` (surfaced to the phone for `/update`, logged for the
auto-check) rather than being swallowed.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT = "git"
_MAKE = "make"
_REMOTE = "origin"
_BRANCH = "main"


class SelfUpdateError(Exception):
    """A git/make step failed. The message already carries the captured output
    so it can be forwarded to the phone verbatim."""


@dataclass
class CompareResult:
    behind: int            # commits on origin/main that HEAD lacks
    subjects: list[str]    # `git log --oneline HEAD..origin/main` lines


@dataclass
class Snapshot:
    pyproject: bytes
    config_template: bytes  # config.example.yaml contents


def repo_root() -> Path:
    """The daemon's own repository root.

    The daemon always runs from an editable install (`pip install -e .`), so
    this module file lives at `<repo>/src/claude_dingtalk_bridge/self_update.py`
    — the repo root is two levels above the package directory.
    """
    return Path(__file__).resolve().parents[2]


async def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run a command in `cwd`; return (returncode, combined stdout+stderr).

    stderr is folded into stdout so the single text blob is what callers report
    on failure (and what `make config` prints its "new keys" notice to).

    A missing binary (`git`/`make` absent under launchd's minimal PATH) raises
    OSError from the spawn; fold it into SelfUpdateError so every caller only
    has to handle the one exception type.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        raise SelfUpdateError(f"could not run {cmd[0]}:\n{exc}") from exc
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace")


async def current_branch(root: Path | None = None) -> str:
    root = root or repo_root()
    rc, out = await _run([_GIT, "rev-parse", "--abbrev-ref", "HEAD"], root)
    if rc != 0:
        raise SelfUpdateError(f"git rev-parse failed:\n{out.strip()}")
    return out.strip()


async def fetch_and_compare(root: Path | None = None) -> CompareResult:
    """Fetch origin/main and report how far HEAD is behind it.

    Refuses to proceed off `main` — comparing/pulling on another branch would
    be wrong. Raises `SelfUpdateError` on any git failure.
    """
    root = root or repo_root()
    branch = await current_branch(root)
    if branch != _BRANCH:
        raise SelfUpdateError(
            f"/update only works on '{_BRANCH}', but you are on '{branch}'."
        )
    rc, out = await _run([_GIT, "fetch", _REMOTE, _BRANCH], root)
    if rc != 0:
        raise SelfUpdateError(f"git fetch failed:\n{out.strip()}")

    rc, count = await _run(
        [_GIT, "rev-list", "--count", f"HEAD..{_REMOTE}/{_BRANCH}"], root
    )
    if rc != 0:
        raise SelfUpdateError(f"git rev-list failed:\n{count.strip()}")
    behind = _parse_behind(count)

    subjects: list[str] = []
    if behind:
        rc, log = await _run(
            [_GIT, "log", "--oneline", f"HEAD..{_REMOTE}/{_BRANCH}"], root
        )
        if rc == 0:
            subjects = [line for line in log.splitlines() if line.strip()]
    return CompareResult(behind=behind, subjects=subjects)


def _parse_behind(out: str) -> int:
    """Extract the commit count from `git rev-list --count` output.

    `_run` folds stderr into stdout, so a git advice/warning line can ride
    alongside the number; pick the lone all-digits line rather than `int()`-ing
    the whole blob (which would raise on the warning text). Empty output (no
    commits) means zero."""
    for line in reversed(out.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    if out.strip() == "":
        return 0
    raise SelfUpdateError(f"git rev-list returned no commit count:\n{out.strip()}")


def snapshot(root: Path | None = None) -> Snapshot:
    """Read the files whose changes decide whether setup/config must re-run.

    Raises SelfUpdateError if either file can't be read (e.g. a pull renamed
    config.example.yaml) so the caller surfaces it like any other git/make
    failure instead of letting a bare OSError escape unreported."""
    root = root or repo_root()
    try:
        return Snapshot(
            pyproject=(root / "pyproject.toml").read_bytes(),
            config_template=(root / "config.example.yaml").read_bytes(),
        )
    except OSError as exc:
        raise SelfUpdateError(f"reading update snapshot failed:\n{exc}") from exc


async def pull(root: Path | None = None) -> None:
    """Fast-forward only — never auto-merge or risk a conflict."""
    root = root or repo_root()
    rc, out = await _run(
        [_GIT, "pull", "--ff-only", _REMOTE, _BRANCH], root
    )
    if rc != 0:
        raise SelfUpdateError(f"git pull failed:\n{out.strip()}")


async def run_make(target: str, root: Path | None = None) -> str:
    """Run `make <target>`; return its combined output. Raise on non-zero."""
    root = root or repo_root()
    rc, out = await _run([_MAKE, target], root)
    if rc != 0:
        raise SelfUpdateError(f"make {target} failed:\n{out.strip()}")
    return out


def trigger_restart_detached(root: Path | None = None) -> None:
    """Restart the daemon via a detached `make daemon-restart`.

    `make daemon-restart` ends in `launchctl kickstart -k`, which kills the
    daemon running this very command. `start_new_session=True` puts the
    restarter in its own session/process group so it survives the daemon's
    SIGTERM and completes the restart. The new instance's CLI path then sends
    the "🔄 Daemon restarted" phone notice.
    """
    root = root or repo_root()
    subprocess.Popen(
        [_MAKE, "daemon-restart"],
        cwd=str(root),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
