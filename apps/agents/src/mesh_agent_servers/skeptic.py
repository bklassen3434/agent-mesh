"""Skeptic A2A agent server entry point."""
from __future__ import annotations

import os

import uvicorn
from mesh_agents.skeptic import SkepticAgent
from mesh_llm import LLMProviderNotReadyError, make_llm_client
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": "skeptic"})


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8006"))
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://skeptic:{port}")

    llm = make_llm_client(agent_name="skeptic")
    try:
        llm.health_check()
    except LLMProviderNotReadyError as exc:
        raise SystemExit(f"LLM provider not ready: {exc}") from exc

    agent = SkepticAgent(llm=llm)
    app = agent.to_a2a_server(url=public_url)
    app.routes.append(Route("/healthz", endpoint=_healthz, methods=["GET"]))

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
