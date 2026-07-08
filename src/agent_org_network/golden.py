"""골든셋 로더 — samples/questions.jsonl을 읽어 typed 리스트로 반환한다.

T6.4 골든셋 데이터 로더. eval 러너(T6.2)가 이 로더를 소비한다.

ADR 0041(큐레이션 골든셋 — 답변 품질 축 + 라벨 provenance): `SampleQuestion`에 옵셔널
`answer_expectation` 필드를 얹는다(신규 병렬 타입 아님 — 결정 3). 답변 기대기준
(`AnswerExpectation`)은 **사람이 확정한 provenance**(`CurationProvenance`)를 필수로
안는다(결정 2 — 라벨 무결성의 타입 수준 강제). 이 모듈은 순수 스키마·로더다 — `Answer`
(runtime.py)로부터 골든 라벨을 파생하는 함수·로더는 **의도적으로 두지 않는다**(구조
가드·B3 — 정답은 사람이 중앙에서 확정하지, 시스템 자기 출력에서 자동 파생되지 않는다).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, field_validator


class CurationProvenance(BaseModel, frozen=True):
    """답변 기대기준을 확정한 사람의 provenance(ADR 0041 결정 2 — 라벨 무결성).

    `curated_by`는 확정자 식별(빈 값 거부 — provenance 없는 라벨은 성립 불가). `source_hint`는
    원 질문이 어디서 왔는지 참고 메모(메일/Slack 등 — 감사·재현용, 라우팅/채점에 쓰지 않는다).
    """

    curated_by: str
    source_hint: str = ""

    @field_validator("curated_by")
    @classmethod
    def _reject_blank_curated_by(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("curated_by는 빈 값일 수 없다(사람 확정자 식별이 라벨 무결성의 최소 조건)")
        return value


class AnswerExpectation(BaseModel, frozen=True):
    """답변 품질 채점 기대기준 — `provenance` 없이는 구성 불가(ADR 0041 결정 2).

    `criteria`는 채점 기준 문자열들(예: `SubstringGrader`가 답변 텍스트에 포함 여부를 본다).
    `match`가 "all"이면 전부, "any"면 하나 이상 충족해야 통과(기본 "all"). `provenance`는
    필수 필드라 `Answer`(runtime.py)에서 자동으로 이 값객체를 만들 수 없다 — 사람이 명시
    구성해야만 성립한다.
    """

    criteria: tuple[str, ...]
    match: Literal["all", "any"] = "all"
    provenance: CurationProvenance


class SampleQuestion(BaseModel, frozen=True):
    question: str
    expected_intent: str
    expected_disposition: str
    expected_primary: str | None = None
    expected_candidates: list[str] | None = None
    expected_approval: bool = False
    expected_collaborators: list[str] | None = None
    note: str = ""
    tier: Literal["easy", "hard", "ambiguous"] = "easy"
    # ADR 0041 결정 3: 옵셔널 확장 필드(신규 병렬 타입 아님) — 없으면 라우팅만 채점
    # (기존 samples/questions.jsonl 30개 하위호환). 있으면 provenance까지 중첩 검증된다.
    answer_expectation: AnswerExpectation | None = None


def load_golden(path: Path) -> list[SampleQuestion]:
    """JSONL 파일을 읽어 SampleQuestion 리스트로 반환한다.

    각 줄을 JSON 파싱 후 pydantic model_validate로 검증한다.
    필수 필드 누락·타입 오류는 ValueError로 상위에 전파된다.
    """
    entries: list[SampleQuestion] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL 파싱 오류 (줄 {lineno}): {exc}") from exc
        try:
            entries.append(SampleQuestion.model_validate(raw))
        except Exception as exc:
            raise ValueError(f"골든셋 항목 검증 오류 (줄 {lineno}): {exc}") from exc
    return entries
