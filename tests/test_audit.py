from datetime import date, datetime, timezone
from pathlib import Path

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg
from agent_org_network.audit import InMemoryAuditLog, JsonlAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.decision import Routed
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User

import json


_FIXED_DT = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _FIXED_DT


def _card(agent_id: str, domains: list[str], knowledge_sources: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="D",
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=knowledge_sources or [],
    )


def _ask_org_with(
    cards: list[AgentCard],
    intent: str,
    audit_log: InMemoryAuditLog | JsonlAuditLog,
) -> AskOrg:
    registry = Registry()
    for c in cards:
        registry.register(c)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root")
    return AskOrg(
        router=router,
        runtime=StubRuntime(),
        audit_log=audit_log,
        classifier=classifier,
        clock=_fixed_clock,
    )


def test_Routed_처리가_내부값까지_JSONL_한줄로_기록된다(tmp_path: Path) -> None:
    sources = ["위키/계약가이드", "Notion/FAQ"]
    c = _card("contract_ops", ["계약 검토"], knowledge_sources=sources)
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    ask = _ask_org_with([c], "계약 검토", audit)
    user = User(id="u1")

    ask.handle("이 계약 조건 바꿔도 돼?", user)

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["timestamp"] == _FIXED_DT.isoformat()
    assert record["user_id"] == "u1"
    assert record["intent"] == "계약 검토"
    assert record["decision"]["disposition"] == "routed"
    assert record["decision"]["primary"] == "contract_ops"
    assert record["decision"]["confidence"] == 1.0
    assert record["answer"]["mode"] == "full"


def test_Unowned_처리가_escalated_to까지_기록된다(tmp_path: Path) -> None:
    c = _card("contract_ops", ["계약 검토"])
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    ask = _ask_org_with([c], "주차장", audit)
    user = User(id="u2")

    ask.handle("주차장 정기권 어떻게 갱신해요?", user)

    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])

    assert record["decision"]["disposition"] == "unowned"
    assert record["decision"]["escalated_to"] == "root"
    assert record["answer"] is None


def test_InMemoryAuditLog_handle_1회에_entries_1개_decision_원형보존() -> None:
    sources = ["위키/계약가이드"]
    c = _card("contract_ops", ["계약 검토"], knowledge_sources=sources)
    audit = InMemoryAuditLog()
    ask = _ask_org_with([c], "계약 검토", audit)
    user = User(id="u3")

    ask.handle("계약 리뷰 부탁해요", user)

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert isinstance(entry.decision, Routed)
    assert entry.decision.primary.agent_id == "contract_ops"
    assert entry.timestamp == _FIXED_DT
