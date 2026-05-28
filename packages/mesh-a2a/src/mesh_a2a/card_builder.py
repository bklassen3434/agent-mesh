"""Pure helper for constructing A2A AgentCard objects."""
from __future__ import annotations

from dataclasses import dataclass

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol


@dataclass(frozen=True)
class SkillSpec:
    """Phase 7a: lightweight value type for multi-skill agents.

    Used by ``build_multi_skill_card`` to declare each skill once. The
    older ``build_agent_card`` stays for the single-skill agents (most
    of the roster — only the scouts grow a second ``investigate`` skill
    in Phase 7a).
    """

    id: str
    name: str
    description: str
    tags: list[str] | None = None


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
    return build_multi_skill_card(
        name=name,
        description=description,
        url=url,
        skills=[
            SkillSpec(
                id=skill_id,
                name=skill_name,
                description=skill_description,
                tags=skill_tags,
            )
        ],
        version=version,
    )


def build_multi_skill_card(
    *,
    name: str,
    description: str,
    url: str,
    skills: list[SkillSpec],
    version: str = "0.1.0",
) -> AgentCard:
    """Build an AgentCard advertising multiple skills on a single agent."""
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
        skills=[
            AgentSkill(
                id=s.id,
                name=s.name,
                description=s.description,
                tags=s.tags or [],
            )
            for s in skills
        ],
    )
