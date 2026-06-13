"""Web Search Scout A2A agent server entry point."""
from __future__ import annotations

import os

import uvicorn
from mesh_agents.web_search_scout import WebSearchScoutAgent


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8013"))
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://web-search-scout:{port}")

    agent = WebSearchScoutAgent()
    app = agent.to_a2a_server(url=public_url)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
