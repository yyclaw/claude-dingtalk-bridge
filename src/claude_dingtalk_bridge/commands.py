from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CommandType(Enum):
    STOP = auto()
    APPROVE = auto()
    DENY = auto()
    VERBOSE = auto()  # arg: "on" | "off" | None (None → report current state)
    DEBUG = auto()  # arg: "on" | "off" | None (None → report current state)
    LIST_PROJECTS = auto()
    SWITCH_PROJECT = auto()  # arg: project name | None
    STATUS = auto()
    PWD = auto()
    CLEAR = auto()
    HELP = auto()
    SESSION = auto()
    RESUME = auto()  # arg: number index | session id | None
    MODEL = auto()  # arg: model name | None (None → list models)
    MODE = auto()  # arg: "default" | "acceptEdits" | "bypassPermissions" |
    # "plan" | "reset" | None (None → report current state)
    UNKNOWN = auto()
    PROMPT = auto()


@dataclass
class Command:
    type: CommandType
    arg: str | None = None


# Slash commands taking no argument.
_SLASH_KEYWORDS: dict[str, CommandType] = {
    "/stop": CommandType.STOP,
    "/ls": CommandType.LIST_PROJECTS,
    "/status": CommandType.STATUS,
    "/pwd": CommandType.PWD,
    "/clear": CommandType.CLEAR,
    "/help": CommandType.HELP,
    "/session": CommandType.SESSION,
}

# Slash commands taking an argument (the rest of the message).
_ARG_COMMANDS: dict[str, CommandType] = {
    "/cd": CommandType.SWITCH_PROJECT,
    "/verbose": CommandType.VERBOSE,
    "/debug": CommandType.DEBUG,
    "/resume": CommandType.RESUME,
    "/model": CommandType.MODEL,
    "/mode": CommandType.MODE,
}

# Slash commands forwarded verbatim to Claude as SDK-dispatchable commands.
_PASSTHROUGH_COMMANDS: set[str] = {"/compact", "/context", "/usage"}

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
