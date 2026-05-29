"""
Drive a Claude Managed Agent session end-to-end.

Creates a session against your self-hosted environment, sends one prompt, and
streams the agent's work (thinking, tool calls, tool results, messages) to your
terminal until the session goes idle. The orchestrator (poll / webhook / cron /
webhook-in-sandbox) must already be running for the work to be picked up.

    uv run python src/drive_session.py "create hello.txt with 'hi' then read it back"
    make session PROMPT="..."

Reuse an existing session for a multi-turn conversation:

    uv run python src/drive_session.py --session ses_... "now delete it"
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import anthropic

from config import load_env


load_env()

# Dim/bold ANSI helpers — fall back to plain text when not a TTY.
_TTY = sys.stdout.isatty()


def _dim(text: str) -> str:
    return f"\033[2m{text}\033[0m" if _TTY else text


def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _TTY else text


def _text_of(content: Any) -> str:
    """Best-effort extraction of human-readable text from an event's content.

    Content may be a string, a list of text/other blocks, or a single block.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_text_of(block) for block in content]
        return "".join(p for p in parts if p)
    text = getattr(content, "text", None)
    if text is not None:
        return text
    block_type = getattr(content, "type", None)
    return f"[{block_type}]" if block_type else str(content)


def _truncate(text: str, limit: int = 500) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit] + _dim(f" … (+{len(text) - limit} chars)")


def _render(event: Any) -> bool:
    """Print one streamed event. Return True when the session has finished."""
    etype = getattr(event, "type", None)

    if etype == "session.status_running":
        print(_dim("· running"))
    elif etype == "agent.thinking":
        print(_dim("· thinking"))
    elif etype == "agent.tool_use":
        name = getattr(event, "name", "?")
        tool_input = getattr(event, "input", None)
        summary = ""
        if isinstance(tool_input, dict):
            # Surface the most useful field for the common tools.
            for key in ("command", "path", "file_path", "pattern", "query", "url"):
                if key in tool_input:
                    summary = str(tool_input[key])
                    break
            else:
                summary = ", ".join(f"{k}={v!r}" for k, v in tool_input.items())
        print(_bold(f"→ {name}") + (f"  {_truncate(summary, 200)}" if summary else ""))
    elif etype == "agent.tool_result":
        body = _truncate(_text_of(getattr(event, "content", None)))
        marker = "✗" if getattr(event, "is_error", False) else "←"
        if body:
            print(_dim(f"{marker} {body}"))
        else:
            print(_dim(f"{marker} (no output)"))
    elif etype == "agent.message":
        body = _text_of(getattr(event, "content", None)).rstrip()
        if body:
            print("\n" + body + "\n")
    elif etype == "session.error":
        print(_bold("session error:"), getattr(event, "error", event), file=sys.stderr)
        return True
    elif etype == "session.status_terminated":
        print(_dim("· session terminated"))
        return True
    elif etype == "session.status_idle":
        stop = getattr(event, "stop_reason", None)
        if getattr(stop, "type", None) == "requires_action":
            # Not the end: the in-sandbox worker still has to execute the
            # pending tool and submit its result, after which the session
            # resumes on this same stream. Keep observing instead of bailing
            # out — bailing here is what made the session look "stuck".
            print(_dim("· requires_action (worker executing tools…)"))
            return False
        print(_dim(f"· done (stop_reason={stop})" if stop else "· done"))
        return True
    # Other span/thread/lifecycle events are intentionally not printed.
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive a Managed Agent session.")
    parser.add_argument("prompt", nargs="?", help="the user message to send")
    parser.add_argument("--prompt", dest="prompt_opt", help="alternative to positional")
    parser.add_argument(
        "--session", help="reuse an existing session id instead of creating one"
    )
    parser.add_argument(
        "--agent", default=os.environ.get("ANTHROPIC_AGENT_ID"), help="agent id"
    )
    parser.add_argument(
        "--environment",
        default=os.environ.get("ANTHROPIC_ENVIRONMENT_ID"),
        help="self-hosted environment id",
    )
    args = parser.parse_args()

    prompt = args.prompt or args.prompt_opt
    if not prompt:
        print("missing prompt (positional or --prompt)", file=sys.stderr)
        return 2

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("missing ANTHROPIC_API_KEY (set it in .env.local)", file=sys.stderr)
        return 1
    if not args.session and not args.agent:
        print("missing ANTHROPIC_AGENT_ID (set it in .env.local)", file=sys.stderr)
        return 1
    if not args.environment:
        print("missing ANTHROPIC_ENVIRONMENT_ID (set it in .env)", file=sys.stderr)
        return 1

    client = anthropic.Anthropic(api_key=api_key)

    if args.session:
        session_id = args.session
        print(_dim(f"reusing session {session_id}"))
    else:
        session = client.beta.sessions.create(
            agent=args.agent, environment_id=args.environment
        )
        session_id = session.id
        print(_bold(f"session {session_id}") + _dim(f"  (env {args.environment})"))

    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    if workspace:
        print(
            _dim(
                f"console: https://platform.claude.com/workspaces/{workspace}"
                f"/managed-agents/sessions/{session_id}"
            )
        )
    print(_dim(f"> {prompt}"))

    try:
        with client.beta.sessions.events.stream(session_id) as stream:
            client.beta.sessions.events.send(
                session_id,
                events=[
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": prompt}],
                    }
                ],
            )
            for event in stream:
                if _render(event):
                    break
    except KeyboardInterrupt:
        print(_dim("\ninterrupted — session keeps running; reattach with "
                   f"--session {session_id}"))
        return 130

    print(_dim(f"\nreattach: uv run python src/drive_session.py --session {session_id} \"...\""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
