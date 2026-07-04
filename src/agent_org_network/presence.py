"""프레즌스 + HITL 정책 분기 — Presence 값 객체·PresenceTracker 포트·presence_to_hitl 순수 함수
(Phase 12 S4, ADR 0033 결정 5·S1 shape 절).

새 기계가 아니라 기존 `resolve_mode`(ADR 0025)의 입력 확장이다:
  - 온라인 = 사전 검토(draft_only) — 프레즌스가 hitl_on을 OR로 보강해 상향.
  - 오프라인 = 자동 발신(full) + 사후 교정 플래그(정정 대상 표식 — 소비는 S5 몫).

불변식:
  - under-claim 단조성(ADR 0025 유지): 카드 approval_when이 요구하는 검토는 어떤
    프레즌스에서도 안 풀린다. 프레즌스는 조일 수만 있다(온라인→검토 상향).
  - 미관측 agent_id는 offline 기본(HitlToggleMap 정신 — 관측 전엔 안전측 기본값).
  - 전이 ≠ 기록: observe_connect/disconnect는 연결 상태 갱신이지 도메인 전이 기록이 아니다.
  - Authority 중앙: 프레즌스·HITL은 신뢰 게이트이지 권한(Authority) 자기보고가 아니다.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Literal, Protocol, overload

from pydantic import BaseModel

from agent_org_network.hitl import resolve_mode
from agent_org_network.runtime import AnswerMode

PresenceStatus = Literal["online", "offline"]


class Presence(BaseModel, frozen=True):
    """담당자 워커 연결 상태 — 1급 개념(ADR 0033 결정 5)."""

    agent_id: str
    status: PresenceStatus
    since: datetime


class PresenceTracker(Protocol):
    """워커 WS 연결/해제를 기록·조회하는 포트(HitlToggleMap 정신의 in-memory 상태 그릇)."""

    def observe_connect(self, agent_id: str, *, at: datetime) -> None: ...

    def observe_disconnect(self, agent_id: str, *, at: datetime) -> None: ...

    def status(self, agent_id: str) -> PresenceStatus: ...


class InMemoryPresenceTracker:
    """in-memory 프레즌스 추적기 — 미관측 agent_id는 offline 기본."""

    def __init__(self) -> None:
        self._state: dict[str, Presence] = {}
        self._lock = threading.Lock()

    def observe_connect(self, agent_id: str, *, at: datetime) -> None:
        with self._lock:
            self._state[agent_id] = Presence(agent_id=agent_id, status="online", since=at)

    def observe_disconnect(self, agent_id: str, *, at: datetime) -> None:
        with self._lock:
            self._state[agent_id] = Presence(agent_id=agent_id, status="offline", since=at)

    def status(self, agent_id: str) -> PresenceStatus:
        with self._lock:
            presence = self._state.get(agent_id)
            return presence.status if presence is not None else "offline"


def presence_to_hitl(status: PresenceStatus) -> bool:
    """프레즌스→HITL 검토 판정 순수 함수(ADR 0033 결정 5) — online이면 사전 검토."""
    return status == "online"


def presence_grace_seconds() -> int:
    """`AON_PRESENCE_GRACE_SECONDS` 설정값(기본 0 — 연결 끊김 즉시 오프라인 판정)."""
    raw = (os.environ.get("AON_PRESENCE_GRACE_SECONDS") or "").strip()
    return int(raw) if raw else 0


@overload
def resolve_mode_with_presence(
    *,
    requires_approval: bool,
    hitl_on: bool,
    presence_status: PresenceStatus,
    current_mode: AnswerMode,
    return_flag: Literal[False] = False,
) -> AnswerMode: ...


@overload
def resolve_mode_with_presence(
    *,
    requires_approval: bool,
    hitl_on: bool,
    presence_status: PresenceStatus,
    current_mode: AnswerMode,
    return_flag: Literal[True],
) -> tuple[AnswerMode, bool]: ...


def resolve_mode_with_presence(
    *,
    requires_approval: bool,
    hitl_on: bool,
    presence_status: PresenceStatus,
    current_mode: AnswerMode,
    return_flag: bool = False,
) -> AnswerMode | tuple[AnswerMode, bool]:
    """프레즌스를 기존 `resolve_mode`의 입력으로 결합한다(ADR 0033 결정 5 — 새 기계 0).

    `hitl_on OR presence_to_hitl(presence_status)`로 토글 입력을 확장해 기존
    `resolve_mode`(under-claim 단조성·backup 우선순위)에 위임한다.

    `return_flag=True`이면 (mode, needs_correction_review) 튜플을 반환한다.
    `needs_correction_review`는 오프라인이면서 결과가 "full"(자동 발신)로 귀결된
    경우에만 True — 사후 교정 대상 표식(플래그만, 소비는 S5 몫).
    """
    effective_hitl_on = hitl_on or presence_to_hitl(presence_status)
    mode = resolve_mode(
        requires_approval=requires_approval,
        hitl_on=effective_hitl_on,
        current_mode=current_mode,
    )
    if not return_flag:
        return mode
    needs_correction_review = presence_status == "offline" and mode == "full"
    return mode, needs_correction_review
