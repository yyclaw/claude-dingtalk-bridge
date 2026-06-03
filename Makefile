.DEFAULT_GOAL := help

VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
BRIDGE  := $(VENV)/bin/claude-dingtalk-bridge
CONFIG  := $(HOME)/.config/claude-dingtalk-bridge/config.yaml
CONFIG_DISP := $(patsubst $(HOME)/%,~/%,$(CONFIG))
LOG_DIR := $(HOME)/Library/Logs/claude-dingtalk-bridge

help: ## Show all available commands
	@awk 'BEGIN{FS=":.*?## "} \
		/^## / { sub(/^## ?/, ""); extra[++n] = $$0; next } \
		/^[a-zA-Z_-]+:.*?## / { \
			printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2; \
			for (i = 1; i <= n; i++) printf "%21s\033[90m%s\033[0m\n", "", extra[i]; \
			n = 0; next \
		} \
		!/^##/ { n = 0 } \
	' $(MAKEFILE_LIST)

setup: ## Create the virtualenv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"

config: ## Create the config file from the template (if absent)
	@mkdir -p $(dir $(CONFIG))
	@if [ -f "$(CONFIG)" ]; then \
		echo "Config already exists: $(CONFIG_DISP)"; \
		if [ -x "$(PY)" ]; then \
			$(PY) scripts/sync_config.py "$(CONFIG)"; \
		fi; \
	else \
		cp config.example.yaml "$(CONFIG)" && \
		echo "Created: $(CONFIG_DISP) -- edit it with client_id / client_secret / authorized_user_id and the project list"; \
	fi
	@# The config holds the DingTalk client_secret -- keep it owner-only,
	@# fixing the file in place whether it was just created or pre-existed.
	@chmod 600 "$(CONFIG)"

test: ## Run the unit tests with a branch-coverage summary
	$(PYTEST) -q --cov

check: ## Smoke-check the Bash permission hook against a table of representative commands
	$(PY) scripts/check_bash_permissions.py

start: ## Run the daemon in the foreground (logs to terminal, Ctrl+C to quit)
	@# Refuse to start a foreground instance while the launchd one is running --
	@# both would race for the same DingTalk Stream connection.
	@if [ -x "$(BRIDGE)" ]; then \
		status=$$($(BRIDGE) status 2>/dev/null || true); \
		case "$$status" in \
			*"state = running"*) \
				echo "✋ Background daemon is running: $$status"; \
				echo "   Stop it first:  make daemon-stop"; \
				exit 1 ;; \
		esac; \
	fi
	$(PY) -m claude_dingtalk_bridge

daemon-install: ## Install as a background daemon that starts at login
	$(BRIDGE) install

daemon-start: ## Start the daemon
	$(BRIDGE) start

daemon-stop: ## Stop the daemon (KeepAlive will not relaunch it)
	$(BRIDGE) stop

daemon-restart: ## Restart the daemon
	$(BRIDGE) restart

daemon-status: ## Show daemon status
	$(BRIDGE) status

daemon-uninstall: ## Uninstall the daemon
	$(BRIDGE) uninstall

logs-tail: ## Tail the daemon logs in the terminal (Ctrl+C to quit)
	@mkdir -p $(LOG_DIR)
	@touch $(LOG_DIR)/daemon.out.log $(LOG_DIR)/daemon.err.log
	tail -f $(LOG_DIR)/daemon.out.log $(LOG_DIR)/daemon.err.log

## Defaults to today's logs (since 00:00:00 local, live-tailing). Override with ARGS:
##   make logs-web ARGS="--since 2026-05-22"
##   make logs-web ARGS="--until 2026-05-22"
##   make logs-web ARGS="--since '2026-05-23 11:00' --until '2026-05-23 13:00'"
##   make logs-web ARGS="--tail-bytes 1048576"
##   make logs-web ARGS="--port 9000 --no-open"
logs-web: ## Open the daemon log live-viewer in a browser (defaults to today)
	$(PY) scripts/log_server.py $(ARGS)

.PHONY: help setup config test check start \
	daemon-install daemon-start daemon-stop daemon-restart \
	daemon-status daemon-uninstall logs-tail logs-web
