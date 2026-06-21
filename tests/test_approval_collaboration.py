"""T2.5 Approval·Collaboration 게이트 — red→green 테스트.

결정론 보장: FakeClassifier(고정 intent) + StubRuntime + InMemoryAuditLog.
실제 LLM 없음. 카드는 테스트에서 직접 생성(demo 카드는 approval_when/collaborate_when 비어있음).
"""

from datetime import date, datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.ask_org import AskOrg, Answered, Pending
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.classifier import FakeClassifier
from agent_org_network.conflict import InMemoryPrecedentStore, Resolution
from agent_org_network.decision import Routed
from agent_org_network.dispatch import LocalRuntimeDispatcher
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User


def _fixed_clock() -> datetime:
    return datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _card(
    agent_id: str,
    domains: list[str],
    owner: str = "O",
    approval_when: list[str] | None = None,
    collaborate_when: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary="요약",
        domains=domains,
        last_reviewed_at=date(2026, 6, 20),
        approval_when=approval_when or [],
        collaborate_when=collaborate_when or [],
    )


def _router_with(
    cards: list[AgentCard],
    intent: str,
    precedents: InMemoryPrecedentStore | None = None,
) -> Router:
    registry = Registry()
    for c in cards:
        registry.register(c)
    return Router(registry, FakeClassifier(intent), root_user="root", precedents=precedents)


def _ask_org_with(
    cards: list[AgentCard],
    intent: str,
    precedents: InMemoryPrecedentStore | None = None,
) -> AskOrg:
    registry = Registry()
    for c in cards:
        registry.register(c)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root", precedents=precedents)
    return AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        classifier=classifier,
        clock=_fixed_clock,
    )


def _ask_org_with_precedent_and_collab(
    primary: AgentCard,
    collab: AgentCard,
    intent: str,
) -> tuple[AskOrg, InMemoryAuditLog]:
    """판례 경로를 통해 단일 primary로 Routed + collab 조합을 만드는 헬퍼.

    primary·collab이 동일 domain을 가지면 candidates 2건 → Contested가 됩니다.
    판례로 primary를 고정하면 _attach_gates 경로에서 collaborators로 collab을 붙일 수 있습니다.
    """
    precedents = InMemoryPrecedentStore(clock=_fixed_clock)
    precedents.record(Resolution(intent=intent, primary=primary.agent_id))
    registry = Registry()
    registry.register(primary)
    registry.register(collab)
    classifier = FakeClassifier(intent)
    router = Router(registry, classifier, root_user="root", precedents=precedents)
    audit = InMemoryAuditLog()
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=audit,
        classifier=classifier,
        clock=_fixed_clock,
    )
    return ask, audit


# ── _collaborators_for ─────────────────────────────────────────────────────────


def test_collaborators_for_intent_매칭_primary_제외():
    """intent를 domains에 가진 primary 외 카드들만 반환한다."""
    primary = _card("contract_ops", ["계약 검토"])
    collab = _card("legal_ops", ["계약 검토"])
    other = _card("sales_ops", ["영업"])

    router = _router_with([primary, collab, other], "계약 검토")
    result = router._collaborators_for("계약 검토", primary)  # pyright: ignore[reportPrivateUsage]

    assert len(result) == 1
    assert result[0].agent_id == "legal_ops"


def test_collaborators_for_primary_는_제외된다():
    """primary 자신은 collaborator 목록에 포함되지 않는다."""
    primary = _card("contract_ops", ["계약 검토"])

    router = _router_with([primary], "계약 검토")
    result = router._collaborators_for("계약 검토", primary)  # pyright: ignore[reportPrivateUsage]

    assert result == ()


def test_collaborators_for_0매칭이면_빈_튜플():
    """매칭 카드가 없으면 빈 튜플."""
    primary = _card("contract_ops", ["계약 검토"])

    router = _router_with([primary], "계약 검토")
    result = router._collaborators_for("영업", primary)  # pyright: ignore[reportPrivateUsage]

    assert result == ()


def test_collaborators_for_결정론_정렬_agent_id():
    """결과는 agent_id 기준 오름차순으로 정렬된다(결정론 보장)."""
    primary = _card("a_ops", ["계약 검토"])
    c1 = _card("z_ops", ["계약 검토"])
    c2 = _card("b_ops", ["계약 검토"])

    router = _router_with([primary, c1, c2], "계약 검토")
    result = router._collaborators_for("계약 검토", primary)  # pyright: ignore[reportPrivateUsage]

    assert [c.agent_id for c in result] == ["b_ops", "z_ops"]


