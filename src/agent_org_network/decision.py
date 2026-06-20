from dataclasses import dataclass

from agent_org_network.agent_card import AgentCard


@dataclass(frozen=True)
class Routed:
    primary: AgentCard
    confidence: float = 1.0
    reason: str = ""


@dataclass(frozen=True)
class Contested:
    candidates: tuple[AgentCard, ...]
    reason: str = ""


@dataclass(frozen=True)
class Unowned:
    escalated_to: str
    reason: str = ""


RoutingDecision = Routed | Contested | Unowned
