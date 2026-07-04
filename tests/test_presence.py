"""Phase 12 S4 — 프레즌스 + HITL 정책 분기 (ADR 0033 결정 5, S1 shape 절).

`Presence` frozen 값 객체 + `PresenceTracker` 포트(Protocol) + `InMemoryPresenceTracker` +
`presence_to_hitl` 순수 함수 + `resolve_mode`의 프레즌스 결합(`resolve_mode_with_presence`).

불변식:
  - 노출 불변식: mode는 원래 노출하는 신뢰 상태값 — 프레즌스 결합이 노출 경계를 안 바꿈.
  - Authority 중앙: 프레즌스·HITL은 신뢰 게이트지 권한 자기보고 아님.
  - 전이 ≠ 기록: observe_connect/disconnect는 상태 갱신이지 도메인 전이 기록이 아님.
  - under-claim 단조성: 카드 approval_when이 요구하는 검토는 어떤 프레즌스에서도 안 풀린다
    (프레즌스는 조일 수만 있다 — 온라인→검토 상향, 오프라인이 카드의 draft_only를 못 품).
  - 미관측 agent_id는 offline 기본(ADR 0033 S1 shape).
  - grace 설정값(`AON_PRESENCE_GRACE_SECONDS`) 기본 0 — 즉시 오프라인 판정.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent_org_network.presence import (
    InMemoryPresenceTracker,
    Presence,
    presence_grace_seconds,
    presence_to_hitl,
    resolve_mode_with_presence,
)
from agent_org_network.runtime import AnswerMode


def _now() -> datetime:
    return datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


# ── Presence 값 객체 ──────────────────────────────────────────────────


def test_presence는_frozen_값_객체다():
    presence = Presence(agent_id="agent_finance", status="online", since=_now())
    assert presence.agent_id == "agent_finance"
    assert presence.status == "online"
    assert presence.since == _now()


def test_presence는_수정_불가():
    import pydantic

    presence = Presence(agent_id="agent_finance", status="online", since=_now())
    try:
        presence.status = "offline"  # type: ignore[misc]
    except (pydantic.ValidationError, AttributeError, TypeError):
        pass
    else:
        raise AssertionError("frozen 값 객체가 수정을 허용함")


# ── PresenceTracker 포트 + InMemory 구현 ──────────────────────────────


def test_미관측_agent는_offline_기본():
    tracker = InMemoryPresenceTracker()
    assert tracker.status("agent_unknown") == "offline"


def test_observe_connect_후_online():
    tracker = InMemoryPresenceTracker()
    tracker.observe_connect("agent_finance", at=_now())
    assert tracker.status("agent_finance") == "online"


def test_observe_disconnect_후_offline():
    tracker = InMemoryPresenceTracker()
    tracker.observe_connect("agent_finance", at=_now())
    tracker.observe_disconnect("agent_finance", at=_now() + timedelta(seconds=10))
    assert tracker.status("agent_finance") == "offline"


def test_에이전트별_독립_추적():
    tracker = InMemoryPresenceTracker()
    tracker.observe_connect("agent_finance", at=_now())
    assert tracker.status("agent_finance") == "online"
    assert tracker.status("agent_hr") == "offline"


def test_재연결_시_online으로_갱신():
    tracker = InMemoryPresenceTracker()
    t0 = _now()
    tracker.observe_connect("agent_finance", at=t0)
    tracker.observe_disconnect("agent_finance", at=t0 + timedelta(seconds=5))
    assert tracker.status("agent_finance") == "offline"
    tracker.observe_connect("agent_finance", at=t0 + timedelta(seconds=20))
    assert tracker.status("agent_finance") == "online"


# ── presence_to_hitl 순수 함수 ────────────────────────────────────────


def test_online이면_사전_검토_True():
    assert presence_to_hitl("online") is True


def test_offline이면_자동_발신_False():
    assert presence_to_hitl("offline") is False


# ── resolve_mode_with_presence — 프레즌스 결합(기존 resolve_mode 입력 확장) ──


def test_온라인_토글off_카드approval_False_이면_full():
    """온라인 + hitl 토글 off + 카드 approval 없음 → full(자동 아님·온라인 자체가 검토를 강제하지 않음).

    ADR 0033 결정 5: 온라인=사전 검토는 "hitl_on 판단의 입력"이 아니라 프레즌스가 hitl_on을
    보강(OR)하는 방식 — presence_to_hitl(online)=True가 hitl_on OR에 들어가 draft_only 상향.
    """
    result = resolve_mode_with_presence(
        requires_approval=False,
        hitl_on=False,
        presence_status="online",
        current_mode="full",
    )
    assert result == "draft_only"


def test_오프라인_토글off_카드approval_False_이면_full_자동발신():
    result = resolve_mode_with_presence(
        requires_approval=False,
        hitl_on=False,
        presence_status="offline",
        current_mode="full",
    )
    assert result == "full"


def test_오프라인이라도_카드approval_True면_draft_only_유지():
    """under-claim 단조성 — 카드가 조인 검토는 오프라인이라도 못 풀린다."""
    result = resolve_mode_with_presence(
        requires_approval=True,
        hitl_on=False,
        presence_status="offline",
        current_mode="full",
    )
    assert result == "draft_only"


def test_오프라인이라도_토글on이면_draft_only_유지():
    result = resolve_mode_with_presence(
        requires_approval=False,
        hitl_on=True,
        presence_status="offline",
        current_mode="full",
    )
    assert result == "draft_only"


def test_온라인_토글on_카드approval_True_이면_draft_only():
    result = resolve_mode_with_presence(
        requires_approval=True,
        hitl_on=True,
        presence_status="online",
        current_mode="full",
    )
    assert result == "draft_only"


def test_backup은_프레즌스와_무관하게_덮지_않는다():
    result = resolve_mode_with_presence(
        requires_approval=True,
        hitl_on=True,
        presence_status="online",
        current_mode="backup",
    )
    assert result == "backup"


def test_오프라인_자동발신_사후교정_플래그가_선다():
    """오프라인 자동 발신 경로는 '사후 교정 대상' 표식을 남긴다(플래그만 — 소비는 S5 몫)."""
    result, needs_correction_review = resolve_mode_with_presence(
        requires_approval=False,
        hitl_on=False,
        presence_status="offline",
        current_mode="full",
        return_flag=True,
    )
    assert result == "full"
    assert needs_correction_review is True


def test_온라인_경로는_사후교정_플래그가_안_선다():
    result, needs_correction_review = resolve_mode_with_presence(
        requires_approval=False,
        hitl_on=False,
        presence_status="online",
        current_mode="full",
        return_flag=True,
    )
    assert result == "draft_only"
    assert needs_correction_review is False


def test_draft_only로_귀결되면_오프라인이어도_사후교정_플래그_안_선다():
    """자동 발신되지 않았으면(검토 대기) 사후 교정 대상이 아니다 — 플래그는 full 자동발신 전용."""
    result, needs_correction_review = resolve_mode_with_presence(
        requires_approval=True,
        hitl_on=False,
        presence_status="offline",
        current_mode="full",
        return_flag=True,
    )
    assert result == "draft_only"
    assert needs_correction_review is False


# ── 단조성 매트릭스 — 온라인/오프라인 × 토글 × 카드 approval_when 전 조합 ──


def test_단조성_매트릭스_카드approval_True는_모든_프레즌스_토글_조합에서_draft_only():
    for presence_status in ("online", "offline"):
        for hitl_on in (True, False):
            result = resolve_mode_with_presence(
                requires_approval=True,
                hitl_on=hitl_on,
                presence_status=presence_status,  # type: ignore[arg-type]
                current_mode="full",
            )
            assert result == "draft_only", (presence_status, hitl_on)


def test_단조성_매트릭스_카드approval_False_온라인은_항상_draft_only():
    for hitl_on in (True, False):
        result = resolve_mode_with_presence(
            requires_approval=False,
            hitl_on=hitl_on,
            presence_status="online",
            current_mode="full",
        )
        assert result == "draft_only", hitl_on


def test_단조성_매트릭스_카드approval_False_오프라인_토글on만_draft_only():
    result_on = resolve_mode_with_presence(
        requires_approval=False,
        hitl_on=True,
        presence_status="offline",
        current_mode="full",
    )
    result_off = resolve_mode_with_presence(
        requires_approval=False,
        hitl_on=False,
        presence_status="offline",
        current_mode="full",
    )
    assert result_on == "draft_only"
    assert result_off == "full"


# ── grace 설정값 경계 (AON_PRESENCE_GRACE_SECONDS, 기본 0) ────────────


def test_grace_설정값_기본은_0():
    assert presence_grace_seconds() == 0


def test_grace_설정값_env로_조정_가능(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_PRESENCE_GRACE_SECONDS", "30")
    assert presence_grace_seconds() == 30


def test_grace_설정값_빈문자열은_기본값_0(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_PRESENCE_GRACE_SECONDS", "")
    assert presence_grace_seconds() == 0


def test_grace_0이면_연결끊김_즉시_offline():
    """MVP 기본 — grace=0이므로 disconnect 순간 바로 offline 판정(InMemoryPresenceTracker에 grace 미적용)."""
    tracker = InMemoryPresenceTracker()
    t0 = _now()
    tracker.observe_connect("agent_finance", at=t0)
    tracker.observe_disconnect("agent_finance", at=t0)
    assert tracker.status("agent_finance") == "offline"


# ── AnswerMode 노출 불변식 ─────────────────────────────────────────────


def test_resolve_mode_with_presence_반환값이_AnswerMode_멤버다():
    valid: tuple[AnswerMode, ...] = ("draft_only", "full", "backup")
    for presence_status in ("online", "offline"):
        for hitl_on in (True, False):
            for requires_approval in (True, False):
                result = resolve_mode_with_presence(
                    requires_approval=requires_approval,
                    hitl_on=hitl_on,
                    presence_status=presence_status,  # type: ignore[arg-type]
                    current_mode="full",
                )
                assert result in valid
