"""Leaderboard Scout A2A agent server entry point."""
from __future__ import annotations

import os

import uvicorn
from mesh_agents.leaderboard_scout import LeaderboardScoutAgent


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8012"))
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://leaderboard-scout:{port}")

    agent = LeaderboardScoutAgent()
    app = agent.to_a2a_server(url=public_url)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
