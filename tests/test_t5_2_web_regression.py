"""T5.2 code-reviewer 지적 수정 — web 호출자 경로(TestClient) 회귀 테스트.

헬퍼/서비스 직접 호출로는 가려지는 web 와이어링 버그를 TestClient 경로로 고정한다.

[Blocker 1] concur→Deadlocked 시 Manager 큐 미적재:
  web concur 엔드포인트가 Deadlocked 결과를 Manager 큐에 넣지 않는다.
  → 같은 "보상" 질문 후 concur로 표 갈림 → GET /manager/{id} 에 항목이 0건.

[Blocker 2] manager_act 가 precedents·case_store 없이 ManagerQueueService 생성:
  AssignOwner 처리 후 Precedent 미기록 → 같은 intent 재질문이 여전히 Pending(Contested).
  case_store 미주입 → FromDeadlock ConflictCase 미종결.

[Major 1] enqueue_deadlock 중복 적재 방지:
  같은 case로 두 번 enqueue_deadlock → 큐에 1건만 있어야 한다.
  현재 get_by_case 가드 없어 2건 적재.

결정론: FakeClassifier, StubRuntime, InMemory store, 고정 clock. 실 LLM·네트워크 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.conflict import (
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
)
from agent_org_network.dispatch import LocalRuntimeDispatcher
from agent_org_network.manager_queue import (
    FromDeadlock,
    InMemoryManagerQueueStore,
)
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User

_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
_CLOCK = lambda: _NOW  # noqa: E731
_DATE = date(2026, 6, 21)


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any


def _result(res: Response) -> HttpResult:
    body: Any = res.json()
    return HttpResult(status=res.status_code, body=body)


# ════════════════════════════════════════════════════════════════════════════
# [Blocker 1] — red: concur→Deadlocked → GET /manager/{id} 항목 0건
#
# 이 테스트는 현재 FAIL 이어야 한다(B1 미수정):
# concur 엔드포인트가 Deadlocked 결과를 enqueue_deadlock 으로 넘기지 않으므로
# GET /manager/root_manager 가 [] 를 돌려준다.
# ════════════════════════════════════════════════════════════════════════════


def _make_full_app() -> Any:
    """완전히 연결된 앱 — manager_queue_store 주입 후 web 경로만으로 검증한다.

    web.create_app 가 build_demo(manager_queue_store=...) 를 호출하므로 같은 큐 인스턴스가
    concur·manager_act·GET /manager/{id} 경로를 공유한다. 테스트는 HTTP 응답만으로 검증.
    """
    from agent_org_network.web import create_app

    queue_store = InMemoryManagerQueueStore()
    return create_app(runtime=StubRuntime(), manager_queue_store=queue_store)


class TestB1_Concur_Deadlocked_Manager_큐_적재:
    """[Blocker 1] web concur → Deadlocked → Manager 큐에 항목이 생겨야 한다.

    시나리오:
      1. POST /ask "보상 기준이 어떻게 되나요?" → Answered(co-grounded) + ConflictCase 개방(결정 5)
      2. POST /cases/{case_id}/concur by cs_lead on cs_ops
      3. POST /cases/{case_id}/concur by finance_lead on finance_ops (표 갈림 → Deadlocked)
      4. GET /manager/root_manager → 항목 1건 (Deadlocked from_deadlock 출처)

    현재 (미수정): concur 결과가 Deadlocked 여도 enqueue_deadlock 미호출 → 항목 0건 = red.
    """

    def test_concur_deadlocked_후_manager_큐에_항목이_생긴다(self) -> None:
        """[Blocker 1] red — Deadlocked 후 GET /manager/root_manager 에 항목이 뜬다."""
        app = _make_full_app()
        client = TestClient(app)

        # 1. "보상" 질문 → Contested (데모: cs_ops·finance_ops 가 "보상" 공유)
        r1 = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            "/ask", json={"question": "보상 기준이 어떻게 되나요?"}
        )))
        assert r1.status == 200
        # co-grounding 활성(ADR 0037 슬라이스 D) 이후 다툼 응답은 `answered`(답+합의 병행)지만
        # ConflictCase는 여전히 열려 아래 concur/Deadlocked/Manager 큐 흐름을 그대로 탄다(결정 5).
        assert r1.body["type"] == "answered", f"answered 여야 하는데 {r1.body['type']}"

        # 2. case_id 조회 — inbox API 사용
        # cs_lead 또는 finance_lead 처리함에서 case 조회
        r_inbox = _result(cast(Response, client.get("/inbox/cs_lead")))  # pyright: ignore[reportUnknownMemberType]
        assert r_inbox.status == 200
        cases: list[Any] = r_inbox.body
        assert len(cases) >= 1, "처리함에 case 가 없다"
        case_id: str = cases[0]["case_id"]

        # 3. cs_lead → cs_ops, finance_lead → finance_ops (표 갈림 → Deadlocked)
        r2 = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            f"/cases/{case_id}/concur",
            json={"by_owner": "cs_lead", "on_agent": "cs_ops"},
        )))
        assert r2.status == 200

        r3 = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            f"/cases/{case_id}/concur",
            json={"by_owner": "finance_lead", "on_agent": "finance_ops"},
        )))
        assert r3.status == 200
        assert r3.body["type"] == "deadlocked", (
            f"표 갈림이 deadlocked 여야 하는데 {r3.body['type']}"
        )

        # 4. [Blocker 1 핵심] Manager 큐에 항목이 생겨야 한다
        r4 = _result(cast(Response, client.get("/manager/root_manager")))  # pyright: ignore[reportUnknownMemberType]
        assert r4.status == 200
        items: list[Any] = r4.body
        assert len(items) >= 1, (
            "[Blocker 1 미수정] GET /manager/root_manager 가 [] — "
            "web concur 가 Deadlocked 를 Manager 큐에 적재하지 않음"
        )
        # 출처가 from_deadlock 이어야 한다
        assert items[0]["source"]["type"] == "from_deadlock", (
            f"source type 이 from_deadlock 이어야 하는데 {items[0]['source']['type']}"
        )

    def test_concur_agreed_는_manager_큐에_적재하지_않는다(self) -> None:
        """합의(Agreed)는 Manager 큐에 들어가지 않아야 한다 — 부작용 검증."""
        app = _make_full_app()
        client = TestClient(app)

        # "보상" 질문 → Contested
        client.post("/ask", json={"question": "보상 기준이 어떻게 되나요?"})  # pyright: ignore[reportUnknownMemberType]

        r_inbox = _result(cast(Response, client.get("/inbox/cs_lead")))  # pyright: ignore[reportUnknownMemberType]
        cases: list[Any] = r_inbox.body
        case_id: str = cases[0]["case_id"]

        # 둘 다 cs_ops 에 동의 → Agreed
        client.post(f"/cases/{case_id}/concur", json={"by_owner": "cs_lead", "on_agent": "cs_ops"})  # pyright: ignore[reportUnknownMemberType]
        r = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            f"/cases/{case_id}/concur",
            json={"by_owner": "finance_lead", "on_agent": "cs_ops"},
        )))
        assert r.body["type"] == "agreed"

        # Manager 큐는 비어 있어야 한다
        r_mgr = _result(cast(Response, client.get("/manager/root_manager")))  # pyright: ignore[reportUnknownMemberType]
        assert r_mgr.body == [], (
            "Agreed 인데 Manager 큐에 항목이 생겼다 — 부작용"
        )


class TestB2_ManagerAct_Precedent_연결:
    """[Blocker 2] web manager_act → AssignOwner → Precedent 기록 + 재질문 자동 Routed.

    시나리오:
      1. Deadlocked → Manager 큐 적재 (B1 수정 전제)
      2. POST /manager/items/{item_id}/act AssignOwner(primary=cs_ops)
      3. 같은 "보상" intent 재질문 → Answered (Precedent 자동 적용)

    현재 (미수정): ManagerQueueService 가 precedents·case_store 없이 생성 →
    AssignOwner 해도 Precedent 미기록 → 재질문 여전히 Pending(contested).
    """

    def test_AssignOwner_후_재질문이_Answered로_전환된다(self) -> None:
        """[Blocker 2] red — manager_act AssignOwner 후 같은 질문이 Answered 로 뜬다."""
        app = _make_full_app()
        client = TestClient(app)

        # B1 이 수정된 뒤에야 이 테스트가 유의미하므로,
        # B1 미수정이면 큐가 비어 item_id 를 못 가져와 건너뛴다.
        client.post("/ask", json={"question": "보상 기준이 어떻게 되나요?"})  # pyright: ignore[reportUnknownMemberType]

        r_inbox = _result(cast(Response, client.get("/inbox/cs_lead")))  # pyright: ignore[reportUnknownMemberType]
        cases: list[Any] = r_inbox.body
        assert len(cases) >= 1
        case_id: str = cases[0]["case_id"]

        # 표 갈림 → Deadlocked
        client.post(f"/cases/{case_id}/concur", json={"by_owner": "cs_lead", "on_agent": "cs_ops"})  # pyright: ignore[reportUnknownMemberType]
        r_dead = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            f"/cases/{case_id}/concur",
            json={"by_owner": "finance_lead", "on_agent": "finance_ops"},
        )))
        assert r_dead.body["type"] == "deadlocked"

        # Manager 큐 조회 (B1 수정 전제)
        r_mgr = _result(cast(Response, client.get("/manager/root_manager")))  # pyright: ignore[reportUnknownMemberType]
        items: list[Any] = r_mgr.body
        if not items:
            pytest.skip("[Blocker 1] 미수정으로 큐가 비어 B2 검증 불가 — B1 먼저 수정")
        item_id: str = items[0]["item_id"]

        # AssignOwner — cs_ops 지정
        r_act = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            f"/manager/items/{item_id}/act",
            json={
                "type": "assign_owner",
                "by_manager": "root_manager",
                "primary": "cs_ops",
                "rationale": "보상은 cs 팀",
            },
        )))
        assert r_act.status == 200
        assert r_act.body["status"] == "resolved"

        # [Blocker 2 핵심] 재질문 → Answered (Precedent 학습으로 자동 라우팅)
        r_re = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            "/ask", json={"question": "보상 기준이 어떻게 되나요?"}
        )))
        assert r_re.body["type"] == "answered", (
            f"[Blocker 2 미수정] 재질문 응답 type={r_re.body['type']} — "
            "AssignOwner 후 Precedent 가 기록되지 않아 재질문이 여전히 Pending. "
            "web manager_act 가 precedents·case_store 를 ManagerQueueService 에 미주입."
        )
        assert r_re.body["answered_by"]["agent_id"] == "cs_ops"

    def test_AssignOwner_후_ConflictCase_종결된다(self) -> None:
        """[Blocker 2] FromDeadlock AssignOwner → ConflictCase 종결 → 처리함서 사라짐."""
        app = _make_full_app()
        client = TestClient(app)

        client.post("/ask", json={"question": "보상 기준이 어떻게 되나요?"})  # pyright: ignore[reportUnknownMemberType]

        r_inbox = _result(cast(Response, client.get("/inbox/cs_lead")))  # pyright: ignore[reportUnknownMemberType]
        cases: list[Any] = r_inbox.body
        case_id: str = cases[0]["case_id"]

        client.post(f"/cases/{case_id}/concur", json={"by_owner": "cs_lead", "on_agent": "cs_ops"})  # pyright: ignore[reportUnknownMemberType]
        r_dead = _result(cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
            f"/cases/{case_id}/concur",
            json={"by_owner": "finance_lead", "on_agent": "finance_ops"},
        )))
        assert r_dead.body["type"] == "deadlocked"

        r_mgr = _result(cast(Response, client.get("/manager/root_manager")))  # pyright: ignore[reportUnknownMemberType]
        items: list[Any] = r_mgr.body
        if not items:
            pytest.skip("[Blocker 1] 미수정으로 큐가 비어 B2 검증 불가 — B1 먼저 수정")
        item_id: str = items[0]["item_id"]

        client.post(  # pyright: ignore[reportUnknownMemberType]
            f"/manager/items/{item_id}/act",
            json={"type": "assign_owner", "by_manager": "root_manager", "primary": "cs_ops"},
        )

        # [핵심] case 가 종결됐으므로 처리함에서 사라져야 한다
        r_inbox2 = _result(cast(Response, client.get("/inbox/cs_lead")))  # pyright: ignore[reportUnknownMemberType]
        remaining: list[Any] = r_inbox2.body
        assert not any(c["case_id"] == case_id for c in remaining), (
            f"[Blocker 2 미수정] case_id={case_id} 가 처리함에 여전히 있다 — "
            "case_store 미주입으로 ConflictCase 미종결"
        )


# ════════════════════════════════════════════════════════════════════════════
# [Major 1] enqueue_deadlock 중복 적재 방지
# ════════════════════════════════════════════════════════════════════════════


class TestM1_EnqueueDeadlock_중복_방지:
    """[Major 1] 같은 case 로 두 번 enqueue_deadlock → 큐에 1건만 있어야 한다."""

    def _make_ask_with_queue(self) -> tuple[AskOrg, InMemoryManagerQueueStore]:
        registry = Registry()
        root = User(id="root_manager")
        alice = User(id="alice", manager="root_manager")
        bob = User(id="bob", manager="root_manager")
        registry.register_user(root)
        registry.register_user(alice)
        registry.register_user(bob)
        card_a = AgentCard(
            agent_id="agent_a", owner="alice", team="t", summary="s",
            domains=["보상"], last_reviewed_at=_DATE,
        )
        card_b = AgentCard(
            agent_id="agent_b", owner="bob", team="t", summary="s",
            domains=["보상"], last_reviewed_at=_DATE,
        )
        registry.register(card_a)
        registry.register(card_b)
        registry.validate()

        classifier = FakeClassifier("보상")
        precedents = InMemoryPrecedentStore()
        case_store = InMemoryConflictCaseStore()
        router = Router(registry, classifier, root_user="root_manager", precedents=precedents)
        queue_store = InMemoryManagerQueueStore()

        def manager_of(uid: str) -> str | None:
            return registry.get_user(uid).manager if uid in registry.user_ids() else None

        ask = AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=InMemoryAuditLog(),
            clock=_CLOCK,
            case_store=case_store,
            manager_queue_store=queue_store,
            manager_of=manager_of,
            manager_root="root_manager",
        )
        return ask, queue_store

    def test_같은_case_두번_enqueue_deadlock_1건만_적재(self) -> None:
        """[Major 1] red — 같은 case 로 두 번 enqueue_deadlock 해도 큐에 1건."""
        from agent_org_network.conflict import (
            Candidate,
            ConflictCase,
        )

        ask, queue_store = self._make_ask_with_queue()

        case = ConflictCase(
            intent="보상",
            question="보상 기준은?",
            candidates=(
                Candidate(agent_id="agent_a", owner="alice"),
                Candidate(agent_id="agent_b", owner="bob"),
            ),
            opened_at=_NOW,
            case_id="case-dedup-001",
        )

        ask.enqueue_deadlock(case, reason="표 갈림")
        ask.enqueue_deadlock(case, reason="재시도")  # 같은 case_id — 무시되어야 함

        pending = queue_store.pending_for_manager("root_manager")
        assert len(pending) == 1, (
            f"[Major 1 미수정] 같은 case 두 번 enqueue_deadlock 했는데 {len(pending)}건 — "
            "get_by_case 가드 없어 중복 적재됨"
        )
        assert isinstance(pending[0].source, FromDeadlock)

    def test_다른_case는_각각_적재된다(self) -> None:
        """다른 case_id 면 각각 독립 적재 — 중복 방지가 과도하게 막지 않음."""
        from agent_org_network.conflict import (
            Candidate,
            ConflictCase,
        )

        ask, queue_store = self._make_ask_with_queue()

        case_a = ConflictCase(
            intent="보상",
            question="보상 기준은?",
            candidates=(Candidate(agent_id="agent_a", owner="alice"),),
            opened_at=_NOW,
            case_id="case-a",
        )
        case_b = ConflictCase(
            intent="보상",
            question="보상 얼마나?",
            candidates=(Candidate(agent_id="agent_b", owner="bob"),),
            opened_at=_NOW,
            case_id="case-b",
        )

        ask.enqueue_deadlock(case_a, reason="갈림1")
        ask.enqueue_deadlock(case_b, reason="갈림2")

        pending = queue_store.pending_for_manager("root_manager")
        assert len(pending) == 2, (
            f"다른 case 인데 {len(pending)}건 — 중복 방지가 과도하게 막음"
        )
