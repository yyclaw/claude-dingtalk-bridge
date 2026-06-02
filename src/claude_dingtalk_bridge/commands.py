from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CommandType(Enum):
    STOP = auto()
    APPROVE = auto()
    DENY = auto()
    VERBOSE = auto()        # arg: "on" | "off" | None (None → report current state)
    DEBUG = auto()          # arg: "on" | "off" | None (None → report current state)
    LIST_PROJECTS = auto()
    SWITCH_PROJECT = auto() # arg: project name | None
    STATUS = auto()
    PWD = auto()
    CLEAR = auto()
    HELP = auto()
    SESSION = auto()
    RESUME = auto()         # arg: number index | session id | None
    MODEL = auto()          # arg: model name | None (None → list models)
    MODE = auto()           # arg: "default" | "acceptEdits" | "bypassPermissions" |
                            # "plan" | "reset" | None (None → report current state)
    QUEUE = auto()          # arg: None (view) | "rm N" | "rm all" | "clear"
    UPDATE = auto()         # update the daemon program itself (no arg)
    UNKNOWN = auto()
    PROMPT = auto()


@dataclass
class Command:
    type: CommandType
    arg: str | None = None


# Slash commands taking no argument.
_SLASH_KEYWORDS: dict[str, CommandType] = {
    "/ls": CommandType.LIST_PROJECTS,
    "/status": CommandType.STATUS,
    "/pwd": CommandType.PWD,
    "/clear": CommandType.CLEAR,
    "/session": CommandType.SESSION,
    "/update": CommandType.UPDATE,
}

# Slash commands taking an argument (the rest of the message). `/stop` and
# `/help` accept an optional one (`/stop all`, `/help <command>`); bare forms
# arrive with arg=None, same as a no-arg keyword.
_ARG_COMMANDS: dict[str, CommandType] = {
    "/stop": CommandType.STOP,
    "/cd": CommandType.SWITCH_PROJECT,
    "/verbose": CommandType.VERBOSE,
    "/debug": CommandType.DEBUG,
    "/resume": CommandType.RESUME,
    "/model": CommandType.MODEL,
    "/mode": CommandType.MODE,
    "/queue": CommandType.QUEUE,
    "/help": CommandType.HELP,
}

# Slash commands forwarded verbatim to Claude as SDK-dispatchable commands.
_PASSTHROUGH_COMMANDS: set[str] = {"/compact", "/context"}

# Conversational replies to a permission prompt — no slash needed.
_REPLY_KEYWORDS: dict[str, CommandType] = {
    "ok": CommandType.APPROVE,
    "yes": CommandType.APPROVE,
    "approve": CommandType.APPROVE,
    "\U0001f44c": CommandType.APPROVE,  # 👌
    "\u274c": CommandType.DENY,  # ❌
    "no": CommandType.DENY,
    "deny": CommandType.DENY,
    "reject": CommandType.DENY,
}

# Skin-tone modifiers (U+1F3FB–U+1F3FF) and the emoji variation selector ride on
# a base emoji; stripping them lets every 👌 variant match the bare 👌 keyword.
_EMOJI_MODIFIERS = dict.fromkeys([*range(0x1F3FB, 0x1F400), 0xFE0F])


def parse_command(text: str) -> Command:
    stripped = text.strip()
    lowered = stripped.lower()
    reply_key = lowered.translate(_EMOJI_MODIFIERS)
    if reply_key in _REPLY_KEYWORDS:
        return Command(_REPLY_KEYWORDS[reply_key])
    if lowered in _SLASH_KEYWORDS:
        return Command(_SLASH_KEYWORDS[lowered])
    parts = stripped.split(maxsplit=1)
    if parts and parts[0].lower() in _ARG_COMMANDS:
        arg = parts[1].strip() if len(parts) == 2 else None
        return Command(_ARG_COMMANDS[parts[0].lower()], arg=arg)
    if parts and parts[0].lower() in _PASSTHROUGH_COMMANDS:
        return Command(CommandType.PROMPT, arg=stripped)
    # A leading "/" signals command intent; an unrecognized one is a typo,
    # not a prompt for Claude.
    if lowered.startswith("/"):
        return Command(CommandType.UNKNOWN, arg=stripped)
    return Command(CommandType.PROMPT, arg=stripped)


@dataclass(frozen=True)
class HelpEntry:
    """One command's documentation — the single source for both the `/help`
    list (`syntax` + `brief`) and the `/help <command>` detail page (which
    appends `detail` when present). Inline "usage" errors reuse the same
    text so help and error messages never drift apart."""

    syntax: str
    brief: str
    group: str
    detail: str | None = None


