"""S1 — Confluence 오너십 부트스트랩 도출 (도메인 shape·ADR 0039 결정 4).

Confluence 오너십 메타데이터(스페이스 관리자·페이지 작성자/기여자·라벨)를 입력으로
**Agent Card 후보 + 후보 domains**를 도출하는 순수 함수·값 객체.

절대 불변식(ADR 0039 결정 4 — 이 모듈이 지켜야 함):
  1. 후보 생성일 뿐, 권한 생성이 아니다. 도출된 domains는 "제안"이지 권한이 아니다.
     권한(Authority)은 여전히 중앙 선언(`routing_rules.yaml`·`card.domains`)이 정한다(ADR 0004).
  2. over-claim은 중앙 수용에서 필터. 넓은 스페이스 → 넓은 후보 domain이 나와도, publish
     수용 시 `domain_authorized`(concept.domain ∈ card.domains·ADR 0028 §14 결정 D)와
     카드 admission(ADR 0023)이 떨군다. 이 모듈은 domain을 *좁히지 않는다*(거짓 정밀 회피) —
     제안만 하고, 좁힘은 중앙이 한다.
  3. 등록 무결성 보존. `AgentCardCandidate`는 `AgentCard`가 *아니다*. team·last_reviewed_at을
     의도적으로 담지 않아 owner 검토(팀·검토일 기입·domains 확인·agent_id 확정) 없이는 카드가
     될 수 없다 — admission 게이트(`validate_card_for_builder` → YAML → git → `Registry.load`)를
     구조적으로 강제한다. 부트스트랩이 경계를 우회하지 않는다.
  4. under-claim 자기보고 정합. Confluence 기여자라는 사실이 권한을 창설하지 않는다 —
     `proposed_owner`는 문자열 제안일 뿐, User 실재 검증은 이 순수 도출이 아니라 admission이 진다.

이 모듈은 IO·Registry·LLM에 의존하지 않는 순수 도출이다(결정론·게이트 테스트 가능).
구현 완료(green·2026-07-08).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, field_validator

from agent_org_network.agent_card import AGENT_ID_MAX_LENGTH, validate_agent_id_format

# space_key 정규화 시 남길 문자(그 외는 '-'로 치환) — wire-format 허용 문자와 동일
# (ADR 0023 정합 — `validate_agent_id_format`이 최종 관문으로 재검증한다).
_AGENT_ID_ALLOWED_CHAR_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]")


class ConfluenceOwnershipSignal(BaseModel, frozen=True):
    """Confluence 스페이스 하나의 오너십 메타데이터 스냅샷 — 부트스트랩 도출의 순수 입력.

    소규모 파일럿(스페이스 1~2·owner 3~5)에 맞춘 거친 단위: 한 스페이스의 오너십
    신호를 집계한 값. 페이지 단위 정밀화(페이지 트리·라벨 온톨로지)는 후속 연기(ADR 0039).

    이 값은 *권한이 아니라 신호*다(불변식 1). 어떤 필드도 권한을 선언하지 않는다.

    `page_contributors`는 MVP 도출(`derive_card_candidates`)에서 **미소비**다 —
    owner/domain 후보 계산에 쓰이지 않는다. provenance·후속 정밀화(페이지 단위
    기여도 가중 등)를 위한 자리로 필드만 보존한다(ADR 0039 — 후속 연기).
    """

    space_key: str
    space_name: str
    space_admins: tuple[str, ...] = ()
    page_authors: tuple[str, ...] = ()
    page_contributors: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    @field_validator("space_key")
    @classmethod
    def _validate_space_key(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("space_key는 빈 문자열/공백이 될 수 없습니다.")
        return value


class ConfluenceProvenance(BaseModel, frozen=True):
    """후보의 출처 — 어떤 Confluence 신호에서 나왔는지(감사·추적·owner 검토 근거).

    도출 결정의 기록일 뿐 권한 근거가 아니다(전이 ≠ 기록 정신). owner가 검토 시
    "이 후보가 왜 나왔나"를 보고 승인/거부/좁힘을 판단하는 재료다.
    """

    space_key: str
    owner_signal: Literal["space_admin", "page_author"]
    source_labels: tuple[str, ...] = ()


class AgentCardCandidate(BaseModel, frozen=True):
    """Confluence 부트스트랩 도출 결과 — Agent Card '후보'(권한 아님·ADR 0039 결정 4).

    이건 `AgentCard`가 *아니다*. team·last_reviewed_at을 담지 않아 owner 검토·승인
    게이트를 통과하기 전엔 카드가 될 수 없다(불변식 3 — admission 우회 불가).

    후보 → 카드 seam(이 모듈 밖·owner 매개):
      candidate → owner가 team·last_reviewed_at 기입·domains 확인·agent_id 확정
                → `validate_card_for_builder`(admission·owner 참조 무결성)
                → YAML → git 커밋(PR) → `Registry.load`.

    `candidate_domains`는 '후보 권한'이 아니라 '제안'이다(불변식 1·2). over-claim이어도
    이 모듈은 좁히지 않고, publish 수용 시 중앙(`domain_authorized`)이 필터한다.
    """

    agent_id: str
    proposed_owner: str
    summary: str
    candidate_domains: tuple[str, ...] = ()
    provenance: ConfluenceProvenance

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id_format(cls, value: str) -> str:
        """후보 agent_id도 wire-format admission 규칙을 만족해야 한다(ADR 0023 정합).

        유효하지 않은 agent_id는 *후보조차 될 수 없다* — 도출 함수는 space_key를
        wire-format으로 정규화하거나, 못 하면 `DerivationAmbiguity`(agent_id_underivable)로
        분리한다(구성 크래시가 아니라 사용자 결정으로 승격). 실제 등록 시 `AgentCard`
        field validator가 동일 규칙을 재차 강제하므로 부트스트랩이 경계를 약화하지 않는다.
        """
        return validate_agent_id_format(value)


class DerivationAmbiguity(BaseModel, frozen=True):
    """도출 애매 케이스 — 자동으로 임의 해소하지 않고 사용자 결정으로 승격한다.

    거친 도출이 확신할 수 없는 지점을 *데이터로* 드러낸다(임의 선택 금지). planner
    외부결정 3과 연결 — 각 kind가 사용자에게 물을 결정 하나다.
    """

    kind: Literal[
        "multiple_space_admins",   # 스페이스 관리자 다수 → 누구를 후보 owner로?
        "multiple_page_authors",   # 관리자 0·페이지 작성자 다수 → 누구를 후보 owner로?
        "no_owner_signal",         # 관리자·작성자 신호 0(또는 신호가 공백) → 후보 owner 없음
        "unmapped_label",          # 라벨→domain 매핑 없음 → 후보 domain에서 제외
        "agent_id_underivable",    # space_key가 wire-format으로 정규화 불가
    ]
    detail: str


class DerivationResult(BaseModel, frozen=True):
    """도출 산출 — 후보들 + 사용자 결정이 필요한 애매 케이스들.

    candidates만이 admission seam으로 흐르고, ambiguities는 owner/planner에게 노출된다.
    """

    candidates: tuple[AgentCardCandidate, ...] = ()
    ambiguities: tuple[DerivationAmbiguity, ...] = ()


def derive_card_candidates(
    signal: ConfluenceOwnershipSignal,
    label_domain_map: Mapping[str, str],
) -> DerivationResult:
    """Confluence 오너십 신호 → Agent Card 후보들(순수·결정론·IO 0).

    거친 도출 규칙(파일럿 MVP — 정밀화는 후속 연기·ADR 0039):
      - 후보 owner: 스페이스 관리자 우선. 단일이면 그 관리자, 다수면
        `multiple_space_admins` 애매(임의 선택 금지), 0명이면 page_authors 대체 시도,
        그것도 0이면 `no_owner_signal`.
      - agent_id: space_key를 wire-format으로 정규화. 정규화 불가면 `agent_id_underivable`.
      - candidate_domains: labels를 `label_domain_map`으로 매핑. 미매핑 라벨은 후보에서
        제외하고 `unmapped_label`로 보고(over-claim/noise 억제). 넓은 스페이스로 넓은
        domain이 나와도 좁히지 않는다 — 중앙 수용이 필터(불변식 2).
      - proposed_owner는 User 실재를 검증하지 *않는다* — admission이 진다(불변식 4).

    label_domain_map은 중앙 선언 입력(자기보고 아님). 함수는 권한을 생성하지 않는다.
    """
    ambiguities: list[DerivationAmbiguity] = []

    owner_result = _derive_owner(signal)
    resolved_owner: tuple[str, Literal["space_admin", "page_author"]] | None
    if isinstance(owner_result, DerivationAmbiguity):
        ambiguities.append(owner_result)
        resolved_owner = None
    else:
        resolved_owner = owner_result

    agent_id_result = _derive_agent_id(signal.space_key)
    resolved_agent_id: str | None
    if isinstance(agent_id_result, DerivationAmbiguity):
        ambiguities.append(agent_id_result)
        resolved_agent_id = None
    else:
        resolved_agent_id = agent_id_result

    candidate_domains, source_labels, label_ambiguities = _derive_domains(
        signal.labels, label_domain_map
    )
    ambiguities.extend(label_ambiguities)

    candidates: tuple[AgentCardCandidate, ...] = ()
    if resolved_owner is not None and resolved_agent_id is not None:
        proposed_owner, owner_signal = resolved_owner
        candidates = (
            AgentCardCandidate(
                agent_id=resolved_agent_id,
                proposed_owner=proposed_owner,
                summary=(
                    f"Confluence 스페이스 '{signal.space_name}' "
                    f"({signal.space_key}) 오너십 부트스트랩 후보"
                ),
                candidate_domains=candidate_domains,
                provenance=ConfluenceProvenance(
                    space_key=signal.space_key,
                    owner_signal=owner_signal,
                    source_labels=source_labels,
                ),
            ),
        )

    return DerivationResult(candidates=candidates, ambiguities=tuple(ambiguities))


def _unique_preserve_order(items: Iterable[str]) -> tuple[str, ...]:
    """입력 순서를 보존한 채 중복 제거 — 정렬 안정성·결정론 보장."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _derive_owner(
    signal: ConfluenceOwnershipSignal,
) -> tuple[str, Literal["space_admin", "page_author"]] | DerivationAmbiguity:
    """후보 owner 도출 — 스페이스 관리자 우선, 0명이면 page_authors 대체(임의 선택 금지).

    빈/공백 문자열 항목은 owner 신호로 세지 않는다(m2 — 빈 proposed_owner는 후보를
    방출하지 않는다). space_admins가 모두 공백이면 0명 취급으로 page_authors로
    대체를 시도하고, 그것도 없으면 `no_owner_signal`이다.
    """
    admins = _unique_preserve_order(a for a in signal.space_admins if a.strip())
    if len(admins) == 1:
        return admins[0], "space_admin"
    if len(admins) > 1:
        return DerivationAmbiguity(
            kind="multiple_space_admins",
            detail=(
                f"스페이스 '{signal.space_key}' 관리자 다수({', '.join(admins)}) — "
                "자동 owner 선택 불가(임의 선택 금지)."
            ),
        )

    authors = _unique_preserve_order(a for a in signal.page_authors if a.strip())
    if len(authors) == 1:
        return authors[0], "page_author"
    if len(authors) > 1:
        # multiple_space_admins와 대칭 — 임의 선택 금지, 사용자 결정으로 승격한다.
        return DerivationAmbiguity(
            kind="multiple_page_authors",
            detail=(
                f"스페이스 '{signal.space_key}' page_authors 다수({', '.join(authors)}) — "
                "자동 owner 선택 불가(임의 선택 금지)."
            ),
        )

    return DerivationAmbiguity(
        kind="no_owner_signal",
        detail=f"스페이스 '{signal.space_key}'에 관리자·작성자 신호가 없습니다.",
    )


