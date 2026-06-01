#!/usr/bin/env python3
"""Manual self-check for the Bash permission hook (`permission_hooks`).

Exercises `decide_bash` and `make_bash_permission_hook` against a table of
representative commands and prints expected-vs-actual verdicts. Every command
is a Python string literal, so shell metacharacters (`$()`, `*`, quotes) reach
the hook verbatim — running these in a real shell would expand them first and
test the wrong thing.

Run:  .venv/bin/python scripts/check_bash_permissions.py
Exits non-zero if any case deviates from its expected verdict.

The hook takes no config: it only hard-denies built-in catastrophic tripwires
and variable-substituted command names. Path-level deny rules live in Claude
Code's own settings layers, not here.
"""
import asyncio
import sys

from claude_dingtalk_bridge.permission_hooks import (
    decide_bash,
    make_bash_permission_hook,
)

# (command, expected verdict, note) — `deny` = hard block, `pass` = fall
# through to the SDK settings layer + permission_mode.
DECIDE_CASES = [
    # —— tripwires (always on) ——
    ("rm -rf /tmp/x",                      "deny", "tripwire: rm -f (bundled)"),
    ("rm -r -f /tmp/x",                    "deny", "tripwire: rm force split across groups"),
    ("rm --recursive -f /tmp/x",           "deny", "tripwire: long recursive + short force"),
    ("rm --force /tmp/x",                  "deny", "tripwire: rm --force"),
    ("dd if=/dev/zero of=/dev/sda",        "deny", "tripwire: dd of=/dev"),
    ("newfs_hfs /dev/disk2",               "deny", "tripwire: newfs (macOS)"),
    ("diskutil eraseDisk APFS X /dev/disk2", "deny", "tripwire: diskutil erase (macOS)"),
    ("diskutil apfs deleteContainer disk2", "deny", "tripwire: diskutil apfs deleteContainer"),
    ("diskutil apfs deleteVolume disk2s1", "deny", "tripwire: diskutil apfs deleteVolume"),
    ("find /tmp/x -delete",                "deny", "tripwire: find -delete"),
    ("find . -name '*.tmp' -delete",       "deny", "tripwire: find -delete with predicates"),
    ("asr restore --source /tmp/x.dmg --target /dev/disk2 --erase --noprompt", "deny", "tripwire: asr restore"),
    ("gpt destroy /dev/disk2",             "deny", "tripwire: gpt destroy"),
    ("gpt remove -i 1 /dev/disk2",         "deny", "tripwire: gpt remove"),
    ("cat foo > /dev/disk2",               "deny", "tripwire: block-device redirect"),
    (":(){ :|:& };:",                      "deny", "tripwire: fork bomb"),
    # —— tripwire recurses into bash -c / eval ——
    ("cd /tmp && bash -c 'rm -rf /foo'",   "deny", "recurse bash -c -> rm -rf"),
    ("eval 'rm -rf /tmp/x'",               "deny", "recurse eval -> rm -rf"),
    ("sh -c 'dd if=/dev/zero of=/dev/sda'", "deny", "recurse sh -c -> dd of=/dev"),
    ("bash -c '\"rm\" -rf /tmp/x'",        "deny", "recursion-only: quoted inner name (raw pass misses)"),
    ("bash -cl 'rm -rf /tmp/x'",           "deny", "recurse: -cl short-flag group order"),
    # —— parsed tripwire: normalize away quoting / path / split words ——
    ('"rm" -rf /tmp/x',                    "deny", "quoted name -> parsed tripwire"),
    ("/bin/rm -rf /tmp/x",                 "deny", "path prefix -> parsed tripwire"),
    ('dd if=/dev/zero of="/dev/sda"',      "deny", "quoted device -> parsed tripwire"),
    ("new''fs_hfs /dev/disk2",             "deny", "split-word newfs -> parsed tripwire"),
    # —— literal tripwire visible inside an interpreter string ——
    ("awk 'BEGIN{system(\"rm -rf /foo\")}'", "deny", "awk: literal rm -rf -> raw tripwire"),
    ("node -e 'require(\"child_process\").execSync(\"rm -rf /tmp/x\")'", "deny", "node: literal rm -rf visible -> raw tripwire"),
    # —— variable-substituted command name ——
    ("C=touch; $C /tmp/x",                 "deny", "$C command name hidden"),
    ("$CMD evil",                          "deny", "${CMD} command name hidden"),
    ("bash -c '$C evil'",                  "deny", "variable command inside sh -c"),
    ("eval \"$C evil\"",                   "deny", "variable command inside eval"),
    # —— pass: no tripwire, literal command name ——
    ("git status && git log",              "pass", "safe chain"),
    ("rm -r /tmp/x",                       "pass", "recursive only, no force (not a tripwire)"),
    ("rm foo",                             "pass", "plain rm, no force"),
    ("go test ./...",                      "pass", "safe build"),
    ("git push || echo done",              "pass", "|| chain"),
    ("ls -la | grep foo",                  "pass", "pipeline"),
    ("echo $HOME",                         "pass", "variable as argument, not command name"),
    ("echo $(date)",                       "pass", "command substitution, no tripwire"),
    ("node -e 'require(\"fs\").rmSync(\"/tmp/x\")'", "pass", "literal rm hidden -> out of scope"),
    ("git status && (",                    "pass", "syntax error -> no tripwire, fall through"),
    ("mkfs.ext4 /dev/sdb1",                "pass", "Linux mkfs intentionally out of scope (macOS daemon)"),
    ("diskutil list",                      "pass", "read-only diskutil verb, not an erase form"),
    ("find /tmp -name '*.log'",            "pass", "find without -delete is fine"),
    ("asr imagescan --source x.dmg",       "pass", "read-only asr verb"),
    ("gpt show /dev/disk0",                "pass", "read-only gpt verb"),
    # —— pass: false-positive guards (looks dangerous, isn't) ——
    ("confirm --force thing",              "pass", "'--force' as substring, not an rm atom"),
]


