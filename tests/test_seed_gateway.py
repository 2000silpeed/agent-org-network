"""seed_gateway_from_disk 결정론 테스트 — 디스크 OKF 베이스라인을 게이트웨이로 시드.

저작→답변 루프 단일 진실원천(ADR 0018 결정 4)을 게이트웨이로 모으는 데모 배선의 헬퍼.
실 git 0(FakeGitGateway in-memory)·실 claude 0. (1) 디스크 okf/{agent_id}/*.md가 카드별
1커밋으로 게이트웨이에 들어가고 extract_snapshot이 그 파일들을 돌려주는지, (2) okf 디렉터리
없는 카드는 건너뛰는지(커밋 없음)만 고정 검증한다.
"""

from datetime import date
from pathlib import Path

from agent_org_network.agent_card import AgentCard
from agent_org_network.git_gateway import FakeGitGateway
from agent_org_network.registry import Registry
from agent_org_network.web import seed_gateway_from_disk


def _card(agent_id: str, owner: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="t",
        summary="s",
        domains=["d"],
        last_reviewed_at=date(2026, 6, 20),
    )


def _registry(*cards: AgentCard) -> Registry:
    reg = Registry()
    for c in cards:
        reg.register(c)
    return reg


def test_디스크_okf를_게이트웨이로_시드하고_스냅샷이_그_파일을_돌려준다(tmp_path: Path):
    bundle = tmp_path / "finance_ops"
    bundle.mkdir()
    (bundle / "index.md").write_text("# 목차", encoding="utf-8")
    (bundle / "pricing.md").write_text("가격 정책 본문", encoding="utf-8")
    reg = _registry(_card("finance_ops", "finance_lead"))

    gw = FakeGitGateway()
    seed_gateway_from_disk(gw, reg, tmp_path)

    sha = gw.head_sha("finance_ops")  # 커밋이 생겼다
    dest = tmp_path / "_snap"
    gw.extract_snapshot(sha, "finance_ops", dest)
    assert (dest / "index.md").read_text(encoding="utf-8") == "# 목차"
    assert (dest / "pricing.md").read_text(encoding="utf-8") == "가격 정책 본문"


def test_okf_디렉터리_없는_카드는_건너뛴다(tmp_path: Path):
    # finance_ops만 디스크 번들이 있고 hr_ops는 없다 → hr_ops엔 커밋이 안 생긴다.
    bundle = tmp_path / "finance_ops"
    bundle.mkdir()
    (bundle / "index.md").write_text("# 목차", encoding="utf-8")
    reg = _registry(_card("finance_ops", "finance_lead"), _card("hr_ops", "hr_lead"))

    gw = FakeGitGateway()
    seed_gateway_from_disk(gw, reg, tmp_path)

    assert gw.head_sha("finance_ops")  # 있다
    try:
        gw.head_sha("hr_ops")
        raise AssertionError("hr_ops엔 커밋이 없어야 한다")
    except ValueError:
        pass


def test_빈_okf_디렉터리는_커밋하지_않는다(tmp_path: Path):
    # 디렉터리는 있지만 *.md가 없으면 빈 커밋을 만들지 않는다(commit_okf_bundle 빈 files 거부 회피).
    (tmp_path / "finance_ops").mkdir()
    reg = _registry(_card("finance_ops", "finance_lead"))

    gw = FakeGitGateway()
    seed_gateway_from_disk(gw, reg, tmp_path)  # 예외 없이 통과

    try:
        gw.head_sha("finance_ops")
        raise AssertionError("빈 디렉터리엔 커밋이 없어야 한다")
    except ValueError:
        pass
