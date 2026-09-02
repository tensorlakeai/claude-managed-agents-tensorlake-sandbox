"""
Webhook-in-sandbox orchestrator: run the FastAPI webhook receiver *inside* a
Tensorlake sandbox with its port exposed publicly, so Anthropic pushes events
straight to Tensorlake — push latency with no host process and no TLS of your
own. Tensorlake's proxy terminates HTTPS at
https://{port}-{sandbox_id}.sandbox.tensorlake.ai.

This is also the wake-on-request experiment: timeout_secs is short by default
(WEBHOOK_SANDBOX_TIMEOUT_SECONDS=600), so the sandbox suspends when idle —
with memory and processes preserved — and the question is whether an inbound
webhook/curl resumes it. See the README's test plan.

Usage:
    uv run python src/launch_webhook_sandbox.py              # get-or-create, print URL
    uv run python src/launch_webhook_sandbox.py --status     # status + URL, no changes
    uv run python src/launch_webhook_sandbox.py --logs       # print the receiver log
    uv run python src/launch_webhook_sandbox.py --terminate  # tear down
"""

from __future__ import annotations

import argparse
import sys

# Import config before the tensorlake SDK: config.load_env() populates
# os.environ from .env, and tensorlake.sandbox snapshots TENSORLAKE_API_KEY
# into its parameter defaults at import time. Reverse the order and a key that
# lives only in .env is missed, so Sandbox.* fails with 401 AUTH_REQUIRED.
from config import (
    WEBHOOK_SANDBOX_IMAGE_NAME,
    WEBHOOK_SANDBOX_NAME,
    WEBHOOK_SANDBOX_PORT,
    WEBHOOK_SANDBOX_SRC_DIR,
    WEBHOOK_SANDBOX_TIMEOUT_SECONDS,
    required_env,
)

from tensorlake.sandbox import Sandbox, SandboxNotFoundError


# Credentials the in-sandbox receiver needs: the environment key for the
# Anthropic queue/webhook client, and the Tensorlake key so it can create
# per-session sandboxes from inside its own sandbox. The current Sandbox API
# no longer accepts `secret_names` on create, so these are passed as process
# env at launch (read host-side from .env) rather than pre-registered secrets.
CREDENTIAL_ENV_NAMES = ["ANTHROPIC_ENVIRONMENT_KEY", "TENSORLAKE_API_KEY"]

RECEIVER_LOG = "/tmp/webhook.log"

UVICORN_CMD = (
    f"exec python3 -m uvicorn --app-dir {WEBHOOK_SANDBOX_SRC_DIR} "
    f"claude_webhook_handler:app --host 0.0.0.0 --port {WEBHOOK_SANDBOX_PORT} "
    f"> {RECEIVER_LOG} 2>&1"
)


def public_url(sandbox_id: str) -> str:
    # The public per-port URL is keyed by sandbox ID, not name:
    # https://{port}-{sandbox_id}.sandbox.tensorlake.ai
    return f"https://{WEBHOOK_SANDBOX_PORT}-{sandbox_id}.sandbox.tensorlake.ai"


def _connect() -> Sandbox | None:
    """Attach to our webhook sandbox by name, or return None if there is none.

    Sandbox.connect() accepts a name as well as an ID and does not resume a
    suspended sandbox, so --status can observe "suspended" without waking it.
    """
    try:
        return Sandbox.connect(WEBHOOK_SANDBOX_NAME)
    except SandboxNotFoundError:
        return None


def _print_endpoints(sandbox_id: str, status: str) -> None:
    url = public_url(sandbox_id)
    print(f"sandbox:  {WEBHOOK_SANDBOX_NAME} (id={sandbox_id}, status={status})")
    print(f"webhook:  {url}/")
    print(f"health:   curl {url}/healthz")
    print()
    print("Register the webhook URL in Claude Platform (Session lifecycle ->")
    print("Run started) and put its signing secret in ANTHROPIC_WEBHOOK_SIGNING_KEY.")


def launch() -> None:
    # Validated host-side so a typo fails here, not silently in the sandbox.
    environment_id = required_env("ANTHROPIC_ENVIRONMENT_ID")
    webhook_secret = required_env("ANTHROPIC_WEBHOOK_SIGNING_KEY")
    # The two credentials that used to ride secret_names now travel as process
    # env; fail fast here if either is missing from the host .env.
    credentials = {name: required_env(name) for name in CREDENTIAL_ENV_NAMES}

    # get_or_create is the whole lifecycle: attach to a running sandbox,
    # resume a suspended one, or create it when the name is free. The size,
    # image, and timeout apply only on create; an existing sandbox keeps its
    # own. (The wake-on-request experiment is the opposite path: skip this
    # script and just curl the public URL while suspended.)
    sb = Sandbox.get_or_create(
        WEBHOOK_SANDBOX_NAME,
        image=WEBHOOK_SANDBOX_IMAGE_NAME,
        cpus=1.0,
        memory_mb=2048,
        timeout_secs=WEBHOOK_SANDBOX_TIMEOUT_SECONDS,
    )
    if sb.bind_outcome != "created":
        # Already existed: the port is exposed and uvicorn is still running
        # (resume restores memory and processes), so there is nothing to start.
        print(f"{sb.bind_outcome} sandbox {WEBHOOK_SANDBOX_NAME} (id={sb.sandbox_id})")
        _print_endpoints(sb.sandbox_id, "running")
        return

    print(f"created sandbox {WEBHOOK_SANDBOX_NAME} (id={sb.sandbox_id})")
    sb.update(
        exposed_ports=[WEBHOOK_SANDBOX_PORT],
        allow_unauthenticated_access=True,
    )
    # All of the receiver's config and credentials ride the process env at
    # launch (the create API no longer takes secret_names).
    sb.start_process(
        "bash",
        ["-lc", UVICORN_CMD],
        env={
            "ANTHROPIC_ENVIRONMENT_ID": environment_id,
            "ANTHROPIC_WEBHOOK_SIGNING_KEY": webhook_secret,
            **credentials,
        },
    )
    _print_endpoints(sb.sandbox_id, "running")


def show_status() -> None:
    sb = _connect()
    if sb is None:
        print(f"sandbox {WEBHOOK_SANDBOX_NAME}: not found")
        return
    # .status is a control-plane read, not proxied traffic, so it does not
    # count as inbound activity and does not wake a suspended sandbox.
    _print_endpoints(sb.sandbox_id, sb.status.value)


def show_logs() -> None:
    sb = _connect()
    if sb is None:
        print(f"sandbox {WEBHOOK_SANDBOX_NAME}: not found", file=sys.stderr)
        raise SystemExit(1)
    print(sb.read_file(RECEIVER_LOG).decode(errors="replace"))


def terminate() -> None:
    sb = _connect()
    if sb is None:
        print(f"sandbox {WEBHOOK_SANDBOX_NAME}: not found, nothing to do")
        return
    sb.terminate()
    print(f"terminated sandbox {WEBHOOK_SANDBOX_NAME} (id={sb.sandbox_id})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--status", action="store_true", help="print status + URL")
    action.add_argument("--logs", action="store_true", help="print the receiver log")
    action.add_argument("--terminate", action="store_true", help="tear down")
    args = parser.parse_args()
    if args.status:
        show_status()
    elif args.logs:
        show_logs()
    elif args.terminate:
        terminate()
    else:
        launch()


if __name__ == "__main__":
    main()