def test_collaborators_for_여러_매칭_모두_반환():
    """primary 제외 매칭 카드가 여럿이면 모두 반환한다."""
    primary = _card("primary_ops", ["법무"])
    c1 = _card("legal_ops", ["법무"])
    c2 = _card("compliance_ops", ["법무"])

    router = _router_with([primary, c1, c2], "법무")
    result = router._collaborators_for("법무", primary)  # pyright: ignore[reportPrivateUsage]

    agent_ids = {c.agent_id for c in result}
    assert agent_ids == {"legal_ops", "compliance_ops"}


# ── _attach_gates ──────────────────────────────────────────────────────────────


def test_attach_gates_approval_when_매칭이면_requires_approval_True():
    """primary.approval_when에 intent가 들면 requires_approval=True."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    router = _router_with([primary], "계약 검토")

    routed = Routed(primary=primary, reason="테스트")
    result = router._attach_gates(routed, "계약 검토")  # pyright: ignore[reportPrivateUsage]

    assert result.requires_approval is True


def test_attach_gates_approval_when_미매칭이면_requires_approval_False():
    """approval_when에 intent가 없으면 requires_approval=False."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["다른 intent"])
    router = _router_with([primary], "계약 검토")

    routed = Routed(primary=primary, reason="테스트")
    result = router._attach_gates(routed, "계약 검토")  # pyright: ignore[reportPrivateUsage]

    assert result.requires_approval is False


def test_attach_gates_collaborate_when_매칭이면_collaborators_부착():
    """primary.collaborate_when에 intent가 들면 collaborators가 채워진다."""
    primary = _card("contract_ops", ["계약 검토"], collaborate_when=["계약 검토"])
    collab = _card("legal_ops", ["계약 검토"])

    router = _router_with([primary, collab], "계약 검토")
    routed = Routed(primary=primary, reason="테스트")
    result = router._attach_gates(routed, "계약 검토")  # pyright: ignore[reportPrivateUsage]

    assert len(result.collaborators) == 1
    assert result.collaborators[0].agent_id == "legal_ops"


def test_attach_gates_collaborate_when_미매칭이면_collaborators_빈():
    """collaborate_when에 intent가 없으면 collaborators는 빈 튜플."""
    primary = _card("contract_ops", ["계약 검토"], collaborate_when=["다른 intent"])
    collab = _card("legal_ops", ["계약 검토"])

    router = _router_with([primary, collab], "계약 검토")
    routed = Routed(primary=primary, reason="테스트")
    result = router._attach_gates(routed, "계약 검토")  # pyright: ignore[reportPrivateUsage]

    assert result.collaborators == ()


def test_attach_gates_approval_and_collaborate_동시_부착():
    """approval_when·collaborate_when 둘 다 매칭이면 둘 다 부착된다(독립 축)."""
    primary = _card(
        "contract_ops",
        ["계약 검토"],
        approval_when=["계약 검토"],
        collaborate_when=["계약 검토"],
    )
    collab = _card("legal_ops", ["계약 검토"])

    router = _router_with([primary, collab], "계약 검토")
    routed = Routed(primary=primary, reason="테스트")
    result = router._attach_gates(routed, "계약 검토")  # pyright: ignore[reportPrivateUsage]

    assert result.requires_approval is True
    assert len(result.collaborators) == 1
    assert result.collaborators[0].agent_id == "legal_ops"


def test_attach_gates_둘_다_미매칭이면_routed_그대로():
    """approval_when·collaborate_when 둘 다 미매칭이면 routed를 그대로 반환."""
    primary = _card("contract_ops", ["계약 검토"])
    router = _router_with([primary], "계약 검토")

    routed = Routed(primary=primary, reason="테스트")
    result = router._attach_gates(routed, "계약 검토")  # pyright: ignore[reportPrivateUsage]

    assert result.requires_approval is False
    assert result.collaborators == ()
    assert result is routed  # 변경 없으면 동일 인스턴스 그대로


def test_attach_gates_단일_매칭_경로에서_부착():
    """route() 단일 매칭 경로를 통해도 _attach_gates가 적용된다."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    router = _router_with([primary], "계약 검토")

    decision = router.route("계약 검토해줘")

    assert isinstance(decision, Routed)
    assert decision.requires_approval is True


def test_attach_gates_판례_경로에서_부착():
    """route() 판례 경로를 통해도 _attach_gates가 적용된다."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    precedents = InMemoryPrecedentStore(clock=_fixed_clock)
    precedents.record(Resolution(intent="계약 검토", primary="contract_ops"))

    router = _router_with([primary], "계약 검토", precedents=precedents)
    decision = router.route("계약 검토해줘")

    assert isinstance(decision, Routed)
    assert decision.requires_approval is True


# ── _apply_approval_gate ───────────────────────────────────────────────────────


