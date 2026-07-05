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
    """담당자 워커 연결 상태 — 1급 개념(ADR 0033 결정 5).

    주의(크로스머신 시연 실결함 4호): `agent_id` 필드명이지만 실제로 담기는 값은
    **담당자(owner) 식별자**다 — 프레즌스는 owner PC의 연결 상태이지 Agent Card 단위가
    아니다(`transport.py`의 `observe_connect(frame.owner_id)`/`observe_disconnect(owner_id)`
    참고 — 어느 워커든 그 owner가 붙어 있으면 온라인). 조회도 owner 키로 해야 한다 —
    agent_id로 조회하면 트래커에 없는 키라 항상 offline로 폴백해 오탐이 난다. 필드명
    rename은 과한 리팩터라 보류(백로그).
    """

    agent_id: str
    status: PresenceStatus
    since: datetime


class PresenceTracker(Protocol):
    """워커 WS 연결/해제를 기록·조회하는 포트(HitlToggleMap 정신의 in-memory 상태 그릇).

    `agent_id` 매개변수명은 호출 관례상 유지하지만 실제로 넘기는 값은 owner 식별자다
    (`Presence` docstring 참고). 담당 owner의 프레즌스 조회는 반드시 owner 키로 한다.
    """

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


# ── PresenceEvent + PresenceLogStore — 프레즌스 이력 (Phase 13 SC2, ADR 0035·
#    TRD §4 스코어카드 shape 절) ──────────────────────────────────────────
#
# 현재 `Presence`(상태 그릇 — owner별 *현재* 상태 하나만 덮어씀)와 구분되는
# *온라인 비율 계산 원천*. 전용 스토어 채택(감사 로그와 별 축 — 감사는 사람이
# 읽는 절차 이력, 이 이력은 계산 원천이라 `SqliteRegistryJournal`이 감사와 별
# 축인 정신). 키는 owner_id로 정직하게 명명(`Presence`의 필드명 agent_id·값
# owner_id인 부채를 이 신설 타입은 안 물려받는다).


class PresenceEvent(BaseModel, frozen=True):
    """프레즌스 connect/disconnect 1건 — append-only 이력의 원소."""

    owner_id: str
    status: PresenceStatus
    at: datetime


class PresenceLogStore(Protocol):
    """`PresenceEvent` append-only 보관·조회 포트."""

    def append(self, event: PresenceEvent) -> None: ...

    def for_owner(self, owner_id: str) -> list[PresenceEvent]: ...


class InMemoryPresenceLogStore:
    """in-memory `PresenceLogStore` — append-only, 삽입 순서(시간순) 보존."""

    def __init__(self) -> None:
        self._by_owner: dict[str, list[PresenceEvent]] = {}
        self._lock = threading.Lock()

    def append(self, event: PresenceEvent) -> None:
        with self._lock:
            self._by_owner.setdefault(event.owner_id, []).append(event)

    def for_owner(self, owner_id: str) -> list[PresenceEvent]:
        with self._lock:
            return list(self._by_owner.get(owner_id, []))


def online_ratio(
    events: list[PresenceEvent], *, since: datetime, until: datetime
) -> float | None:
    """`[since, until)` 구간의 온라인 시간 / 기간 길이 — connect/disconnect 이력 구간 적분.

    경계 처리:
      - **미관측**(`events`가 비어 있음)이면 `None`(0.0과 구분 — 판정 불가. `Presence`의
        미관측=offline 안전 기본과 달리, 여기선 "관측 자체가 없다"를 정직하게 표기한다).
      - **since 이전 마지막 상태**를 구간 시작(`since`)의 초기 상태로 채택한다(그 이전부터
        연결/해제 상태였던 것이 그대로 이어짐). since 이전 이벤트가 전혀 없으면 초기 상태는
        offline(안전측 기본 — 관측 전 구간은 온라인으로 셀 근거가 없다).
      - **미해제 열린 구간**: 구간 내 마지막 이벤트가 online이고 그 뒤로 disconnect가 없으면
        `until`까지 온라인으로 셈한다.
      - **재연결 반복**: online↔offline이 여러 번 바뀌어도 각 구간을 그대로 합산한다.
      - **online→online 중복(재관측)**: 상태 변화가 없으므로 구간 경계에 영향 없음(멱등).

    기간 길이가 0 이하(`until <= since`)면 `None`(구간 자체가 무의미).
    """
    if not events:
        return None
    duration = (until - since).total_seconds()
    if duration <= 0:
        return None

    ordered = sorted(events, key=lambda e: e.at)

    # since 시점의 초기 상태 — since 이전(또는 같은 시각) 마지막 이벤트를 채택, 없으면 offline.
    status: PresenceStatus = "offline"
    for event in ordered:
        if event.at <= since:
            status = event.status
        else:
            break

    online_seconds = 0.0
    cursor = since
    for event in ordered:
        if event.at <= since:
            continue
        if event.at >= until:
            break
        if status == "online":
            online_seconds += (event.at - cursor).total_seconds()
        cursor = event.at
        status = event.status

    if status == "online":
        online_seconds += (until - cursor).total_seconds()

    return online_seconds / duration
