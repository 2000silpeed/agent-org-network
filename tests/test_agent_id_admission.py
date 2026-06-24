"""agent_id wire-format admission 명시 테스트 (ADR 0023).

red -> green 절차:
  - 후행 개행 거부 단언은 AGENT_ID_PATTERN이 `$`일 때 red가 된다.
  - `$` -> `\\Z` 수정 후 green.

커버리지:
  - 거부 단언: 경로 탈출·빈/공백·선행 비영숫자·내부 공백·후행 개행(★)·비ASCII·길이 초과
  - 수용 단언: 실값·경계값·길이 상한
  - 경로 정합: 빌더(validate_card_for_builder)·로더(Registry.load)로 거부가 자연 매핑됨
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AGENT_ID_MAX_LENGTH, AgentCard
from agent_org_network.registry import Registry, RegistryError
from agent_org_network.web import BuilderValidateRequest, validate_card_for_builder


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────────


def _make_card(agent_id: str) -> AgentCard:
    """agent_id만 바꾸고 나머지 필드는 유효값으로 고정한 AgentCard 생성 시도."""
    return AgentCard(
        agent_id=agent_id,
        owner="alice",
        team="ops",
        summary="테스트 카드",
        domains=["운영"],
        last_reviewed_at=date(2026, 6, 24),
    )


# ════════════════════════════════════════════════════════════════════════════
# 1. 거부 단언 — ValidationError가 발생해야 한다
# ════════════════════════════════════════════════════════════════════════════


class TestAgentIdRejected:
    """형식 위반 agent_id는 AgentCard 구성 시 ValidationError."""

    # ── 경로 탈출 ──────────────────────────────────────────────────────────

    def test_경로탈출_상위디렉토리_점점슬래시(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("../x")

    def test_경로탈출_절대경로(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("/abs")

    def test_경로탈출_슬래시_포함(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("a/b")

    def test_경로탈출_백슬래시_포함(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("a\\b")

    def test_경로탈출_점점(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("..")

    def test_경로탈출_점(self) -> None:
        with pytest.raises(ValidationError):
            _make_card(".")

    # ── 빈 / 공백 ──────────────────────────────────────────────────────────

    def test_빈_문자열(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("")

    def test_공백만(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("   ")

    # ── 선행 비영숫자 ──────────────────────────────────────────────────────

    def test_선행_하이픈(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("-x")

    def test_선행_언더스코어(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("_x")

    # ── 내부 공백 ──────────────────────────────────────────────────────────

    def test_내부_공백(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("a b")

    # ── 후행 개행 (★ 보안 엣지) ────────────────────────────────────────────
    # Python `re`의 `$`는 문자열 끝 직전 개행에도 매칭된다.
    # admission은 `\Z`(문자열 절대 끝)를 써야 이 케이스를 차단한다.

    def test_후행_개행_거부(self) -> None:
        """★ AGENT_ID_PATTERN이 `$`이면 이 테스트가 red — `\\Z`로 수정해야 green."""
        with pytest.raises(ValidationError):
            _make_card("cs_ops\n")

    def test_내부_개행_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("a\nb")

    # ── 제어문자 / 적대적 입력 (보안 회귀 안전망 — ADR 0023) ────────────────
    # 정규식 allowlist가 구조적으로 막지만, 미래에 누군가 패턴을 만질 때 red로
    # 잡히도록 명시 단언으로 고정한다(code-reviewer Nit).

    def test_널바이트_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("a\x00b")

    def test_캐리지리턴_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("cs_ops\r")

    def test_탭_거부(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("a\tb")

    def test_전각숫자_거부(self) -> None:
        # 전각 '０'(U+FF10)은 [A-Za-z0-9]에 안 든다 — 유니코드 우회 차단.
        with pytest.raises(ValidationError):
            _make_card("０")

    # ── 비ASCII ────────────────────────────────────────────────────────────

    def test_비ASCII_한글(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("카드")

    def test_비ASCII_악센트(self) -> None:
        with pytest.raises(ValidationError):
            _make_card("café")

    # ── 길이 초과 ──────────────────────────────────────────────────────────

    def test_길이초과_65자(self) -> None:
        assert AGENT_ID_MAX_LENGTH == 64
        with pytest.raises(ValidationError):
            _make_card("a" * 65)


# ════════════════════════════════════════════════════════════════════════════
# 2. 수용 단언 — 구성 성공 + agent_id 보존
# ════════════════════════════════════════════════════════════════════════════


class TestAgentIdAccepted:
    """형식 준수 agent_id는 AgentCard 구성이 성공하고 agent_id를 보존한다."""

    def test_기존_실값_cs_ops(self) -> None:
        card = _make_card("cs_ops")
        assert card.agent_id == "cs_ops"

    def test_기존_실값_contract_ops(self) -> None:
        card = _make_card("contract_ops")
        assert card.agent_id == "contract_ops"

    def test_대문자_포함(self) -> None:
        card = _make_card("agent_X")
        assert card.agent_id == "agent_X"

    def test_단일_소문자(self) -> None:
        card = _make_card("n")
        assert card.agent_id == "n"

    def test_단일_대문자(self) -> None:
        card = _make_card("A")
        assert card.agent_id == "A"

    def test_단일_숫자_문자_시작_대문자(self) -> None:
        card = _make_card("Z9_x-1")
        assert card.agent_id == "Z9_x-1"

    def test_길이_상한_64자(self) -> None:
        """정확히 64자 — 수용 경계."""
        agent_id = "a" * AGENT_ID_MAX_LENGTH
        assert len(agent_id) == 64
        card = _make_card(agent_id)
        assert card.agent_id == agent_id


# ════════════════════════════════════════════════════════════════════════════
# 3. 경로 정합 — 빌더·로더 에러 경로로 자연 매핑
# ════════════════════════════════════════════════════════════════════════════


def _make_registry_for_builder() -> Registry:
    """빌더 경로 정합 테스트용 최소 Registry."""
    from agent_org_network.user import User

    reg = Registry()
    reg.register_user(User(id="root"))
    reg.register_user(User(id="alice", manager="root"))
    return reg


class TestBuilderPathMapping:
    """validate_card_for_builder가 형식 위반 agent_id를 ok:False로 반환한다."""

    def _req(self, agent_id: str) -> BuilderValidateRequest:
        return BuilderValidateRequest(
            agent_id=agent_id,
            owner="alice",
            team="ops",
            summary="경로 정합 테스트",
            domains=["운영"],
            last_reviewed_at="2026-06-24",
        )

    def test_빌더_경로탈출_슬래시_포함_거부(self) -> None:
        reg = _make_registry_for_builder()
        result = validate_card_for_builder(self._req("a/b"), reg)
        assert result["ok"] is False
        assert len(result["errors"]) > 0

    def test_빌더_후행_개행_거부(self) -> None:
        """★ 후행 개행도 빌더 경로에서 거부된다."""
        reg = _make_registry_for_builder()
        result = validate_card_for_builder(self._req("cs_ops\n"), reg)
        assert result["ok"] is False
        assert len(result["errors"]) > 0

    def test_빌더_정상_agent_id_수용(self) -> None:
        reg = _make_registry_for_builder()
        result = validate_card_for_builder(self._req("valid_ops"), reg)
        assert result["ok"] is True


# ── 로더 경로 정합 ────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")


class TestLoaderPathMapping:
    """형식 위반 agent_id를 가진 카드 YAML을 Registry.load하면 RegistryError."""

    def test_경로탈출_슬래시_포함_로더_거부(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "users.yaml",
            """
            users:
              - id: root
              - id: alice
                manager: root
            """,
        )
        _write(
            tmp_path / "agents" / "bad_card.yaml",
            """
            agent_id: "a/b"
            owner: alice
            team: ops
            summary: 나쁜 카드
            domains:
              - 운영
            last_reviewed_at: "2026-06-24"
            """,
        )
        registry = Registry()
        with pytest.raises(RegistryError):
            registry.load(tmp_path)

    def test_후행_개행_로더_거부(self, tmp_path: Path) -> None:
        """★ 후행 개행 agent_id를 YAML에서 로드하면 RegistryError."""
        _write(
            tmp_path / "users.yaml",
            """
            users:
              - id: root
              - id: alice
                manager: root
            """,
        )
        # YAML에서 개행 포함 agent_id를 표현하려면 리터럴 블록 스칼라 사용
        _write(
            tmp_path / "agents" / "newline_card.yaml",
            ''.join([
                "agent_id: \"cs_ops\\n\"\n",
                "owner: alice\n",
                "team: ops\n",
                "summary: 개행 카드\n",
                "domains:\n",
                "  - 운영\n",
                "last_reviewed_at: \"2026-06-24\"\n",
            ]),
        )
        registry = Registry()
        with pytest.raises(RegistryError):
            registry.load(tmp_path)