def test_apply_approval_gate_full이면_draft_only로_격상():
    """requires_approval=True + Answered(mode='full') → mode='draft_only'."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    router = _router_with([primary], "계약 검토")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        classifier=FakeClassifier("계약 검토"),
        clock=_fixed_clock,
    )

    routed = Routed(primary=primary, requires_approval=True)
    reply = Answered(text="답", answered_by=("O", "contract_ops"), mode="full")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(result, Answered)
    assert result.mode == "draft_only"
    assert result.text == "답"  # text 보존


def test_apply_approval_gate_backup은_보존():
    """requires_approval=True + Answered(mode='backup') → mode='backup' 그대로(더 강한 하향)."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    router = _router_with([primary], "계약 검토")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        classifier=FakeClassifier("계약 검토"),
        clock=_fixed_clock,
    )

    routed = Routed(primary=primary, requires_approval=True)
    reply = Answered(text="백업 답", answered_by=("O", "contract_ops"), mode="backup")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(result, Answered)
    assert result.mode == "backup"  # draft_only로 덮지 않는다


def test_apply_approval_gate_draft_only는_그대로():
    """requires_approval=True + Answered(mode='draft_only') → 그대로."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    router = _router_with([primary], "계약 검토")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        classifier=FakeClassifier("계약 검토"),
        clock=_fixed_clock,
    )

    routed = Routed(primary=primary, requires_approval=True)
    reply = Answered(text="답", answered_by=("O", "contract_ops"), mode="draft_only")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(result, Answered)
    assert result.mode == "draft_only"


def test_apply_approval_gate_requires_approval_False이면_그대로():
    """requires_approval=False이면 reply를 그대로 반환한다."""
    primary = _card("contract_ops", ["계약 검토"])
    router = _router_with([primary], "계약 검토")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        classifier=FakeClassifier("계약 검토"),
        clock=_fixed_clock,
    )

    routed = Routed(primary=primary, requires_approval=False)
    reply = Answered(text="답", answered_by=("O", "contract_ops"), mode="full")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert result is reply  # 동일 인스턴스 그대로


def test_apply_approval_gate_Pending이면_그대로():
    """Pending(dispatched)에는 approval gate가 적용되지 않는다(답 자체가 없음)."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    router = _router_with([primary], "계약 검토")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=InMemoryAuditLog(),
        classifier=FakeClassifier("계약 검토"),
        clock=_fixed_clock,
    )

    routed = Routed(primary=primary, requires_approval=True)
    reply = Pending(kind="dispatched", message="처리 중")
    result = ask._apply_approval_gate(reply, routed)  # pyright: ignore[reportPrivateUsage]

    assert result is reply  # Pending은 그대로


# ── 노출 불변식 ────────────────────────────────────────────────────────────────


