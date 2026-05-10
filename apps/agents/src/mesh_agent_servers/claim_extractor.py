"""Claim Extractor A2A agent server entry point."""
from __future__ import annotations

import os

import uvicorn
from mesh_agents.claim_extractor import ClaimExtractorAgent
from mesh_llm.client import OllamaClient, OllamaNotReadyError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def _healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "agent": "claim_extractor"})


def main() -> None:
    host = os.environ.get("AGENT_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_PORT", "8002"))
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://claim-extractor:{port}")

    llm = OllamaClient()
    try:
        llm.health_check()
    except OllamaNotReadyError as exc:
        raise SystemExit(f"Ollama not ready: {exc}") from exc

    agent = ClaimExtractorAgent(llm=llm)
    app = agent.to_a2a_server(url=public_url)
    app.routes.append(Route("/healthz", endpoint=_healthz, methods=["GET"]))

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
