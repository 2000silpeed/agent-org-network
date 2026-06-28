"""build_demo 라우터 선택 seam(AON_ROUTER) 결정론 테스트 (Phase 10 라이브 슬라이스).

`select_router(flag, ...)` 분기와 *기본 무회귀*만 게이트 내에서 잠근다:
  - flag="index" → TwoStageRouter(인덱스 기반·OKF 시드).
  - 기타/미설정 → 기존 Router(분류기 기반·기본).
라이브 인덱스 라우팅(브라우저 분기 시연)은 수동(게이트 밖). 여기선 와이어 분기와
인덱스 경로가 실제로 RoutingDecision을 내는지(미아 없음·단일 Routed)만 결정론으로 본다.
"""

from __future__ import annotations

import pytest

from agent_org_network.ask_org import Answered, Pending
from agent_org_network.classifier import RuleBasedClassifier
from agent_org_network.conflict import InMemoryPrecedentStore
from agent_org_network.decision import Routed
from agent_org_network.demo import build_demo, select_router
from agent_org_network.registry import Registry
from agent_org_network.router import Router
from agent_org_network.runtime import StubRuntime
from agent_org_network.two_stage_router import TwoStageRouter
from agent_org_network.user import User

_USER = User(id="tester")


def _registry() -> Registry:
    """데모 카드/유저가 등록된 레지스트리 — build_demo가 노출하는 그 registry를 빌려온다."""
    return build_demo(runtime=StubRuntime()).registry


# ── select_router 분기(단위) ─────────────────────────────────────────────────


def test_select_router_index_flag_returns_two_stage_router() -> None:
    """flag="index" → TwoStageRouter."""
    reg = _registry()
    router = select_router("index", reg, RuleBasedClassifier({}), InMemoryPrecedentStore())
    assert isinstance(router, TwoStageRouter)


def test_select_router_default_returns_legacy_router() -> None:
    """미설정/빈/기타 플래그 → 기존 Router(분류기 기반·무회귀)."""
    reg = _registry()
    prec = InMemoryPrecedentStore()
    for flag in ("", "  ", "rule", "classifier", "unknown"):
        router = select_router(flag, reg, RuleBasedClassifier({}), prec)
        assert isinstance(router, Router)


def test_select_router_index_routes_via_index() -> None:
    """flag="index" 라우터가 인덱스 경로로 단일 담당을 Routed로 낸다.

    "환불" 질문은 cs_ops refund-policy concept(domain=환불) 단독 매칭 → Routed cs_ops.
    OKF 시드 인덱스가 실제로 라우팅에 쓰였다는 증거(미아 없음 보존).
    """
    reg = _registry()
    router = select_router("index", reg, RuleBasedClassifier({}), InMemoryPrecedentStore())
    decision = router.route("환불 규정 알려줘")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "cs_ops"
    assert decision.intent == "환불"


# ── build_demo 와이어(env 분기·기본 무회귀) ──────────────────────────────────


def test_build_demo_default_uses_legacy_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """AON_ROUTER 미설정 → 기존 Router(데모 무회귀). 키워드 라우팅이 그대로 동작."""
    monkeypatch.delenv("AON_ROUTER", raising=False)
    bundle = build_demo(runtime=StubRuntime())
    assert isinstance(bundle.ask._router, Router)  # pyright: ignore[reportPrivateUsage]
    reply = bundle.ask.handle("환불 규정 알려줘", _USER)
    assert isinstance(reply, Answered)
    assert reply.answered_by == ("cs_lead", "cs_ops")


def test_build_demo_index_flag_uses_two_stage_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AON_ROUTER=index → TwoStageRouter가 ask에 꽂혀 인덱스 기반으로 답한다.

    브라우저에서 보게 될 분기: "환불" 질문이 인덱스 경로로 cs_ops에 라우팅돼 답이 나온다.
    """
    monkeypatch.setenv("AON_ROUTER", "index")
    bundle = build_demo(runtime=StubRuntime())
    assert isinstance(bundle.ask._router, TwoStageRouter)  # pyright: ignore[reportPrivateUsage]
    reply = bundle.ask.handle("환불 규정 알려줘", _USER)
    assert isinstance(reply, Answered)
    assert reply.answered_by == ("cs_lead", "cs_ops")


def test_build_demo_index_flag_no_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    """인덱스 경로도 미아 없음: 매칭 0건 질문은 Pending(unowned)으로 종착."""
    monkeypatch.setenv("AON_ROUTER", "index")
    bundle = build_demo(runtime=StubRuntime())
    reply = bundle.ask.handle("점심 메뉴 추천해줘 오늘 날씨도", _USER)
    assert isinstance(reply, Pending)


def test_build_demo_index_flag_value_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AON_ROUTER 값은 strip+lower 정규화 — "INDEX"·" index "도 인덱스 경로."""
    monkeypatch.setenv("AON_ROUTER", "  INDEX  ")
    bundle = build_demo(runtime=StubRuntime())
    assert isinstance(bundle.ask._router, TwoStageRouter)  # pyright: ignore[reportPrivateUsage]
