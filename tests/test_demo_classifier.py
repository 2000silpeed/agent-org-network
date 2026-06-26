"""build_demo 분류기 주입 seam 결정론 테스트 (정교한 분류기 배선·AON_CLASSIFIER).

`build_demo(classifier=)` 주입 우선 / env(`AON_CLASSIFIER=llm`) / 기본 RuleBased 순.
env=llm 경로(실 claude Haiku)는 비결정·외부 의존이라 게이트 밖(라이브 검증) — 여기선
*주입 seam*과 *기본값*만 결정론으로 잠근다. 분류는 `router.route`에서 질문당 1회뿐이므로
(ADR 0015 단일 출처·ask_org는 `decision.intent` 재사용) 분류기 교체가 안전하다.
"""

from __future__ import annotations

import pytest

from agent_org_network.ask_org import Answered, Pending
from agent_org_network.classifier import FakeClassifier
from agent_org_network.demo import build_demo
from agent_org_network.runtime import StubRuntime
from agent_org_network.user import User

_USER = User(id="tester")
_NO_KEYWORD = "키워드가 전혀 없는 그냥 아무런 문장입니다"


def test_주입한_분류기가_라우터에_쓰인다() -> None:
    """build_demo(classifier=)로 주입한 분류기가 기본 RuleBased를 대체해 라우팅에 쓰인다.

    FakeClassifier가 키워드와 무관하게 항상 '환불'을 내므로, 키워드 없는 문장도 cs_ops로
    라우팅된다 — 주입 분류기가 실제로 라우터에 꽂혔다는 증거(기본 RuleBased면 미아였을 것).
    """
    bundle = build_demo(runtime=StubRuntime(), classifier=FakeClassifier("환불"))
    reply = bundle.ask.handle(_NO_KEYWORD, _USER)
    assert isinstance(reply, Answered)
    assert reply.answered_by == ("cs_lead", "cs_ops")


def test_기본_분류기는_RuleBased_키워드없으면_미아(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """주입·env 없으면 기본 RuleBased — 키워드 없는 질문은 0매칭 → Pending(미아 없음).

    `AON_CLASSIFIER`를 명시적으로 비워(env 누수 방어) 기본 경로를 고정한다.
    """
    monkeypatch.delenv("AON_CLASSIFIER", raising=False)
    bundle = build_demo(runtime=StubRuntime())
    reply = bundle.ask.handle(_NO_KEYWORD, _USER)
    assert isinstance(reply, Pending)


def test_env_미설정과_빈값은_기본_RuleBased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AON_CLASSIFIER`가 빈 문자열/공백이면 llm이 아니라 기본 RuleBased로 떨어진다."""
    monkeypatch.setenv("AON_CLASSIFIER", "   ")
    bundle = build_demo(runtime=StubRuntime())
    # 키워드 있는 질문은 정상 라우팅(RuleBased가 살아있음 — env 빈값이 llm으로 오인 안 됨)
    reply = bundle.ask.handle("환불 규정 알려줘", _USER)
    assert isinstance(reply, Answered)
    assert reply.answered_by == ("cs_lead", "cs_ops")
