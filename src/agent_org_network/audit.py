import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.runtime import Answer

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AuditEntry:
    timestamp: datetime
    user_id: str
    question: str
    intent: str
    decision: RoutingDecision
    answer: Answer | None

    def to_jsonl(self) -> str:
        return json.dumps(self._as_record(), ensure_ascii=False)

    def _as_record(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "user_id": self.user_id,
            "question": self.question,
            "intent": self.intent,
            "decision": _decision_record(self.decision),
            "answer": _answer_record(self.answer),
        }


def _decision_record(d: RoutingDecision) -> dict[str, Any]:
    match d:
        case Routed():
            return {
                "disposition": "routed",
                "primary": d.primary.agent_id,
                "owner": d.primary.owner,
                "confidence": d.confidence,
                "reason": d.reason,
            }
        case Contested():
            return {
                "disposition": "contested",
                "candidates": [c.agent_id for c in d.candidates],
                "reason": d.reason,
            }
        case Unowned():
            return {
                "disposition": "unowned",
                "escalated_to": d.escalated_to,
                "reason": d.reason,
            }


def _answer_record(a: Answer | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {"text": a.text, "mode": a.mode, "sources": list(a.sources)}


class AuditLog(Protocol):
    def record(self, entry: AuditEntry) -> None: ...


class JsonlAuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path

    def record(self, entry: AuditEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry.to_jsonl() + "\n")


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)
