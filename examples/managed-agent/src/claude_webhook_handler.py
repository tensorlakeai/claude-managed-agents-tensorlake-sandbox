"""
FastAPI webhook endpoint. Bring your own host (Fly, Vercel, Cloud Run, local
+ ngrok for dev). Anthropic requires a public HTTPS URL on port 443 for the
registered webhook, so put TLS termination in front of `:5051`.

Run locally:
    uv run uvicorn --app-dir src claude_webhook_handler:app --host 0.0.0.0 --port 5051

Run in production behind your reverse proxy:
    same command, port `${PORT}`.
"""

from __future__ import annotations

import math
import os
import threading

import orchestrator_lib
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request


def _positive_float_from_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a positive number, got {raw!r}") from e
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number, got {value}")
    return value


def _nonnegative_int_from_env(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}") from e
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value}")
    return value


PORT = int(os.environ.get("PORT", "5051"))
WEBHOOK_SECRET = os.environ.get("ANTHROPIC_WEBHOOK_SIGNING_KEY")
WEBHOOK_DRAIN_SECONDS = _positive_float_from_env("WEBHOOK_DRAIN_SECONDS", "30")
WEBHOOK_RECLAIM_OLDER_THAN_MS = _nonnegative_int_from_env(
    "WEBHOOK_RECLAIM_OLDER_THAN_MS", "2000"
)

ORCH = orchestrator_lib.Orchestrator.from_env()

app = FastAPI()


def _fallback_drain_loop() -> None:
    """Periodic safety-net drain so a dropped webhook can't strand work."""
    attempts = 0
    while not orchestrator_lib.shutdown.is_set():
        try:
            result = ORCH.drain_work(
                reclaim_older_than_ms=WEBHOOK_RECLAIM_OLDER_THAN_MS,
                raise_poll_errors=True,
            )
            attempts = 0
            processed = len(result["spawned"]) + len(result["failed"])
            if processed:
                orchestrator_lib.log.info(f"fallback drain processed={processed}")
            wait = WEBHOOK_DRAIN_SECONDS
        except Exception as e:
            attempts += 1
            wait = min(60.0, 2.0 ** min(attempts, 6))
            orchestrator_lib.log.warning(
                f"fallback drain failed; retry in {wait:.1f}s "
                f"({type(e).__name__}: {e})"
            )
        orchestrator_lib.shutdown.wait(wait)


@app.post("/")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> dict:
    if WEBHOOK_SECRET is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_WEBHOOK_SIGNING_KEY not set")
    raw = await request.body()
    try:
        event = ORCH.client.beta.webhooks.unwrap(
            raw.decode(),
            headers=dict(request.headers),
            key=WEBHOOK_SECRET,
        )
    except Exception as e:
        # standardwebhooks raises WebhookVerificationError; ValueError can come
        # from the SDK helper. Either way, hide payload contents from the 401.
        orchestrator_lib.log.warning(
            f"signature reject: {type(e).__name__}: {e}"
        )
        raise HTTPException(
            status_code=401, detail="signature verification failed"
        ) from None
    ev_type = event.data.type
    session_id = event.data.id
    orchestrator_lib.log.info(f"event={ev_type} session={session_id}")
    if ev_type != "session.status_run_started":
        return {"status": "ignored", "type": ev_type}
    # Ack the webhook immediately and drain after the response, since starting
    # a sandbox takes seconds and would otherwise risk Anthropic timing out
    # and retrying. The Orchestrator's drain lock serializes overlapping
    # drains.
    background_tasks.add_task(
        ORCH.drain_work,
        reclaim_older_than_ms=WEBHOOK_RECLAIM_OLDER_THAN_MS,
    )
    return {"status": "queued"}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "environment_id": ORCH.environment_id}


@app.on_event("startup")
def on_startup() -> None:
    if WEBHOOK_SECRET is None:
        raise RuntimeError(
            "ANTHROPIC_WEBHOOK_SIGNING_KEY is not set; webhook mode cannot verify "
            "signatures. Set it in .env, or run host_orchestrator_polling.py."
        )
    orchestrator_lib.acquire_orchestrator_lock("webhook", ORCH.environment_id)
    threading.Thread(target=_fallback_drain_loop, daemon=True).start()
    threading.Thread(target=orchestrator_lib.janitor_loop, daemon=True).start()
    orchestrator_lib.log.info(
        f"webhook orchestrator listening on :{PORT} env={ORCH.environment_id}"
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    orchestrator_lib.shutdown.set()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
