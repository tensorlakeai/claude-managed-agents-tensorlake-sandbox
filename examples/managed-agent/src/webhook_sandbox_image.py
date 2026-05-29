"""
Builds the Tensorlake image for the webhook-in-sandbox orchestrator mode: the
same FastAPI receiver that `make webhook` runs on a host, baked into an image
so it can run inside a Tensorlake sandbox with its port exposed publicly.

Run: `uv run python src/webhook_sandbox_image.py`  (make build-webhook)
"""

from pathlib import Path

from tensorlake import Image

from config import (
    SANDBOX_IMAGE_BASE,
    WEBHOOK_SANDBOX_IMAGE_NAME,
    WEBHOOK_SANDBOX_SRC_DIR,
)


_SRC = Path(__file__).parent

# The receiver needs the tensorlake SDK too (it creates per-session sandboxes
# from inside its own sandbox), unlike the agent image which only needs the
# anthropic worker deps.
_ORCHESTRATOR_FILES = ["claude_webhook_handler.py", "orchestrator_lib.py", "config.py"]


def build() -> None:
    print(f"image:      {WEBHOOK_SANDBOX_IMAGE_NAME}")
    print(f"base:       {SANDBOX_IMAGE_BASE}")
    print(f"copying:    {', '.join(_ORCHESTRATOR_FILES)} -> {WEBHOOK_SANDBOX_SRC_DIR}/")
    print("pip deps:   anthropic, tensorlake, fastapi, uvicorn")
    image = (
        Image(name=WEBHOOK_SANDBOX_IMAGE_NAME, base_image=SANDBOX_IMAGE_BASE)
        .run(
            "apt-get update && apt-get install -y --no-install-recommends "
            "ca-certificates python3 python3-pip && "
            "rm -rf /var/lib/apt/lists/*"
        )
        .run(
            "python3 -m pip install --break-system-packages --no-cache-dir "
            "'anthropic[webhooks]>=0.103' 'tensorlake>=0.2' 'fastapi>=0.136' 'uvicorn>=0.30'"
        )
        .run(f"mkdir -p {WEBHOOK_SANDBOX_SRC_DIR}")
    )
    for file_name in _ORCHESTRATOR_FILES:
        # Sources are relative to context_dir (src/), like Dockerfile COPY.
        image = image.copy(file_name, f"{WEBHOOK_SANDBOX_SRC_DIR}/{file_name}")
    print("building in a Tensorlake build sandbox (a few minutes; progress below)...")
    # verbose=True streams each build step (apt, pip, copies, snapshot) to stderr.
    # cpus/memory_mb here size only the transient *builder* sandbox (SDK default
    # 2.0/4096) — the webhook sandbox's own size is set in launch_webhook_sandbox.
    # context_dir=src/ keeps the uploaded build context tiny — the default is
    # cwd, which would ship the whole 110MB .venv and time out the builder.
    image.build(
        registered_name=WEBHOOK_SANDBOX_IMAGE_NAME,
        context_dir=str(_SRC),
        verbose=True,
    )
    print(f"done — registered image: {WEBHOOK_SANDBOX_IMAGE_NAME}")
    print("next: make webhook-sandbox")


if __name__ == "__main__":
    build()
