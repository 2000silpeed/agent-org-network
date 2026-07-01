"""scale_eval.py 결정론 테스트 — 격리 조립(build_scale_*)·tier 집계 러너.

tmp_path 미니 픽스처(카드 3장·users.yaml·okf 문서 몇 개·골든 질문 jsonl)만 참조한다.
실 registry/scale/·okf_scale/은 이 테스트가 참조하지 않는다(게이트 밖 러너는
@pytest.mark.scale 마커 테스트 1개 — 실 디렉터리 없으면 skip).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.golden import SampleQuestion
from agent_org_network.index_matcher import ConceptOverlapMatcher
from agent_org_network.scale_eval import (
    ScaleEvalReport,
    build_scale_index_store,
    build_scale_registry,
    build_scale_router,
    run_scale_eval,
    stage1_top_margin,
)

GENERATED_AT = datetime(2026, 7, 2, tzinfo=timezone.utc)
ROOT_USER = "desk_root"


def _write_users_yaml(registry_dir: Path) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "users.yaml").write_text(
        """
users:
  - id: desk_root
  - id: cs_lead
    manager: desk_root
  - id: finance_lead
    manager: desk_root
  - id: hr_lead
    manager: desk_root
""",
        encoding="utf-8",
    )


def _write_card(registry_dir: Path, agent_id: str, owner: str, domains: list[str]) -> None:
    agents_dir = registry_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    domains_yaml = "\n".join(f"  - {d}" for d in domains)
    (agents_dir / f"{agent_id}.yaml").write_text(
        f"""
agent_id: {agent_id}
owner: {owner}
team: {agent_id}
summary: 테스트용 {agent_id} 카드.
domains:
{domains_yaml}
last_reviewed_at: "2026-06-20"
""",
        encoding="utf-8",
    )


def _write_okf_doc(
    okf_root: Path, agent_id: str, filename: str, title: str, description: str, tags: list[str]
) -> None:
    agent_dir = okf_root / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    tags_yaml = "[" + ", ".join(tags) + "]"
    (agent_dir / filename).write_text(
        f"""---
type: concept
title: {title}
description: {description}
tags: {tags_yaml}
---

# {title}

본문 내용은 목차 도출에 쓰이지 않는다.
""",
        encoding="utf-8",
    )


@pytest.fixture
def scale_dir(tmp_path: Path) -> Path:
    """카드 3장(cs_ops·finance_ops·hr_ops) — 미니 registry 픽스처."""
    registry_dir = tmp_path / "registry_scale"
    _write_users_yaml(registry_dir)
    _write_card(registry_dir, "cs_ops", "cs_lead", ["환불"])
    _write_card(registry_dir, "finance_ops", "finance_lead", ["가격"])
    _write_card(registry_dir, "hr_ops", "hr_lead", ["휴가"])
    return registry_dir


@pytest.fixture
def okf_root(tmp_path: Path) -> Path:
    """카드별 OKF 문서 — 각 도메인 코어 토큰이 겹치지 않게 구성."""
    root = tmp_path / "okf_scale"
    _write_okf_doc(
        root, "cs_ops", "refund.md", "환불 안내", "환불 가능 기간과 수수료 설명", ["환불"]
    )
    _write_okf_doc(
        root, "finance_ops", "pricing.md", "가격표", "제품 가격과 할인 기준 목록", ["가격"]
    )
    _write_okf_doc(
        root, "hr_ops", "leave.md", "휴가 규정", "연차와 휴가 신청 절차 설명", ["휴가"]
    )
    return root


# ── 1. build_scale_registry ──────────────────────────────────────────────────


def test_build_scale_registry_카드_3장_로드_검증(scale_dir: Path):
    registry = build_scale_registry(scale_dir)
    assert len(registry.all_cards()) == 3
    assert {c.agent_id for c in registry.all_cards()} == {"cs_ops", "finance_ops", "hr_ops"}


def test_build_scale_registry_무결성_위반_시_예외(tmp_path: Path):
    """owner 미등록 카드 → validate()가 예외를 낸다(등록 무결성)."""
    from agent_org_network.registry import RegistryError

    bad_dir = tmp_path / "bad_registry"
    (bad_dir / "agents").mkdir(parents=True)
    (bad_dir / "users.yaml").write_text("users: []\n", encoding="utf-8")
    (bad_dir / "agents" / "cs_ops.yaml").write_text(
        """
