"""T1.3 — Registry.load(dir) YAML 로더 테스트.

결정론: 파일 픽스처(tmp_path) 사용, 실 LLM 없음.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from agent_org_network.registry import Registry, RegistryError


# ── 픽스처 헬퍼 ────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content), encoding="utf-8")
    return path


def _make_users_yaml(dir: Path, content: str) -> None:
    _write(dir / "users.yaml", content)


def _make_agent_yaml(dir: Path, name: str, content: str) -> None:
    _write(dir / "agents" / f"{name}.yaml", content)


def _minimal_dir(tmp_path: Path) -> Path:
    """유저 2명·카드 1장의 최소 유효 디렉터리."""
    _make_users_yaml(
        tmp_path,
        """
        users:
          - id: root
          - id: alice
            manager: root
        """,
    )
    _make_agent_yaml(
        tmp_path,
        "ops_card",
        """
        agent_id: ops_card
        owner: alice
        team: ops
        summary: 운영 카드
        domains:
          - 운영
        last_reviewed_at: "2026-06-20"
        """,
    )
    return tmp_path


# ── 정상 로드 ────────────────────────────────────────────────────────────────


def test_load_유저가_등록된다(tmp_path: Path) -> None:
    _make_users_yaml(
        tmp_path,
        """
        users:
          - id: root
          - id: alice
            manager: root
        """,
    )
    registry = Registry()
    registry.load(tmp_path)
    # validate 전에도 user_ids에 반영돼 있어야 한다
    assert "root" in registry.user_ids()
    assert "alice" in registry.user_ids()
    assert registry.get_user("alice").manager == "root"


def test_load_카드가_등록된다(tmp_path: Path) -> None:
    _minimal_dir(tmp_path)
    registry = Registry()
    registry.load(tmp_path)
    card = registry.get("ops_card")
    assert card.agent_id == "ops_card"
    assert card.owner == "alice"
    assert card.domains == ["운영"]


def test_load_카드_날짜_필드가_파싱된다(tmp_path: Path) -> None:
    _minimal_dir(tmp_path)
    registry = Registry()
    registry.load(tmp_path)
    assert registry.get("ops_card").last_reviewed_at == date(2026, 6, 20)


def test_load_후_validate_통과(tmp_path: Path) -> None:
    _minimal_dir(tmp_path)
    registry = Registry()
    registry.load(tmp_path)
    registry.validate()  # 예외 없음


def test_load_유저_먼저_카드_나중에_등록된다(tmp_path: Path) -> None:
    """유저 로드 후 카드 로드 — owner 참조 무결성이 validate에서 통과해야 한다."""
    _minimal_dir(tmp_path)
    registry = Registry()
    registry.load(tmp_path)
    # validate가 통과한다 = owner가 user_ids에 먼저 들어 있다
    registry.validate()


def test_load_카드_여러_장_모두_등록된다(tmp_path: Path) -> None:
    _make_users_yaml(
        tmp_path,
        """
        users:
          - id: root
          - id: alice
            manager: root
          - id: bob
            manager: root
        """,
    )
    _make_agent_yaml(
        tmp_path,
        "card_a",
        """
        agent_id: card_a
        owner: alice
        team: ops
        summary: A 카드
        domains: [계약]
        last_reviewed_at: "2026-06-20"
        """,
    )
    _make_agent_yaml(
        tmp_path,
        "card_b",
        """
        agent_id: card_b
        owner: bob
        team: finance
        summary: B 카드
        domains: [가격]
        last_reviewed_at: "2026-06-20"
        """,
    )
    registry = Registry()
    registry.load(tmp_path)
    registry.validate()
    ids = {c.agent_id for c in registry.all_cards()}
    assert ids == {"card_a", "card_b"}


def test_load_선택_필드_없어도_등록된다(tmp_path: Path) -> None:
    """AgentCard 선택 필드(knowledge_sources 등) 없이도 로드 성공."""
    _minimal_dir(tmp_path)
    registry = Registry()
    registry.load(tmp_path)
    card = registry.get("ops_card")
    assert card.knowledge_sources == []


def test_load_knowledge_sources_로드된다(tmp_path: Path) -> None:
    _make_users_yaml(tmp_path, "users:\n  - id: root\n  - id: alice\n    manager: root\n")
    _make_agent_yaml(
        tmp_path,
        "rich_card",
        """
        agent_id: rich_card
        owner: alice
        team: ops
        summary: 풍부한 카드
        domains: [운영]
        last_reviewed_at: "2026-06-20"
        knowledge_sources:
          - 위키/운영가이드
          - Notion/절차서
        """,
    )
    registry = Registry()
    registry.load(tmp_path)
    assert registry.get("rich_card").knowledge_sources == ["위키/운영가이드", "Notion/절차서"]


# ── 샘플 YAML(registry/) 무결성 ──────────────────────────────────────────────


def test_샘플_registry_load_validate_통과() -> None:
    """repo의 registry/ 샘플이 로드·검증을 통과한다."""
    registry_dir = Path(__file__).resolve().parent.parent / "registry"
    assert registry_dir.is_dir(), "registry/ 디렉터리가 없음"
    registry = Registry()
    registry.load(registry_dir)
    registry.validate()
    assert len(registry.all_cards()) == 3
    assert len(registry.user_ids()) == 4


def test_샘플_registry_카드_agent_id_집합() -> None:
    """샘플 카드가 demo._CARDS와 같은 agent_id 집합을 가진다."""
    registry_dir = Path(__file__).resolve().parent.parent / "registry"
    registry = Registry()
    registry.load(registry_dir)
    ids = {c.agent_id for c in registry.all_cards()}
    assert ids == {"contract_ops", "cs_ops", "finance_ops"}


def test_샘플_registry_유저_id_집합() -> None:
    """샘플 유저가 demo._USERS와 같은 id 집합을 가진다."""
    registry_dir = Path(__file__).resolve().parent.parent / "registry"
    registry = Registry()
    registry.load(registry_dir)
    ids = set(registry.user_ids())
    assert ids == {"root_manager", "legal_lead", "cs_lead", "finance_lead"}


def test_샘플_registry_카드_내용이_demo와_일치() -> None:
    """샘플 YAML 카드 내용이 demo.py _CARDS와 동일한 핵심 필드를 가진다."""
    registry_dir = Path(__file__).resolve().parent.parent / "registry"
    registry = Registry()
    registry.load(registry_dir)

    cs = registry.get("cs_ops")
    assert cs.owner == "cs_lead"
    assert "환불" in cs.domains
    assert "보상" in cs.domains

    finance = registry.get("finance_ops")
    assert finance.owner == "finance_lead"
    assert "가격" in finance.domains
    assert "보상" in finance.domains

    contract = registry.get("contract_ops")
    assert contract.owner == "legal_lead"
    assert "계약 검토" in contract.domains


# ── 오류 케이스 ────────────────────────────────────────────────────────────


def test_load_미등록_owner면_validate_실패(tmp_path: Path) -> None:
    _make_users_yaml(tmp_path, "users:\n  - id: root\n")
    _make_agent_yaml(
        tmp_path,
        "ghost_card",
        """
        agent_id: ghost_card
        owner: nobody
        team: ops
        summary: 유령 카드
        domains: [운영]
        last_reviewed_at: "2026-06-20"
        """,
    )
    registry = Registry()
    registry.load(tmp_path)
    with pytest.raises(RegistryError, match="미등록 owner"):
        registry.validate()


def test_load_미등록_manager면_validate_실패(tmp_path: Path) -> None:
    _make_users_yaml(
        tmp_path,
        """
        users:
          - id: orphan
            manager: ghost
        """,
    )
    registry = Registry()
    registry.load(tmp_path)
    with pytest.raises(RegistryError, match="미등록 manager"):
        registry.validate()


def test_load_카드_필수_필드_누락이면_RegistryError(tmp_path: Path) -> None:
    """필수 필드(agent_id) 없는 카드 YAML → RegistryError."""
    _make_users_yaml(tmp_path, "users:\n  - id: root\n")
    _make_agent_yaml(
        tmp_path,
        "broken_card",
        """
        owner: root
        team: ops
        summary: 필수 필드 없음
        domains: [운영]
        last_reviewed_at: "2026-06-20"
        """,
    )
    registry = Registry()
    with pytest.raises(RegistryError, match="카드 로드 실패"):
        registry.load(tmp_path)


def test_load_users_yaml_형식_오류면_RegistryError(tmp_path: Path) -> None:
    """users.yaml에 'users' 키 없으면 RegistryError."""
    _make_users_yaml(tmp_path, "not_users:\n  - id: root\n")
    registry = Registry()
    with pytest.raises(RegistryError, match="형식 오류"):
        registry.load(tmp_path)


def test_load_agents_디렉터리_없어도_유저만_로드(tmp_path: Path) -> None:
    """agents/ 없으면 유저만 로드하고 예외 없음."""
    _make_users_yaml(tmp_path, "users:\n  - id: root\n")
    # agents/ 디렉터리 만들지 않음
    registry = Registry()
    registry.load(tmp_path)
    assert "root" in registry.user_ids()
    assert registry.all_cards() == []


def test_load_users_yaml_없어도_카드만_로드(tmp_path: Path) -> None:
    """users.yaml 없으면 카드만 로드하고 예외 없음(validate에서 잡힘)."""
    _make_agent_yaml(
        tmp_path,
        "no_user_card",
        """
        agent_id: no_user_card
        owner: nobody
        team: ops
        summary: 유저 없는 카드
        domains: [운영]
        last_reviewed_at: "2026-06-20"
        """,
    )
    registry = Registry()
    registry.load(tmp_path)
    assert registry.get("no_user_card").agent_id == "no_user_card"
    # owner가 미등록이라 validate에서 실패
    with pytest.raises(RegistryError, match="미등록 owner"):
        registry.validate()
