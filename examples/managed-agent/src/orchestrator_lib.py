"""
Shared orchestration logic used by every orchestrator entrypoint — host
polling, host webhook, and the webhook receiver running inside a Tensorlake
sandbox: get-or-create a Tensorlake sandbox per active session, launch the
in-sandbox runner, and drain Anthropic's work queue.

The environment-specific state (environment ID/key, Anthropic client, locks)
lives on the `Orchestrator` class, populated from env vars via
`Orchestrator.from_env()`.

Key Tensorlake-specific details:
- Sandbox lookup is by `name`, not by ID, via Sandbox.connect().
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from contextlib import suppress
from datetime import datetime, timezone

# Importing config runs load_env(), populating os.environ from .env. This MUST
# happen before importing the tensorlake SDK: tensorlake.sandbox snapshots
# TENSORLAKE_API_KEY into module-level parameter defaults at import time
# (sandbox/_defaults.py), which every Sandbox.create()/connect()/list() call
# then inherits. Import it after the SDK and a key that lives only in .env
# (never exported in the shell) is missed entirely — Sandbox.* 401s with
# AUTH_REQUIRED. Keep this import first among the third-party imports.
from config import (
    APP_RESUME_SUSPENDED_SESSIONS,
    APP_SANDBOX_ENTRYPOINT_PATH,
    SANDBOX_CPUS,
    SANDBOX_IMAGE_NAME,
    SANDBOX_MEMORY_MB,
    SANDBOX_TIMEOUT_SECONDS,
    required_env,
)

import anthropic
from anthropic.lib.environments import iter_work
from tensorlake.sandbox import Sandbox


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("orchestrator")

JANITOR_SECONDS = int(os.environ.get("JANITOR_SECONDS", "60"))

shutdown = threading.Event()

_ORCHESTRATOR_LOCK_FD: int | None = None


def sandbox_name(session_id: str) -> str:
    # Tensorlake sandbox names allow only lowercase letters, digits, and
    # hyphens, but Anthropic session IDs contain underscores and uppercase
    # (e.g. "sesn_01H2ZZ..."). Lowercase and map every disallowed character to
    # a hyphen so the name is valid. This is deterministic, so the get-or-create
    # lookup in find_sandbox_by_name stays consistent.
    slug = re.sub(r"[^a-z0-9-]", "-", session_id.lower())
    return f"agent-{slug}"


def _status_str(info: object) -> str | None:
    """Normalize a SandboxStatus enum (or plain str) to its string value."""
    status = getattr(info, "status", None)
    return getattr(status, "value", status)


def find_sandbox_by_name(name: str, *, include_suspended: bool = True) -> Sandbox | None:
    """Resolve a named sandbox to a connected handle.

    Sandbox.connect() takes a sandbox ID, not a name, so we list sandboxes,
    match on name, and connect by sandbox_id. Terminated/failed matches are
    skipped; suspended ones are skipped too when include_suspended is False.
    """
    try:
        infos = [i for i in Sandbox.list() if getattr(i, "name", None) == name]
    except Exception as e:
        log.debug(f"Sandbox.list() failed: {type(e).__name__}: {e}")
        return None
    for info in infos:
        status = _status_str(info)
        if status in ("terminated", "failed"):
            continue
        if status == "suspended" and not include_suspended:
            log.info(f"sandbox {name} exists but status={status}")
            continue
        try:
            return Sandbox.connect(info.sandbox_id)
        except Exception as e:
            log.debug(f"connect({info.sandbox_id}) failed: {type(e).__name__}: {e}")
    return None


def _find_live_sandbox(name: str) -> Sandbox | None:
    # Default: a suspended sandbox is treated as not-live, so process_work_item
    # recreates it (clean slate per burst) rather than reusing a paused one.
    #
    # With APP_RESUME_SUSPENDED_SESSIONS set, suspended sandboxes are included
    # instead: find_sandbox_by_name connects to one by sandbox_id, and the
    # subsequent inbound operation (the runner relaunch in process_work_item)
    # resumes it — a memory-snapshot restore that brings /workspace, installed
    # deps, and warm caches back intact in well under a second. Use this for
    # long-running sessions whose accumulated in-sandbox state is worth keeping
    # across idle gaps; leave it off when each burst should start fresh.
    return find_sandbox_by_name(
        name, include_suspended=APP_RESUME_SUSPENDED_SESSIONS
    )


class Orchestrator:
    """Per-environment orchestration: one instance per ANTHROPIC_ENVIRONMENT_ID."""

    def __init__(self, *, environment_id: str, environment_key: str) -> None:
        self.environment_id = environment_id
        self.environment_key = environment_key
        self.client = anthropic.Anthropic(auth_token=environment_key)
        self._drain_lock = threading.RLock()
        self._session_locks_lock = threading.Lock()
        self._session_locks: dict[str, threading.RLock] = {}

    @classmethod
    def from_env(cls) -> Orchestrator:
        """Host-side construction from ANTHROPIC_* env vars (.env via config)."""
        return cls(
            environment_id=required_env("ANTHROPIC_ENVIRONMENT_ID"),
            environment_key=required_env("ANTHROPIC_ENVIRONMENT_KEY"),
        )

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._session_locks_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock

    def _session_env(self, *, session_id: str, work_id: str) -> dict[str, str]:
        # The environment key used to be injected via secret_names; the current
        # Sandbox API drops that field, so it rides the process env too.
        return {
            "ANTHROPIC_SESSION_ID": session_id,
            "ANTHROPIC_WORK_ID": work_id,
            "ANTHROPIC_ENVIRONMENT_ID": self.environment_id,
            "ANTHROPIC_ENVIRONMENT_KEY": self.environment_key,
        }

    def _launch_runner(self, sb: Sandbox, *, session_id: str, work_id: str) -> None:
        # Every ANTHROPIC_* var the runner needs (including the environment key)
        # is passed via env=; bash is only needed to redirect the runner's
        # output to a log file.
        sb.start_process(
            "bash",
            ["-lc", f"exec python3 {APP_SANDBOX_ENTRYPOINT_PATH} > /tmp/runner.log 2>&1"],
            env=self._session_env(session_id=session_id, work_id=work_id),
        )

    def _create_sandbox(self, session_id: str, work_id: str) -> Sandbox:
        name = sandbox_name(session_id)
        sb = Sandbox.create(
            name=name,
            image=SANDBOX_IMAGE_NAME,
            cpus=SANDBOX_CPUS,
            memory_mb=SANDBOX_MEMORY_MB,
            timeout_secs=SANDBOX_TIMEOUT_SECONDS,
        )
        self._launch_runner(sb, session_id=session_id, work_id=work_id)
        return sb

    def process_work_item(self, *, session_id: str, work_id: str) -> dict:
        """Get-or-create a Tensorlake sandbox for one already-ack'd work item."""
        with self._session_lock(session_id):
            existing = _find_live_sandbox(sandbox_name(session_id))
            if existing is not None:
                log.info(
                    f"work={work_id} session={session_id} "
                    f"sandbox={existing.sandbox_id} (live)"
                )
                # Relaunch the runner with fresh per-session env. The old
                # process may have exited at max_idle while the sandbox stayed
                # up; or (with APP_RESUME_SUSPENDED_SESSIONS) the sandbox was
                # suspended and this start_process is the inbound op that resumes
                # it. Either way the filesystem state carries over — only the
                # short-lived runner process is restarted.
                self._launch_runner(existing, session_id=session_id, work_id=work_id)
                return {
                    "session_id": session_id,
                    "work_id": work_id,
                    "sandbox_id": existing.sandbox_id,
                    "created": False,
                }
            sb = self._create_sandbox(session_id, work_id)
            log.info(
                f"work={work_id} session={session_id} "
                f"sandbox={sb.sandbox_id} (created)"
            )
            return {
                "session_id": session_id,
                "work_id": work_id,
                "sandbox_id": sb.sandbox_id,
                "created": True,
            }

    def drain_work(
        self,
        *,
        block_ms: int | None = None,
        reclaim_older_than_ms: int = 2000,
        raise_poll_errors: bool = False,
    ) -> dict:
        """Poll Anthropic's queue until empty, spawning a sandbox per work item.

        block_ms=None (the default) makes each poll non-blocking, so the final
        empty poll returns immediately — what the docs recommend for
        webhook-triggered drains. The host poller passes block_ms=999 to
        long-poll instead.

        Returns {"spawned": [...], "failed": [...]}.
        """
        spawned: list[dict] = []
        failed: list[dict] = []
        with self._drain_lock:
            # `iter_work` is the sync poller generator (`work.poller(...)` only
            # exists on AsyncAnthropic). drain=True returns once the queue is
            # empty; auto_stop=False because the spawned sandbox's worker owns
            # the stop call. Each yielded item has already been ack'd; auth
            # comes from the bound client (auth_token=environment_key).
            try:
                for work in iter_work(
                    self.client.beta.environments.work,
                    environment_id=self.environment_id,
                    block_ms=block_ms,
                    reclaim_older_than_ms=reclaim_older_than_ms,
                    drain=True,
                    auto_stop=False,
                ):
                    if work.data.type != "session":
                        log.info(f"skipping work={work.id} type={work.data.type}")
                        continue
                    session_id = work.data.id
                    try:
                        spawned.append(
                            self.process_work_item(
                                session_id=session_id,
                                work_id=work.id,
                            )
                        )
                    except Exception as e:
                        log.exception(
                            "FAILED work=%s session=%s: %s: %s",
                            work.id,
                            session_id,
                            type(e).__name__,
                            e,
                        )
                        failed.append(
                            {
                                "work_id": work.id,
                                "session_id": session_id,
                                "error": type(e).__name__,
                            }
                        )
            except Exception:
                if raise_poll_errors:
                    raise
                log.exception("transient poll error during drain")
        if failed:
            log.warning(
                f"drain finished: spawned={len(spawned)} failed={len(failed)}"
            )
        return {"spawned": spawned, "failed": failed}


