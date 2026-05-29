import os
from pathlib import Path

try:
    import dotenv
except ImportError:
    # Inside the webhook-orchestrator sandbox image: python-dotenv isn't
    # installed there and there's no .env file — every value arrives as
    # process env injected at launch (see launch_webhook_sandbox.py).
    dotenv = None


EXAMPLE_ROOT = Path(__file__).parents[1]


def load_env() -> None:
    if dotenv is None:
        return
    # override=True so .env wins over a stale TENSORLAKE_API_KEY left exported
    # in the shell: python-dotenv defaults to letting the shell win, which made
    # the orchestrator silently target whichever Tensorlake project the shell
    # key belonged to instead of the one in .env. .env.local still wins last.
    dotenv.load_dotenv(EXAMPLE_ROOT / ".env", override=True)
    dotenv.load_dotenv(EXAMPLE_ROOT / ".env.local", override=True)


load_env()


APP_NAME = "claude-managed-agents-tensorlake"

# Image and sandbox config.
SANDBOX_IMAGE_NAME = os.environ.get("SANDBOX_IMAGE_NAME", "agent-cli")
# ubuntu-minimal is the docs-recommended base for fastest cold starts; nothing
# here needs systemd (all processes are launched via start_process).
SANDBOX_IMAGE_BASE = "tensorlake/ubuntu-minimal"
SANDBOX_CPUS = 2.0
SANDBOX_MEMORY_MB = 4096
SANDBOX_DISK_MB = 25_600
SANDBOX_TIMEOUT_SECONDS = 3600
APP_SANDBOX_WORKDIR = "/workspace"

# Long-running sessions: resume instead of recreate.
# At SANDBOX_TIMEOUT_SECONDS an idle per-session sandbox auto-suspends (state
# frozen to a snapshot, nothing billed). When the session's next work item
# arrives, this flag decides what happens:
#   False (default) — skip the suspended sandbox and create a fresh one from the
#     base image. Clean slate per burst; safe when each burst is independent.
#   True            — resume the suspended sandbox (memory-snapshot restore,
#     sub-second, with /workspace + installed deps + warm caches intact). The
#     right call for a long-lived session whose accumulated in-sandbox state is
#     the point. Idle cost is identical either way — both suspend; the
#     difference is whether resume rebuilds or restores.
APP_RESUME_SUSPENDED_SESSIONS = os.environ.get(
    "RESUME_SUSPENDED_SESSIONS", "false"
).lower() in ("1", "true", "yes")
# Must live somewhere the sandbox's non-root runtime user can read. /root is
# mode 700, so the runner launched via start_process() (which does NOT run as
# root) gets "Permission denied" opening the script and the worker never starts.
# /opt is world-readable (same place the seed repo and webhook orchestrator use).
APP_SANDBOX_ENTRYPOINT_PATH = "/opt/sandbox_entrypoint.py"

# Runner tuning, surfaced as env vars on the in-sandbox process.
APP_LOG_LEVEL = os.environ.get("APP_LOG_LEVEL", "INFO")
APP_SANDBOX_IDLE_TIMEOUT_SECONDS = float(os.environ.get("RUNNER_MAX_IDLE_SECONDS", "300"))

# Demonstration repository seeded into /workspace at session start.
APP_SANDBOX_REPO_URL = "https://github.com/tensorlakeai/tensorlake.git"
APP_SANDBOX_REPO_IMAGE_PATH = "/opt/tensorlake-sdk"
APP_SANDBOX_REPO_WORKDIR_NAME = "tensorlake-sdk"

# Webhook-in-sandbox orchestrator mode: the webhook receiver itself runs in a
# Tensorlake sandbox with its port exposed publicly, so Anthropic can push to
# it — no host process anywhere. timeout_secs is deliberately short by default
# to exercise suspend + wake-on-request.
WEBHOOK_SANDBOX_NAME = os.environ.get("WEBHOOK_SANDBOX_NAME", "webhook-orchestrator")
WEBHOOK_SANDBOX_IMAGE_NAME = os.environ.get(
    "WEBHOOK_SANDBOX_IMAGE_NAME", "webhook-orchestrator"
)
WEBHOOK_SANDBOX_PORT = 5051
WEBHOOK_SANDBOX_TIMEOUT_SECONDS = int(
    os.environ.get("WEBHOOK_SANDBOX_TIMEOUT_SECONDS", "600")
)
WEBHOOK_SANDBOX_SRC_DIR = "/opt/orchestrator"


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var: {name}")
    return value