def _normalize_agent_id(space_key: str) -> str | None:
    """space_key를 wire-format agent_id로 정규화. 정규화 불가면 None."""
    replaced = "".join(
        ch if _AGENT_ID_ALLOWED_CHAR_RE.match(ch) else "-" for ch in space_key
    )
    stripped = replaced.lstrip("-_")
    if not stripped:
        return None
    truncated = stripped[:AGENT_ID_MAX_LENGTH]
    try:
        return validate_agent_id_format(truncated)
    except ValueError:
        return None


def _derive_agent_id(space_key: str) -> str | DerivationAmbiguity:
    normalized = _normalize_agent_id(space_key)
    if normalized is None:
        return DerivationAmbiguity(
            kind="agent_id_underivable",
            detail=f"space_key {space_key!r}를 유효한 agent_id로 정규화할 수 없습니다.",
        )
    return normalized


def _derive_domains(
    labels: tuple[str, ...],
    label_domain_map: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...], list[DerivationAmbiguity]]:
    """labels → candidate_domains(정렬·중복제거) + source_labels(입력 순서 보존) + 애매.

    미매핑 라벨은 후보 domain에서 제외하고 unmapped_label로 보고한다(over-claim/noise
    억제). 매핑된 domain은 좁히지 않고 그대로 제안한다(불변식 2 — 중앙 수용이 필터).
    """
    mapped_domains: set[str] = set()
    source_labels: list[str] = []
    ambiguities: list[DerivationAmbiguity] = []
    seen_labels: set[str] = set()

    for label in labels:
        if label in seen_labels:
            continue
        seen_labels.add(label)

        domain = label_domain_map.get(label)
        if domain is None:
            ambiguities.append(
                DerivationAmbiguity(
                    kind="unmapped_label",
                    detail=f"라벨 {label!r}에 매핑된 domain이 없습니다 — 후보 domain에서 제외.",
                )
            )
            continue
        if not domain.strip():
            # m2 — 매핑은 있으나 빈 문자열 domain은 candidate_domains에 반영하지 않는다
            # (unmapped_label과 달리 매핑 자체는 존재하므로 애매 방출은 하지 않는다).
            continue

        mapped_domains.add(domain)
        source_labels.append(label)

    return tuple(sorted(mapped_domains)), tuple(source_labels), ambiguities
