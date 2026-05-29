"""
Polling entrypoint. Long-polls Anthropic's work queue with no inbound network
requirements — useful behind any firewall, no webhook secret needed.

Run: `uv run python src/host_orchestrator_polling.py`
"""

from __future__ import annotations

import os
import signal
import threading

import orchestrator_lib


POLL_RECLAIM_OLDER_THAN_MS = int(os.environ.get("POLL_RECLAIM_OLDER_THAN_MS", "2000"))


def _poll_block_ms_from_env() -> int:
    raw = os.environ.get("POLL_BLOCK_MS", "999")
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"POLL_BLOCK_MS must be an integer in 1..999, got {raw!r}") from e
    if not 1 <= value <= 999:
        raise ValueError(f"POLL_BLOCK_MS must be in 1..999, got {value}")
    return value


POLL_BLOCK_MS = _poll_block_ms_from_env()


def _install_signal_handlers() -> None:
    def request_shutdown(signum, _frame) -> None:
        orchestrator_lib.log.info(f"shutdown requested by signal {signum}")
        orchestrator_lib.shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


def _poll_loop(orch: orchestrator_lib.Orchestrator) -> None:
    transient_attempts = 0
    while not orchestrator_lib.shutdown.is_set():
        try:
            orch.drain_work(
                block_ms=POLL_BLOCK_MS,
                reclaim_older_than_ms=POLL_RECLAIM_OLDER_THAN_MS,
                raise_poll_errors=True,
            )
            transient_attempts = 0
        except Exception as e:
            if orchestrator_lib.is_permanent_poll_error(e):
                raise
            transient_attempts += 1
            wait = min(60.0, 2.0 ** min(transient_attempts, 6))
            orchestrator_lib.log.warning(
                f"transient poll failure; retry in {wait:.1f}s "
                f"({type(e).__name__}: {e})"
            )
            orchestrator_lib.shutdown.wait(wait)


def main() -> None:
    orch = orchestrator_lib.Orchestrator.from_env()
    orchestrator_lib.acquire_orchestrator_lock("polling", orch.environment_id)
    _install_signal_handlers()
    threading.Thread(target=orchestrator_lib.janitor_loop, daemon=True).start()
    orchestrator_lib.log.info(
        f"polling orchestrator running env={orch.environment_id} "
        f"block_ms={POLL_BLOCK_MS} reclaim_older_than_ms={POLL_RECLAIM_OLDER_THAN_MS}"
    )
    _poll_loop(orch)


if __name__ == "__main__":
    main()
