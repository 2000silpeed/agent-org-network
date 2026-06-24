import re
from datetime import date
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
