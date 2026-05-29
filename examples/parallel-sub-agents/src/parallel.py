"""
Fork-from-snapshot helpers for Claude Managed Agents on Tensorlake.

The primitive is two SDK calls:
  - sandbox.checkpoint()         -> freezes filesystem (and optionally memory)
  - Sandbox.create(snapshot_id=) -> boots a fresh sandbox from that frozen state

Repeating the second call N times yields N independent children that diverge
from the same parent state. This is the basis for parallel sub-agents,
best-of-N candidate exploration, retry-with-divergence, and RL-style rollouts.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from tensorlake.sandbox import CheckpointType, Sandbox


log = logging.getLogger("parallel")


@dataclass
class Branch:
    """One candidate exploration. `name` is for logging; `command` is what
    actually runs inside the forked child sandbox."""
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class BranchResult:
    name: str
    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    score: float | None = None


def _run_branch(
    *,
    snapshot_id: str,
    branch: Branch,
    parent_name: str,
    timeout_secs: int,
    judge: Callable[[BranchResult], float] | None,
) -> BranchResult:
    child_name = f"{parent_name}-{branch.name}"
    log.info(f"[{child_name}] booting from snapshot={snapshot_id}")
    child = Sandbox.create(
        name=child_name,
        snapshot_id=snapshot_id,
        timeout_secs=timeout_secs,
    )
    started = time.monotonic()
    try:
        # `command[0]` is argv0; the rest are arguments. Matches the shape
        # documented for sandbox.run().
        result = child.run(
            branch.command[0],
            branch.command[1:],
            env=branch.env or None,
            timeout=timeout_secs,
        )
    finally:
        elapsed = time.monotonic() - started

    branch_result = BranchResult(
        name=branch.name,
        sandbox_id=child.sandbox_id,
        exit_code=getattr(result, "exit_code", -1),
        stdout=getattr(result, "stdout", "") or "",
        stderr=getattr(result, "stderr", "") or "",
        elapsed_seconds=elapsed,
    )
    if judge is not None:
        try:
            branch_result.score = float(judge(branch_result))
        except Exception as e:
            log.warning(f"[{child_name}] judge failed: {type(e).__name__}: {e}")
    return branch_result


def fork_explore(
    *,
    parent: Sandbox,
    branches: list[Branch],
    checkpoint_type: CheckpointType = CheckpointType.FILESYSTEM,
    judge: Callable[[BranchResult], float] | None = None,
    child_timeout_secs: int = 300,
    keep_children: bool = False,
    delete_snapshot: bool = True,
) -> list[BranchResult]:
    """Fork the parent sandbox into N children and run each branch in parallel.

    Returns the per-branch results in the same order as `branches`. If `judge`
    is provided, each result is scored; sort downstream if you want best-of-N.

    By default, children are terminated and the intermediate snapshot is
    deleted after all branches finish. Set `keep_children=True` to leave the
    child sandboxes alive (e.g. to materialize the winner's filesystem back
    into the parent later), and `delete_snapshot=False` to keep the snapshot
    for inspection or re-forking.
    """
    if not branches:
        raise ValueError("fork_explore requires at least one branch")

    parent_name = parent.name or parent.sandbox_id
    log.info(
        f"[{parent_name}] checkpointing type={checkpoint_type.value} "
        f"branches={len(branches)}"
    )
    snapshot = parent.checkpoint(checkpoint_type=checkpoint_type)
    if snapshot is None:
        raise RuntimeError(
            f"[{parent_name}] checkpoint() returned None; sandbox state may "
            "be invalid for snapshotting (e.g. terminated)"
        )
    snapshot_id = snapshot.snapshot_id
    log.info(f"[{parent_name}] snapshot={snapshot_id} created")

    results: list[BranchResult] = []
    child_ids: list[str] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(branches),
        ) as pool:
            futures = [
                pool.submit(
                    _run_branch,
                    snapshot_id=snapshot_id,
                    branch=branch,
                    parent_name=parent_name,
                    timeout_secs=child_timeout_secs,
                    judge=judge,
                )
                for branch in branches
            ]
            for future in futures:
                br = future.result()
                child_ids.append(br.sandbox_id)
                results.append(br)
    finally:
        if not keep_children:
            for sandbox_id in child_ids:
                try:
                    Sandbox.connect(sandbox_id).terminate()
                except Exception as e:
                    log.warning(
                        f"could not terminate child {sandbox_id}: "
                        f"{type(e).__name__}: {e}"
                    )
        if delete_snapshot:
            try:
                Sandbox.delete_snapshot(snapshot_id)
            except Exception as e:
                log.warning(
                    f"could not delete snapshot {snapshot_id}: "
                    f"{type(e).__name__}: {e}"
                )

    return results


def pick_best(results: list[BranchResult]) -> BranchResult | None:
    """Highest-scoring branch (ties broken by lower elapsed time). Returns
    None if no branch produced a score."""
    scored = [r for r in results if r.score is not None]
    if not scored:
        return None
    scored.sort(key=lambda r: (-(r.score or 0.0), r.elapsed_seconds))
    return scored[0]