# Display order of groups in the `/help` list. "Help" is intentionally absent:
# `/help` itself would be a self-referential entry that just duplicates the
# list's trailing "/help <command>" hint, so it stays in HELP (for /help help)
# but isn't rendered as a list row.
HELP_GROUPS: tuple[str, ...] = (
    "Task",
    "Project",
    "Session",
    "Info",
    "Modes",
    "System",
)

# Keyed by the bare command name (no leading slash). Every command in
# _SLASH_KEYWORDS / _ARG_COMMANDS / _PASSTHROUGH_COMMANDS must appear here —
# test_every_command_has_a_help_entry guards that.
HELP: dict[str, HelpEntry] = {
    "stop": HelpEntry(
        "/stop [all]", "interrupt current running turn", "Task",
        "- `/stop` interrupts the running turn; the next queued prompt (if any) "
        "then starts automatically.\n"
        "- `/stop all` interrupts **and** clears the queue.\n"
        "- The session is kept — use `/clear` to also reset it.",
    ),
    "clear": HelpEntry(
        "/clear", "Interrupt current turn & reset session", "Task",
        "Interrupts the running turn, empties the queue, and resets the session.\n\n"
        "The next message starts a fresh conversation.",
    ),
    "queue": HelpEntry(
        "/queue [rm N | rm all | clear]", "View or edit the queued prompts", "Task",
        "Prompts sent while a turn is running wait in a queue and run in "
        "order.\n"
        "- `/queue` — list the queued prompts, numbered from 1\n"
        "- `/queue rm N` — remove the Nth queued prompt\n"
        "- `/queue rm all` or `/queue clear` — drop every queued prompt",
    ),
    "pwd": HelpEntry(
        "/pwd", "Show current working directory", "Project",
    ),
    "ls": HelpEntry(
        "/ls", "List projects", "Project",
    ),
    "cd": HelpEntry(
        "/cd <name>", "Switch working directory", "Project",
        "Switches the active project and resets its session.\n\n"
        "`/ls` lists the configured project names.",
    ),
    "session": HelpEntry(
        "/session", "Show current session id", "Session",
    ),
    "resume": HelpEntry(
        "/resume [n | id]", "List or switch session", "Session",
        "- `/resume` — list this project's recent sessions, numbered\n"
        "- `/resume <number>` — resume one from that list\n"
        "- `/resume <session-id>` — resume a specific session "
        "(e.g. one started in the desktop TUI)",
    ),
    "compact": HelpEntry(
        "/compact", "Compact current conversation history", "Session",
        "Forwarded to Claude — summarizes the conversation so far to free up "
        "context. Add a hint after it (`/compact keep the API design`).",
    ),
    "status": HelpEntry(
        "/status", "Show runtime status and token usage", "Info",
    ),
    "context": HelpEntry(
        "/context", "Show context window usage", "Info",
        "Forwarded to Claude — reports how much of the context window the "
        "current session is using.",
    ),
    "model": HelpEntry(
        "/model [name]", "List or switch model", "Modes",
        "- `/model` lists the known models and the current one.\n"
        "- `/model <name>` switches for the next turn (e.g. `/model claude-opus-4-8[1m]`).",
    ),
    "mode": HelpEntry(
        "/mode [name]", "List or switch permission mode", "Modes",
        "- `/mode` lists the permission modes and the current one.\n"
        "- `/mode <name>` switches.\n"
        "- `/mode reset` falls back to the TUI's settings.",
    ),
    "verbose": HelpEntry(
        "/verbose on|off", "Stream every step", "Modes",
        "When on, every tool call and subagent step is streamed to the phone.\n\n"
        "`/verbose` with no argument reports the current state.",
    ),
    "debug": HelpEntry(
        "/debug on|off", "Skip Claude Code, debug the daemon only", "Modes",
        "When on, prompts are echoed back instead of running Claude — useful "
        "for testing the daemon plumbing.\n\n"
        "`/debug` with no argument reports the current state.",
    ),
    "update": HelpEntry(
        "/update", "Update the daemon program itself", "System",
        "Checks this daemon's own repo (`main` vs `origin/main`), and if behind:\n"
        "- pulls the new commits (fast-forward only)\n"
        "- runs `make setup` if dependencies changed\n"
        "- runs `make config` if the config template changed (reports new keys; "
        "never overwrites your config)\n"
        "- then asks you to confirm before restarting the daemon.\n\n"
        "Unrelated to your project list — it updates the daemon itself.",
    ),
    "help": HelpEntry(
        "/help [command]", "List commands, or show one in detail", "Help",
        "- `/help` lists every command.\n"
        "- `/help <command>` shows its full usage (e.g. `/help queue`).",
    ),
}
