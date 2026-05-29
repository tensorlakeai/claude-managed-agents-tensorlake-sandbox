# Parallel Sub-Agents — fork from snapshot

This example shows off the primitive that makes Tensorlake distinctive among the Claude Managed Agents launch-partner sandboxes: **forking N children from a single snapshot for parallel exploration.**

Two SDK calls do the whole thing:

```python
snap = parent.checkpoint(checkpoint_type=CheckpointType.FILESYSTEM)
child = Sandbox.create(snapshot_id=snap.snapshot_id, name=...)
```

Repeat the second call N times in parallel threads and you have N independent children that all start from the same parent state and diverge.

## What this is good for

* **Best-of-N candidate exploration.** An agent generates N candidate fixes; each fix runs in its own forked sandbox against the test suite. Pick the winner.
* **Parallel sub-agents.** A planning agent spawns N worker agents from a single snapshot, each handed a different sub-task. No setup duplication; no cross-contamination.
* **Retry with divergence.** A tool call failed unexpectedly; re-run it in K forks with K different inputs/seeds. Keep the one that didn't fail.
* **RL rollouts.** Tensorlake's own docs lead with this primitive for agentic RL; see [Reproducible Environments for RL](https://docs.tensorlake.ai/sandboxes/agentic-rl-reproducible-env.md).

## Run the demo

```bash
cd examples/parallel-sub-agents
cp .env.example .env
# Edit .env: set TENSORLAKE_API_KEY
uv sync
uv run python src/demo.py
```

You should see four candidate fixes evaluated in parallel against a tiny pytest suite, with the passing candidate selected as the winner:

```
[swap-operator]  PASS  exit=0 elapsed=2.4s
[double-negate]  PASS  exit=0 elapsed=2.7s
[use-sum]        PASS  exit=0 elapsed=2.3s
[still-broken]   FAIL  exit=1 elapsed=2.5s
winner: use-sum (elapsed 2.3s)
```

## How this composes with Managed Agents

The Claude Managed Agents toolset (bash, read, write, edit, glob, grep, web_fetch, web_search) doesn't natively expose "fork a sandbox" — but the agent has `bash`, and `bash` can invoke any helper you bundle into your sandbox image. Three integration patterns, roughly in increasing complexity:

1. **As a build-time tool inside the image.** Bake `fork_explore` into a CLI helper script (`tl-fork-explore --n 4 --task "..."`) at image-build time. The agent calls it via bash. Authentication: the orchestrator forwards `TENSORLAKE_API_KEY` to the parent sandbox via `start_process(env={...})` (the create API no longer takes `secret_names`), so the helper inherits it from the process environment. Caveats: the agent now has Tensorlake API access from inside its own sandbox — fine for trusted code, audit before exposing to untrusted prompts.
2. **As an MCP tool.** Stand up a small MCP server that exposes `fork_explore` as a tool, register it with the agent. The agent invokes it just like any other MCP tool. The MCP server holds the Tensorlake credential, not the sandbox.
3. **In the orchestrator, gated by session metadata.** Have the agent set a `session.metadata` key (e.g. `tensorlake.fork_branches=[...]`); the orchestrator polls for that key and performs the fork host-side. Most secure (no Tensorlake creds in the sandbox at all) but least ergonomic for the agent.

The demo in `src/demo.py` runs the host-side variant directly so you can validate the SDK call sequence without setting up an MCP server or an MA session.

## Files

- `src/parallel.py` — reusable `fork_explore(parent, branches, judge, ...) -> list[BranchResult]` library
- `src/demo.py` — runnable demo: spin up a parent, write a buggy module + test suite, fork four candidate fixes in parallel, pick the winner
- `.env.example` — `TENSORLAKE_API_KEY` only; this example doesn't need any Anthropic credentials

## Caveats and limits

- **Filesystem vs. memory checkpoints.** The demo uses `CheckpointType.FILESYSTEM` (fast, captures disk state only). `CheckpointType.MEMORY` captures running processes too — useful if the parent is mid-task with state in RAM, but heavier and the children inherit fixed CPU/memory from the snapshot.
- **Concurrent sandbox limits.** Forking N children consumes N sandbox slots in your Tensorlake account. Check your quota before running with large N.
- **Snapshots are not free.** Each `parent.checkpoint()` writes a snapshot to durable storage. The demo deletes its snapshot after the fork completes; for repeated runs in the same parent, consider reusing one snapshot across multiple `fork_explore` calls.
- **Children inherit secrets and exposed ports.** A child boots with the parent's secret bindings and any exposed port config carried in the snapshot. If you exposed port 8080 on the parent for a preview, all children will too — set `allow_unauthenticated_access` carefully.
