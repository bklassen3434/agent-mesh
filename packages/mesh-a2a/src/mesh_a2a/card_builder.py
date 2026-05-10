"""Pure helper for constructing A2A AgentCard objects."""
from __future__ import annotations

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol


def build_agent_card(
    *,
    name: str,
    description: str,
    url: str,
    skill_id: str,
    skill_name: str,
    skill_description: str,
    skill_tags: list[str] | None = None,
    version: str = "0.1.0",
) -> AgentCard:
    """Build an AgentCard for a single-skill mesh agent.

    No auth is declared in Phase 2.
    # TODO(phase-6): auth — add securitySchemes here and a matching
    # SecurityRequirement on the skill once we need bearer-token or API-key
    # protection between coordinator and agents.
    """
    skill = AgentSkill(
        id=skill_id,
        name=skill_name,
        description=skill_description,
        tags=skill_tags or [],
    )
    return AgentCard(
        name=name,
        description=description,
        version=version,
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extended_agent_card=False,
        ),
        supported_interfaces=[
            AgentInterface(
                protocol_binding=TransportProtocol.JSONRPC,
                url=url,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        skills=[skill],
    )