def _check_decide() -> int:
    print("=" * 78)
    print("decide_bash — expected vs actual")
    print("=" * 78)
    mism = 0
    for cmd, exp, note in DECIDE_CASES:
        got = decide_bash(cmd).verdict
        ok = "OK      " if got == exp else "MISMATCH"
        if got != exp:
            mism += 1
        shown = cmd.replace("\n", "\\n")
        print(f"{ok} exp={exp:5} got={got:5} | {shown:42} {note}")
    return mism


def _inp(tool: str, cmd: str = "") -> dict:
    return {"tool_name": tool, "tool_input": {"command": cmd} if cmd else {}}


async def _check_hook() -> int:
    print("\n" + "=" * 78)
    print("make_bash_permission_hook — output mapping")
    print("=" * 78)
    hook = make_bash_permission_hook()
    cases = [
        (_inp("Write"),                         "{}",   "non-Bash -> delegate"),
        (_inp("Bash", ""),                      "{}",   "empty -> delegate"),
        (_inp("Bash", "git status"),            "{}",   "safe -> settings layer"),
        (_inp("Bash", "go build ./..."),        "{}",   "safe -> settings layer"),
        (_inp("Bash", "rm -rf /tmp/x"),         "deny", "tripwire -> deny"),
        (_inp("Bash", "$C evil"),               "deny", "variable command -> deny"),
    ]
    mism = 0
    for data, exp, note in cases:
        out = await hook(data, None, {})
        got = "{}" if out == {} else out["hookSpecificOutput"]["permissionDecision"]
        ok = "OK      " if got == exp else "MISMATCH"
        if got != exp:
            mism += 1
        label = data["tool_input"].get("command") or f"<{data['tool_name']}>"
        print(f"{ok} exp={exp:5} got={got:5} | {label:28} {note}")
    return mism


def main() -> int:
    mism = _check_decide()
    mism += asyncio.run(_check_hook())
    print("\n" + "=" * 78)
    print(f"total MISMATCH = {mism}  (0 = everything matches the design)")
    print("=" * 78)
    return 1 if mism else 0


if __name__ == "__main__":
    sys.exit(main())
