"""HITL 런타임 토글 — 에이전트별 on/off 상태 + →mode 매핑 순수 로직 (T9.3(a), ADR 0025).

새 기계가 아니라 기존 draft_only/Approval 재사용:
  - hitl_on → mode="draft_only"(owner 검토·수정·전송 대기)
  - hitl_off → mode="full"(자동 전송)

불변식:
  - mode는 원래 노출하는 신뢰 상태값(Answer 절) — HITL 토글이 값을 바꿔도 노출 경계 그대로.
  - backup은 draft_only로 덮지 않는다(backup이 더 강한 하향).
  - under-claim 단조성: requires_approval이 True면 hitl_on=False여도 draft_only 유지.
    운영자는 조이기(상향)만 가능, 카드 approval 정책 완화는 불가.
  - 전이≠기록: 토글 변경은 운영 설정 set이지 도메인 전이 아님.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_org_network.runtime import AnswerMode

if TYPE_CHECKING:
    from agent_org_network.agent_card import AgentCard


def hitl_to_mode(hitl_on: bool) -> AnswerMode:
    """HITL 토글 상태를 Answer mode로 매핑하는 순수 함수(ADR 0025 결정 2)."""
    return "draft_only" if hitl_on else "full"


def resolve_mode(
    *,
    requires_approval: bool,
    hitl_on: bool,
    current_mode: AnswerMode,
) -> AnswerMode:
    """라우팅 approval + HITL 토글 OR 결합으로 최종 mode를 결정한다(순수 함수).

    우선순위(ADR 0025 결정 2·ADR 0012):
      - backup은 draft_only로 덮지 않는다(backup > draft_only).
      - requires_approval OR hitl_on 중 하나라도 True면 draft_only로 격상.
      - 둘 다 False면 current_mode 그대로(full 또는 이미 draft_only).
    """
    if current_mode == "backup":
        return "backup"
    if requires_approval or hitl_on:
        return "draft_only"
    return current_mode


def seed_from_card(card: "AgentCard") -> bool:
    """카드 approval_when 정책에서 HITL 기본값을 시드한다(ADR 0025 결정 1).

    approval_when이 하나라도 있으면 HITL on(True), 없으면 off(False).
    under-claim 자기보고(ADR 0004)가 기본값을 정하고 운영자가 런타임에 덮어쓴다.
    """
    return bool(card.approval_when)


class HitlToggleMap:
    """에이전트별 HITL on/off 상태를 보관하는 in-memory 토글 맵.

    ADR 0025 Consequences: in-memory 토글 맵(agent_id → bool)이 MVP.
    런타임에 바뀌므로 정적 상수는 아니되, durable·전이 기록은 불요.
    콘솔이 set, 답 생성이 read.
    """

    def __init__(self) -> None:
        self._state: dict[str, bool] = {}

    def is_on(self, agent_id: str) -> bool:
        return self._state.get(agent_id, False)

    def set(self, agent_id: str, on: bool) -> None:
        self._state[agent_id] = on
