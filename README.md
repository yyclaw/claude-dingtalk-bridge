# claude-dingtalk-bridge

[English](README.md) | [简体中文](README.zh.md)

Remotely drive the Claude Code running on your computer from your phone over DingTalk — kick off tasks, receive progress, and approve risky operations, turning your phone into a "safe remote screen" for your computer.

## Features

1. Stream-mode outbound persistent connection; the computer listens on no ports and needs no public IP — **no tunneling solution involved, keeping intranet data secure**
2. The computer-side daemon is bound to your DingTalk account; no other account can drive the daemon — secure by design
3. Before every Claude Code task run, the exit IP's country code is verified — if it doesn't match expectations, the task is terminated immediately, **lowering the risk of your Claude account being flagged / banned**
4. A default one-hour Cache TTL improves the cache hit rate for low-frequency interaction scenarios like a phone, saving token consumption; after a round of tasks completes, the `/status` command on your phone lets you check the Session Cost and the last round's cache hit situation at any time, keeping token usage clear
5. Integrates the official Claude Agent SDK; the computer needs no terminal open, just keep it continuously online; use the `/resume` command for seamless handoff between phone and computer

## Quick start

### On the computer

```bash
# 1. Create a DingTalk Stream-mode robot — https://open-dev.dingtalk.com

# 2. Install dependencies, initialize the program's virtualenv
make setup

# 3. Create the config file (~/.config/claude-dingtalk-bridge/config.yaml)
#    (also automatically set file permissions with `chmod 600`)
make config
#    then manually edit it: client_id, client_secret, authorized_user_id, projects

# 4. Run as a background daemon that starts at login
#    (requires Xcode command line tools — if not installed, run `xcode-select --install` once first)
make daemon-install
make daemon-start

# 5. View runtime logs (Optional)
make logs-web
```

### On the phone

Chat 1:1 with the robot you created and send `/help` to see all supported commands.
