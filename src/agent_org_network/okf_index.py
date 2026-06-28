"""OKF→KnowledgeIndex 어댑터 (ADR 0028 §13 Phase 10 라이브 슬라이스·데모 지름길).

owner의 OKF 번들(`okf/{agent_id}/*.md`, YAML 프론트매터)을 읽어 중앙용 경량
*목차*(KnowledgeIndex)를 도출한다. 순수·결정론(stdlib+pyyaml만·공급자 SDK 0).

정직한 경계(데모 지름길):
  실 경로(T10.4)는 *owner가 자기 환경에서 인덱스를 도출해 중앙에 publish*한다 —
  중앙은 owner의 OKF 내용을 보지 않는다(중앙 비소유). 이 함수는 데모를 한 바퀴
  돌리려고 *중앙이 repo의 okf/를 직접 읽어 인덱스를 시드*하는 지름길이다. 도출된
  인덱스 자체는 여전히 목차(내용 0·Concept는 title/description 토큰만)지만, "owner가
  도출·배포"라는 분산 경로를 데모에선 in-process 시드로 단축한다. 이 차이를 명시.

도출 규칙(파일당 1 Concept):
  - 대상: `okf_root/{card.agent_id}/*.md`를 *파일명 정렬*로 읽는다.
  - 프론트매터: 첫 `---`…`---` 블록을 yaml.safe_load(없으면 빈 dict).
  - id        = 파일 stem(파일명 확장자 제거).
  - label     = 프론트매터 title(없으면 stem).
  - core_question = f"{title}. {description}"(매칭 토큰 확보) — title/description이
                    비면 가용한 쪽만, 둘 다 비면 stem으로 폴백(빈 core_question 금지).
  - type      = 프론트매터 type(없으면 None).
  - domain    = 프론트매터 tags 중 *card.domains에 든 첫 태그*. 없으면 card.domains[0]
                로 폴백. card.domains가 비면 그 문서 skip(권한 불가 — domain 못 정함).

  OKF 디렉터리 없음/문서 없음 → concepts=() 빈 인덱스.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml

from agent_org_network.agent_card import AgentCard
from agent_org_network.knowledge_index import Concept, KnowledgeIndex


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """문서 앞머리의 첫 `---`…`---` YAML 블록을 dict로 파싱한다.

    프론트매터가 없거나 형식이 어긋나면 빈 dict(목차 도출은 폴백으로 계속).
    yaml.safe_load 결과가 dict가 아니면(리스트·스칼라) 빈 dict 취급.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    # 두 번째 `---`(닫는 구분자)을 찾는다.
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            block = "\n".join(lines[1:end])
            loaded: object = yaml.safe_load(block)
            if isinstance(loaded, dict):
                return cast(dict[str, Any], loaded)
            return {}
    return {}


def _pick_domain(tags: Sequence[str], card_domains: Sequence[str]) -> str | None:
    """tags 중 card_domains에 든 첫 태그를 고른다(없으면 domains[0] 폴백).

    card_domains가 비면 None(권한 불가 — 호출자가 그 문서를 skip).
    tags 순서를 보존해 결정론(파일 내 선언 순서가 우선순위).
    """
    if not card_domains:
        return None
    for tag in tags:
        if tag in card_domains:
            return tag
    return card_domains[0]


def _build_core_question(title: str, description: str, stem: str) -> str:
    """매칭 토큰 확보용 core_question을 만든다.

    title·description 둘 다 있으면 "title. description", 한쪽만 있으면 그쪽,
    둘 다 비면 stem(빈 문자열 금지 — Concept.core_question 검증 통과 보장).
    """
    title = (title or "").strip()
    description = (description or "").strip()
    if title and description:
        return f"{title}. {description}"
    if title:
        return title
    if description:
        return description
    return stem


def build_knowledge_index_from_okf(
    card: AgentCard,
    okf_root: Path,
    *,
    generated_at: datetime,
    version: str = "okf-1",
) -> KnowledgeIndex:
    """card.agent_id의 OKF 번들에서 KnowledgeIndex(목차)를 도출한다(순수·결정론).

    okf_root/{card.agent_id}/*.md를 파일명 정렬로 읽어 각 문서의 프론트매터에서
    Concept를 도출한다(위 도출 규칙). OKF 디렉터리/문서가 없으면 빈 인덱스를 돌려준다.

    데모 지름길: 중앙이 repo okf/를 직접 읽어 인덱스를 시드한다(실 경로는 owner publish·
    모듈 docstring 참조). 같은 OKF·같은 generated_at → 같은 인덱스(결정론).
    """
    agent_dir = okf_root / card.agent_id
    concepts: list[Concept] = []

    if agent_dir.is_dir():
        for md_path in sorted(agent_dir.glob("*.md"), key=lambda p: p.name):
            text = md_path.read_text(encoding="utf-8")
            front = _parse_frontmatter(text)

            stem = md_path.stem
            title = str(front.get("title", "") or "")
            description = str(front.get("description", "") or "")

            raw_tags: object = front.get("tags", [])
            tags: list[str] = (
                [str(t) for t in cast(list[object], raw_tags)]
                if isinstance(raw_tags, list)
                else []
            )
            domain = _pick_domain(tags, card.domains)
            if domain is None:
                # card.domains가 비어 domain을 정할 수 없음 → 권한 불가 → skip.
                continue

            raw_type = front.get("type")
            concept_type = str(raw_type) if raw_type is not None else None

            concepts.append(
                Concept(
                    id=stem,
                    label=title or stem,
                    core_question=_build_core_question(title, description, stem),
                    domain=domain,
                    type=concept_type,
                )
            )

    return KnowledgeIndex(
        agent_id=card.agent_id,
        version=version,
        generated_at=generated_at,
        concepts=tuple(concepts),
    )
