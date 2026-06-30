"""OKF Authoring 값 객체 — T11.1 (ADR 0029 S1·경로 위생은 ADR 0028 §15).
T11.2 — OkfAuthor 포트·FakeAuthor·staged 오케스트레이터·실 어댑터는 T11.7 게이트 밖.
T11.3 — HITL 상태기계·OKF admission 검증·마크다운 직렬화·commit seam.

owner가 OKF 초안을 작성할 때 쓰는 frozen pydantic v2 값 객체.
SDK 0·IO 0·결정론. 권한(domain∈card.domains) 검증은 하지 않는다(ADR 0004·형식만).

agent_card.py: validate_agent_id_format·validate_safe_path_component 공유 헬퍼 재사용.
knowledge_index.py: ConceptEdge 재사용(새 edge 타입 정의 금지).

concept_id admission은 소비측 okf_index 도출(파일 stem=concept.id·glob)보다 *더 엄격한
superset*이다 — okf_index는 파일시스템이 stem을 보증해 별도 거부를 안 하지만, 저작은
구분자·`..`·절대경로를 능동 거부한다(저작 산출 ⊆ okf_index 수용 집합·안전 방향). T11.4
합류 시 이 비대칭을 결함으로 오해하지 말 것.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

import yaml
from pydantic import BaseModel, field_validator, model_validator

from agent_org_network.agent_card import validate_agent_id_format, validate_safe_path_component
from agent_org_network.git_gateway import BuilderCommitRequest, OkfFile
from agent_org_network.knowledge_index import ConceptEdge

if TYPE_CHECKING:
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.knowledge_index import KnowledgeIndex
    from agent_org_network.transport import PublishIndex


class RawSource(BaseModel, frozen=True):
    """owner가 제공하는 원본 소스 텍스트 — concept 추출의 입력 단위.

    source_id: owner 내부 식별자(파일/중앙 미도달) — path sanitization 적용 안 함.
    content: 비어 있으면 추출 불가이므로 거부.
    """

    source_id: str
    content: str

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("RawSource.source_id는 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("RawSource.content는 빈 문자열/공백이 될 수 없습니다.")
        return value


class OkfDocumentDraft(BaseModel, frozen=True):
    """OKF 문서 초안 — concept 한 건의 작성 단위.

    concept_id: OKF 파일 stem이 되므로 path-traversal 화이트리스트 적용
                (validate_safe_path_component — agent_card.py 단일 권위).
    domain: 형식 검증만. 권한(domain∈card.domains) 검증은 라우팅/publish 수용 시
            중앙에서 한다(ADR 0004 — 자기보고 금지·Authority 중앙).
    type: 무검증·선택 필드.
    """

    concept_id: str
    title: str
    body: str
    core_question: str
    domain: str
    type: str | None = None

    @field_validator("concept_id")
    @classmethod
    def _validate_concept_id(cls, value: str) -> str:
        return validate_safe_path_component(value)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.title은 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.body는 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("core_question")
    @classmethod
    def _validate_core_question(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.core_question은 빈 문자열/공백이 될 수 없습니다.")
        return value

    @field_validator("domain")
    @classmethod
    def _validate_domain(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("OkfDocumentDraft.domain은 빈 문자열/공백이 될 수 없습니다.")
        return value


class OkfDraft(BaseModel, frozen=True):
    """OKF 초안 번들 — 한 agent의 문서 집합과 개념 간 관계.

    agent_id: validate_agent_id_format 공유 헬퍼(ADR 0023 — agent_card.py 단일 권위).
    documents: 중복 concept_id 거부. 빈 튜플 허용.
    edges: ConceptEdge 재사용(knowledge_index.py). 기본 빈 튜플.
           dangling edge(documents에 없는 concept)는 거부하지 않는다 — T11.3 책임.
    """

    agent_id: str
    documents: tuple[OkfDocumentDraft, ...]
    edges: tuple[ConceptEdge, ...] = ()

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        return validate_agent_id_format(value)

    @model_validator(mode="after")
    def _validate_no_duplicate_concept_id(self) -> OkfDraft:
        ids = [doc.concept_id for doc in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("OkfDraft.documents 내 concept_id는 유일해야 합니다(중복 거부).")
        return self


# ── T11.2: OkfAuthor 포트·AuthoredOkf·FakeAuthor·staged 오케스트레이터 ────────


class OkfAuthor(Protocol):
    """staged OKF 저작 포트 — Classifier·KnowledgeIndexMatcher 포트와 동일 패턴.

    단계 분리: split(2단계 개념 분할) → derive_core_questions(3단계 core_question 정련)
               → link(4단계 edges). one-shot 단일 메서드 두지 않는다(T11.2 설계).

    구현: FakeAuthor(테스트)·실 어댑터는 T11.7 게이트 밖.
    """

    def split(
        self, sources: Sequence[RawSource], allowed_domains: Sequence[str]
    ) -> tuple[OkfDocumentDraft, ...]:
        """2단계: 원본 소스를 개념 단위로 분할한다.

        allowed_domains: 이 작성자의 권한 domain *힌트*(card.domains). 개념이 이 중 하나에
            해당하면 그 라벨을 정확히 그대로 domain에 쓰게 유도한다(매칭률↑). 강제 분류가
            아니다 — 어디에도 안 맞으면 실제 domain을 쓰고 over-claim 필터(admit_okf)가
            정상 drop한다. 빈 시퀀스면 기존 자유 분류(하위호환).
        """
        ...

    def derive_core_questions(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[OkfDocumentDraft, ...]:
        """3단계: 각 개념 초안의 core_question을 정련한다."""
        ...

    def link(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[ConceptEdge, ...]:
        """4단계: 개념 간 관계(edges)를 도출한다."""
        ...


class AuthoredOkf(BaseModel, frozen=True):
    """staged 파이프라인 산출 래퍼 — OkfDraft + 원본 RawSource 보존.

    sources: T11.5 증분 diff seam — 어느 raw가 어느 concept 낳았나 추적 자리.
             지금은 보존만·매핑 0.
    """

    draft: OkfDraft
    sources: tuple[RawSource, ...]


class FakeAuthor:
    """OkfAuthor Protocol 테스트 더블 — FakeMatcher·StubRuntime 정신.

    생성 시 주입한 고정 산출을 입력 무관하게 그대로 반환한다.
    관측 seam(split_seen·derive_seen·link_seen)으로 staged 순차 흐름·데이터 배선을 단언한다.
    """

    def __init__(
        self,
        split_result: tuple[OkfDocumentDraft, ...],
        derive_result: tuple[OkfDocumentDraft, ...],
        link_result: tuple[ConceptEdge, ...],
    ) -> None:
        self._split_result = split_result
        self._derive_result = derive_result
        self._link_result = link_result
        self.split_seen: tuple[RawSource, ...] | None = None
        self.split_allowed_domains_seen: tuple[str, ...] | None = None
        self.derive_seen: tuple[OkfDocumentDraft, ...] | None = None
        self.link_seen: tuple[OkfDocumentDraft, ...] | None = None

    def split(
        self, sources: Sequence[RawSource], allowed_domains: Sequence[str]
    ) -> tuple[OkfDocumentDraft, ...]:
        """입력 무관 고정 산출 반환·split_seen/split_allowed_domains_seen에 입력 기록.

        allowed_domains는 테스트 더블 정신으로 *무시*하고 고정 산출을 유지한다.
        다만 파이프라인이 권한 domain을 실제로 전달하는지 단언할 관측 seam에 기록한다.
        """
        self.split_seen = tuple(sources)
        self.split_allowed_domains_seen = tuple(allowed_domains)
        return self._split_result

    def derive_core_questions(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[OkfDocumentDraft, ...]:
        """입력 무관 고정 산출 반환·derive_seen에 입력 기록."""
        self.derive_seen = tuple(drafts)
        return self._derive_result

    def link(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[ConceptEdge, ...]:
        """입력 무관 고정 산출 반환·link_seen에 입력 기록."""
        self.link_seen = tuple(drafts)
        return self._link_result


def run_authoring_pipeline(
    agent_id: str,
    sources: Sequence[RawSource],
    author: OkfAuthor,
    allowed_domains: Sequence[str] = (),
) -> AuthoredOkf:
    """staged OKF 저작 오케스트레이터 — 순수 함수.

    1단계(인제스트)는 sources 인자로 이미 받았다고 전제(T11.6 Ingestor seam).
    5단계(인덱싱)는 하지 않는다(T11.4 seam — KnowledgeIndex 미선취).
    HITL 승인 상태는 부여하지 않는다(T11.3 seam).

    allowed_domains: 작성자 권한 domain 힌트(card.domains) — split에 그대로 전달한다.
        domain 라벨링을 권한과 정렬해 매칭률을 높이는 힌트지 강제가 아니다(over-claim
        필터 보존). 기본 빈 tuple은 자유 분류(하위호환).
    """
    split_drafts = author.split(sources, allowed_domains)  # 2단계: 개념 분할
    derived = author.derive_core_questions(split_drafts)  # 3단계: core_question 정련
    edges = author.link(derived)                    # 4단계: edges 도출
    draft = OkfDraft(agent_id=agent_id, documents=derived, edges=edges)
    return AuthoredOkf(draft=draft, sources=tuple(sources))


# ── T11.3: HITL 상태기계 + OKF admission 검증 ────────────────────────────────


# 처분 sealed sum (decision.py 패턴)
@dataclass(frozen=True)
class Approved:
    """owner 검토 결과 — 원 산출 그대로 승인."""


@dataclass(frozen=True)
class Edited:
    """owner 수정 — 교체본. 단계 산출을 교체한다.

    split/derive 단계 교체본은 documents에, link 단계 교체본은 edges에 넣는다.
    교체본도 OkfDocumentDraft/ConceptEdge 값 객체 검증을 그대로 받는다.
    """

    documents: tuple[OkfDocumentDraft, ...] = ()
    edges: tuple[ConceptEdge, ...] = ()


@dataclass(frozen=True)
class Rejected:
    """owner 검토 결과 — 거부 종착. 다음 단계 입력 제외."""

    reason: str = ""


StageDisposition = Approved | Edited | Rejected


@dataclass(frozen=True)
class StageReview:
    """단계 검토 래퍼(frozen) — 단계 산출과 owner 처분을 묶는다.

    stage: 검토 대상 단계("split" | "derive" | "link").
    documents: ②split·③derive 단계 산출(④link이면 ()).
    edges: ④link 단계 산출(②③이면 ()).
    disposition: None=staged(미검토), Approved/Edited/Rejected=처분 완료.

    processed disposition은 단조(되돌리기 없음) — 이미 처분된 것을 재처분하면 ValueError.
    단계×payload 불일치(split 단계인데 Edited.edges만 채움 등)는 ValueError.
    """

    stage: Literal["split", "derive", "link"]
    documents: tuple[OkfDocumentDraft, ...] = ()
    edges: tuple[ConceptEdge, ...] = ()
    disposition: StageDisposition | None = None


def set_disposition(review: StageReview, disposition: StageDisposition) -> StageReview:
    """처분 부여 — 함수형 전이(frozen→새 StageReview).

    이미 처분된 StageReview 재처분 거부(단조·되돌리기 없음).
    단계×payload 불일치 거부 — 단계가 요구하는 필드는 필수·금지 필드는 거부:
      - "split"/"derive": documents 교체본 필수(비면 거부)·edges 금지(차면 거부)
      - "link": edges 교체본 필수(비면 거부)·documents 금지(차면 거부)
    빈 Edited()·혼합 Edited(documents+edges) 모두 거부된다.
    """
    if review.disposition is not None:
        raise ValueError(
            f"이미 처분된 StageReview를 재처분할 수 없습니다: {review.disposition!r}"
        )
    if isinstance(disposition, Edited):
        if review.stage in ("split", "derive"):
            if not disposition.documents:
                raise ValueError(
                    f"단계×payload 불일치: {review.stage!r} 단계 Edited는 "
                    f"documents 교체본이 필요합니다(비어 있음)."
                )
            if disposition.edges:
                raise ValueError(
                    f"단계×payload 불일치: {review.stage!r} 단계 Edited에 "
                    f"edges가 들어갈 수 없습니다(documents 교체)."
                )
        elif review.stage == "link":
            if not disposition.edges:
                raise ValueError(
                    "단계×payload 불일치: link 단계 Edited는 "
                    "edges 교체본이 필요합니다(비어 있음)."
                )
            if disposition.documents:
                raise ValueError(
                    "단계×payload 불일치: link 단계 Edited에 "
                    "documents가 들어갈 수 없습니다(edges 교체)."
                )
    return StageReview(
        stage=review.stage,
        documents=review.documents,
        edges=review.edges,
        disposition=disposition,
    )


# admission 검증 결과
@dataclass(frozen=True)
class AdmissionResult:
    """OKF admission 검증 결과(순수·IO 0).

    admitted: 권한·닫힘 통과 후 publish 가능한 OkfDraft.
    dropped_concepts: over-claim 필터(domain_authorized 거짓 concept — 전체 거부 아님).
    dropped_edges: 떨군 개념 탓 dangling으로 제거된 edge.
    violations: 필터로 못 흡수한 잔여 dangling(있으면 publish 불가).
    """

    admitted: OkfDraft
    dropped_concepts: tuple[str, ...] = ()
    dropped_edges: tuple[ConceptEdge, ...] = ()
    violations: tuple[str, ...] = ()


def admit_okf(draft: OkfDraft, card: "AgentCard") -> AdmissionResult:
    """OKF admission 순수 함수 — 권한 필터 → dangling 제거 → 잔여 violations.

    검증 순서:
      ① 권한 필터: domain_authorized 거짓 concept를 dropped_concepts로 제거(전체 거부 아님).
      ② 제거로 dangling된 edge를 dropped_edges로 제거.
      ③ 잔여 dangling edge(documents에 없는 concept 가리킴)는 violations(진짜 결함).

    admission 실패는 예외 아님 — AdmissionResult(violations 채워짐) 반환.
    """
    from agent_org_network.agent_card import domain_authorized

    # ① 권한 필터
    kept_docs: list[OkfDocumentDraft] = []
    dropped: list[str] = []
    for doc in draft.documents:
        if domain_authorized(doc.domain, card):
            kept_docs.append(doc)
        else:
            dropped.append(doc.concept_id)

    kept_ids = {doc.concept_id for doc in kept_docs}

    # ② 제거 탓 dangling edge → dropped_edges
    kept_edges: list[ConceptEdge] = []
    dropped_edges: list[ConceptEdge] = []
    for edge in draft.edges:
        if edge.from_id in dropped or edge.to_id in dropped:
            dropped_edges.append(edge)
        else:
            kept_edges.append(edge)

    # ③ 잔여 dangling edge(kept_ids에 없는 concept 가리킴) → violations
    violations: list[str] = []
    for edge in kept_edges:
        if edge.from_id not in kept_ids or edge.to_id not in kept_ids:
            violations.append(
                f"dangling edge: {edge.from_id!r} → {edge.to_id!r} "
                f"(참조하는 concept가 번들에 없습니다)"
            )

    admitted_draft = OkfDraft(
        agent_id=draft.agent_id,
        documents=tuple(kept_docs),
        edges=tuple(kept_edges),
    )
    return AdmissionResult(
        admitted=admitted_draft,
        dropped_concepts=tuple(dropped),
        dropped_edges=tuple(dropped_edges),
        violations=tuple(violations),
    )


def render_okf_markdown(doc: OkfDocumentDraft) -> str:
    """OkfDocumentDraft → 마크다운 직렬화(순수·IO 0).

    프론트매터 + 본문. okf_index 도출 정합(round-trip):
      - concept_id → 파일 stem(호출부 책임)
      - title → frontmatter title
      - core_question → frontmatter description (okf_index가 description에서 재도출)
      - domain → frontmatter tags 리스트에 실음 (okf_index가 tags에서 domain 도출)
      - type → frontmatter type (있을 때만)
      - body → 본문

    T11.4가 이 렌더러를 재사용(중복 금지).

    프론트매터는 yaml.safe_dump으로 직렬화한다 — 값에 콜론·`#` 등 YAML 특수문자가 있어도
    안전하게 인용돼 okf_index._parse_frontmatter(yaml.safe_load) round-trip이 깨지지 않는다.
    """
    front: dict[str, object] = {
        "title": doc.title,
        "description": doc.core_question,
        "tags": [doc.domain],
    }
    if doc.type is not None:
        front["type"] = doc.type
    block = yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
    return f"---\n{block}---\n\n{doc.body}"


def build_index_from_admitted(
    admitted: OkfDraft,
    card: "AgentCard",
    okf_root: Path,
    *,
    generated_at: datetime,
    version: str = "okf-1",
) -> "KnowledgeIndex":
    """승인된 OkfDraft → KnowledgeIndex(목차) 생성 합류(T11.4).

    각 doc을 render_okf_markdown으로 직렬화해 okf_root/{agent_id}/{concept_id}.md에 쓴 뒤,
    build_knowledge_index_from_okf(도출 단일 권위)를 그대로 호출해 반환한다.
    새 도출 규칙 0 — render(직렬화)+build(도출) 두 기존 함수만 조립.
    빈 admitted.documents → 파일 0개 → 빈 인덱스(concepts=()) 정상 반환.

    **호출자는 임시/격리 디렉터리를 okf_root로 줘야 한다** — 이 함수는
    okf_root/{agent_id}/{concept_id}.md를 무조건 덮어쓴다(mkdir exist_ok·write_text).
    실 OKF 본체 디렉터리를 주면 owner 지식을 덮어쓴다(경로는 sanitize됐으나 덮어쓰기는 막지
    않음). 실 OKF 디렉터리 커밋은 prepare_commit_request → commit_okf_bundle 경로(T11.7).
    """
    from agent_org_network.okf_index import build_knowledge_index_from_okf

    agent_dir = okf_root / admitted.agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)

    for doc in admitted.documents:
        content = render_okf_markdown(doc)
        (agent_dir / f"{doc.concept_id}.md").write_text(content, encoding="utf-8")

    return build_knowledge_index_from_okf(
        card, okf_root, generated_at=generated_at, version=version
    )


def to_publish_index(index: "KnowledgeIndex") -> "PublishIndex":
    """KnowledgeIndex → PublishIndex 프레임 변환(T11.4).

    기존 PublishIndex 프레임 구성(transport.py). index.agent_id 보존.
    """
    from agent_org_network.transport import PublishIndex

    return PublishIndex(index=index)


def publish_index_from_admission(
    result: AdmissionResult,
    card: "AgentCard",
    okf_root: Path,
    *,
    generated_at: datetime,
    version: str = "okf-1",
) -> "PublishIndex | None":
    """AdmissionResult에서 PublishIndex를 구성하는 얇은 헬퍼(T11.4 violations 가드).

    violations가 비어있지 않으면 None(publish 안 함).
    violations가 없으면 build_index_from_admitted → to_publish_index 순으로 구성해 반환.
    """
    if result.violations:
        return None
    index = build_index_from_admitted(
        result.admitted, card, okf_root, generated_at=generated_at, version=version
    )
    return to_publish_index(index)


# ── T11.5: 증분 diff 로직 ─────────────────────────────────────────────────────


class ReindexRequest(BaseModel, frozen=True):
    """증분 재인덱싱 요청 — 거친 매칭 키·변경 소스·직전 산출·staleness 키.

    agent_id: 거친 매칭 키(prior.draft.agent_id와 반드시 일치해야 함).
    changed_sources: 이번에 바뀐 raw 소스(빈 튜플 허용 → no-op).
    prior: 직전 AuthoredOkf 산출(보존 원천).
    generated_at: 재인덱싱 staleness 키(호출자가 build_index_from_admitted에 전달).
    """

    agent_id: str
    changed_sources: tuple[RawSource, ...]
    prior: AuthoredOkf
    generated_at: datetime

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, value: str) -> str:
        return validate_agent_id_format(value)


class ReindexResult(BaseModel, frozen=True):
    """증분 재인덱싱 결과.

    reauthored: 재처리 후 합친 전체 AuthoredOkf.
    reprocessed_concept_ids: 이번 changed_sources에서 새로 도출된 concept_id 집합(정렬).
    preserved_concept_ids: prior에서 그대로 보존된 concept_id 집합.
    relinked: link 전체 재호출 여부(no-op이면 False).
    """

    reauthored: AuthoredOkf
    reprocessed_concept_ids: tuple[str, ...]
    preserved_concept_ids: tuple[str, ...]
    relinked: bool


def reindex_incrementally(req: ReindexRequest, author: OkfAuthor) -> ReindexResult:
    """증분 재인덱싱 순수 함수 — changed_sources에 영향받은 concept만 재처리·나머지 보존.

    흐름:
      정합 검증: agent_id != prior.draft.agent_id → ValueError(changed 유무 무관).
      no-op: changed_sources가 비면 prior 그대로 반환(relinked=False).
      ② 영향 재처리: changed_sources만 split → derive_core_questions.
      ③ 합치기: 재도출분이 충돌 concept_id에서 승(prior에서 제외·merged 병합).
      ④ link 전체 재호출: merged_docs 전체 → dangling 구조적 방지.
      sources 갱신: source_id 기준 changed_sources 덮어쓰기·신규 추가.

    재인덱싱(5단계)·리스너·ReindexMode은 여기서 안 한다(T11.4/T11.7 이월).
    """
    prior = req.prior

    # 정합 검증 — changed 유무 무관하게 bundle 키 어긋남 거부(no-op보다 앞)
    if req.agent_id != prior.draft.agent_id:
        raise ValueError(
            f"agent_id 불일치: ReindexRequest.agent_id={req.agent_id!r}이지만 "
            f"prior.draft.agent_id={prior.draft.agent_id!r}입니다. "
            "bundle 거친 매칭 키가 어긋납니다."
        )

    # no-op: changed_sources 비면 prior 그대로
    if not req.changed_sources:
        preserved_ids = tuple(sorted(d.concept_id for d in prior.draft.documents))
        return ReindexResult(
            reauthored=prior,
            reprocessed_concept_ids=(),
            preserved_concept_ids=preserved_ids,
            relinked=False,
        )

    # ② 영향 재처리: changed_sources만 split
    # allowed_domains는 ReindexRequest에 없어 빈 tuple(자유 분류) — 증분 재인덱싱은
    # prior와 동일 작성자 컨텍스트를 가정하고, domain 정합은 admit_okf가 최종 보증한다.
    split_drafts = author.split(req.changed_sources, ())
    reprocessed = author.derive_core_questions(split_drafts)

    # ③ 합치기: 재도출분이 충돌 concept_id에서 승
    reprocessed_ids = {d.concept_id for d in reprocessed}
    preserved = [d for d in prior.draft.documents if d.concept_id not in reprocessed_ids]
    merged_docs = tuple(reprocessed) + tuple(preserved)

    # ④ link 전체 재호출(부분 패치 아님·전체 집합 → dangling 구조적 방지)
    edges = author.link(merged_docs)

    # sources 갱신: source_id별 changed 승·신규 source 추가
    changed_map = {s.source_id: s for s in req.changed_sources}
    updated_sources_list: list[RawSource] = []
    for src in prior.sources:
        if src.source_id in changed_map:
            updated_sources_list.append(changed_map[src.source_id])
        else:
            updated_sources_list.append(src)
    # 신규 source(prior에 없던 것) 추가
    prior_source_ids = {s.source_id for s in prior.sources}
    for src in req.changed_sources:
        if src.source_id not in prior_source_ids:
            updated_sources_list.append(src)
    updated_sources = tuple(updated_sources_list)

    reauthored = AuthoredOkf(
        draft=OkfDraft(
            agent_id=req.agent_id,
            documents=merged_docs,
            edges=edges,
        ),
        sources=updated_sources,
    )

    return ReindexResult(
        reauthored=reauthored,
        reprocessed_concept_ids=tuple(sorted(reprocessed_ids)),
        preserved_concept_ids=tuple(sorted(d.concept_id for d in preserved)),
        relinked=True,
    )


def prepare_commit_request(admitted: OkfDraft, *, owner: str) -> BuilderCommitRequest:
    """승인된 OkfDraft를 BuilderCommitRequest seam으로 변환(순수·IO 0).

    각 doc을 OkfFile(path="{concept_id}.md", content=render_okf_markdown(doc))로 매핑.
    author=owner, agent_id=draft.agent_id(ADR 0018).

    실 commit_okf_bundle 호출·gateway 주입·propagator는 안 함(mcp-runtime T11.7 seam).
    """
    files = tuple(
        OkfFile(
            path=f"{doc.concept_id}.md",
            content=render_okf_markdown(doc),
        )
        for doc in admitted.documents
    )
    return BuilderCommitRequest(
        agent_id=admitted.agent_id,
        owner=owner,
        files=files,
        message=f"OKF 번들 업데이트: {admitted.agent_id}",
    )


# ── T11.6: Ingestor 포트 + 텍스트 어댑터(MVP) ────────────────────────────────


class Ingestor(Protocol):
    """인제스트 포트 — N 입력 → N RawSource(1:1·분할 안 함).

    각 원소 = (source_id, raw_text). 분할은 OkfAuthor.split 책임.
    구현: TextIngestor(MVP·IO 0)·실 PDF/위키 어댑터는 T11.7.
    """

    def ingest(self, items: Sequence[tuple[str, str]]) -> tuple[RawSource, ...]:
        """(source_id, raw_text) 시퀀스 → RawSource 튜플(순서 보존)."""
        ...


class TextIngestor:
    """Ingestor MVP 어댑터 — 순수 결정론·IO 0.

    각 (source_id, content) → RawSource(source_id=source_id.strip(), content=content.strip()).
    content 앞뒤 공백만 trim — 내부 개행/공백 보존(마크다운 구조).
    빈/공백 거부는 RawSource 검증에 위임(별도 로직 없음).
    중복 source_id 거부 안 함(MVP). content 해시·source_id 생성 안 함.
    """

    def ingest(self, items: Sequence[tuple[str, str]]) -> tuple[RawSource, ...]:
        """(source_id, raw_text) 시퀀스 → RawSource 튜플(순서 보존)."""
        return tuple(
            RawSource(source_id=source_id.strip(), content=content.strip())
            for source_id, content in items
        )


# ── T11.7c: LlmAuthor 실 어댑터 + 프롬프트/파싱 순수 함수 ──────────────────────


import json as _json  # noqa: E402
from typing import cast  # noqa: E402


from agent_org_network.provider_runtime import (  # noqa: E402
    ProviderRequest,
    ProviderTransport,
    assemble_stream,
)

_SPLIT_SYSTEM = (
    "원본 소스를 개념 단위로 분할해 JSON 배열만 반환하라. 코드펜스·산문 금지.\n"
    '스키마: [{"concept_id": "파일명 안전 슬러그·소문자/하이픈만·구분자/`..`/공백 금지",'
    ' "title": "str", "body": "str", "core_question": "str",'
    ' "domain": "str", "type": null}]'
)

_DERIVE_SYSTEM = (
    "각 개념의 core_question을 정련해 JSON 배열만 반환하라. 코드펜스·산문 금지.\n"
    '스키마: [{"concept_id": "str", "core_question": "str"}]'
)

_LINK_SYSTEM_HEADER = (
    "개념 간 관계(edges)를 도출해 JSON 배열만 반환하라. 코드펜스·산문 금지.\n"
    "아래 화이트리스트에 없는 concept_id를 가리키는 관계는 절대 만들지 마라.\n"
    "유효 concept_id 목록:\n"
)
_LINK_SYSTEM_FOOTER = (
    '\n스키마: [{"from_id": "str", "to_id": "str", "relation": "str"}]'
)


def _split_system_for(allowed_domains: Sequence[str]) -> str:
    """allowed_domains가 비어있지 않으면 _SPLIT_SYSTEM에 권한 domain 절을 덧댄다.

    권한 목록은 *힌트/컨텍스트*다(강제 분류 아님): 개념이 권한 domain 중 하나에 해당하면
    그 라벨을 정확히 그대로 쓰게 유도(매칭률↑), 어디에도 안 맞으면 실제 주제를 domain으로
    쓰게 둔다(over-claim 필터가 정상 drop). 빈 시퀀스면 권한 절을 안 넣는다(하위호환).
    """
    if not allowed_domains:
        return _SPLIT_SYSTEM
    listed = ", ".join(allowed_domains)
    clause = (
        f"\n이 작성자의 권한 도메인: [{listed}].\n"
        "각 개념이 이 권한 도메인 중 하나에 해당하면 그 라벨을 정확히 그대로 "
        '"domain"에 쓰라(글자 그대로·임의 변형 금지).\n'
        '어느 것에도 해당하지 않으면 개념의 실제 주제를 "domain"으로 쓰라'
        "(권한 밖으로 분류될 수 있음 — 강제로 권한 도메인에 끼워 넣지 마라)."
    )
    return _SPLIT_SYSTEM + clause


def build_split_request(
    sources: Sequence[RawSource],
    *,
    model: str,
    allowed_domains: Sequence[str] = (),
) -> ProviderRequest:
    """raw sources → 개념 분할 요청(순수·IO 0).

    allowed_domains: 작성자 권한 domain 힌트. 비어있지 않으면 system 프롬프트에 권한 절을
        주입해 domain 라벨링을 권한과 정렬한다(힌트지 강제 아님 — over-claim 필터 보존).
        빈 시퀀스면 기존 자유 분류(하위호환).
    """
    lines = [f"[{src.source_id}] {src.content}" for src in sources]
    user_content = "\n\n".join(lines)
    return ProviderRequest(
        model=model,
        system=_split_system_for(allowed_domains),
        messages=[{"role": "user", "content": user_content}],
    )


def build_derive_request(
    drafts: Sequence[OkfDocumentDraft],
    *,
    model: str,
) -> ProviderRequest:
    """drafts → core_question 정련 요청(순수·IO 0)."""
    lines = [
        f"[{d.concept_id}] {d.title} / {d.body[:80]} / 현 core_question: {d.core_question}"
        for d in drafts
    ]
    user_content = "\n\n".join(lines)
    return ProviderRequest(
        model=model,
        system=_DERIVE_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )


def build_link_request(
    drafts: Sequence[OkfDocumentDraft],
    *,
    model: str,
) -> ProviderRequest:
    """drafts → edges 도출 요청(순수·IO 0)."""
    whitelist = "\n".join(f"- {d.concept_id}" for d in drafts)
    system = _LINK_SYSTEM_HEADER + whitelist + _LINK_SYSTEM_FOOTER
    lines = [f"[{d.concept_id}] {d.title} — {d.core_question}" for d in drafts]
    user_content = "\n".join(lines)
    return ProviderRequest(
        model=model,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )


def _strip_code_fence(text: str) -> str:
    """LLM 응답의 마크다운 코드펜스(```json … ```)를 벗긴다(순수·IO 0).

    프롬프트로 "코드펜스 금지"를 지시해도 LLM이 간헐적으로 펜스를 붙이므로,
    JSON 파싱 전 방어적으로 제거한다(parse_split/derive/link 공통 전처리).
    """
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _require_str(item: dict[str, object], key: str) -> str:
    """dict에서 key를 꺼내 str로 반환. 누락·None이면 ValueError."""
    if key not in item:
        raise ValueError(f"필드 누락: {key!r}")
    value = item[key]
    if value is None:
        raise ValueError(f"필드가 null입니다: {key!r}")
    return str(value)


def parse_split_response(text: str) -> tuple[OkfDocumentDraft, ...]:
    """split 응답 JSON → OkfDocumentDraft tuple(순수·IO 0).

    JSON 깨짐·배열 아님 → ValueError(fail-loud).
    값 객체 생성자에 검증 위임(빈 body·unsafe concept_id 등).
    """
    try:
        raw = _json.loads(_strip_code_fence(text))
    except _json.JSONDecodeError as exc:
        raise ValueError(f"split 응답 JSON 파싱 실패: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"split 응답이 JSON 배열이 아닙니다: {type(raw).__name__}")
    data = cast(list[dict[str, object]], raw)
    try:
        return tuple(
            OkfDocumentDraft(
                concept_id=_require_str(item, "concept_id"),
                title=_require_str(item, "title"),
                body=_require_str(item, "body"),
                core_question=_require_str(item, "core_question"),
                domain=_require_str(item, "domain"),
                type=str(item["type"]) if item.get("type") is not None else None,
            )
            for item in data
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"split 응답 필드 누락/타입 오류: {exc}") from exc


def parse_derive_response(
    text: str,
    originals: Sequence[OkfDocumentDraft],
) -> tuple[OkfDocumentDraft, ...]:
    """derive 응답 JSON → originals에 core_question 델타만 적용(순수·IO 0).

    title/body/domain/type 보존(환각 차단).
    originals에 없는 concept_id는 무시.
    """
    try:
        raw = _json.loads(_strip_code_fence(text))
    except _json.JSONDecodeError as exc:
        raise ValueError(f"derive 응답 JSON 파싱 실패: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"derive 응답이 JSON 배열이 아닙니다: {type(raw).__name__}")
    data = cast(list[dict[str, object]], raw)
    try:
        delta: dict[str, str] = {
            _require_str(item, "concept_id"): _require_str(item, "core_question")
            for item in data
        }
    except (KeyError, TypeError) as exc:
        raise ValueError(f"derive 응답 필드 누락/타입 오류: {exc}") from exc
    result: list[OkfDocumentDraft] = []
    for doc in originals:
        if doc.concept_id in delta:
            result.append(OkfDocumentDraft(
                concept_id=doc.concept_id,
                title=doc.title,
                body=doc.body,
                core_question=delta[doc.concept_id],
                domain=doc.domain,
                type=doc.type,
            ))
        else:
            result.append(doc)
    return tuple(result)


def parse_link_response(
    text: str,
    valid_ids: frozenset[str],
) -> tuple[ConceptEdge, ...]:
    """link 응답 JSON → ConceptEdge tuple(순수·IO 0).

    valid_ids 밖 from/to는 드롭(과생성 흡수·최종 dangling 방어는 admit_okf).
    """
    try:
        raw = _json.loads(_strip_code_fence(text))
    except _json.JSONDecodeError as exc:
        raise ValueError(f"link 응답 JSON 파싱 실패: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"link 응답이 JSON 배열이 아닙니다: {type(raw).__name__}")
    data = cast(list[dict[str, object]], raw)
    result: list[ConceptEdge] = []
    try:
        for item in data:
            from_id = _require_str(item, "from_id")
            to_id = _require_str(item, "to_id")
            if from_id not in valid_ids or to_id not in valid_ids:
                continue
            result.append(ConceptEdge(from_id=from_id, to_id=to_id, relation=str(item["relation"])))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"link 응답 필드 누락/타입 오류: {exc}") from exc
    return tuple(result)


class LlmAuthor:
    """OkfAuthor 포트 구현 — transport 주입·공급자 중립(ADR 0029).

    model은 필수 키워드 인자(공급자별 기본 모델은 게이트 밖 팩토리가 주입).
    파이프라인: build_*_request → transport → assemble_stream → parse_*_response.
    """

    def __init__(self, transport: ProviderTransport, *, model: str) -> None:
        self._transport = transport
        self._model = model

    def split(
        self, sources: Sequence[RawSource], allowed_domains: Sequence[str]
    ) -> tuple[OkfDocumentDraft, ...]:
        """2단계: 원본 소스를 개념 단위로 분할한다.

        allowed_domains를 build_split_request에 전달해 권한 domain 힌트를 프롬프트에 싣는다.
        """
        request = build_split_request(
            sources, model=self._model, allowed_domains=allowed_domains
        )
        text = assemble_stream(self._transport(request))
        return parse_split_response(text)

    def derive_core_questions(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[OkfDocumentDraft, ...]:
        """3단계: 각 개념 초안의 core_question을 정련한다."""
        request = build_derive_request(drafts, model=self._model)
        text = assemble_stream(self._transport(request))
        return parse_derive_response(text, drafts)

    def link(
        self, drafts: Sequence[OkfDocumentDraft]
    ) -> tuple[ConceptEdge, ...]:
        """4단계: 개념 간 관계(edges)를 도출한다."""
        valid_ids = frozenset(d.concept_id for d in drafts)
        request = build_link_request(drafts, model=self._model)
        text = assemble_stream(self._transport(request))
        return parse_link_response(text, valid_ids)
