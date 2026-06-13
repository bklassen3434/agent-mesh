"""ResearchQA A2A agent server entry point (Phase 21)."""
from __future__ import annotations

import os

import uvicorn
from mesh_agents.research_qa import ResearchQAAgent
from mesh_llm import LLMProviderNotReadyError, make_routed_llm_client


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8016"))
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://research-qa:{port}")

    # Synthesis benefits from the strong tier on hard questions; routing (when
    # enabled) escalates only when a difficulty signal fires.
    llm = make_routed_llm_client(agent_name="research_qa")
    try:
        llm.health_check()
    except LLMProviderNotReadyError as exc:
        raise SystemExit(f"LLM provider not ready: {exc}") from exc

    agent = ResearchQAAgent(llm=llm)
    app = agent.to_a2a_server(url=public_url)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