agent_id: cs_ops
owner: no_such_user
team: cs
summary: 요약
domains:
  - 환불
last_reviewed_at: "2026-06-20"
""",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError):
        build_scale_registry(bad_dir)


# ── 2. build_scale_index_store ───────────────────────────────────────────────


def test_build_scale_index_store_카드별_인덱스_생성(scale_dir: Path, okf_root: Path):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    indexes = {idx.agent_id: idx for idx in store.all_indexes()}
    assert set(indexes.keys()) == {"cs_ops", "finance_ops", "hr_ops"}
    assert len(indexes["cs_ops"].concepts) == 1
    assert indexes["cs_ops"].concepts[0].domain == "환불"


# ── 3. build_scale_router ────────────────────────────────────────────────────


def test_build_scale_router_routed_단일_후보(scale_dir: Path, okf_root: Path):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    decision = router.route("환불 정책이 어떻게 되나요?")
    assert isinstance(decision, Routed)
    assert decision.primary.agent_id == "cs_ops"


def test_build_scale_router_unowned_0매칭(scale_dir: Path, okf_root: Path):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    decision = router.route("오늘 날씨가 어떤가요?")
    assert isinstance(decision, Unowned)
    assert decision.escalated_to == ROOT_USER


def test_build_scale_router_assessor_미주입_2단계_자동해소_없음(scale_dir: Path, okf_root: Path):
    """assessor=None — 현 한계 실측 목적. ≥2 후보는 Contested로 남는다(자동해소 없음)."""
    # 두 카드가 같은 토큰으로 겹치도록 추가 문서를 심는다.
    _write_okf_doc(
        okf_root, "finance_ops", "refund-fee.md", "환불 수수료", "환불 시 수수료 정책 안내", ["가격"]
    )
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    decision = router.route("환불 수수료가 어떻게 되나요?")
    assert isinstance(decision, Contested)


# ── 4. run_scale_eval — tier 집계 ────────────────────────────────────────────


@pytest.fixture
def golden_samples() -> list[SampleQuestion]:
    return [
        SampleQuestion(
            question="환불 정책이 어떻게 되나요?",
            expected_intent="환불",
            expected_disposition="routed",
            expected_primary="cs_ops",
            tier="easy",
        ),
        SampleQuestion(
            question="가격 정책 알려주세요",
            expected_intent="가격",
            expected_disposition="routed",
            expected_primary="finance_ops",
            tier="easy",
        ),
        # 오라우팅 케이스 — 실제론 hr_ops로 안 감(라벨이 틀린 기대값)
        SampleQuestion(
            question="환불 정책이 어떻게 되나요?",
            expected_intent="환불",
            expected_disposition="routed",
            expected_primary="hr_ops",
            tier="hard",
        ),
        # unowned 케이스
        SampleQuestion(
            question="오늘 날씨가 어떤가요?",
            expected_intent="날씨",
            expected_disposition="unowned",
            expected_primary=None,
            tier="hard",
        ),
        # contested 케이스(라우터가 실제 Contested를 반환하도록 별도 라우터에서 사용)
        SampleQuestion(
            question="환불 수수료가 어떻게 되나요?",
            expected_intent="환불",
            expected_disposition="routed",
            expected_primary="cs_ops",
            tier="ambiguous",
        ),
    ]


def test_run_scale_eval_전체_및_tier_집계(
    scale_dir: Path, okf_root: Path, golden_samples: list[SampleQuestion]
):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    report = run_scale_eval(router, golden_samples)

    assert isinstance(report, ScaleEvalReport)
    assert report.total == 5
    # 5건 중 hard tier의 hr_ops 오답 기대 1건만 top-1 불일치 → 4/5.
    assert report.overall_top1_accuracy == pytest.approx(4 / 5)
    assert report.by_tier["easy"].total == 2
    assert report.by_tier["easy"].top1_accuracy == pytest.approx(1.0)
    assert report.by_tier["hard"].total == 2
    assert report.by_tier["hard"].top1_accuracy == pytest.approx(0.5)
    assert report.by_tier["ambiguous"].total == 1
    assert report.by_tier["ambiguous"].top1_accuracy == pytest.approx(1.0)


def test_run_scale_eval_오라우팅_케이스_1건(
    scale_dir: Path, okf_root: Path, golden_samples: list[SampleQuestion]
):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    report = run_scale_eval(router, golden_samples)

    assert report.misrouted_count == 1
    misrouted = [f for f in report.failures if f.expected_primary == "hr_ops"]
    assert len(misrouted) == 1
    assert misrouted[0].actual_disposition == "routed"
    assert misrouted[0].actual_primary == "cs_ops"
    assert misrouted[0].tier == "hard"


def test_run_scale_eval_0매칭_escalation률(
    scale_dir: Path, okf_root: Path, golden_samples: list[SampleQuestion]
):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    report = run_scale_eval(router, golden_samples)

    assert report.unowned_rate == pytest.approx(1 / 5)


def test_run_scale_eval_contested_률(scale_dir: Path, okf_root: Path):
    """환불 수수료 질문이 실제로 Contested를 내는 픽스처(finance_ops에 겹치는 문서 추가)."""
    _write_okf_doc(
        okf_root, "finance_ops", "refund-fee.md", "환불 수수료", "환불 시 수수료 정책 안내", ["가격"]
    )
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    samples = [
        SampleQuestion(
            question="환불 수수료가 어떻게 되나요?",
            expected_intent="환불",
            expected_disposition="routed",
            expected_primary="cs_ops",
            tier="ambiguous",
        ),
    ]
    report = run_scale_eval(router, samples)
    assert report.contested_rate == pytest.approx(1.0)
    assert report.failures[0].actual_disposition == "contested"


def test_run_scale_eval_리포트_실패케이스_필드(
    scale_dir: Path, okf_root: Path, golden_samples: list[SampleQuestion]
):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    router = build_scale_router(registry, store, root_user=ROOT_USER)

    report = run_scale_eval(router, golden_samples)
    for failure in report.failures:
        assert failure.question
        assert failure.tier in ("easy", "hard", "ambiguous")
        assert failure.expected_disposition in ("routed", "contested", "unowned")
        assert failure.actual_disposition in ("routed", "contested", "unowned")


# ── stage1_top_margin 보조 함수 ──────────────────────────────────────────────


def test_stage1_top_margin_후보_2건_이상_margin_계산(scale_dir: Path, okf_root: Path):
    _write_okf_doc(
        okf_root, "finance_ops", "refund-fee.md", "환불 수수료", "환불 시 수수료 정책 안내", ["가격"]
    )
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    matcher = ConceptOverlapMatcher()

    margin = stage1_top_margin(matcher, "환불 수수료가 어떻게 되나요?", store)
    assert margin is not None
    assert margin >= 0.0


def test_stage1_top_margin_후보_1건_이하_None(scale_dir: Path, okf_root: Path):
    registry = build_scale_registry(scale_dir)
    store = build_scale_index_store(registry, okf_root, generated_at=GENERATED_AT)
    matcher = ConceptOverlapMatcher()

    assert stage1_top_margin(matcher, "환불 정책이 어떻게 되나요?", store) is None
    assert stage1_top_margin(matcher, "오늘 날씨가 어떤가요?", store) is None


# ── 5. 게이트 밖 실 데이터 러너(마커) ────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SCALE_REGISTRY = REPO_ROOT / "registry" / "scale"
REAL_OKF_SCALE = REPO_ROOT / "okf_scale"
REAL_SCALE_GOLDEN = REPO_ROOT / "samples" / "scale_questions.jsonl"


@pytest.mark.scale
def test_실_스케일_데이터_라우팅_품질_리포트():
    if not (REAL_SCALE_REGISTRY.is_dir() and REAL_OKF_SCALE.is_dir() and REAL_SCALE_GOLDEN.is_file()):
        pytest.skip("실 registry/scale·okf_scale·samples/scale_questions.jsonl 미준비 — 게이트 밖 러너")

    from agent_org_network.golden import load_golden

    registry = build_scale_registry(REAL_SCALE_REGISTRY)
    store = build_scale_index_store(
        registry, REAL_OKF_SCALE, generated_at=datetime.now(tz=timezone.utc)
    )
    router = build_scale_router(registry, store, root_user=ROOT_USER)
    samples = load_golden(REAL_SCALE_GOLDEN)
    report = run_scale_eval(router, samples)
    assert report.total == len(samples)
