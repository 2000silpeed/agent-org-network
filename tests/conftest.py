"""pytest 공용 설정 — 결정론·격리 가드.

`build_demo`/`build_demo_ask_org`/`create_app`를 `audit_log` 미주입으로 부르는 테스트는
production 기본값(`demo._DEFAULT_AUDIT_LOG_PATH` = `logs/audit.jsonl`)을 상속해 실파일에
쓴다(TRD §7·CLAUDE.md 격리 위반: 실파일 0). autouse fixture로 매 테스트 그 기본 경로를
테스트별 tmp 경로로 치환해, 명시 주입이 없는 호출도 repo `logs/`를 더럽히지 않게 한다.
audit_log를 직접 주입하는 테스트는 영향받지 않는다(자기 인스턴스를 그대로 쓴다).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_org_network import demo


@pytest.fixture(autouse=True)
def _isolate_default_audit_log(  # pyright: ignore[reportUnusedFunction]  # pytest가 수집으로 적용
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """미주입 audit_log 기본 파일 경로를 테스트별 tmp로 격리(실파일 0)."""
    monkeypatch.setattr(demo, "_DEFAULT_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
