"""T10.4 실 인덱스 publish 경로 — 게이트 내 결정론 테스트 (ADR 0028 §14 결정 A~F).

owner 워커가 자기 인덱스를 PublishIndex 프레임으로 배포·중앙은 받아 보관(데모 지름길 제거 방향).
실 WS는 게이트 밖. 여기선 결정론 코어만 red→green:

  - 프레임 DTO 왕복(결정 A): PublishIndex model_dump(mode="json")↔재파싱·KnowledgeIndex 중첩 보존.
  - union 파싱 무회귀(결정 A): publish_index 분기·기존 5종 무회귀·미지 None(전방호환).
  - put staleness(결정 C): 첫 수용·더 새 교체·동률/역행 거부·per-agent 격리.
  - over-claim 필터(결정 D): 권한 밖 제외·in-domain 보존·cannot_answer 제외·전부 떨어지면 빈 concepts.
  - 워커-소유자 스코핑(결정 B): 일치 수용·타 owner 거부·미등록 거부.
  - publish_frames(결정 E): 소유 카드마다 프레임·agent_id/index 정확·주입 clock 결정론.
  - 핸들러 처리(결정 F): PublishIndex→스코핑→필터→put 통합(가짜 프레임·InMemory).
  - 공유 authorized 술어: routing·accept 둘 다 같은 함수 호출(중복 0).

불변식: 중앙 토큰 0·비소유 강화·Authority 중앙·등록 무결성·무회귀.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from agent_org_network.agent_card import AgentCard, domain_authorized
from agent_org_network.knowledge_index import Concept, KnowledgeIndex
from agent_org_network.registry import Registry
from agent_org_network.runtime import StubRuntime
from agent_org_network.server import _parse_worker_frame  # pyright: ignore[reportPrivateUsage]
from agent_org_network.transport import (
    Ack,
    Heartbeat,
    PublishIndex,
    RegisterWorker,
    SubmitAnswer,
    WebSocketDispatcher,
)
from agent_org_network.two_stage_router import (
    InMemoryPublishedIndexStore,
    accept_published_index,
    filter_authorized_concepts,
    publishable,
)
from agent_org_network.user import User
from agent_org_network.worker import WorkerLogic

# ── 헬퍼 픽스처 ──────────────────────────────────────────────────────────────

_T0 = datetime(2026, 6, 28, 9, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 6, 28, 10, 0, 0, tzinfo=timezone.utc)  # _T0 보다 새 것
_REVIEWED = date(2026, 6, 28)


def _card(
    agent_id: str,
    owner: str = "alice",
    domains: list[str] | None = None,
    cannot_answer: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="ops",
        summary=f"{agent_id} 요약",
        domains=domains if domains is not None else ["환불"],
        last_reviewed_at=_REVIEWED,
        cannot_answer=cannot_answer or [],
    )


def _concept(cid: str, domain: str) -> Concept:
    return Concept(id=cid, label=cid, core_question=f"{cid} 질문", domain=domain)


def _index(
    agent_id: str,
    *concepts: Concept,
    version: str = "okf-1",
    generated_at: datetime = _T0,
) -> KnowledgeIndex:
    return KnowledgeIndex(
        agent_id=agent_id,
        version=version,
        generated_at=generated_at,
        concepts=concepts,
    )


def _registry(*cards: AgentCard) -> Registry:
    reg = Registry()
    owners = {c.owner for c in cards}
    for owner in sorted(owners):
        reg.register_user(User(id=owner))
    for card in cards:
        reg.register(card)
    return reg


# ── 1. 프레임 DTO 왕복(결정 A) ──────────────────────────────────────────────


def test_publish_index_프레임_json_왕복이_index를_보존한다() -> None:
    """model_dump(mode="json")→model_validate 왕복·중첩 KnowledgeIndex·generated_at ISO."""
    idx = _index("cs_ops", _concept("refund", "환불"), _concept("comp", "보상"))
    frame = PublishIndex(index=idx)

    dumped = frame.model_dump(mode="json")
    # generated_at은 ISO 문자열로 직렬화돼야 한다(send_json의 json.dumps 안전).
    assert isinstance(dumped["index"]["generated_at"], str)
    assert dumped["type"] == "publish_index"

    restored = PublishIndex.model_validate(dumped)
    assert restored == frame
    assert restored.index.agent_id == "cs_ops"
    assert restored.index.generated_at == _T0
    assert tuple(c.id for c in restored.index.concepts) == ("refund", "comp")
    assert restored.index.concepts[0].domain == "환불"


def test_publish_index_model_dump_json_문자열_왕복() -> None:
    """워커 송신 형식(model_dump_json) → 파서 복원 왕복(실 와이어 경로)."""
    import json

    idx = _index("it_ops", _concept("vpn", "보안"))
    frame = PublishIndex(index=idx)

    wire = frame.model_dump_json()
    parsed = _parse_worker_frame(json.loads(wire))  # pyright: ignore[reportPrivateUsage]

    assert isinstance(parsed, PublishIndex)
    assert parsed == frame


# ── 2. union 파싱 무회귀(결정 A) ────────────────────────────────────────────


def test_파서가_publish_index를_판별한다() -> None:
    idx = _index("cs_ops", _concept("refund", "환불"))
    raw = PublishIndex(index=idx).model_dump(mode="json")
    parsed = _parse_worker_frame(raw)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(parsed, PublishIndex)


def test_기존_4종_프레임_파싱_무회귀() -> None:
    """publish_index 추가가 register_worker/submit_answer/heartbeat/ack 파싱을 안 깬다."""
    from agent_org_network.transport import AnswerFrame

    rw = RegisterWorker(owner_id="alice").model_dump(mode="json")
    sa = SubmitAnswer(
        ticket_id="t1", answer=AnswerFrame(text="답")
    ).model_dump(mode="json")
    hb = Heartbeat().model_dump(mode="json")
    ack = Ack(ticket_id="t1").model_dump(mode="json")

    assert isinstance(_parse_worker_frame(rw), RegisterWorker)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(_parse_worker_frame(sa), SubmitAnswer)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(_parse_worker_frame(hb), Heartbeat)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(_parse_worker_frame(ack), Ack)  # pyright: ignore[reportPrivateUsage]


def test_미지_type은_None_전방호환() -> None:
    """미지 type 프레임은 None(else: return None) — 구버전 중앙도 안 깨짐."""
    assert _parse_worker_frame({"type": "future_frame", "x": 1}) is None  # pyright: ignore[reportPrivateUsage]
    assert _parse_worker_frame({"type": "publish_index"}) is None  # pyright: ignore[reportPrivateUsage]  # index 누락 → 검증 실패 None
    assert _parse_worker_frame("not a dict") is None  # pyright: ignore[reportPrivateUsage]


def test_publish_index_type이_기존_키와_안_겹친다() -> None:
    """publish_index 판별 키가 기존 5종과 충돌하지 않는다."""
    type_literals: list[object] = [
        RegisterWorker.model_fields["type"].default,
        SubmitAnswer.model_fields["type"].default,
        PublishIndex.model_fields["type"].default,
        Heartbeat.model_fields["type"].default,
        Ack.model_fields["type"].default,
    ]
    assert "publish_index" in type_literals
    assert len(type_literals) == len(set(type_literals))  # 키 유일


# ── 3. put staleness(결정 C) ────────────────────────────────────────────────


def test_put_첫_인덱스_무조건_수용() -> None:
    store = InMemoryPublishedIndexStore()
    idx = _index("cs_ops", _concept("a", "환불"), generated_at=_T0)
    store.put(idx)
    assert store.get("cs_ops") == idx


def test_put_더_새_것은_교체() -> None:
    store = InMemoryPublishedIndexStore()
    old = _index("cs_ops", _concept("a", "환불"), generated_at=_T0)
    new = _index("cs_ops", _concept("b", "환불"), generated_at=_T1)
    store.put(old)
    store.put(new)
    got = store.get("cs_ops")
    assert got is not None
    assert got.generated_at == _T1
    assert got.concepts[0].id == "b"


def test_put_동률은_거부_멱등() -> None:
    """같은 generated_at 재도착 → no-op(기존 보존·멱등)."""
    store = InMemoryPublishedIndexStore()
    first = _index("cs_ops", _concept("a", "환불"), generated_at=_T0)
    again = _index("cs_ops", _concept("b", "환불"), generated_at=_T0)
    store.put(first)
    store.put(again)
    got = store.get("cs_ops")
    assert got is not None
    assert got.concepts[0].id == "a"  # 첫 것 보존(동률 교체 안 함)


def test_put_역행은_거부() -> None:
    """옛 인덱스가 늦게 도착해도 최신을 안 덮는다."""
    store = InMemoryPublishedIndexStore()
    new = _index("cs_ops", _concept("new", "환불"), generated_at=_T1)
    old = _index("cs_ops", _concept("old", "환불"), generated_at=_T0)
    store.put(new)
    store.put(old)  # 역행 — 거부
    got = store.get("cs_ops")
    assert got is not None
    assert got.concepts[0].id == "new"


def test_put_per_agent_격리() -> None:
    """한 agent 갱신이 다른 agent 인덱스에 영향 0."""
    store = InMemoryPublishedIndexStore()
    a = _index("cs_ops", _concept("a", "환불"), generated_at=_T0)
    b = _index("it_ops", _concept("b", "보안"), generated_at=_T0)
    store.put(a)
    store.put(b)
    a2 = _index("cs_ops", _concept("a2", "환불"), generated_at=_T1)
    store.put(a2)
    assert store.get("it_ops") == b  # it_ops 무영향
    got = store.get("cs_ops")
    assert got is not None
    assert got.concepts[0].id == "a2"


# ── 4. over-claim 필터(결정 D) ──────────────────────────────────────────────


def test_filter_권한_안_concept_보존() -> None:
    card = _card("cs_ops", domains=["환불", "보상"])
    idx = _index("cs_ops", _concept("a", "환불"), _concept("b", "보상"))
    filtered = filter_authorized_concepts(idx, card)
    assert tuple(c.id for c in filtered.concepts) == ("a", "b")


def test_filter_over_claim_concept_제외() -> None:
    """domain ∉ card.domains concept 떨굼."""
    card = _card("cs_ops", domains=["환불"])
    idx = _index("cs_ops", _concept("a", "환불"), _concept("b", "보안"))  # 보안 over-claim
    filtered = filter_authorized_concepts(idx, card)
    assert tuple(c.id for c in filtered.concepts) == ("a",)


def test_filter_cannot_answer_concept_제외() -> None:
    card = _card("cs_ops", domains=["환불", "보상"], cannot_answer=["보상"])
    idx = _index("cs_ops", _concept("a", "환불"), _concept("b", "보상"))
    filtered = filter_authorized_concepts(idx, card)
    assert tuple(c.id for c in filtered.concepts) == ("a",)


def test_filter_전부_떨어지면_빈_concepts_보관() -> None:
    """authorized 0개여도 인덱스 자체 거부 안 함(빈 concepts·메타 보존)."""
    card = _card("cs_ops", domains=["환불"])
    idx = _index("cs_ops", _concept("a", "보안"), _concept("b", "계약"))  # 전부 over-claim
    filtered = filter_authorized_concepts(idx, card)
    assert filtered.concepts == ()
    assert filtered.agent_id == "cs_ops"
    assert filtered.generated_at == _T0
    assert filtered.version == "okf-1"


def test_filter_전부_통과면_원_인덱스_그대로() -> None:
    card = _card("cs_ops", domains=["환불"])
    idx = _index("cs_ops", _concept("a", "환불"))
    filtered = filter_authorized_concepts(idx, card)
    assert filtered is idx  # 새 객체 안 만듦(불변·동일성)


# ── 5. 워커-소유자 스코핑(결정 B) ──────────────────────────────────────────


def test_publishable_owner_일치_수용() -> None:
    card = _card("cs_ops", owner="alice", domains=["환불"])
    reg = _registry(card)
    idx = _index("cs_ops", _concept("a", "환불"))
    assert publishable("alice", idx, reg) is True


def test_publishable_타_owner_거부() -> None:
    """다른 owner의 agent_id로 publish하는 사칭 차단."""
    card = _card("cs_ops", owner="alice", domains=["환불"])
    reg = _registry(card)
    idx = _index("cs_ops", _concept("a", "환불"))
    assert publishable("mallory", idx, reg) is False  # cs_ops는 alice 소유


def test_publishable_미등록_agent_거부() -> None:
    """미등록 agent_id 인덱스 거부(등록 무결성·KeyError)."""
    reg = _registry(_card("cs_ops", owner="alice", domains=["환불"]))
    idx = _index("ghost_ops", _concept("a", "환불"))
    assert publishable("alice", idx, reg) is False


# ── 6. publish_frames(결정 E) ───────────────────────────────────────────────


def _write_okf(root: Path, agent_id: str, filename: str, *, front: str) -> None:
    d = root / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(f"---\n{front}\n---\n\n본문\n", encoding="utf-8")


def test_publish_frames_소유_카드마다_프레임(tmp_path: Path) -> None:
    """자기 소유 카드마다 PublishIndex·agent_id 정확."""
    card_a = _card("cs_ops", owner="alice", domains=["환불"])
    card_b = _card("it_ops", owner="alice", domains=["보안"])
    _write_okf(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    _write_okf(tmp_path, "it_ops", "vpn.md", front="title: VPN\ndescription: d\ntags: [보안]")

    logic = WorkerLogic(
        owner_id="alice",
        cards={"cs_ops": card_a, "it_ops": card_b},
        runtime=StubRuntime(),
        okf_root=tmp_path,
    )
    frames = logic.publish_frames(generated_at=_T0)

    assert len(frames) == 2
    agent_ids = {f.index.agent_id for f in frames}
    assert agent_ids == {"cs_ops", "it_ops"}
    for f in frames:
        assert isinstance(f, PublishIndex)
        assert f.index.generated_at == _T0


def test_publish_frames_okf_root_없으면_빈_리스트() -> None:
    """okf_root 미주입 워커는 배포 안 함(하위호환)."""
    logic = WorkerLogic(
        owner_id="alice",
        cards={"cs_ops": _card("cs_ops")},
        runtime=StubRuntime(),
    )
    assert logic.publish_frames(generated_at=_T0) == []


def test_publish_frames_주입_clock_결정론(tmp_path: Path) -> None:
    """같은 OKF·같은 주입 clock → 같은 프레임(결정론)."""
    card = _card("cs_ops", owner="alice", domains=["환불"])
    _write_okf(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    logic = WorkerLogic(
        owner_id="alice",
        cards={"cs_ops": card},
        runtime=StubRuntime(),
        okf_root=tmp_path,
    )
    a = logic.publish_frames(generated_at=_T0)
    b = logic.publish_frames(generated_at=_T0)
    assert a == b
    # 다른 clock → generated_at 다름
    c = logic.publish_frames(generated_at=_T1)
    assert c[0].index.generated_at == _T1


# ── 7. 핸들러 처리 통합(결정 F) ────────────────────────────────────────────


def test_accept_published_index_스코핑_필터_put_통합() -> None:
    """일치 owner → over-claim 필터 후 보관."""
    card = _card("cs_ops", owner="alice", domains=["환불"])
    reg = _registry(card)
    store = InMemoryPublishedIndexStore()
    idx = _index("cs_ops", _concept("a", "환불"), _concept("b", "보안"))  # 보안 over-claim

    ok = accept_published_index("alice", idx, reg, store)

    assert ok is True
    stored = store.get("cs_ops")
    assert stored is not None
    assert tuple(c.id for c in stored.concepts) == ("a",)  # 보안 필터됨


def test_accept_타_owner_거부_보관_안_함() -> None:
    card = _card("cs_ops", owner="alice", domains=["환불"])
    reg = _registry(card)
    store = InMemoryPublishedIndexStore()
    idx = _index("cs_ops", _concept("a", "환불"))

    ok = accept_published_index("mallory", idx, reg, store)

    assert ok is False
    assert store.get("cs_ops") is None  # 사칭 거부 — 미보관


def test_accept_미등록_agent_거부() -> None:
    reg = _registry(_card("cs_ops", owner="alice", domains=["환불"]))
    store = InMemoryPublishedIndexStore()
    idx = _index("ghost", _concept("a", "환불"))

    ok = accept_published_index("alice", idx, reg, store)

    assert ok is False
    assert store.get("ghost") is None


def test_accept_staleness_역행_no_op이지만_스코핑은_통과() -> None:
    """스코핑 통과 후 staleness로 put이 no-op이어도 반환 True(거부 아님)."""
    card = _card("cs_ops", owner="alice", domains=["환불"])
    reg = _registry(card)
    store = InMemoryPublishedIndexStore()
    new = _index("cs_ops", _concept("new", "환불"), generated_at=_T1)
    old = _index("cs_ops", _concept("old", "환불"), generated_at=_T0)
    accept_published_index("alice", new, reg, store)

    ok = accept_published_index("alice", old, reg, store)  # 역행

    assert ok is True  # 스코핑 통과(put no-op은 거부 아님)
    stored = store.get("cs_ops")
    assert stored is not None
    assert stored.concepts[0].id == "new"  # 최신 보존


def test_dispatcher_accept_index_위임() -> None:
    """WebSocketDispatcher.accept_index가 registry·store를 묶어 accept_published_index 위임."""
    card = _card("cs_ops", owner="alice", domains=["환불"])
    reg = _registry(card)
    store = InMemoryPublishedIndexStore()
    dispatcher = WebSocketDispatcher(registry=reg, published_index_store=store)

    frame = PublishIndex(index=_index("cs_ops", _concept("a", "환불"), _concept("b", "보안")))
    ok = dispatcher.accept_index("alice", frame)

    assert ok is True
    stored = store.get("cs_ops")
    assert stored is not None
    assert tuple(c.id for c in stored.concepts) == ("a",)  # over-claim 필터됨


def test_dispatcher_accept_index_미주입이면_no_op() -> None:
    """registry/store 미주입 디스패처는 publish 수용 안 함(하위호환)."""
    dispatcher = WebSocketDispatcher()  # registry·store 없음
    frame = PublishIndex(index=_index("cs_ops", _concept("a", "환불")))
    assert dispatcher.accept_index("alice", frame) is False


def test_dispatcher_accept_index_타_owner_거부() -> None:
    card = _card("cs_ops", owner="alice", domains=["환불"])
    reg = _registry(card)
    store = InMemoryPublishedIndexStore()
    dispatcher = WebSocketDispatcher(registry=reg, published_index_store=store)
    frame = PublishIndex(index=_index("cs_ops", _concept("a", "환불")))
    assert dispatcher.accept_index("mallory", frame) is False
    assert store.get("cs_ops") is None


# ── 8. 공유 authorized 술어(중복 0) ────────────────────────────────────────


def test_routing과_accept이_같은_술어_함수를_쓴다() -> None:
    """two_stage_router·accept 권한 검증이 모두 agent_card.domain_authorized를 호출한다."""
    import inspect

    import agent_org_network.two_stage_router as tsr

    src = inspect.getsource(tsr)
    # filter_authorized_concepts·route 권한 재검증·precedent 재검증 모두 공유 술어 호출.
    assert "domain_authorized" in src
    # 인라인 중복 정의가 없어야 한다(공유 함수 단일 권위) — 옛 인라인 표현 부재.
    assert "domain in card.domains and domain not in card.cannot_answer" not in src
    assert (
        "representative_intent in card.domains" not in src
        or "and representative_intent not in card.cannot_answer" not in src
    )


def test_domain_authorized_술어_규칙() -> None:
    """공유 술어: domain ∈ domains AND ∉ cannot_answer."""
    card = _card("cs_ops", domains=["환불", "보상"], cannot_answer=["보상"])
    assert domain_authorized("환불", card) is True
    assert domain_authorized("보상", card) is False  # cannot_answer
    assert domain_authorized("보안", card) is False  # ∉ domains
