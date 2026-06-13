"""REST/JSON Scout A2A agent server entry point."""
from __future__ import annotations

import os

import uvicorn
from mesh_agents.rest_json_scout import RestJsonScoutAgent


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8015"))
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://rest-json-scout:{port}")

    agent = RestJsonScoutAgent()
    app = agent.to_a2a_server(url=public_url)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