def acquire_orchestrator_lock(mode: str, environment_id: str) -> None:
    """Fail fast if another same-host orchestrator owns this environment."""
    import fcntl
    import pathlib

    global _ORCHESTRATOR_LOCK_FD
    lock_path = pathlib.Path(
        os.environ.get(
            "ORCHESTRATOR_LOCK_FILE",
            f"/tmp/anthropic-selfhosted-orchestrator-{environment_id}.lock",
        )
    )
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        existing = os.read(fd, 4096).decode(errors="replace")
        os.close(fd)
        raise RuntimeError(
            f"another orchestrator already holds {lock_path}: "
            f"{existing or '<unknown>'}"
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()} mode={mode} env={environment_id}\n".encode())
    _ORCHESTRATOR_LOCK_FD = fd


def is_permanent_poll_error(err: Exception) -> bool:
    return (
        isinstance(err, anthropic.APIStatusError)
        and 400 <= err.status_code < 500
        and err.status_code not in (408, 429)
    )


def janitor_loop() -> None:
    """Sweep failed sandboxes carrying our app's name prefix.

    Tensorlake's timeout_secs handles the common case of auto-suspending idle
    sandboxes; the janitor catches edge cases where a sandbox is stuck in a
    failed state.

    Sandbox.list() yields SandboxInfo records (no callable lifecycle methods);
    Sandbox.connect(sandbox_id) returns an operable Sandbox handle.
    """
    name_prefix = "agent-"
    while not shutdown.is_set():
        try:
            for info in Sandbox.list():
                name = getattr(info, "name", None) or ""
                if not name.startswith(name_prefix):
                    continue
                # status is a SandboxStatus enum, so compare its string value.
                if _status_str(info) == "failed":
                    log.info(f"janitor terminating failed sandbox {name}")
                    with suppress(Exception):
                        Sandbox.connect(info.sandbox_id).terminate()
        except Exception as e:
            log.warning(f"janitor sweep failed: {type(e).__name__}: {e}")
        shutdown.wait(JANITOR_SECONDS)


def iso8601_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def now_monotonic() -> float:
    return time.monotonic()
