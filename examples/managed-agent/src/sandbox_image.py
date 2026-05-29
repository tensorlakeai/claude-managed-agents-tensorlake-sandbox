"""
Builds the Tensorlake sandbox image used by the orchestrator. Idempotent:
named registration means re-running this script after a Dockerfile-equivalent
change rebuilds, while no-op changes reuse the existing registered name.

Run: `uv run python src/sandbox_image.py`
"""

from pathlib import Path

from tensorlake import Image

from config import (
    APP_LOG_LEVEL,
    APP_SANDBOX_ENTRYPOINT_PATH,
    APP_SANDBOX_IDLE_TIMEOUT_SECONDS,
    APP_SANDBOX_REPO_IMAGE_PATH,
    APP_SANDBOX_REPO_URL,
    APP_SANDBOX_REPO_WORKDIR_NAME,
    APP_SANDBOX_WORKDIR,
    SANDBOX_CPUS,
    SANDBOX_DISK_MB,
    SANDBOX_IMAGE_BASE,
    SANDBOX_IMAGE_NAME,
    SANDBOX_MEMORY_MB,
)


_LOCAL_ENTRYPOINT = Path(__file__).parent / "sandbox_entrypoint.py"


def build() -> None:
    print(f"image:      {SANDBOX_IMAGE_NAME}")
    print(f"base:       {SANDBOX_IMAGE_BASE}")
    print(f"seed repo:  {APP_SANDBOX_REPO_URL} -> {APP_SANDBOX_REPO_IMAGE_PATH}")
    print(f"entrypoint: {APP_SANDBOX_ENTRYPOINT_PATH}")
    image = (
        Image(name=SANDBOX_IMAGE_NAME, base_image=SANDBOX_IMAGE_BASE)
        .run("apt-get update && apt-get install -y --no-install-recommends "
             "ca-certificates curl git gh python3 python3-pip && "
             "rm -rf /var/lib/apt/lists/*")
        .run("python3 -m pip install --break-system-packages --no-cache-dir "
             "'anthropic>=0.103' 'httpx>=0.27'")
        .run(f"git clone --depth 1 {APP_SANDBOX_REPO_URL} {APP_SANDBOX_REPO_IMAGE_PATH}")
        .run(f"mkdir -p {APP_SANDBOX_WORKDIR}")
        # Source is relative to context_dir (src/), like Dockerfile COPY.
        .copy(_LOCAL_ENTRYPOINT.name, APP_SANDBOX_ENTRYPOINT_PATH)
        .env("APP_LOG_LEVEL", APP_LOG_LEVEL)
        .env("APP_SANDBOX_WORKDIR", APP_SANDBOX_WORKDIR)
        .env("APP_SANDBOX_REPO_IMAGE_PATH", APP_SANDBOX_REPO_IMAGE_PATH)
        .env("APP_SANDBOX_REPO_WORKDIR_NAME", APP_SANDBOX_REPO_WORKDIR_NAME)
        .env("RUNNER_MAX_IDLE_SECONDS", str(APP_SANDBOX_IDLE_TIMEOUT_SECONDS))
        .workdir(APP_SANDBOX_WORKDIR)
    )
    print("building in a Tensorlake build sandbox (a few minutes; progress below)...")
    # verbose=True streams each build step (apt, pip, copies, snapshot) to stderr.
    # context_dir=src/ keeps the uploaded build context tiny — the default is
    # cwd, which would ship the whole .venv and time out the builder sandbox.
    image.build(
        registered_name=SANDBOX_IMAGE_NAME,
        cpus=SANDBOX_CPUS,
        memory_mb=SANDBOX_MEMORY_MB,
        disk_mb=SANDBOX_DISK_MB,
        context_dir=str(_LOCAL_ENTRYPOINT.parent),
        verbose=True,
    )
    print(f"done — registered image: {SANDBOX_IMAGE_NAME}")


if __name__ == "__main__":
    build()
