import os
import re
from datetime import date
from pathlib import Path
from typing import Final

from pydantic import BaseModel, field_validator

# agent_id wire-format admission 규칙(ADR 0023 — 식별자 위생).
# 영숫자로 시작 + 영숫자/`_`/`-`로 구성. 경로 탈출(`/`·`\`·`..`·절대·빈·공백)을
# *구조적으로* 차단한다 — 어떤 매칭 문자도 경로구분자·상위참조가 될 수 없다.
# 대문자 수용(기존 `agent_X`/`agent_Y` 픽스처·미래 카드 — 소문자 강제는 명명 스타일이지
# 보안 경계가 아니므로 기각, ADR 0023). admission은 *형식 권위*(positive format)고,
# 어댑터(`git_gateway.validate_agent_id`)는 *경로 안전 백스톱*(traversal block)으로 역할 분리.
AGENT_ID_MAX_LENGTH: Final[int] = 64
# `\Z`를 사용한다 — Python `re`의 `$`는 문자열 끝 직전 개행(`\n`)에도 매칭되어
# "cs_ops\n" 같은 후행 개행이 통과하는 wire-format 보안 엣지가 생긴다. `\Z`는
# 문자열의 절대 끝만 매칭하므로 후행 개행을 구조적으로 차단한다(ADR 0023).
AGENT_ID_PATTERN: Final[str] = r"^[A-Za-z0-9][A-Za-z0-9_-]*\Z"
_AGENT_ID_RE: Final[re.Pattern[str]] = re.compile(AGENT_ID_PATTERN)


def is_safe_path_component(value: str) -> bool:
    """단일 파일명 컴포넌트(구분자·상대경로·절대경로 0)인지 확인하는 bool 술어.

    다음을 거부한다:
      - 빈 문자열
      - 예약 stem: "." ".."
      - os.sep·'/'·'\\' 포함 — 구분자가 있으면 다단계 경로
      - 절대경로(os.path.isabs)
      - Path(value).name != value — 상위 경로 성분이 숨어 있음

    순수 파일 stem("refund-policy", "pricing_v2") 등만 True를 반환한다.
    단일 권위: worker.py·okf_authoring.py 모두 이 함수로 위임한다(ADR 0028 §15·ADR 0023).
    """
    if not value:
        return False
    if value in (".", ".."):
        return False
    if os.sep in value or "/" in value or "\\" in value:
        return False
    if os.path.isabs(value):
        return False
    if Path(value).name != value:
        return False
    return True


def validate_safe_path_component(value: str) -> str:
    """단일 파일명 컴포넌트 검증기 — 통과 시 value 반환, 실패 시 ValueError.

    pydantic field_validator에서 바로 호출할 수 있도록 값을 반환하는 스타일
    (validate_agent_id_format 선례·ADR 0023). 다음을 거부한다:
      - 빈/공백 문자열
      - '..'·'.'(예약 stem)
      - 구분자('/'·'\\'·os.sep) 포함
      - 절대경로
      - Path(value).name != value
    """
    if not value or not value.strip():
        raise ValueError(
            f"path component는 빈 문자열/공백이 될 수 없습니다: {value!r}"
        )
    if not is_safe_path_component(value):
        raise ValueError(
            f"path component가 올바르지 않습니다(빈 문자열·'..'·'.'·구분자·절대경로 금지): "
            f"{value!r}"
        )
    return value


def domain_authorized(domain: str, card: "AgentCard") -> bool:
    """domain이 card의 owned 권한 안인가 — 단일 권위 술어(ADR 0028 §13 결정 B·§14 결정 D).

    `domain ∈ card.domains AND domain ∉ card.cannot_answer`. publish 수용 시 over-claim
    concept 필터(T10.4 결정 D)와 라우팅 권한 재검증(`TwoStageRouter.route` stage-1·
    precedent 단축, 결정 B)이 *같은* 이 함수를 호출한다 — 중복 정의 금지·단일 권위
    (`attach_gates`를 모듈 함수로 뽑은 정신). 자기보고가 권한을 *넓힐* 수 없게 막는
    Authority 중앙 술어(ADR 0004) — under-claim(권한 안쪽)만 통과, over-claim은 거부.
    """
    return domain in card.domains and domain not in card.cannot_answer


def validate_agent_id_format(value: str) -> str:
    """agent_id 형식 검증 공유 헬퍼(ADR 0023).

    AgentCard·KnowledgeIndex 등 agent_id를 갖는 *모든* 값 객체가 이 함수를
    field_validator 내부에서 호출해 동일 admission 규칙을 보장한다.
    중복 정의 없이 단일 권위 소스를 공유한다.
    """
    if len(value) > AGENT_ID_MAX_LENGTH:
        raise ValueError(
            f"agent_id는 {AGENT_ID_MAX_LENGTH}자 이하여야 합니다: {value!r}"
        )
    if not _AGENT_ID_RE.match(value):
        raise ValueError(
            f"agent_id 형식이 올바르지 않습니다(영숫자로 시작 + 영숫자/_/- 만 허용): "
            f"{value!r}"
        )
    return value


class AgentCard(BaseModel, frozen=True):
    agent_id: str
    owner: str
    team: str
    summary: str
    domains: list[str]
    last_reviewed_at: date
    maintainer: str | None = None
    can_answer: list[str] = []
    cannot_answer: list[str] = []
    approval_when: list[str] = []
    collaborate_when: list[str] = []
    knowledge_sources: list[str] = []
    trust_labels: list[str] = []

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id_format(cls, value: str) -> str:
        """agent_id 형식을 admission에서 근본 강제한다(ADR 0023 — 등록 무결성).

        *모든* 카드 구성(YAML `model_validate`·빌더 검증·demo·테스트)이 이 경계를 지나므로
        유효하지 않은 agent_id를 *구성 불가*로 만든다("유효하지 않은 카드는 등록되지 않는다"의
        최강 지점). `AGENT_ID_PATTERN`(영숫자 시작 + 영숫자/`_`/`-`)에 맞지 않거나
        길이 상한(`AGENT_ID_MAX_LENGTH`)을 넘으면 ValueError → pydantic ValidationError로
        승격되어 빌더 `{ok:False, errors}`·로더 `RegistryError`로 자연히 매핑된다.
        """
        return validate_agent_id_format(value)
