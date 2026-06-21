"""골든셋 로더 — samples/questions.jsonl을 읽어 typed 리스트로 반환한다.

T6.4 골든셋 데이터 로더. eval 러너(T6.2)가 이 로더를 소비한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class SampleQuestion(BaseModel, frozen=True):
    question: str
    expected_intent: str
    expected_disposition: str
    expected_primary: str | None = None
    expected_candidates: list[str] | None = None
    expected_approval: bool = False
    expected_collaborators: list[str] | None = None
    note: str = ""


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
