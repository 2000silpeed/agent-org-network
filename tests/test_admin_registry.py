"""관리 UI 도메인 — 라이브 카드 등록 + 오너 변경 전이(Phase 12 3라운드·ADR 0034).

결정론 단위 테스트:
1. admit_card — 유효/무효 카드 admission 판정(우회 없음).
2. register_card — 라이브 반영·중복 거부·무효 거부·감사 기록.
3. transfer_ownership — 재-admission·스위치·구 owner 토큰 revoke·감사·WS 끊기.
4. 라우팅 반영 — 라이브 등록/전이가 다음 route에 즉시 잡힘.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from agent_org_network.admin_registry import (
    AdminRegistryService,
    AdmissionError,
    CardCandidate,
    DuplicateCardError,
    UnknownCardError,
    admit_card,
)
from agent_org_network.agent_card import AgentCard
from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.registry import Registry
from agent_org_network.token import InMemoryTokenStore
from agent_org_network.user import User

_DATE = "2026-06-20"
_NOW = datetime(2026, 7, 4, tzinfo=timezone.utc)


def _clock() -> datetime:
    return _NOW


def _registry() -> Registry:
    reg = Registry()
    reg.register_user(User(id="root_mgr"))
    reg.register_user(User(id="alice", manager="root_mgr"))
    reg.register_user(User(id="bob", manager="root_mgr"))
    reg.register(
        AgentCard(
            agent_id="cs_ops",
            owner="alice",
            team="cs",
            summary="환불 안내",
            domains=["환불"],
            last_reviewed_at=date(2026, 6, 20),
        )
    )
    reg.validate()
    return reg


def _candidate(**kwargs: object) -> CardCandidate:
    defaults: dict[str, object] = {
        "agent_id": "new_ops",
        "owner": "bob",
        "team": "new",
        "summary": "새 담당",
        "domains": ["신규"],
        "last_reviewed_at": _DATE,
    }
    defaults.update(kwargs)
    return CardCandidate(**defaults)  # type: ignore[arg-type]


class TestAdmitCard:
    def test_유효_카드_통과(self) -> None:
        card, errors = admit_card(_candidate(), _registry())
        assert card is not None
        assert errors == []
        assert card.owner == "bob"

    def test_미등록_owner_거부(self) -> None:
        card, errors = admit_card(_candidate(owner="ghost"), _registry())
        assert card is None
        assert any("미등록 owner" in e for e in errors)

    def test_빈_agent_id_거부(self) -> None:
        card, _errors = admit_card(_candidate(agent_id="  "), _registry())
        assert card is None

    def test_잘못된_agent_id_형식_거부(self) -> None:
        card, errors = admit_card(_candidate(agent_id="_bad start"), _registry())
        assert card is None
        assert errors

    def test_미등록_maintainer_거부(self) -> None:
        card, errors = admit_card(_candidate(maintainer="ghost"), _registry())
        assert card is None
        assert any("maintainer" in e for e in errors)


class TestRegisterCard:
    def test_라이브_반영(self) -> None:
        reg = _registry()
        svc = AdminRegistryService(reg, clock=_clock)
        card = svc.register_card(_candidate(), by="root_mgr")
        assert card.agent_id == "new_ops"
        assert reg.has_card("new_ops")
        assert reg.get("new_ops").owner == "bob"

    def test_무효_카드_등록_안됨(self) -> None:
        reg = _registry()
        svc = AdminRegistryService(reg, clock=_clock)
        try:
            svc.register_card(_candidate(owner="ghost"), by="root_mgr")
            assert False, "AdmissionError가 나야 한다"
        except AdmissionError as exc:
            assert exc.errors
        assert not reg.has_card("new_ops")

    def test_중복_agent_id_거부(self) -> None:
        reg = _registry()
        svc = AdminRegistryService(reg, clock=_clock)
        try:
            svc.register_card(_candidate(agent_id="cs_ops"), by="root_mgr")
            assert False, "DuplicateCardError가 나야 한다"
        except DuplicateCardError:
            pass

    def test_감사_기록_남는다(self) -> None:
        reg = _registry()
        audit = InMemoryAuditLog()
        svc = AdminRegistryService(reg, audit_sink=audit, clock=_clock)
        svc.register_card(_candidate(), by="root_mgr")
        recs = audit.records()
        assert len(recs) == 1
        assert recs[0]["action"]["kind"] == "CardRegistered"
        assert recs[0]["action"]["subject_id"] == "new_ops"
        assert recs[0]["action"]["by"] == "root_mgr"


class TestTransferOwnership:
    def _transfer_candidate(self, owner: str) -> CardCandidate:
        return CardCandidate(
            agent_id="cs_ops",
            owner=owner,
            team="cs",
            summary="환불 안내",
            domains=["환불"],
            last_reviewed_at=_DATE,
        )

    def test_스위치_owner_교체(self) -> None:
        reg = _registry()
        svc = AdminRegistryService(reg, clock=_clock)
        result = svc.transfer_ownership(self._transfer_candidate("bob"), by="root_mgr")
        assert reg.get("cs_ops").owner == "bob"
        assert result.from_owner == "alice"
        assert result.to_owner == "bob"
        assert result.agent_id == "cs_ops"

    def test_agent_id_불변(self) -> None:
        reg = _registry()
        svc = AdminRegistryService(reg, clock=_clock)
        svc.transfer_ownership(self._transfer_candidate("bob"), by="root_mgr")
        assert reg.has_card("cs_ops")
        assert len(reg.all_cards()) == 1

    def test_무효_새owner_스위치_없음(self) -> None:
        reg = _registry()
        svc = AdminRegistryService(reg, clock=_clock)
        try:
            svc.transfer_ownership(self._transfer_candidate("ghost"), by="root_mgr")
            assert False
        except AdmissionError:
            pass
        assert reg.get("cs_ops").owner == "alice"

    def test_미존재_카드_전이_거부(self) -> None:
        reg = _registry()
        svc = AdminRegistryService(reg, clock=_clock)
        cand = CardCandidate(
            agent_id="ghost_ops",
            owner="bob",
            team="x",
            summary="x",
            domains=["신규"],
            last_reviewed_at=_DATE,
        )
        try:
            svc.transfer_ownership(cand, by="root_mgr")
            assert False
        except UnknownCardError:
            pass

    def test_구_owner_토큰_revoke(self) -> None:
        reg = _registry()
        tokens = InMemoryTokenStore(clock=_clock)
        raw_alice, tok_alice = tokens.issue("alice", "primary", now=_NOW)
        raw_bob, _tok_bob = tokens.issue("bob", "primary", now=_NOW)
        svc = AdminRegistryService(reg, token_store=tokens, clock=_clock)
        result = svc.transfer_ownership(self._transfer_candidate("bob"), by="root_mgr")
        # 구 owner(alice) 토큰은 revoke → verify None.
        assert tokens.verify(raw_alice, now=_NOW) is None
        assert tok_alice.token_id in result.revoked_token_ids
        # 새 owner(bob) 토큰은 그대로 살아 있다.
        assert tokens.verify(raw_bob, now=_NOW) is not None

    def test_WS_세션_끊기_호출(self) -> None:
        reg = _registry()
        disconnected: list[str] = []
        svc = AdminRegistryService(
            reg, disconnect_owner=disconnected.append, clock=_clock
        )
        svc.transfer_ownership(self._transfer_candidate("bob"), by="root_mgr")
        assert disconnected == ["alice"]

    def test_감사_ownership_transfer_기록(self) -> None:
        reg = _registry()
        audit = InMemoryAuditLog()
        svc = AdminRegistryService(reg, audit_sink=audit, clock=_clock)
        result = svc.transfer_ownership(self._transfer_candidate("bob"), by="root_mgr")
        recs = audit.records()
        assert recs[-1]["action"]["kind"] == "OwnershipTransfer"
        assert recs[-1]["action"]["from_owner"] == "alice"
        assert recs[-1]["action"]["to_owner"] == "bob"
        assert result.audit_index == len(recs) - 1


class TestRoutingReflectsLive:
    def test_라이브_등록_라우팅_즉시_반영(self) -> None:
        from agent_org_network.classifier import RuleBasedClassifier
        from agent_org_network.router import Router
        from agent_org_network.decision import Routed, Unowned

        reg = _registry()
        classifier = RuleBasedClassifier({"신규": "신규"})
        router = Router(reg, classifier, root_user="root_mgr")
        # 등록 전: 담당 없음 → Unowned.
        assert isinstance(router.route("신규"), Unowned)
        svc = AdminRegistryService(reg, clock=_clock)
        svc.register_card(_candidate(), by="root_mgr")
        # 등록 후: 같은 router 인스턴스가 라이브 카드를 즉시 잡는다(재색인 불요).
        decision = router.route("신규")
        assert isinstance(decision, Routed)
        assert decision.primary.agent_id == "new_ops"

    def test_오너_변경_라우팅_새_owner_반영(self) -> None:
        from agent_org_network.classifier import RuleBasedClassifier
        from agent_org_network.router import Router
        from agent_org_network.decision import Routed

        reg = _registry()
        classifier = RuleBasedClassifier({"환불": "환불"})
        router = Router(reg, classifier, root_user="root_mgr")
        svc = AdminRegistryService(reg, clock=_clock)
        svc.transfer_ownership(
            CardCandidate(
                agent_id="cs_ops",
                owner="bob",
                team="cs",
                summary="환불 안내",
                domains=["환불"],
                last_reviewed_at=_DATE,
            ),
            by="root_mgr",
        )
        decision = router.route("환불")
        assert isinstance(decision, Routed)
        assert decision.primary.owner == "bob"
