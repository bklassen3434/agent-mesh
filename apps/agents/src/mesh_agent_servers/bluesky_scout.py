"""Bluesky Scout A2A agent server entry point."""
from __future__ import annotations

import os

import uvicorn
from mesh_agents.bluesky_scout import BlueskyScoutAgent


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8009"))
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://bluesky-scout:{port}")

    agent = BlueskyScoutAgent()
    app = agent.to_a2a_server(url=public_url)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
