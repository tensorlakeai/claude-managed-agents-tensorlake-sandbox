.PHONY: build agent session webhook poll \
	build-webhook webhook-sandbox webhook-sandbox-status webhook-sandbox-logs webhook-sandbox-rm

NAME ?= Managed Agent
PROMPT ?= create hello.txt containing 'hi from the sandbox', then read it back

# --- Shared setup ---

build:
	uv run python src/sandbox_image.py

agent:
	uv run python src/create_agent.py "$(NAME)"

# Drive a session end-to-end: needs the agent created + one orchestrator running.
#   make session PROMPT="list /workspace"
#   make session SESSION=ses_... PROMPT="now delete hello.txt"   (reuse a session)
session:
	uv run python src/drive_session.py $(if $(SESSION),--session $(SESSION),) "$(PROMPT)"

# --- Host orchestrator modes (run exactly one orchestrator per environment) ---

# `python -m uvicorn` (not the bare `uvicorn` script) forces the project .venv
# interpreter, so an active conda/other env on PATH can't shadow it.
webhook:
	uv run python -m uvicorn --app-dir src claude_webhook_handler:app --host 0.0.0.0 --port 5051

poll:
	uv run python src/host_orchestrator_polling.py

# --- Webhook-in-sandbox mode (scale-to-zero push, no host process) ---

build-webhook:
	uv run python src/webhook_sandbox_image.py

webhook-sandbox:
	uv run python src/launch_webhook_sandbox.py

webhook-sandbox-status:
	uv run python src/launch_webhook_sandbox.py --status

webhook-sandbox-logs:
	uv run python src/launch_webhook_sandbox.py --logs

webhook-sandbox-rm:
	uv run python src/launch_webhook_sandbox.py --terminate
