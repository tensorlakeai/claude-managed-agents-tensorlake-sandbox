"""
Create a long-lived Claude Managed Agent for this example.

Convenience over clicking through the Claude Platform console: creates an agent
with the sandbox + web tools enabled and prints its id for ANTHROPIC_AGENT_ID.

Fails if a non-archived agent with the same name already exists, so it is safe
to run more than once.

    uv run python src/create_agent.py "Managed Agent CLI"
"""

from __future__ import annotations

import argparse
import os
import sys

import anthropic

from config import load_env


load_env()

# The in-sandbox runner provides a real shell + filesystem; expose the matching
# tools plus web access. All set to always_allow so the agent runs unattended.
SANDBOX_TOOLS = ["bash", "read", "write", "edit", "glob", "grep"]
WEB_TOOLS = ["web_fetch", "web_search"]

DEFAULT_MODEL = os.environ.get("ANTHROPIC_AGENT_MODEL", "claude-sonnet-4-6")
DEFAULT_SYSTEM = (
    "You have a working sandbox. Use your tools to do what is asked. Be terse."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Claude Managed Agent.")
    parser.add_argument(
        "name", help="agent name; must be unique among non-archived agents"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model id")
    parser.add_argument("--system", default=DEFAULT_SYSTEM, help="system prompt")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "missing ANTHROPIC_API_KEY (set it in .env.local)", file=sys.stderr
        )
        return 1

    client = anthropic.Anthropic(api_key=api_key)

    for existing in client.beta.agents.list():
        if existing.name == args.name and existing.archived_at is None:
            print(
                f"agent named {args.name!r} already exists: {existing.id}",
                file=sys.stderr,
            )
            return 1

    agent = client.beta.agents.create(
        name=args.name,
        model=args.model,
        system=args.system,
        tools=[
            {
                "type": "agent_toolset_20260401",
                "default_config": {
                    "enabled": False,
                    "permission_policy": {"type": "always_allow"},
                },
                "configs": [
                    {
                        "name": tool_name,
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                    for tool_name in SANDBOX_TOOLS + WEB_TOOLS
                ],
            }
        ],
    )

    print(f"created agent {agent.id} (name: {agent.name}, version: {agent.version})")
    print()
    print(f"set in .env.local:  ANTHROPIC_AGENT_ID={agent.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
