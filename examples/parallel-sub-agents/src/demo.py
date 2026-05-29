"""
Standalone demo of fork-from-snapshot parallel exploration on Tensorlake.

Scenario: an agent (or a developer) has a working directory with a Python
module under test. We want to evaluate N candidate "fixes" in parallel —
each fix is materialized into a forked sandbox that boots from a shared
snapshot, runs its fix script, then runs the test suite. The winner is
whichever child has the test suite pass with the lowest elapsed time.

Run: `uv run python src/demo.py`

This file does not depend on Claude Managed Agents — it showcases the
underlying primitive that the cookbook's `parallel.py` exposes. To wire it
into a Managed Agents session, expose `fork_explore` as a custom shell
helper bundled into your sandbox image; the agent invokes it via bash.
"""

from __future__ import annotations

import logging
import textwrap

import dotenv
from parallel import Branch, BranchResult, fork_explore, pick_best
from tensorlake.sandbox import CheckpointType, Sandbox


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("demo")


# The default sandbox base image runs commands as the unprivileged `tl-user`,
# so `/` (and thus `/workspace`) is not writable. Use a path under the user's
# home, which is writable and lives on the persistent ext4 disk — so it is
# captured by a FILESYSTEM checkpoint and inherited by every forked child.
WORKSPACE = "/home/tl-user/workspace"


SUBJECT_UNDER_TEST = textwrap.dedent(
    """
    def add(a, b):
        # buggy implementation; subtracts instead of adding
        return a - b
    """
).lstrip()


TEST_SUITE = textwrap.dedent(
    """
    from app import add

    def test_basic():
        assert add(2, 3) == 5

    def test_zero():
        assert add(0, 0) == 0
    """
).lstrip()


# Each candidate fix writes a different repair to app.py and then runs pytest.
# In a real agent loop these would come from N divergent LLM completions.
CANDIDATE_FIXES = {
    "swap-operator": "def add(a, b):\n    return a + b\n",
    "double-negate": "def add(a, b):\n    return -(-(a) - b)\n",
    "use-sum":       "def add(a, b):\n    return sum([a, b])\n",
    "still-broken":  "def add(a, b):\n    return a * b\n",
}


def make_branch(name: str, fix: str) -> Branch:
    # Write the candidate to app.py, then run pytest. Exit code 0 == pass.
    script = (
        f"set -e; "
        f"cat > {WORKSPACE}/app.py <<'PY'\n{fix}\nPY\n"
        f"cd {WORKSPACE} && python3 -m pytest -q test_app.py"
    )
    return Branch(name=name, command=["bash", "-lc", script])


def judge(result: BranchResult) -> float:
    """Score: 1.0 if tests pass, 0.0 otherwise. Lower elapsed wins ties."""
    return 1.0 if result.exit_code == 0 else 0.0


def main() -> None:
    dotenv.load_dotenv()
    log.info("booting parent sandbox")
    parent = Sandbox.create(
        name="parallel-demo-parent",
        cpus=1.0,
        memory_mb=1024,
        timeout_secs=600,
    )
    try:
        log.info(f"parent={parent.sandbox_id} writing seed files")
        parent.run("bash", ["-lc", f"mkdir -p {WORKSPACE}"])
        parent.write_file(f"{WORKSPACE}/app.py", SUBJECT_UNDER_TEST.encode())
        parent.write_file(f"{WORKSPACE}/test_app.py", TEST_SUITE.encode())
        # Install pytest into the parent so children inherit it via the
        # filesystem snapshot — much faster than installing in each child.
        # The base image is PEP 668-managed and we run as the unprivileged
        # tl-user, so install into the user site (~/.local, on the persistent
        # disk) with --break-system-packages.
        parent.run(
            "bash",
            ["-lc", "pip install --quiet --user --break-system-packages pytest"],
        )

        branches = [make_branch(name, fix) for name, fix in CANDIDATE_FIXES.items()]
        results = fork_explore(
            parent=parent,
            branches=branches,
            checkpoint_type=CheckpointType.FILESYSTEM,
            judge=judge,
        )

        print()
        print("=" * 60)
        for r in results:
            verdict = "PASS" if r.score and r.score > 0 else "FAIL"
            print(f"  [{r.name:14s}] {verdict}  exit={r.exit_code} "
                  f"elapsed={r.elapsed_seconds:.1f}s")
        print("=" * 60)
        winner = pick_best(results)
        if winner is not None:
            print(f"winner: {winner.name} (elapsed {winner.elapsed_seconds:.1f}s)")
        else:
            print("no candidate passed")
    finally:
        log.info(f"terminating parent={parent.sandbox_id}")
        parent.terminate()


if __name__ == "__main__":
    main()