def test_approval_gate_handle_경유시_mode_draft_only_노출():
    """approval_when 카드로 handle → Answered(mode='draft_only')가 사용자에게 노출된다."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    ask = _ask_org_with([primary], "계약 검토")
    user = User(id="u1")

    reply = ask.handle("계약서 검토해줘", user)

    assert isinstance(reply, Answered)
    assert reply.mode == "draft_only"


def test_collaborators_Answered에_미노출():
    """collaborators는 사용자向 Answered에 싣지 않는다(노출 불변식).

    판례 경로: primary가 판례로 단일 Routed 고정, collab은 같은 domain → collaborator로 부착.
    """
    primary = _card("contract_ops", ["계약 검토"], collaborate_when=["계약 검토"])
    collab = _card("legal_ops", ["계약 검토"])

    ask, _ = _ask_org_with_precedent_and_collab(primary, collab, "계약 검토")
    user = User(id="u1")

    reply = ask.handle("계약서 검토해줘", user)

    assert isinstance(reply, Answered)
    assert not hasattr(reply, "collaborators")


def test_serialize_reply에_collaborators_미노출():
    """serialize_reply(Answered)에 collaborators 키가 없다(_LEAKY_KEYS 정신)."""
    from agent_org_network.web import serialize_reply

    reply = Answered(
        text="계약서 검토 완료",
        answered_by=("O", "contract_ops"),
        mode="full",
        sources=(),
    )
    body = serialize_reply(reply)

    assert "collaborators" not in body


def test_serialize_reply에_approval_draft_only_노출():
    """serialize_reply(Answered(mode='draft_only'))는 mode='draft_only'를 그대로 담는다."""
    from agent_org_network.web import serialize_reply

    reply = Answered(
        text="초안",
        answered_by=("O", "contract_ops"),
        mode="draft_only",
        sources=(),
    )
    body = serialize_reply(reply)

    assert body["mode"] == "draft_only"


# ── audit 보존 ─────────────────────────────────────────────────────────────────


def test_audit_requires_approval_보존():
    """Routed.requires_approval=True가 audit에 원형으로 기록된다."""
    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"])
    audit = InMemoryAuditLog()
    registry = Registry()
    registry.register(primary)
    classifier = FakeClassifier("계약 검토")
    router = Router(registry, classifier, root_user="root")
    ask = AskOrg(
        router=router,
        dispatcher=LocalRuntimeDispatcher(StubRuntime()),
        audit_log=audit,
        classifier=classifier,
        clock=_fixed_clock,
    )

    ask.handle("계약서 검토해줘", User(id="u1"))

    assert len(audit.entries) == 1
    decision = audit.entries[0].decision
    assert isinstance(decision, Routed)
    assert decision.requires_approval is True


def test_audit_collaborators_보존():
    """Routed.collaborators가 audit에 원형으로 기록된다.

    판례 경로: primary가 판례로 Routed 고정, collab은 같은 domain → collaborator로 부착 후 audit에 기록.
    """
    primary = _card("contract_ops", ["계약 검토"], collaborate_when=["계약 검토"])
    collab = _card("legal_ops", ["계약 검토"])

    ask, audit = _ask_org_with_precedent_and_collab(primary, collab, "계약 검토")
    ask.handle("계약서 검토해줘", User(id="u1"))

    assert len(audit.entries) == 1
    decision = audit.entries[0].decision
    assert isinstance(decision, Routed)
    assert len(decision.collaborators) == 1
    assert decision.collaborators[0].agent_id == "legal_ops"


def test_audit_jsonl_requires_approval_collaborators_직렬화():
    """AuditEntry.to_jsonl()에 requires_approval·collaborators가 포함된다."""
    import json

    from agent_org_network.audit import AuditEntry
    from agent_org_network.dispatch import Delivered, WorkTicket
    from agent_org_network.runtime import Answer

    primary = _card("contract_ops", ["계약 검토"], approval_when=["계약 검토"], collaborate_when=["계약 검토"])
    collab = _card("legal_ops", ["계약 검토"])

    routed = Routed(primary=primary, requires_approval=True, collaborators=(collab,), reason="테스트")
    ticket = WorkTicket(
        owner_id="O",
        agent_id="contract_ops",
        question="계약서 검토해줘",
        enqueued_at=_fixed_clock(),
    )
    entry = AuditEntry(
        timestamp=_fixed_clock(),
        user_id="u1",
        question="계약서 검토해줘",
        intent="계약 검토",
        decision=routed,
        dispatch_outcome=Delivered(ticket=ticket, answer=Answer(text="초안", sources=(), mode="full")),
    )
    record = json.loads(entry.to_jsonl())

    assert record["decision"]["requires_approval"] is True
    assert record["decision"]["collaborators"] == ["legal_ops"]


# ── 통합: PRD §7 시나리오2 — approval_when+collaborate_when 카드 ──────────────


def test_통합_approval_and_collaborate_세팅_카드_end_to_end():
    """approval_when+collaborate_when 세팅 카드: Routed 둘 다 부착 → mode=draft_only·collaborator 미노출.

    판례 경로: primary가 판례로 단일 Routed 고정, collab은 같은 domain을 갖고
    _attach_gates에서 collaborator로 부착된다.
    """
    primary = _card(
        "contract_ops",
        ["계약 검토"],
        approval_when=["계약 검토"],
        collaborate_when=["계약 검토"],
    )
    collab = _card("legal_ops", ["계약 검토"])

    ask, audit = _ask_org_with_precedent_and_collab(primary, collab, "계약 검토")
    user = User(id="u1")

    reply = ask.handle("계약서 검토해줘", user)

    # 사용자에게 mode=draft_only로 노출
    assert isinstance(reply, Answered)
    assert reply.mode == "draft_only"
    # collaborators는 Answered 필드에 없음(노출 불변식)
    assert not hasattr(reply, "collaborators")
    # audit에는 둘 다 원형으로 기록
    decision = audit.entries[0].decision
    assert isinstance(decision, Routed)
    assert decision.requires_approval is True
    assert len(decision.collaborators) == 1
    assert decision.collaborators[0].agent_id == "legal_ops"


def test_통합_기존_빈_게이트_카드_회귀_불변():
    """approval_when/collaborate_when이 비어있는 기존 카드는 기존 동작 그대로(회귀 0)."""
    primary = _card("contract_ops", ["계약 검토"])  # 게이트 필드 비어있음
    ask = _ask_org_with([primary], "계약 검토")
    user = User(id="u1")

    reply = ask.handle("계약서 검토해줘", user)

    assert isinstance(reply, Answered)
    assert reply.mode == "full"  # 게이트 없으면 full 그대로
