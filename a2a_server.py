"""Expose the Table 7 copilot over the A2A protocol (Cloud Run or any host).

Run locally:   python a2a_server.py            (card at http://localhost:8080/.well-known/agent-card.json)
On Cloud Run:  set A2A_PUBLIC_URL to the service URL so the advertised card URL is reachable.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from starlette.applications import Starlette

from app.agent import build_agent
from app.loader import REPO_ROOT

load_dotenv(REPO_ROOT / ".env")

PORT = int(os.environ.get("PORT", "8080"))


def build_app() -> Starlette:
    """Build the A2A Starlette app around the active Seed Pack agent (plain function tools)."""
    public_url = os.environ.get("A2A_PUBLIC_URL", "").strip()
    if public_url:
        parsed = urlparse(public_url)
        protocol = parsed.scheme or "https"
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if protocol == "https" else 80)
    else:
        protocol, host, port = "http", "localhost", PORT
    agent, _pack = build_agent(enable_mcp_stdio=False)
    return to_a2a(agent, host=host, port=port, protocol=protocol)


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
