"""Minimal A2A client helper: send one text message to an A2A agent and return its text reply.

Used by `scripts/a2a_ping.py` (what a sister Table copies to call us) and by the
`notify_travel_concierge` tool (what we use to call Table 8). Built on `a2a-sdk`
so protocol details (card resolution, transport, task vs. message replies) stay
in the SDK.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any
from uuid import uuid4

import httpx
from a2a.client import ClientConfig, ClientFactory
import a2a.types as a2a_types

_RE_ENGINE_RE = re.compile(r"(projects/[^/]+/locations/[^/]+/reasoningEngines/\d+)")
_DEFAULT_T08_A2A_FALLBACK = "https://t08-travel-concierge-816403371995.europe-west1.run.app"


def _client_timeout(timeout_s: float) -> httpx.Timeout:
    """Total budget `timeout_s`, but a blackholed endpoint must fail at connect within 5 s."""
    return httpx.Timeout(float(timeout_s), connect=min(5.0, float(timeout_s)))


def _unwrap(obj: Any) -> Any:
    """`StreamResponse` / `SendMessageResponse` carry their payload in a protobuf oneof (task | message)."""
    has_field = getattr(obj, "HasField", None)
    if callable(has_field):
        for field in ("task", "message"):
            try:
                if has_field(field):
                    return getattr(obj, field)
            except ValueError:  # object has no such oneof member
                continue
    return obj


def _texts_from(obj: Any) -> list[str]:
    """Collect text parts from an A2A Message or Task (status message + artifacts)."""
    obj = _unwrap(obj)
    texts: list[str] = []
    for part in getattr(obj, "parts", None) or []:
        if getattr(part, "text", ""):
            texts.append(part.text)
    status = getattr(obj, "status", None)
    status_msg = getattr(status, "message", None) if status is not None else None
    for part in getattr(status_msg, "parts", None) or []:
        if getattr(part, "text", ""):
            texts.append(part.text)
    for artifact in getattr(obj, "artifacts", None) or []:
        for part in getattr(artifact, "parts", None) or []:
            if getattr(part, "text", ""):
                texts.append(part.text)
    return texts


async def _query_reasoning_engine(resource_name: str, text: str) -> str:
    """Query a Vertex AI Reasoning Engine resource directly using ADC credentials."""
    from vertexai import agent_engines

    remote = agent_engines.get(resource_name)
    chunks: list[str] = []
    async for event in remote.async_stream_query(user_id="t07-sre-copilot", message=text):
        content = event.get("content") if isinstance(event, dict) else None
        for part in (content or {}).get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


async def send_a2a_text(
    agent_url: str,
    text: str,
    *,
    timeout_s: float = 30.0,
    httpx_client: httpx.AsyncClient | None = None,
) -> str:
    """Resolve the agent card at `agent_url`, send `text` as a user message, return the reply text.

    `agent_url` is the agent's base URL (the SDK appends `/.well-known/agent-card.json`), or a
    Vertex AI Reasoning Engine endpoint (`.../projects/.../locations/.../reasoningEngines/<id>:query`).
    Returns an empty string when the agent replied without any text part.
    """
    own_client = httpx_client is None
    http = httpx_client or httpx.AsyncClient(timeout=_client_timeout(timeout_s))
    try:
        async def _exchange_a2a(target_url: str) -> str:
            factory = ClientFactory(ClientConfig(streaming=False, httpx_client=http))
            client = await factory.create_from_url(target_url.rstrip("/"))
            request = a2a_types.SendMessageRequest(
                message=a2a_types.Message(
                    message_id=str(uuid4()),
                    role=a2a_types.Role.ROLE_USER,
                    parts=[a2a_types.Part(text=text)],
                )
            )
            collected: list[str] = []
            async for item in client.send_message(request):
                for obj in (item if isinstance(item, tuple) else (item,)):
                    for chunk in _texts_from(obj):
                        if chunk not in collected:
                            collected.append(chunk)
            return "\n".join(collected).strip()

        async def _exchange() -> str:
            re_match = _RE_ENGINE_RE.search(agent_url or "")
            if re_match:
                try:
                    return await _query_reasoning_engine(re_match.group(1), text)
                except Exception:
                    fallback = os.environ.get("T08_FALLBACK_A2A_URL", _DEFAULT_T08_A2A_FALLBACK).strip()
                    if fallback and not _RE_ENGINE_RE.search(fallback):
                        return await _exchange_a2a(fallback)
                    raise
            return await _exchange_a2a(agent_url)

        return await asyncio.wait_for(_exchange(), timeout=timeout_s)
    finally:
        if own_client:
            await http.aclose()
