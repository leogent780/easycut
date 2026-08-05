"""Thin wrapper around the Anthropic Claude API.

Structured JSON output is implemented via forced tool-use (tool_choice pinned to a single
tool whose input_schema is the caller's desired JSON schema) rather than a speculative
"native structured output" parameter — tool-use is the long-stable, well-documented
mechanism for guaranteeing schema-conforming JSON back from the model.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

MODEL_SONNET = "claude-sonnet-5"
MODEL_HAIKU = "claude-haiku-4-5-20251001"


@lru_cache(maxsize=1)
def get_client():
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
    return anthropic.Anthropic(api_key=api_key)


def call_structured(
    system_prompt: str,
    user_content: str,
    schema: dict[str, Any],
    tool_name: str,
    model: str = MODEL_SONNET,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Call Claude and force it to return data matching `schema` via a pinned tool call."""
    client = get_client()
    tool = {
        "name": tool_name,
        "description": "Return the result as data matching this schema. Do not explain — call this tool with your answer.",
        "input_schema": schema,
    }
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    raise RuntimeError(f"Claude did not return a tool_use block for tool '{tool_name}'")


def call_text(system_prompt: str, user_content: str, model: str = MODEL_HAIKU, max_tokens: int = 2048) -> str:
    """Plain text completion — for cases where strict JSON isn't needed (e.g. quick translation)."""
    client = get_client()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in response.content if block.type == "text")
