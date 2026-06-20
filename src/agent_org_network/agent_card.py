from datetime import date

from pydantic import BaseModel


class AgentCard(BaseModel, frozen=True):
    agent_id: str
    owner: str
    team: str
    summary: str
    domains: list[str]
    last_reviewed_at: date
    maintainer: str | None = None
    can_answer: list[str] = []
    cannot_answer: list[str] = []
    approval_when: list[str] = []
    collaborate_when: list[str] = []
    knowledge_sources: list[str] = []
    trust_labels: list[str] = []
