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

from tensorlake.sandbox import Sandbox, SandboxInfo

from config import (
    WEBHOOK_SANDBOX_IMAGE_NAME,
    WEBHOOK_SANDBOX_NAME,
    WEBHOOK_SANDBOX_PORT,
    WEBHOOK_SANDBOX_SRC_DIR,
    WEBHOOK_SANDBOX_TIMEOUT_SECONDS,
    required_env,
)


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


def _status_str(info: SandboxInfo) -> str | None:
    """Normalize a SandboxStatus enum (or plain str) to its string value."""
    status = getattr(info, "status", None)
    return getattr(status, "value", status)


def _find_info() -> SandboxInfo | None:
    """Return the live SandboxInfo for our webhook sandbox, or None.

    Lookup is by name via Sandbox.list() because connect() takes a sandbox ID,
    not a name. Terminated/failed records are skipped so a stale one doesn't
    mask a missing sandbox.
    """
    try:
        for info in Sandbox.list():
            if getattr(info, "name", None) != WEBHOOK_SANDBOX_NAME:
                continue
            if _status_str(info) in ("terminated", "failed"):
                continue
            return info
    except Exception as e:
        print(
            f"warning: Sandbox.list() failed: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
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

    info = _find_info()
    status = _status_str(info) if info is not None else None

    if info is not None and status == "suspended":
        # Explicit resume for the launcher path. The wake-on-request
        # experiment is the opposite: skip this script and just curl the
        # public URL while suspended.
        print(f"resuming suspended sandbox {WEBHOOK_SANDBOX_NAME} (id={info.sandbox_id})...")
        Sandbox.connect(info.sandbox_id).resume()
        _print_endpoints(info.sandbox_id, "running")
        return
    if info is not None:
        _print_endpoints(info.sandbox_id, str(status))
        return

    print(f"creating sandbox {WEBHOOK_SANDBOX_NAME}...")
    sb = Sandbox.create(
        name=WEBHOOK_SANDBOX_NAME,
        image=WEBHOOK_SANDBOX_IMAGE_NAME,
        cpus=1.0,
        memory_mb=2048,
        timeout_secs=WEBHOOK_SANDBOX_TIMEOUT_SECONDS,
    )
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
    info = _find_info()
    if info is None:
        print(f"sandbox {WEBHOOK_SANDBOX_NAME}: not found")
        return
    _print_endpoints(info.sandbox_id, str(_status_str(info)))


def show_logs() -> None:
    info = _find_info()
    if info is None:
        print(f"sandbox {WEBHOOK_SANDBOX_NAME}: not found", file=sys.stderr)
        raise SystemExit(1)
    sb = Sandbox.connect(info.sandbox_id)
    print(sb.read_file(RECEIVER_LOG).decode(errors="replace"))


def terminate() -> None:
    info = _find_info()
    if info is None:
        print(f"sandbox {WEBHOOK_SANDBOX_NAME}: not found, nothing to do")
        return
    Sandbox.connect(info.sandbox_id).terminate()
    print(f"terminated sandbox {WEBHOOK_SANDBOX_NAME} (id={info.sandbox_id})")


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
