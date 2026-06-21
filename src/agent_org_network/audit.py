import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, assert_never

from agent_org_network.decision import Contested, RoutingDecision, Routed, Unowned
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    DispatchOutcome,
    EscalatedToManager,
)
from agent_org_network.runtime import Answer

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AuditEntry:
    """Audit log의 한 줄 — 한 질문 처리의 두 절차를 *내부값까지* 기록한다.

    (1) 라우팅 → `decision`(RoutingDecision 원형). (2) 디스패치 →
    `dispatch_outcome`(DispatchOutcome 원형, Routed일 때만; Contested/Unowned는
    디스패치를 안 하므로 `None`). escalation(`EscalatedToManager`)의 `manager_id`·
    `reason`은 사용자向 `Pending`에선 떨궈지지만 여기선 *전부* 남는다 — `Unowned`가
    `escalated_to`를 남기는 것과 대칭(둘 다 "escalation 대상"). audit는 노출 불변식과
    무관하다: 내부값을 *기록하는 게* 목적이다(ADR 0011, T6.3 2b 선결).

    `answer`는 별도 생성자 필드가 아니라 `dispatch_outcome`에서 유도하는 파생
    프로퍼티다(`Delivered.answer`만 답을 가짐) — 같은 답을 두 곳에 두지 않기 위함
    (SSOT는 `dispatch_outcome`). 기존 호출처/직렬화의 `answer` 접근은 그대로 산다.
    """

    timestamp: datetime
    user_id: str
    question: str
    intent: str
    decision: RoutingDecision
    dispatch_outcome: DispatchOutcome | None = None

    @property
    def answer(self) -> Answer | None:
        """디스패치 결말에서 유도한 답 — `Delivered`면 그 `answer`, 아니면 `None`.

        하위호환 접근자(중복 저장 회피). 미회신·escalation엔 답이 없으니 `None`.
        """
        if isinstance(self.dispatch_outcome, Delivered):
            return self.dispatch_outcome.answer
        return None

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
            "dispatch": _dispatch_record(self.dispatch_outcome),
        }


def _decision_record(d: RoutingDecision) -> dict[str, Any]:
    match d:
        case Routed():
            # decision 원형 보존(audit 계약): T2.5 Approval·Collaborator도 내부값까지
            # 남긴다(노출 불변식과 무관 — audit는 내부값 기록이 목적). collaborators는
            # 식별자(agent_id)만(카드 출처는 Registry — Contested.candidates와 같은 정신).
            return {
                "disposition": "routed",
                "primary": d.primary.agent_id,
                "owner": d.primary.owner,
                "confidence": d.confidence,
                "reason": d.reason,
                "requires_approval": d.requires_approval,
                "collaborators": [c.agent_id for c in d.collaborators],
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


def _dispatch_record(o: DispatchOutcome | None) -> dict[str, Any] | None:
    """DispatchOutcome을 JSONL 레코드로 — escalation 대상까지 *전부* 기록한다.

    Contested/Unowned(디스패치 없음)면 `None`. escalation 키는 `Unowned`의
    `escalated_to`와 *통일성*을 둔다: `EscalatedToManager`의 `manager_id`도
    "escalation 대상" 개념이므로 같은 결을 갖는 `disposition`+`escalated_to`(=manager_id)
    +`reason`으로 직렬화해, audit 독자가 두 escalation을 같은 모양으로 읽게 한다.
    `AwaitingWorker`는 대기라 `waited`(초)를 남긴다. `Delivered`의 답 본문은
    상위 `answer` 키가 이미 담으므로 여기선 처분 라벨만(중복 회피).
    match+assert_never로 DispatchOutcome 망라.

    NotImplementedError 없음 — 직렬화 분기는 시그니처가 곧 동작이라 여기서 확정한다.
    """
    if o is None:
        return None
    match o:
        case Delivered():
            return {"disposition": "delivered"}
        case AwaitingWorker():
            return {
                "disposition": "awaiting_worker",
                "waited_seconds": o.waited.total_seconds(),
            }
        case EscalatedToManager():
            return {
                "disposition": "escalated_to_manager",
                "escalated_to": o.manager_id,
                "reason": o.reason,
            }
        case _ as never:
            assert_never(never)


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
