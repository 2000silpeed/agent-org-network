"""T7.3 슬라이스 1~7 — 지식 신선도·변경 전파 결정론 테스트 (ADR 0019).

전부 결정론: 고정 clock, InMemory store, FakeGitGateway, Fake propagator/owner_of.
실 LLM·실 git·실 네트워크 0.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from agent_org_network.conflict import (
    InMemoryPrecedentStore,
    Precedent,
    Resolution,
)
from agent_org_network.git_gateway import (
    BuilderCommitRequest,
    FakeGitGateway,
    OkfChangeEvent,
    OkfFile,
    commit_okf_bundle,
)
from agent_org_network.reeval import (
    AcknowledgeAnswer,
    AnswerSubject,
    InMemoryReevalStore,
    InvalidatePrecedent,
    KeepPrecedent,
    PrecedentSubject,
    ReevalItem,
    ReevalService,
    ReAnswer,
    StalenessPropagator,
    SupersedePrecedent,
)

_TS = datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc)
_CLOCK = lambda: _TS  # noqa: E731


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 1 — OkfChangeEvent 발화
# ══════════════════════════════════════════════════════════════════════════════


class _FakePropagator:
    """on_okf_committed 호출 기록 Fake."""

    def __init__(self) -> None:
        self.events: list[OkfChangeEvent] = []

    def on_okf_committed(self, event: OkfChangeEvent) -> None:
        self.events.append(event)


def _make_req(
    agent_id: str = "cs_ops",
    owner: str = "cs_lead",
    files: tuple[OkfFile, ...] | None = None,
    message: str = "정책 갱신",
) -> BuilderCommitRequest:
    if files is None:
        files = (OkfFile(path="policy.md", content="# 환불\n"),)
    return BuilderCommitRequest(
        agent_id=agent_id,
        owner=owner,
        files=files,
        message=message,
    )


class TestSlice1OkfChangeEvent:
    def test_propagator_None이면_CommitResult만_반환된다(self) -> None:
        gw = FakeGitGateway()
        result = commit_okf_bundle(_make_req(), gw, propagator=None)
        assert result.sha != ""
        assert result.agent_id == "cs_ops"

    def test_propagator_None_기존_동작_하위호환(self) -> None:
        """propagator=None이면 기존 commit 동작이 완전히 그대로."""
        gw = FakeGitGateway()
        r1 = commit_okf_bundle(_make_req(), gw)
        gw2 = FakeGitGateway()
        r2 = commit_okf_bundle(_make_req(), gw2, propagator=None)
        assert r1.sha == r2.sha

    def test_propagator_주입시_on_okf_committed_1회_호출된다(self) -> None:
        gw = FakeGitGateway()
        fake = _FakePropagator()
        commit_okf_bundle(_make_req(), gw, propagator=fake, clock=_CLOCK)
        assert len(fake.events) == 1

    def test_event_agent_id가_req_agent_id다(self) -> None:
        gw = FakeGitGateway()
        fake = _FakePropagator()
        commit_okf_bundle(_make_req(agent_id="cs_ops"), gw, propagator=fake, clock=_CLOCK)
        assert fake.events[0].agent_id == "cs_ops"

    def test_event_new_sha가_CommitResult_sha와_같다(self) -> None:
        gw = FakeGitGateway()
        fake = _FakePropagator()
        result = commit_okf_bundle(_make_req(), gw, propagator=fake, clock=_CLOCK)
        assert fake.events[0].new_sha == result.sha

    def test_event_parent_sha가_최초_커밋이면_None이다(self) -> None:
        """최초 커밋이라 커밋 전 HEAD가 없으면 parent_sha=None."""
        gw = FakeGitGateway()
        fake = _FakePropagator()
        commit_okf_bundle(_make_req(), gw, propagator=fake, clock=_CLOCK)
        assert fake.events[0].parent_sha is None

    def test_event_parent_sha가_두번째_커밋이면_첫_SHA다(self) -> None:
        """두 번째 커밋: parent_sha = 첫 커밋 SHA."""
        gw = FakeGitGateway()
        fake = _FakePropagator()
        r1 = commit_okf_bundle(_make_req(message="첫"), gw, propagator=fake, clock=_CLOCK)
        fake2 = _FakePropagator()
        commit_okf_bundle(_make_req(message="두번째"), gw, propagator=fake2, clock=_CLOCK)
        assert fake2.events[0].parent_sha == r1.sha

    def test_event_changed_paths가_req_files의_path_튜플이다(self) -> None:
        gw = FakeGitGateway()
        fake = _FakePropagator()
        files = (
            OkfFile(path="a.md", content="A"),
            OkfFile(path="b.md", content="B"),
        )
        commit_okf_bundle(_make_req(files=files), gw, propagator=fake, clock=_CLOCK)
        assert fake.events[0].changed_paths == ("a.md", "b.md")

    def test_event_committed_at이_주입_clock과_같다(self) -> None:
        gw = FakeGitGateway()
        fake = _FakePropagator()
        commit_okf_bundle(_make_req(), gw, propagator=fake, clock=_CLOCK)
        assert fake.events[0].committed_at == _TS

    def test_event_author가_req_owner다(self) -> None:
        gw = FakeGitGateway()
        fake = _FakePropagator()
        commit_okf_bundle(_make_req(owner="cs_lead"), gw, propagator=fake, clock=_CLOCK)
        assert fake.events[0].author == "cs_lead"


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — PrecedentStore 역색인·flag_stale
# ══════════════════════════════════════════════════════════════════════════════


class TestSlice2PrecedentStore:
    def test_record_후_find_by_primary_색인된다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        result = store.find_by_primary("cs_ops")
        assert len(result) == 1
        assert result[0].resolution.primary == "cs_ops"

    def test_find_by_primary_없는_agent_id는_빈_리스트(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        assert store.find_by_primary("unknown") == []

    def test_다수_판례_같은_primary_모두_반환(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.record(Resolution(intent="교환", primary="cs_ops"))
        result = store.find_by_primary("cs_ops")
        assert len(result) == 2

    def test_다른_primary는_섞이지_않는다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.record(Resolution(intent="법무", primary="legal_ops"))
        assert len(store.find_by_primary("cs_ops")) == 1
        assert len(store.find_by_primary("legal_ops")) == 1

    def test_list_all이_모든_판례를_반환한다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.record(Resolution(intent="법무", primary="legal_ops"))
        all_p = store.list_all()
        assert len(all_p) == 2

    def test_list_all_비어있으면_빈_리스트(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        assert store.list_all() == []

    def test_flag_stale이_needs_review_True로_전이된다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        flagged = store.flag_stale("환불", trigger_sha="sha-123", at=_TS)
        assert flagged is not None
        assert flagged.needs_review is True

    def test_flag_stale이_last_flagged_at을_설정한다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        flagged = store.flag_stale("환불", trigger_sha="sha-123", at=_TS)
        assert flagged is not None
        assert flagged.last_flagged_at == _TS

    def test_flag_stale이_새_인스턴스를_store에_반영한다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.flag_stale("환불", trigger_sha="sha-123", at=_TS)
        looked_up = store.lookup("환불")
        assert looked_up is not None
        assert looked_up.needs_review is True

    def test_flag_stale_이미_needs_review면_멱등(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        first = store.flag_stale("환불", trigger_sha="sha-1", at=_TS)
        second = store.flag_stale("환불", trigger_sha="sha-2", at=_TS)
        assert first is not None
        assert second is not None
        assert first is second  # 멱등: 같은 인스턴스 반환

    def test_flag_stale_없는_intent는_None(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        result = store.flag_stale("없는_인텐트", trigger_sha="sha-1", at=_TS)
        assert result is None

    def test_flag_stale이_history에도_반영된다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.flag_stale("환불", trigger_sha="sha-1", at=_TS)
        flagged_in_history = [p for p in store.history if p.needs_review]
        assert len(flagged_in_history) == 1

    def test_Precedent_기본값_하위호환(self) -> None:
        """needs_review·last_flagged_at 기본값 보장."""
        p = Precedent(
            resolution=Resolution(intent="환불", primary="cs_ops"),
            recorded_at=_TS,
        )
        assert p.needs_review is False
        assert p.last_flagged_at is None

    def test_flag_stale이_by_primary_역색인도_갱신한다(self) -> None:
        store = InMemoryPrecedentStore(clock=_CLOCK)
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.flag_stale("환불", trigger_sha="sha-1", at=_TS)
        by_primary = store.find_by_primary("cs_ops")
        assert by_primary[0].needs_review is True


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 3 — 라우터 불변성(플래그≠무효화)
# ══════════════════════════════════════════════════════════════════════════════


class TestSlice3RouterInvariant:
    def _make_router_and_store(
        self,
    ) -> tuple[Any, InMemoryPrecedentStore]:
        from datetime import date

        from agent_org_network.agent_card import AgentCard
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router

        card = AgentCard(
            agent_id="cs_ops",
            owner="cs_lead",
            team="cs",
            summary="환불 안내",
            domains=["환불"],
            last_reviewed_at=date(2026, 6, 22),
        )
        registry = Registry()
        registry.register(card)
        classifier = FakeClassifier("환불")
        store = InMemoryPrecedentStore(clock=_CLOCK)
        router = Router(registry, classifier, root_user="root", precedents=store)
        return router, store

    def test_needs_review_False_판례로_Routed_반환(self) -> None:
        from agent_org_network.decision import Routed

        router, store = self._make_router_and_store()
        store.record(Resolution(intent="환불", primary="cs_ops"))
        decision = router.route("환불 되나요?")
        assert isinstance(decision, Routed)
        assert decision.primary.agent_id == "cs_ops"

    def test_needs_review_True_판례도_Routed_반환된다(self) -> None:
        """stale 판례도 라우팅을 방해하지 않는다(미아 없음 불변식)."""
        from agent_org_network.decision import Routed

        router, store = self._make_router_and_store()
        store.record(Resolution(intent="환불", primary="cs_ops"))
        store.flag_stale("환불", trigger_sha="sha-1", at=_TS)
        decision = router.route("환불 되나요?")
        assert isinstance(decision, Routed)
        assert decision.primary.agent_id == "cs_ops"

    def test_needs_review_전후_라우팅_결과_동일(self) -> None:
        """flag_stale 전후 route 결과가 같아야 한다."""
        from agent_org_network.decision import Routed

        router, store = self._make_router_and_store()
        store.record(Resolution(intent="환불", primary="cs_ops"))

        before = router.route("환불 되나요?")
        store.flag_stale("환불", trigger_sha="sha-1", at=_TS)
        after = router.route("환불 되나요?")

        assert isinstance(before, Routed)
        assert isinstance(after, Routed)
        assert before.primary.agent_id == after.primary.agent_id


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 4 — ReevalItem·Store·Service 전이
# ══════════════════════════════════════════════════════════════════════════════


def _reeval_item(
    subject: PrecedentSubject | AnswerSubject = PrecedentSubject(intent="환불"),
    owner_id: str = "cs_lead",
    agent_id: str = "cs_ops",
    trigger_sha: str = "sha-001",
    item_id: str = "item-001",
) -> ReevalItem:
    return ReevalItem(
        subject=subject,
        owner_id=owner_id,
        agent_id=agent_id,
        trigger_sha=trigger_sha,
        flagged_at=_TS,
        item_id=item_id,
    )


class TestSlice4ReevalItem:
    def test_ReevalItem_frozen이다(self) -> None:
        item = _reeval_item()
        with pytest.raises((AttributeError, TypeError)):
            item.owner_id = "other"  # type: ignore[misc]

    def test_기본_status가_pending_review다(self) -> None:
        item = _reeval_item()
        assert item.status == "pending_review"
        assert item.review is None

    def test_review_with가_reviewed_새_인스턴스를_돌려준다(self) -> None:
        item = _reeval_item(item_id="item-abc")
        review = KeepPrecedent(by_owner="cs_lead")
        reviewed = item.review_with(review)
        assert reviewed is not item
        assert reviewed.item_id == "item-abc"
        assert reviewed.status == "reviewed"
        assert reviewed.review == review

    def test_review_with가_원본을_불변으로_남긴다(self) -> None:
        item = _reeval_item()
        _ = item.review_with(KeepPrecedent(by_owner="cs_lead"))
        assert item.status == "pending_review"
        assert item.review is None

    def test_review_with_InvalidatePrecedent_보존(self) -> None:
        item = _reeval_item()
        review = InvalidatePrecedent(by_owner="cs_lead", rationale="이유")
        reviewed = item.review_with(review)
        assert isinstance(reviewed.review, InvalidatePrecedent)
        assert reviewed.review.rationale == "이유"

    def test_review_with_SupersedePrecedent_보존(self) -> None:
        item = _reeval_item()
        review = SupersedePrecedent(by_owner="cs_lead", new_primary="new_cs_ops")
        reviewed = item.review_with(review)
        assert isinstance(reviewed.review, SupersedePrecedent)
        assert reviewed.review.new_primary == "new_cs_ops"

    def test_review_with_AcknowledgeAnswer_보존(self) -> None:
        item = _reeval_item(subject=AnswerSubject(audit_index=0))
        review = AcknowledgeAnswer(by_owner="cs_lead")
        reviewed = item.review_with(review)
        assert isinstance(reviewed.review, AcknowledgeAnswer)

    def test_review_with_ReAnswer_보존(self) -> None:
        item = _reeval_item(subject=AnswerSubject(audit_index=0))
        review = ReAnswer(by_owner="cs_lead")
        reviewed = item.review_with(review)
        assert isinstance(reviewed.review, ReAnswer)


class TestSlice4InMemoryReevalStore:
    def test_add_후_get으로_조회된다(self) -> None:
        store = InMemoryReevalStore()
        item = _reeval_item(item_id="item-001")
        store.add(item)
        assert store.get("item-001") == item

    def test_get_없는_item_id는_None(self) -> None:
        store = InMemoryReevalStore()
        assert store.get("nonexistent") is None

    def test_pending_for_owner가_해당_owner_pending만_돌려준다(self) -> None:
        store = InMemoryReevalStore()
        alice = _reeval_item(owner_id="alice", item_id="item-alice")
        bob = _reeval_item(owner_id="bob", item_id="item-bob")
        store.add(alice)
        store.add(bob)
        assert len(store.pending_for_owner("alice")) == 1
        assert store.pending_for_owner("alice")[0].owner_id == "alice"
        assert len(store.pending_for_owner("bob")) == 1

    def test_pending_for_owner_없으면_빈_리스트(self) -> None:
        store = InMemoryReevalStore()
        assert store.pending_for_owner("unknown") == []

    def test_mark_reviewed_후_pending에서_사라진다(self) -> None:
        store = InMemoryReevalStore()
        item = _reeval_item(item_id="item-001")
        store.add(item)
        reviewed = item.review_with(KeepPrecedent(by_owner="cs_lead"))
        store.mark_reviewed(reviewed)
        assert store.pending_for_owner("cs_lead") == []

    def test_mark_reviewed_후_history에_남는다(self) -> None:
        store = InMemoryReevalStore()
        item = _reeval_item(item_id="item-001")
        store.add(item)
        reviewed = item.review_with(KeepPrecedent(by_owner="cs_lead"))
        store.mark_reviewed(reviewed)
        assert any(h.status == "reviewed" for h in store.history)

    def test_mark_reviewed_후_get으로_reviewed_항목_조회된다(self) -> None:
        store = InMemoryReevalStore()
        item = _reeval_item(item_id="item-001")
        store.add(item)
        reviewed = item.review_with(KeepPrecedent(by_owner="cs_lead"))
        store.mark_reviewed(reviewed)
        result = store.get("item-001")
        assert result is not None
        assert result.status == "reviewed"

    def test_owner_격리_다른_owner_섞이지_않는다(self) -> None:
        store = InMemoryReevalStore()
        for i in range(3):
            store.add(_reeval_item(owner_id="alice", item_id=f"item-alice-{i}"))
        store.add(_reeval_item(owner_id="bob", item_id="item-bob"))
        assert len(store.pending_for_owner("alice")) == 3
        assert len(store.pending_for_owner("bob")) == 1


class TestSlice4ReevalService:
    def test_KeepPrecedent_전이(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-001")
        store.add(item)
        reviewed = service.review("item-001", KeepPrecedent(by_owner="cs_lead"))
        assert reviewed.status == "reviewed"
        assert isinstance(reviewed.review, KeepPrecedent)

    def test_InvalidatePrecedent_전이(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-001")
        store.add(item)
        reviewed = service.review("item-001", InvalidatePrecedent(by_owner="cs_lead"))
        assert reviewed.status == "reviewed"

    def test_SupersedePrecedent_전이(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-001")
        store.add(item)
        reviewed = service.review(
            "item-001", SupersedePrecedent(by_owner="cs_lead", new_primary="new_cs")
        )
        assert reviewed.status == "reviewed"

    def test_AcknowledgeAnswer_전이(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(
            subject=AnswerSubject(audit_index=0), owner_id="cs_lead", item_id="item-001"
        )
        store.add(item)
        reviewed = service.review("item-001", AcknowledgeAnswer(by_owner="cs_lead"))
        assert reviewed.status == "reviewed"

    def test_ReAnswer_전이(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(
            subject=AnswerSubject(audit_index=0), owner_id="cs_lead", item_id="item-001"
        )
        store.add(item)
        reviewed = service.review("item-001", ReAnswer(by_owner="cs_lead"))
        assert reviewed.status == "reviewed"

    def test_1인칭_위반_ValueError(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-001")
        store.add(item)
        with pytest.raises(ValueError, match="cs_lead"):
            service.review("item-001", KeepPrecedent(by_owner="other_owner"))

    def test_미존재_item_id_ValueError(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        with pytest.raises(ValueError, match="미존재"):
            service.review("nonexistent", KeepPrecedent(by_owner="cs_lead"))

    def test_이미_reviewed면_멱등(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-001")
        store.add(item)
        first = service.review("item-001", KeepPrecedent(by_owner="cs_lead"))
        second = service.review("item-001", KeepPrecedent(by_owner="cs_lead"))
        assert first.status == "reviewed"
        assert second.status == "reviewed"

    def test_5arm_sealed_sum_망라(self) -> None:
        """ReevalOutcome 5-arm 모두 처리 가능(match+assert_never 회귀)."""
        from typing import assert_never

        outcomes: list[
            KeepPrecedent
            | InvalidatePrecedent
            | SupersedePrecedent
            | AcknowledgeAnswer
            | ReAnswer
        ] = [
            KeepPrecedent(by_owner="o"),
            InvalidatePrecedent(by_owner="o"),
            SupersedePrecedent(by_owner="o", new_primary="x"),
            AcknowledgeAnswer(by_owner="o"),
            ReAnswer(by_owner="o"),
        ]
        kind: str = ""
        for outcome in outcomes:
            match outcome:
                case KeepPrecedent():
                    kind = "keep"
                case InvalidatePrecedent():
                    kind = "invalidate"
                case SupersedePrecedent():
                    kind = "supersede"
                case AcknowledgeAnswer():
                    kind = "acknowledge"
                case ReAnswer():
                    kind = "reanswer"
                case _ as never:
                    assert_never(never)
        assert kind == "reanswer"


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 5 — StalenessPropagator Precedent 축
# ══════════════════════════════════════════════════════════════════════════════


def _event(
    agent_id: str = "cs_ops",
    new_sha: str = "sha-new",
    parent_sha: str | None = None,
    committed_at: datetime | None = None,
) -> OkfChangeEvent:
    return OkfChangeEvent(
        agent_id=agent_id,
        new_sha=new_sha,
        parent_sha=parent_sha,
        changed_paths=("policy.md",),
        author="cs_lead",
        committed_at=committed_at or _TS,
    )


class TestSlice5PrecedentAxis:
    def _make_fake_audit(self) -> Any:
        """빈 records()를 돌려주는 최소 audit reader."""

        class _EmptyAudit:
            def records(self) -> list[dict[str, Any]]:
                return []

            def record_at(self, index: int) -> dict[str, Any] | None:
                return None

        return _EmptyAudit()

    def test_판례_flag_및_ReevalItem_적재된다(self) -> None:
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="환불", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=self._make_fake_audit(),
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))

        flagged = precedents.lookup("환불")
        assert flagged is not None
        assert flagged.needs_review is True

        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 1
        subject = items[0].subject
        assert isinstance(subject, PrecedentSubject)
        assert subject.intent == "환불"

    def test_ReevalItem_trigger_sha가_event_new_sha다(self) -> None:
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="환불", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=self._make_fake_audit(),
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-xyz"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert items[0].trigger_sha == "sha-xyz"

    def test_이미_needs_review_판례는_중복_큐잉_안_함(self) -> None:
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="환불", primary="cs_ops"))
        precedents.flag_stale("환불", trigger_sha="sha-0", at=_TS)

        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=self._make_fake_audit(),
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 0

    def test_이미_invalidated_판례는_재적재_안_함(self) -> None:
        """T8.4(d): owner가 무효화(InvalidatePrecedent)한 판례는 OKF 커밋이 와도
        재평가 큐에 다시 안 올린다 — 무효화로 라우팅에서 뺀 걸 또 묻는 처리함 노이즈 방지.
        """
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="환불", primary="cs_ops"))
        precedents.invalidate("환불", by_owner="cs_lead", at=_TS)

        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=self._make_fake_audit(),
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        assert len(reeval_store.pending_for_owner("cs_lead")) == 0
        # 무효화는 그대로 보존(append-only·재표식 없음)
        invalidated = precedents.lookup("환불")
        assert invalidated is not None
        assert invalidated.invalidated is True
        assert invalidated.needs_review is False

    def test_다른_agent_판례는_영향_없음(self) -> None:
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="법무", primary="legal_ops"))
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=self._make_fake_audit(),
            reeval_store=reeval_store,
            owner_of=lambda _: "legal_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        assert len(reeval_store.pending_for_owner("legal_lead")) == 0

    def test_owner_of_None이면_빈_owner로_처리된다(self) -> None:
        """owner_of 콜백 미주입 시 안전 처리(예외 발생 X)."""
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="환불", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=self._make_fake_audit(),
            reeval_store=reeval_store,
            owner_of=None,
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items_empty = reeval_store.pending_for_owner("")
        assert len(items_empty) == 1
        assert items_empty[0].owner_id == ""

    def test_다수_판례_모두_적재된다(self) -> None:
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="환불", primary="cs_ops"))
        precedents.record(Resolution(intent="교환", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=self._make_fake_audit(),
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 2


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 6 — StalenessPropagator Answer 축
# ══════════════════════════════════════════════════════════════════════════════


def _make_audit_with_entries(entries: list[dict[str, Any]]) -> Any:
    """고정 entries dict를 돌려주는 최소 audit reader."""

    class _FixedAudit:
        def records(self) -> list[dict[str, Any]]:
            return entries

        def record_at(self, index: int) -> dict[str, Any] | None:
            if 0 <= index < len(entries):
                return entries[index]
            return None

    return _FixedAudit()


def _routed_record(
    agent_id: str = "cs_ops",
    owner: str = "cs_lead",
    snapshot_sha: str | None = "sha-old",
) -> dict[str, Any]:
    """실제 InMemoryAuditLog.records()가 돌려주는 구조의 mock."""
    answer: dict[str, Any] = {"text": "답변입니다.", "mode": "full", "sources": []}
    if snapshot_sha is not None:
        answer["snapshot_sha"] = snapshot_sha
    return {
        "timestamp": "2026-06-22T00:00:00+00:00",
        "user_id": "user1",
        "question": "환불 되나요?",
        "intent": "환불",
        "decision": {
            "disposition": "routed",
            "primary": agent_id,
            "owner": owner,
            "confidence": 0.9,
            "reason": "판례",
            "requires_approval": False,
            "collaborators": [],
        },
        "answer": answer,
        "dispatch": {"disposition": "delivered"},
    }


def _contested_record(agent_id: str = "cs_ops") -> dict[str, Any]:
    return {
        "timestamp": "2026-06-22T00:00:00+00:00",
        "user_id": "user1",
        "question": "환불?",
        "intent": "환불",
        "decision": {
            "disposition": "contested",
            "candidates": [agent_id],
            "reason": "후보 다수",
        },
        "answer": None,
        "dispatch": None,
    }


def _unowned_record() -> dict[str, Any]:
    return {
        "timestamp": "2026-06-22T00:00:00+00:00",
        "user_id": "user1",
        "question": "모르는 것?",
        "intent": "미분류",
        "decision": {
            "disposition": "unowned",
            "escalated_to": "root",
            "reason": "담당 없음",
        },
        "answer": None,
        "dispatch": None,
    }


class TestSlice6AnswerAxis:
    def _empty_precedents(self) -> InMemoryPrecedentStore:
        return InMemoryPrecedentStore(clock=_CLOCK)

    def test_routed_답_snapshot_sha_다르면_ReevalItem_적재된다(self) -> None:
        audit = _make_audit_with_entries(
            [_routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha-old")]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 1
        assert isinstance(items[0].subject, AnswerSubject)

    def test_routed_답_snapshot_sha_None이면_보수적_포함(self) -> None:
        """snapshot_sha 키 부재 답도 영향 대상 포함."""
        audit = _make_audit_with_entries(
            [_routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha=None)]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 1

    def test_routed_답_snapshot_sha_같으면_영향_아님(self) -> None:
        """snapshot_sha == new_sha면 최신 — 영향 아님."""
        audit = _make_audit_with_entries(
            [_routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha-new")]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 0

    def test_contested_disposition은_가드_skip된다(self) -> None:
        audit = _make_audit_with_entries([_contested_record(agent_id="cs_ops")])
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        assert reeval_store.pending_for_owner("cs_lead") == []

    def test_unowned_disposition은_가드_skip된다(self) -> None:
        audit = _make_audit_with_entries([_unowned_record()])
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        assert reeval_store.pending_for_owner("cs_lead") == []

    def test_다른_agent_routed_답은_영향_아님(self) -> None:
        audit = _make_audit_with_entries(
            [_routed_record(agent_id="legal_ops", owner="legal_lead", snapshot_sha="sha-old")]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        assert reeval_store.pending_for_owner("cs_lead") == []
        assert reeval_store.pending_for_owner("legal_lead") == []

    def test_AnswerSubject_audit_index가_audit_기록순_정수다(self) -> None:
        audit = _make_audit_with_entries(
            [
                _routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha-old"),
                _routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha-old"),
            ]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        subjects = [item.subject for item in reeval_store.pending_for_owner("cs_lead")]
        assert all(isinstance(s, AnswerSubject) for s in subjects)
        indices = sorted(s.audit_index for s in subjects if isinstance(s, AnswerSubject))
        assert indices == [0, 1]

    def test_owner_id가_rec_decision_owner에서_온다(self) -> None:
        audit = _make_audit_with_entries(
            [_routed_record(agent_id="cs_ops", owner="real_owner", snapshot_sha="sha-old")]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("real_owner")
        assert len(items) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 7 — owner 처리함 공존 + 1인칭 처분 + SupersedePrecedent→lookup
# ══════════════════════════════════════════════════════════════════════════════


class TestSlice7OwnerInbox:
    def test_pending_for_owner_세탭_공존(self) -> None:
        """ConflictCase·BackupReviewItem·ReevalItem이 각자 store에서 독립 조회된다."""
        from agent_org_network.review import (
            BackupReviewItem,
            InMemoryBackupReviewStore,
        )

        reeval_store = InMemoryReevalStore()
        backup_store = InMemoryBackupReviewStore()

        reeval_item = _reeval_item(owner_id="cs_lead", item_id="reeval-001")
        reeval_store.add(reeval_item)

        backup_item = BackupReviewItem(
            owner_id="cs_lead",
            agent_id="cs_ops",
            question="환불?",
            backup_answer_text="답",
            ticket_id="ticket-001",
            snapshot_at=_TS,
            answered_at=_TS,
            item_id="backup-001",
        )
        backup_store.add(backup_item)

        reeval_items = reeval_store.pending_for_owner("cs_lead")
        backup_items = backup_store.pending_for_owner("cs_lead")

        assert len(reeval_items) == 1
        assert len(backup_items) == 1
        assert reeval_items[0].item_id == "reeval-001"
        assert backup_items[0].item_id == "backup-001"

    def test_1인칭_처분_SupersedePrecedent_후_lookup_새_primary(self) -> None:
        """owner가 SupersedePrecedent 처분 → PrecedentStore.record 호출 → lookup 새 primary."""
        precedents = InMemoryPrecedentStore(clock=_CLOCK)
        precedents.record(Resolution(intent="환불", primary="cs_ops_old"))

        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(
            subject=PrecedentSubject(intent="환불"),
            owner_id="cs_lead",
            item_id="item-super",
        )
        store.add(item)

        supersede = SupersedePrecedent(by_owner="cs_lead", new_primary="cs_ops_new")
        result = service.review("item-super", supersede)

        assert result.status == "reviewed"
        assert isinstance(result.review, SupersedePrecedent)

        # SupersedePrecedent 처분 후 새 판례 기록 → lookup 새 primary
        precedents.record(Resolution(intent="환불", primary=result.review.new_primary))
        looked_up = precedents.lookup("환불")
        assert looked_up is not None
        assert looked_up.resolution.primary == "cs_ops_new"

    def test_Answered_노출에_신선도_필드_없음(self) -> None:
        """Answered(ask_org)에 needs_review·snapshot_sha 미노출 불변식."""
        from agent_org_network.ask_org import Answered

        ans = Answered(
            text="답변입니다.",
            answered_by=("cs_ops", "cs_lead"),
            mode="full",
            sources=("위키",),
        )
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(ans)}
        assert "needs_review" not in field_names
        assert "snapshot_sha" not in field_names

    def test_ReevalService_1인칭_후_mark_reviewed_store_반영(self) -> None:
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-002")
        store.add(item)
        _ = service.review("item-002", KeepPrecedent(by_owner="cs_lead"))
        assert store.pending_for_owner("cs_lead") == []
        result = store.get("item-002")
        assert result is not None
        assert result.status == "reviewed"


# ══════════════════════════════════════════════════════════════════════════════
# 결함 수정 — adversarial 리뷰 회귀 2건 (ADR 0019 결정 3 1인칭·결정 2 멱등)
# ══════════════════════════════════════════════════════════════════════════════


class TestDefect1ReviewedItemFirstPartyLeak:
    """수정 1: reviewed 항목에 타인 by_owner로 review 시 1인칭 검증이 선행되어야 한다."""

    def test_reviewed_항목에_타인_by_owner로_review하면_ValueError(self) -> None:
        """reviewed 상태라도 타인 by_owner는 ValueError — 권한 누설 방지."""
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-defect1")
        store.add(item)
        # 먼저 정당한 owner가 review해서 reviewed 상태로 만든다
        service.review("item-defect1", KeepPrecedent(by_owner="cs_lead"))
        # 이미 reviewed인 항목에 타인이 review 시도 → ValueError여야 한다 (현재 누설)
        with pytest.raises(ValueError):
            service.review("item-defect1", KeepPrecedent(by_owner="other_owner"))

    def test_이미_reviewed면_같은_owner는_멱등_통과(self) -> None:
        """reviewed 상태 + 같은 owner → 멱등(기존 동작 보존)."""
        store = InMemoryReevalStore()
        service = ReevalService(store)
        item = _reeval_item(owner_id="cs_lead", item_id="item-idempotent")
        store.add(item)
        first = service.review("item-idempotent", KeepPrecedent(by_owner="cs_lead"))
        second = service.review("item-idempotent", KeepPrecedent(by_owner="cs_lead"))
        assert first.status == "reviewed"
        assert second.status == "reviewed"


class TestDefect2AnswerAxisDuplicatePropagation:
    """수정 2: Answer 축 on_okf_committed 2회 호출 시 중복 적재 금지."""

    def _empty_precedents(self) -> InMemoryPrecedentStore:
        return InMemoryPrecedentStore(clock=_CLOCK)

    def test_answer_축_동일_okf_커밋_2회_호출_시_pending_1건(self) -> None:
        """같은 agent·같은 audit 답 대상으로 on_okf_committed 2회 → pending 1건."""
        audit = _make_audit_with_entries(
            [_routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha-old")]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 1, f"중복 적재: {len(items)}건"

    def test_precedent_축_2회_호출_시_pending_0건_유지(self) -> None:
        """Precedent 축은 needs_review 가드로 이미 멱등 — 2회 호출 시 1건 (기존 동작 보존)."""
        precedents = self._empty_precedents()
        precedents.record(Resolution(intent="환불", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()

        class _EmptyAudit:
            def records(self) -> list[dict[str, Any]]:
                return []

            def record_at(self, index: int) -> dict[str, Any] | None:
                return None

        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=_EmptyAudit(),
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 1, f"Precedent 축 중복: {len(items)}건"

    def test_서로_다른_subject_ref_답은_각각_적재된다(self) -> None:
        """다른 subject_ref(다른 audit 인덱스)는 각각 독립 적재 — 과소검출 금지."""
        audit = _make_audit_with_entries(
            [
                _routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha-old"),
                _routed_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha-old"),
            ]
        )
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=self._empty_precedents(),
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda _: "cs_lead",
            clock=_CLOCK,
        )
        propagator.on_okf_committed(_event(agent_id="cs_ops", new_sha="sha-new"))
        items = reeval_store.pending_for_owner("cs_lead")
        assert len(items) == 2, f"서로 다른 답이 각각 적재되어야 함: {len(items)}건"
