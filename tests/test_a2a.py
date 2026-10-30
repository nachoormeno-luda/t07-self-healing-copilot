"""A2A plumbing tests: client helper round-trip against a local echo agent, and the server's agent card."""

from __future__ import annotations

import asyncio
import importlib
import socket
import threading
import time

import pytest
import uvicorn
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.genai import types
from starlette.testclient import TestClient

from seed_packs.t07_self_healing_ops.a2a_client import send_a2a_text


class _EchoAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        text = ctx.user_content.parts[0].text
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part.from_text(text=f"echo:{text}")]),
        )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def echo_server():
    port = _free_port()
    app = to_a2a(_EchoAgent(name="echo"), host="127.0.0.1", port=port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn echo server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_a2a_round_trip_against_local_echo_agent(echo_server):
    reply = asyncio.run(send_a2a_text(echo_server, "ping from table 7", timeout_s=10))
    assert "echo:ping from table 7" in reply


def test_a2a_server_builds_app_and_serves_agent_card(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SEED_OFFLINE_MODE", "1")
    monkeypatch.setenv("A2A_PUBLIC_URL", "https://t07.example.run.app")
    monkeypatch.setenv("SEED_PACK", "t07")  # do not rely on the ambient environment
    import a2a_server

    importlib.reload(a2a_server)
    with TestClient(a2a_server.app) as client:
        card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "expedition_t07"
    # A2A v1.0 card schema (a2a-sdk 1.x): the RPC endpoint lives under supportedInterfaces.
    assert card["supportedInterfaces"][0]["url"].startswith("https://t07.example.run.app")
    assert card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"


def test_client_timeout_bounds_connect_separately():
    from seed_packs.t07_self_healing_ops.a2a_client import _client_timeout

    t = _client_timeout(90)
    assert t.connect == 5.0 and t.read == 90.0
    assert _client_timeout(3).connect == 3.0


def test_reasoning_engine_url_falls_back_to_a2a_when_cross_project_iam_denied(
    monkeypatch: pytest.MonkeyPatch, echo_server: str
):
    import seed_packs.t07_self_healing_ops.a2a_client as mod

    async def _denied(_resource: str, _text: str) -> str:
        raise PermissionError("403 PERMISSION_DENIED on expedition-dn26lon-5921")

    monkeypatch.setattr(mod, "_query_reasoning_engine", _denied)
    monkeypatch.setenv("T08_FALLBACK_A2A_URL", echo_server)
    re_url = (
        "https://europe-west1-aiplatform.googleapis.com/v1/"
        "projects/expedition-dn26lon-5921/locations/europe-west1/reasoningEngines/5598468017542397952:query"
    )
    reply = asyncio.run(send_a2a_text(re_url, "advisory for INC-0001", timeout_s=10))
    assert "echo:advisory for INC-0001" in reply

