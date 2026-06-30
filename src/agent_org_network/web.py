"""웹 백엔드 — 이미 완성된 AskOrg 핸들러를 감싸는 얇은 어댑터.

비즈니스 로직 없음: POST /ask 가 질문을 받아 핸들러를 호출하고
OrgReply(Answered | Pending)를 JSON으로 직렬화해 돌려준다.
내부값(confidence·candidates·escalated_to)은 Answered/Pending에 필드 자체가
없으므로 구조적으로 새지 않는다. 사용자에겐 담당·모드·출처(또는 안내)만 간다.

처리함(Inbox)은 Owner向 *운영 화면*이라 다른 면이다 — 케이스의 후보·intent 등
내부값을 그대로 노출한다(실 사용자 채팅 OrgReply의 노출 불변식은 여기 적용 안 됨).
채팅과 처리함은 한 `DemoBundle`(공유 store)을 보므로, 처리함서 합의가 성립하면
채팅의 같은 질문이 판례 자동 라우팅으로 답해진다.

운영 면 인증(T6.5, ADR 0009·0016):
    운영 엔드포인트(처리함·Manager 큐·모니터링)는 *세션 신원*을 요구한다. 채팅
    (`/ask`·`/`)은 익명(다른 공간). `POST /login`(body `user_id`·Registry 실재
    검사·401)이 무비밀번호 서명 쿠키 세션을 set, `POST /logout`이 클리어 —
    starlette `SessionMiddleware`(`itsdangerous` 서명, `session_secret` env/주입).
    **신원 출처 = 세션**(path/body 아님 — 위조 차단): 자기 면 조회는 path param을
    제거(`/inbox/cases`·`/inbox/backup-reviews`·`/manager/queue` — 세션 owner/manager),
    1인칭 처분(concur·review·act)은 body `by_owner`/`by_manager`를 세션에서 채운다.
    스코프=자기 것만 — 도메인 1인칭 ValueError를 403으로 매핑(미로그인 401·미존재
    404·형식 400). 세션 읽기는 `_session_identity` 한 곳으로 격리(메커니즘 교체에
    엔드포인트 무흔들 — 헥사고날).
"""

import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, assert_never

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from agent_org_network.ask_org import (
    Answered,
    ErrorEvent,
    OrgReply,
    Pending,
    project_answered,
    project_pending,
    serialize_sse_event,
)
from agent_org_network.audit import InMemoryAuditLog, JsonlAuditLog

if TYPE_CHECKING:
    from agent_org_network.audit import AuditReader
from agent_org_network.conflict import (
    Agreed,
    ConcurOnPrimary,
    ConflictCase,
    ConsensusOutcome,
    Deadlocked,
    StillOpen,
)
from agent_org_network.demo import build_demo, seed_demo_reeval_items
from agent_org_network.dispatch import RuntimeDispatcher
from agent_org_network.git_gateway import (
    BuilderCommitRequest,
    FakeGitGateway,
    GitGateway,
    OkfFile,
    commit_okf_bundle,
)
from agent_org_network.manager_queue import (
    AssignOwner as MgrAssignOwner,
    Dismiss as MgrDismiss,
    ManagerAction,
    ManagerItem,
    ManagerQueueService,
    ManagerQueueStore,
    ManagerResolution,
    Reroute as MgrReroute,
)
from agent_org_network.oidc import (
    OidcProvider,
    OidcVerificationError,
    resolve_identity,
)
from agent_org_network.review import (
    ApproveBackup,
    BackupReview,
    BackupReviewItem,
    BackupReviewService,
    BackupReviewStore,
    CorrectBackup,
    DismissBackup,
)
from agent_org_network.reeval import (
    AcknowledgeAnswer,
    AnswerSubject,
    InMemoryReevalStore,
    InvalidatePrecedent,
    KeepPrecedent,
    PrecedentSubject,
    ReAnswer,
    ReevalItem,
    ReevalOutcome,
    ReevalService,
    ReevalStore,
    SupersedePrecedent,
)
from agent_org_network.index_matcher import relevant_concepts
from agent_org_network.registry import Registry
from agent_org_network.runtime import AgentRuntime
from agent_org_network.two_stage_router import PublishedIndexStore
from agent_org_network.session import InMemorySessionStore, SessionAskOrg, SessionStore
from agent_org_network.user import User

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"
_INBOX_HTML = _WEB_DIR / "inbox.html"
_MONITOR_HTML = _WEB_DIR / "monitor.html"
_ORG_HTML = _WEB_DIR / "org.html"
_BUILDER_HTML = _WEB_DIR / "builder.html"

# 웹챗에서 오는 익명 end-user. 채팅(`/ask`·`/`)은 운영 세션을 요구하지 않는다
# (ADR 0009·0016 — 실 사용자 면은 운영 면과 다른 별개 공간, 익명 유지).
_WEB_USER = User(id="web_guest")

# 세션에 운영 신원을 담는 키(ADR 0016). 서명 쿠키 세션 dict의 이 키에 로그인된
# User.id가 박힌다 — `_session_identity`가 이 키로 읽는다.
_SESSION_USER_KEY = "operator_user_id"


class LoginRequest(BaseModel):
    """POST /login 요청 바디 — 무비밀번호 신원 선택(ADR 0016 결정 2).

    `user_id`는 Registry에 실재하는 User여야 한다(없으면 401). 비밀번호 없음 —
    v0는 *신원 선택*을 세션에 고정해 per-request 가장을 차단하는 것까지(PRD §6).
    """

    user_id: str


class SsoLoginRequest(BaseModel):
    """POST /login/sso 요청 바디 — SSO 신원 *증명*(T7.1·ADR 0021 결정 4).

    `id_token`은 IdP가 발급한 OIDC id_token(불투명 — 우리가 `oidc_provider.verify`로 서명·
    만료·aud를 검증한다). 무비밀번호 `/login`의 "user_id *선택*"과 달리, 여기선 IdP가 *증명*한
    신원만 `resolve_identity`로 registry user_id에 매핑돼 세션에 박힌다(선택 우회 차단).
    """

    id_token: str


# ── 운영 면 인증 헬퍼 ──────────────────────────────────────────────────────


class NotAuthenticatedError(Exception):
    """운영 면 진입에 세션 신원이 없음 — 401로 매핑(ADR 0016 결정 4)."""


def _session_identity(request: Request) -> str:
    """세션에서 운영 신원(User.id)을 읽는다. 없으면 NotAuthenticatedError(→401).

    starlette `request.session`(SessionMiddleware 주입 dict-like)에서
    `_SESSION_USER_KEY`를 꺼낸다. SessionMiddleware 미부착(세션 속성 없음)도
    NotAuthenticated로 처리한다. 엔드포인트는 이 한 곳만 보고 신원을 얻어
    path/body 가장이 구조적으로 불가능해진다.
    """
    try:
        session = request.session
    except AssertionError:
        # SessionMiddleware 미부착 시 starlette가 AssertionError를 올린다.
        raise NotAuthenticatedError("세션 미들웨어 미부착")
    user_id: str | None = session.get(_SESSION_USER_KEY)
    if not user_id:
        raise NotAuthenticatedError("세션 신원 없음 — 로그인 필요")
    return user_id


class AskRequest(BaseModel):
    question: str


class ConcurRequest(BaseModel):
    """POST /cases/{case_id}/concur 요청 바디.

    인증 활성 시: by_owner는 세션에서 채워진다(body 값 무시). on_agent·rationale만 읽음.
    하위호환(미인증): by_owner를 body에서 읽는다(기존 테스트 보존).
    """

    on_agent: str
    rationale: str = ""
    by_owner: str = ""  # 하위호환 — 인증 활성 시 무시, 미활성 시 body에서 읽음


class FetchDocumentRequest(BaseModel):
    """POST /inbox/cases/{case_id}/document 요청 바디 — on-demand 문서 fetch(ADR 0028 §15 결정 E).

    인박스에서 owner가 한 후보의 연관 개념을 클릭하면 그 문서 본문을 *그때* owner 워커에서
    끌어온다. `agent_id`(어느 후보 카드)·`concept_id`(OKF 파일 stem)로 그 owner 워커에
    `FetchDocument`를 보낸다. 권한은 요청 owner를 *자기 케이스 후보 문서*로 제한한다(결정 E).

    1차 경로 traversal 방어: concept_id에 경로 구분자·'..'·절대경로가 포함되면 422로 거부.
    **워커측이 최종 신뢰 경계**(분산 신뢰 경계 — 워커 `handle_fetch_document`가 최종 권위로
    재검증). 이 web 검증은 조기 차단으로 불필요한 dispatch를 막는 1차 방어다.
    """

    agent_id: str
    concept_id: str

    @field_validator("concept_id")
    @classmethod
    def concept_id_must_be_safe_stem(cls, v: str) -> str:
        """concept_id가 순수 파일명 컴포넌트인지 검증(경로 traversal 1차 방어).

        구분자('/', '\\', os.sep)·'..'·절대경로·빈 문자열을 포함하면 422로 거부.
        워커측이 최종 권위이므로 이 검증은 1차 조기 차단이다.
        """
        import os
        from pathlib import Path as _Path

        if not v:
            raise ValueError("concept_id는 빈 문자열일 수 없습니다")
        if v in (".", ".."):
            raise ValueError("concept_id는 '.' 또는 '..'일 수 없습니다")
        if os.sep in v or "/" in v or "\\" in v:
            raise ValueError("concept_id에 경로 구분자가 포함될 수 없습니다")
        if os.path.isabs(v):
            raise ValueError("concept_id는 절대경로일 수 없습니다")
        if _Path(v).name != v:
            raise ValueError("concept_id는 단일 파일명 컴포넌트여야 합니다")
        return v


def serialize_reply(reply: OrgReply) -> dict[str, Any]:
    """OrgReply를 사용자에게 보낼 dict로 변환한다(내부값 미포함).

    노출 투영 SSOT(ADR 0031 결정 3): `project_answered`·`project_pending`(ask_org)을 공유한다 —
    스트리밍 SSE(`serialize_sse_event`)와 *같은 투영*을 거쳐 두 경로가 노출 불변식을 다르게
    흘릴 여지를 제거한다. 답 회수용 불투명 추적 토큰은 dispatched에만(project_pending이 처리).
    """
    match reply:
        case Answered():
            return project_answered(reply)
        case Pending():
            return project_pending(reply)
        case _ as never:
            assert_never(never)


def serialize_case(
    case: ConflictCase,
    registry: Registry,
    published_index_store: PublishedIndexStore | None = None,
) -> dict[str, Any]:
    """ConflictCase를 처리함 운영 화면向 dict로 변환한다(내부값 노출 OK).

    registry: 각 후보 카드의 커버리지(summary·domains·knowledge_sources)를 조회한다.
    published_index_store: 주어지면 후보에 relevant_concepts(질문 연관 개념) 추가.
    미등록 agent_id는 agent_id·owner만 담고 커버리지·relevant_concepts 생략(방어적).
    """
    candidates: list[dict[str, Any]] = []
    for c in case.candidates:
        cand: dict[str, Any] = {"agent_id": c.agent_id, "owner": c.owner}
        try:
            card = registry.get(c.agent_id)
            cand["summary"] = card.summary
            cand["domains"] = list(card.domains)
            cand["knowledge_sources"] = list(card.knowledge_sources)
        except KeyError:
            pass
        if published_index_store is not None:
            index = published_index_store.get(c.agent_id)
            if index is not None:
                concepts = relevant_concepts(case.question, index)
                cand["relevant_concepts"] = [
                    {"id": rc.id, "label": rc.label, "core_question": rc.core_question}
                    for rc in concepts
                ]
        candidates.append(cand)
    return {
        "case_id": case.case_id,
        "intent": case.intent,
        "question": case.question,
        "candidates": candidates,
    }


def serialize_outcome(outcome: ConsensusOutcome) -> dict[str, Any]:
    """ConsensusOutcome(타입이 곧 상태)을 처리함向 dict로 변환한다."""
    match outcome:
        case Agreed():
            return {
                "type": "agreed",
                "primary": outcome.resolution.primary,
                "intent": outcome.resolution.intent,
            }
        case StillOpen():
            return {
                "type": "still_open",
                "pending_owners": list(outcome.pending_owners),
            }
        case Deadlocked():
            return {"type": "deadlocked"}
        case _:
            assert_never(outcome)


def serialize_review_item(item: BackupReviewItem) -> dict[str, Any]:
    """BackupReviewItem을 처리함 운영 화면向 dict로 변환한다(내부값 노출 OK)."""
    d: dict[str, Any] = {
        "item_id": item.item_id,
        "owner_id": item.owner_id,
        "agent_id": item.agent_id,
        "question": item.question,
        "backup_answer_text": item.backup_answer_text,
        "ticket_id": item.ticket_id,
        "snapshot_at": item.snapshot_at.isoformat(),
        "answered_at": item.answered_at.isoformat(),
        "status": item.status,
        "review": _serialize_backup_review(item.review) if item.review is not None else None,
    }
    return d


def _serialize_backup_review(review: BackupReview) -> dict[str, Any]:
    match review:
        case ApproveBackup():
            return {"type": "approve", "by_owner": review.by_owner, "rationale": review.rationale}
        case CorrectBackup():
            return {
                "type": "correct",
                "by_owner": review.by_owner,
                "corrected_text": review.corrected_text,
                "sources": list(review.sources),
                "rationale": review.rationale,
            }
        case DismissBackup():
            return {"type": "dismiss", "by_owner": review.by_owner, "rationale": review.rationale}
        case _ as never:
            assert_never(never)


def serialize_reeval_item(
    item: ReevalItem,
    registry: Registry,
    audit_reader: "AuditReader | None",
) -> dict[str, Any]:
    """ReevalItem을 처리함 재평가 탭(세 번째 탭) 운영 화면向 dict로 변환한다.

    `serialize_review_item`(둘째 탭) 미러. ReevalItem은 질문 텍스트를 직접 안 들고
    subject(intent 또는 audit_index)만 들므로 표시용 `question`·`reason`을 파생한다.
    `match item.subject`(PrecedentSubject | AnswerSubject) + assert_never로 두 대상 망라
    (sealed sum 정신). audit_reader가 None이거나 인덱스 범위 밖이면 안전 폴백 라벨.

    노출(운영 면): owner 자기 처리함 데이터라 owner/agent_id/intent 표시 OK(라우팅 점수·
    후보 아님). trigger_sha는 짧게(앞 12자) — 감사 표식 용도.
    """
    subject = item.subject
    subject_kind: Literal["precedent", "answer"]
    subject_ref: str
    question: str
    target_label: str
    match subject:
        case PrecedentSubject():
            subject_kind = "precedent"
            subject_ref = subject.intent
            question = f"'{subject.intent}' 판례"
            target_label = "판례"
        case AnswerSubject():
            subject_kind = "answer"
            subject_ref = str(subject.audit_index)
            question = _reeval_answer_question(subject.audit_index, audit_reader)
            target_label = "답"
        case _ as never:
            assert_never(never)

    reason = f"'{item.agent_id}' 지식이 바뀌어 이 {target_label}이 stale 표식됨"

    d: dict[str, Any] = {
        "item_id": item.item_id,
        "owner_id": item.owner_id,
        "agent_id": item.agent_id,
        "subject_kind": subject_kind,
        "subject_ref": subject_ref,
        "trigger_sha": item.trigger_sha[:12],
        "flagged_at": item.flagged_at.isoformat(),
        "status": item.status,
        "question": question,
        "reason": reason,
        "review": _serialize_reeval_outcome(item.review) if item.review is not None else None,
    }
    return d


def _reeval_answer_question(audit_index: int, audit_reader: "AuditReader | None") -> str:
    """AnswerSubject의 표시용 question을 audit 기록에서 파생한다(안전 폴백 포함).

    audit_reader가 None이거나 인덱스 범위 밖이거나 question 키가 없으면 폴백 라벨.
    """
    if audit_reader is not None:
        record = audit_reader.record_at(audit_index)
        if record is not None:
            q = record.get("question")
            if isinstance(q, str) and q:
                return q
    return f"과거 답 #{audit_index}"


def _serialize_reeval_outcome(review: ReevalOutcome) -> dict[str, Any]:
    """ReevalOutcome(sealed sum 5-arm)을 처리함向 dict로 변환한다(match + assert_never)."""
    match review:
        case KeepPrecedent():
            return {"kind": "keep", "by_owner": review.by_owner, "rationale": review.rationale}
        case InvalidatePrecedent():
            return {
                "kind": "invalidate",
                "by_owner": review.by_owner,
                "rationale": review.rationale,
            }
        case SupersedePrecedent():
            return {
                "kind": "supersede",
                "by_owner": review.by_owner,
                "new_primary": review.new_primary,
                "rationale": review.rationale,
            }
        case AcknowledgeAnswer():
            return {
                "kind": "acknowledge",
                "by_owner": review.by_owner,
                "rationale": review.rationale,
            }
        case ReAnswer():
            return {"kind": "reanswer", "by_owner": review.by_owner, "rationale": review.rationale}
        case _ as never:
            assert_never(never)


def serialize_manager_item(item: ManagerItem) -> dict[str, Any]:
    """ManagerItem을 Manager 큐 운영 화면向 dict로 변환한다(내부값 노출 OK)."""
    from agent_org_network.manager_queue import FromDeadlock, FromDispatch, FromUnowned

    source = item.source
    source_dict: dict[str, Any]
    match source:
        case FromUnowned():
            source_dict = {
                "type": "from_unowned",
                "question": source.question,
                "escalated_to": source.decision.escalated_to,
            }
        case FromDeadlock():
            source_dict = {
                "type": "from_deadlock",
                "case_id": source.case.case_id,
                "intent": source.case.intent,
                "question": source.case.question,
                "reason": source.reason,
            }
        case FromDispatch():
            source_dict = {
                "type": "from_dispatch",
                "ticket_id": source.outcome.ticket.ticket_id,
                "owner_id": source.outcome.ticket.owner_id,
                "question": source.outcome.ticket.question,
                "manager_id": source.outcome.manager_id,
                "reason": source.outcome.reason,
            }
        case _ as never:
            assert_never(never)

    d: dict[str, Any] = {
        "item_id": item.item_id,
        "manager_id": item.manager_id,
        "status": item.status,
        "created_at": item.created_at.isoformat(),
        "source": source_dict,
    }
    if item.resolution is not None:
        d["resolution"] = _serialize_manager_resolution(item.resolution)
    return d


def _serialize_manager_resolution(resolution: ManagerResolution) -> dict[str, Any]:
    r = resolution
    action_dict: dict[str, Any]
    match r.action:
        case MgrAssignOwner():
            action_dict = {
                "type": "assign_owner",
                "by_manager": r.action.by_manager,
                "primary": r.action.primary,
                "rationale": r.action.rationale,
            }
        case MgrReroute():
            action_dict = {
                "type": "reroute",
                "by_manager": r.action.by_manager,
                "to_agent": r.action.to_agent,
                "rationale": r.action.rationale,
            }
        case MgrDismiss():
            action_dict = {
                "type": "dismiss",
                "by_manager": r.action.by_manager,
                "rationale": r.action.rationale,
            }
        case _ as never:
            assert_never(never)
    return {"action": action_dict}


def dedupe_audit_records(records: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """같은 tracking의 복수 엔트리를 마지막(최신)만 남기고 dedup한다(모니터 목록 뷰).

    - tracking이 None/없는 레코드는 dedup 대상 아님 — 그대로 유지.
    - 같은 비None tracking을 가진 레코드는 마지막 출현(=최신=delivered)만 남긴다.
    - 원래 인덱스(enumerate 기준)를 튜플로 보존한다(/monitor/{index} 상세 링크용).
    - 입력 순서(마지막 출현 순서) 보존.
    """
    # 1패스: tracking별 마지막 원래인덱스 기록
    last_index_for: dict[str, int] = {}
    for i, record in enumerate(records):
        t: Any = record.get("tracking")
        if t is not None and isinstance(t, str):
            last_index_for[t] = i

    # 2패스: 원래 순서(인덱스 오름차순) 재조합 — tracking 없는 것은 그대로, tracking 있는 것은 마지막만
    result: list[tuple[int, dict[str, Any]]] = []
    for i, record in enumerate(records):
        t2: Any = record.get("tracking")
        if t2 is None or not isinstance(t2, str):
            result.append((i, record))
        elif last_index_for.get(t2) == i:
            result.append((i, record))
    return result


def summarize_audit_record(index: int, record: dict[str, Any]) -> dict[str, Any]:
    """감사 레코드(dict)를 운영 모니터링 *목록 요약*向으로 줄인다(T5.1, 운영 면)."""
    decision: dict[str, Any] = record.get("decision") or {}
    answer: dict[str, Any] | None = record.get("answer")
    return {
        "index": index,
        "timestamp": record.get("timestamp"),
        "user_id": record.get("user_id"),
        "question": record.get("question"),
        "intent": record.get("intent"),
        "disposition": decision.get("disposition"),
        "mode": answer.get("mode") if answer is not None else None,
        "answered": answer is not None,
    }


def serialize_org_graph(registry: "Registry") -> dict[str, Any]:
    """Registry를 Org 그래프 운영 화면向 {nodes, edges}로 *순수 파생*한다(T5.3).

    새 도메인 상태·전이 0 — 이미 admission으로 무결성 보증된 Registry(진실)를 읽어
    User·Agent Card 2노드 그래프(CONTEXT Graph model·ADR 0005)로 투영할 뿐이다.
    모니터링이 감사 로그를 순수 읽기로 투영하듯, 여긴 원천이 registry다. 운영 면이라
    내부값(domains 등) 노출 OK(채팅 OrgReply 불변식의 반대).

    노드: User(`{type:"user", id, manager?}`) + Agent Card(`{type:"card", agent_id,
    owner, team, domains, maintainer?}`).
    엣지: `owns`(owner User→card) · `manages`(user.manager→user) · `maintains`
    (maintainer User→card, **카드에 maintainer 있을 때만** — MVP는 owns가 대신, ADR 0005).

    web과 분리(`serialize_reply`·`summarize_audit_record`와 같은 경계) — 순수 함수라
    결정론 테스트(레지스트리 주입→노드/엣지 단언).
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    # 유저 노드 (id 정렬 — 결정론)
    for uid in sorted(registry.user_ids()):
        user = registry.get_user(uid)
        node: dict[str, Any] = {"type": "user", "id": user.id}
        if user.manager is not None:
            node["manager"] = user.manager
        else:
            node["manager"] = None
        nodes.append(node)

    # 카드 노드 (agent_id 정렬 — 결정론)
    for card in sorted(registry.all_cards(), key=lambda c: c.agent_id):
        card_node: dict[str, Any] = {
            "type": "card",
            "agent_id": card.agent_id,
            "owner": card.owner,
            "team": card.team,
            "domains": list(card.domains),
        }
        if card.maintainer is not None:
            card_node["maintainer"] = card.maintainer
        nodes.append(card_node)

    # owns 엣지 (카드마다 owner→card, agent_id 정렬)
    for card in sorted(registry.all_cards(), key=lambda c: c.agent_id):
        edges.append({"type": "owns", "source": card.owner, "target": card.agent_id})

    # manages 엣지 (user.manager 있을 때만, id 정렬)
    for uid in sorted(registry.user_ids()):
        user = registry.get_user(uid)
        if user.manager is not None:
            edges.append({"type": "manages", "source": user.manager, "target": user.id})

    # maintains 엣지 (card.maintainer 있을 때만, agent_id 정렬)
    for card in sorted(registry.all_cards(), key=lambda c: c.agent_id):
        if card.maintainer is not None:
            edges.append({"type": "maintains", "source": card.maintainer, "target": card.agent_id})

    return {"nodes": nodes, "edges": edges}


class BuilderValidateRequest(BaseModel):
    """POST /builder/validate 요청 바디 — Agent 빌더 카드 구성 검증(T5.3).

    카드 필드를 그대로 받는다(`AgentCard` 필드 미러). 핸들러가 `AgentCard.model_validate`
    (필수 필드·타입) + admission 규칙(owner 실재·참조 무결성, `Registry.validate`와 같은
    검사)을 돌려 통과면 YAML 미리보기, 실패면 사유를 낸다. 라이브 레지스트리 mutation은
    하지 않는다(편집 채널 = git/PR, CONTEXT Maintainer). 새 도메인 타입 0.

    `last_reviewed_at`은 ISO date 문자열로 받는다(pydantic이 date로 강제). 선택 필드는
    빈 기본값(AgentCard와 동일) — 폼에서 비우면 빈 리스트/None.
    """

    agent_id: str
    owner: str
    team: str
    summary: str
    domains: list[str]
    last_reviewed_at: str
    maintainer: str | None = None
    can_answer: list[str] = []
    cannot_answer: list[str] = []
    approval_when: list[str] = []
    collaborate_when: list[str] = []
    knowledge_sources: list[str] = []
    trust_labels: list[str] = []


def validate_card_for_builder(
    req: BuilderValidateRequest, registry: "Registry"
) -> dict[str, Any]:
    """빌더 카드 후보를 admission 규칙으로 검증해 결과 dict를 낸다(T5.3, 순수 함수).

    절차: ① `AgentCard.model_validate`(필수 필드·타입·date 파싱) — 실패면
    `{ok: False, errors: [...]}`. ② admission 참조 무결성 — `card.owner`가 Registry에
    실재하는 User여야 한다(`Registry.validate` 정신; maintainer 있으면 그것도 실재 검사).
    실패면 `{ok: False, errors: [...]}`. ③ 통과면 `{ok: True, yaml: "<registry/agents/
    {agent_id}.yaml 텍스트>"}`(PyYAML safe_dump). **라이브 등록 안 함** — YAML은 Owner가
    복사→git 커밋(PR)할 편집 채널 출력일 뿐(CONTEXT Maintainer).

    web과 분리한 순수 함수라 결정론 테스트(레지스트리 주입→유효/무효 카드 단언).
    """
    import yaml
    from pydantic import ValidationError

    from agent_org_network.agent_card import AgentCard

    # ① 빈 agent_id 거부
    if not req.agent_id or not req.agent_id.strip():
        return {"ok": False, "errors": ["agent_id는 비어 있을 수 없습니다."]}

    # ② AgentCard.model_validate — 필수 필드·타입·date 파싱
    try:
        card = AgentCard.model_validate(req.model_dump())
    except ValidationError as exc:
        return {"ok": False, "errors": [str(e["msg"]) for e in exc.errors()]}

    # ③ admission 참조 무결성
    admission_errors: list[str] = []
    if card.owner not in registry.user_ids():
        admission_errors.append(f"미등록 owner: {card.owner}")
    if card.maintainer is not None and card.maintainer not in registry.user_ids():
        admission_errors.append(f"미등록 maintainer: {card.maintainer}")
    if admission_errors:
        return {"ok": False, "errors": admission_errors}

    # ④ YAML 직렬화 (registry/agents/{agent_id}.yaml 형태)
    card_dict: dict[str, Any] = {
        "agent_id": card.agent_id,
        "owner": card.owner,
        "team": card.team,
        "summary": card.summary,
        "domains": list(card.domains),
        "last_reviewed_at": card.last_reviewed_at.isoformat(),
    }
    if card.maintainer is not None:
        card_dict["maintainer"] = card.maintainer
    if card.can_answer:
        card_dict["can_answer"] = list(card.can_answer)
    if card.cannot_answer:
        card_dict["cannot_answer"] = list(card.cannot_answer)
    if card.approval_when:
        card_dict["approval_when"] = list(card.approval_when)
    if card.collaborate_when:
        card_dict["collaborate_when"] = list(card.collaborate_when)
    if card.knowledge_sources:
        card_dict["knowledge_sources"] = list(card.knowledge_sources)
    if card.trust_labels:
        card_dict["trust_labels"] = list(card.trust_labels)

    yaml_text: str = yaml.safe_dump(card_dict, allow_unicode=True, sort_keys=False)
    return {"ok": True, "yaml": yaml_text}


class BuilderOkfCommitRequest(BaseModel):
    """POST /builder/okf/commit 요청 바디 — OKF 번들 파일 커밋(ADR 0018 결정 1·5).

    `agent_id`·`files`·`message`를 body에서 받는다. **`author`는 세션 신원**으로 채워진다
    (ADR 0016 위조 차단 — body가 아닌 세션). `files`는 번들 내 상대 경로+내용 리스트.
    """

    agent_id: str
    files: list[dict[str, str]]
    message: str = ""


class ManagerActionRequest(BaseModel):
    """POST /manager/items/{item_id}/act 요청 바디.

    인증 활성 시: by_manager는 세션에서 채워진다(body 값 무시).
    하위호환(미인증): by_manager를 body에서 읽는다(기존 테스트 보존).
    """

    type: Literal["assign_owner", "reroute", "dismiss"]
    by_manager: str = ""  # 하위호환 — 인증 활성 시 무시, 미활성 시 body에서 읽음
    primary: str = ""
    to_agent: str = ""
    rationale: str = ""


def _parse_manager_action(req: ManagerActionRequest, by_manager: str) -> ManagerAction:
    if req.type == "assign_owner":
        return MgrAssignOwner(
            by_manager=by_manager,
            primary=req.primary,
            rationale=req.rationale,
        )
    elif req.type == "reroute":
        return MgrReroute(
            by_manager=by_manager,
            to_agent=req.to_agent,
            rationale=req.rationale,
        )
    else:
        return MgrDismiss(by_manager=by_manager, rationale=req.rationale)


class BackupReviewRequest(BaseModel):
    """POST /backup-reviews/{item_id} 요청 바디.

    인증 활성 시: by_owner는 세션에서 채워진다(body 값 무시).
    하위호환(미인증): by_owner를 body에서 읽는다(기존 테스트 보존).
    """

    type: Literal["approve", "correct", "dismiss"]
    by_owner: str = ""  # 하위호환 — 인증 활성 시 무시, 미활성 시 body에서 읽음
    corrected_text: str = ""
    sources: list[str] = []
    rationale: str = ""


def _parse_backup_review(req: BackupReviewRequest, by_owner: str) -> BackupReview:
    if req.type == "approve":
        return ApproveBackup(by_owner=by_owner, rationale=req.rationale)
    elif req.type == "correct":
        return CorrectBackup(
            by_owner=by_owner,
            corrected_text=req.corrected_text,
            sources=tuple(req.sources),
            rationale=req.rationale,
        )
    else:
        return DismissBackup(by_owner=by_owner, rationale=req.rationale)


class ReevalReviewRequest(BaseModel):
    """POST /reeval/{item_id}/review 요청 바디 — 처리함 세 번째 탭 처분(BackupReviewRequest 미러).

    `kind`가 ReevalOutcome 5-arm을 고른다: keep|invalidate|supersede(Precedent 축) /
    acknowledge|reanswer(Answer 축). `supersede`는 `new_primary` 필수. 인증 활성 시
    by_owner는 세션에서 채워진다(body 값 없음 — 위조 차단·ADR 0016). 미인증이면 body의
    by_owner를 읽는다(하위호환).
    """

    kind: Literal["keep", "invalidate", "supersede", "acknowledge", "reanswer"]
    by_owner: str = ""  # 하위호환 — 인증 활성 시 무시, 미활성 시 body에서 읽음
    new_primary: str = ""
    rationale: str = ""


def _parse_reeval_outcome(req: ReevalReviewRequest, by_owner: str) -> ReevalOutcome:
    """ReevalReviewRequest를 ReevalOutcome으로 빌드한다(_parse_backup_review 미러).

    `supersede`는 `new_primary`가 비면 ValueError(라우트가 400으로 매핑) — 새 결론으로
    갈음하려면 새 primary가 있어야 한다(SupersedePrecedent 계약).
    """
    if req.kind == "keep":
        return KeepPrecedent(by_owner=by_owner, rationale=req.rationale)
    elif req.kind == "invalidate":
        return InvalidatePrecedent(by_owner=by_owner, rationale=req.rationale)
    elif req.kind == "supersede":
        if not req.new_primary:
            raise ValueError("supersede는 new_primary가 필요합니다.")
        return SupersedePrecedent(
            by_owner=by_owner, new_primary=req.new_primary, rationale=req.rationale
        )
    elif req.kind == "acknowledge":
        return AcknowledgeAnswer(by_owner=by_owner, rationale=req.rationale)
    else:
        return ReAnswer(by_owner=by_owner, rationale=req.rationale)


def create_app(
    runtime: AgentRuntime | None = None,
    dispatcher: RuntimeDispatcher | None = None,
    review_store: BackupReviewStore | None = None,
    review_service: BackupReviewService | None = None,
    reeval_store: ReevalStore | None = None,
    reeval_service: ReevalService | None = None,
    manager_queue_store: ManagerQueueStore | None = None,
    audit_log: JsonlAuditLog | InMemoryAuditLog | None = None,
    session_secret: str | None = None,
    git_gateway: GitGateway | None = None,
    oidc_provider: OidcProvider | None = None,
    session_store: SessionStore | None = None,
) -> FastAPI:
    """웹 앱을 조립한다. 기본 런타임은 `build_demo`의 기본(진짜 Claude).

    결정론이 필요한 테스트는 `runtime=StubRuntime()`을 넘겨 실제 claude 호출을 막는다.
    `session_secret`(T6.5·ADR 0016): 운영 면 세션 서명 키. 주입 시 `SessionMiddleware`를
    부착해 운영 엔드포인트가 세션 신원을 요구한다 — 테스트는 고정 키 주입(결정론),
    운영은 env. 커밋 금지. **미주입이면 세션 미부착**(하위호환 — 기존 동작·기존 테스트 보존).
    `git_gateway`(T7.2·ADR 0018): OKF 번들 커밋 포트. 미주입이면 `FakeGitGateway`(안전한
    기본 — 실 git subprocess는 `SubprocessGitGateway`를 명시 주입).
    `oidc_provider`(T7.1·ADR 0021): SSO 신원 검증 포트. 주입 시 **SSO 모드**(`POST /login/sso`
    활성·무비밀번호 `POST /login`은 403 거부 — 신원 *선택* 우회 차단). 미주입이면 기존 동작
    (인증 모드 3단 중 OFF/무비밀번호 — ADR 0021 결정 4). 결정론 테스트는 `FakeOidcProvider` 주입.
    `session_store`(Phase 9·ADR 0024 결정 A): 채팅 세션 저장소 관찰 seam. 주입 시
    그 store를 쓴다(테스트가 active_for_user 등으로 세션 상태를 직접 검사). 미주입이면
    `InMemorySessionStore()` 내부 생성(하위호환 — 기존 동작).
    """
    from starlette.middleware.sessions import SessionMiddleware

    app = FastAPI(title="Agent Org Network — 채팅·처리함(데모)")

    # SessionMiddleware 부착 (T6.5 슬라이스 1 — ADR 0016 결정 1).
    # session_secret 주입 시에만 붙인다(미주입이면 인증 없이 동작 — 하위호환).
    _auth_enabled = session_secret is not None
    if _auth_enabled:
        app.add_middleware(SessionMiddleware, secret_key=session_secret)

    bundle = build_demo(
        runtime=runtime,
        dispatcher=dispatcher,
        review_store=review_store,
        manager_queue_store=manager_queue_store,
        audit_log=audit_log,
    )
    # 라이브 publish 배선(T10.4 Blocker B1·ADR 0028 §14 결정 F): index 모드면 라우터가
    # 보는 *바로 그* published 인덱스 스토어를 디스패처에 같이 꽂는다 — 그래야 워커
    # publish(`recv_loop`→`accept_index`→`put`)가 라우터가 라우팅에 쓰는 store에 도달한다.
    # 미바인딩이면 `accept_index`가 무조건 False·no-op이라 받은 PublishIndex가 버려진다.
    # 디스패처가 `WebSocketDispatcher`이고 store가 있을 때만(분산 전송·index 모드).
    from agent_org_network.transport import WebSocketDispatcher

    if (
        isinstance(dispatcher, WebSocketDispatcher)
        and bundle.published_index_store is not None
    ):
        dispatcher.bind_published_index(bundle.registry, bundle.published_index_store)
    # T9.1(d): 세션 층 래퍼 — AskOrg를 *수정하지 않고* 감싸기로 세션을 붙인다.
    # /ask 엔드포인트만 교체. retrieve·dispatched·mcp_server는 이번 스코프 밖.
    # Phase 9 쿠키 세션 seam: 주입 store가 있으면 그것을, 없으면 새 InMemory 생성(하위호환).
    _session_store: SessionStore = session_store if session_store is not None else InMemorySessionStore()
    _session_ask = SessionAskOrg(ask=bundle.ask, session_store=_session_store)
    _review_store = review_store
    _review_service = review_service
    _reeval_store = reeval_store
    _reeval_service = reeval_service
    _manager_queue_store = manager_queue_store
    _git_gateway: GitGateway = git_gateway if git_gateway is not None else FakeGitGateway()
    # SSO 모드(T7.1·ADR 0021 결정 4) — oidc_provider 주입 시 SSO 모드(POST /login/sso 활성·
    # 무비밀번호 POST /login은 403 거부). 미주입이면 기존 동작(OFF/무비밀번호).
    _oidc_provider = oidc_provider
    _sso_enabled = oidc_provider is not None

    # NotAuthenticatedError → 401 매핑
    from fastapi import Request as FastApiRequest
    from fastapi.responses import JSONResponse

    @app.exception_handler(NotAuthenticatedError)
    async def not_authenticated_handler(  # pyright: ignore[reportUnusedFunction]
        request: FastApiRequest, exc: NotAuthenticatedError
    ) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    _COOKIE_NAME = "aon_uid"

    @app.post("/ask")
    def ask_endpoint(  # pyright: ignore[reportUnusedFunction]
        req: AskRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        uid: str | None = request.cookies.get(_COOKIE_NAME)
        if uid is None:
            uid = secrets.token_urlsafe(16)
            response.set_cookie(
                key=_COOKIE_NAME,
                value=uid,
                httponly=True,
                samesite="lax",
                path="/",
            )
        reply = _session_ask.handle(req.question, User(id=uid))
        return serialize_reply(reply)

    @app.post("/ask/stream")
    def ask_stream_endpoint(  # pyright: ignore[reportUnusedFunction]
        req: AskRequest,
        request: Request,
    ) -> StreamingResponse:
        """`/ask`의 SSE 스트리밍 형제 — 답을 토큰 단위로 점진 푸시한다(ADR 0031 결정 2·3·5).

        요청 본문·익명 세션 쿠키(`_COOKIE_NAME`)는 `/ask`와 동일 패턴. `handle_stream`을 순회해
        각 `AskEvent`를 `serialize_sse_event`로 SSE 프레임으로 흘린다. 런타임 예외·timeout 시
        내부 예외·스택을 절대 노출하지 않고(노출 불변식) 마지막에 `ErrorEvent` 1프레임만 흘리고
        종료한다. 기본 런타임이 이미 `ClaudeCodeRuntime`(이제 `answer_stream` 구현)이라 스트리밍
        디스패처면 여러 델타가, 미지원이면 한 델타가 흐른다(폴백 규약).
        """
        uid: str | None = request.cookies.get(_COOKIE_NAME)
        set_cookie_uid: str | None = None
        if uid is None:
            uid = secrets.token_urlsafe(16)
            set_cookie_uid = uid
        user = User(id=uid)
        question = req.question

        def generate() -> Iterator[str]:
            try:
                for event in _session_ask.handle_stream(question, user):
                    yield serialize_sse_event(event)
            except Exception:
                # 런타임 실패·timeout — 내부 예외·스택은 절대 노출하지 않고(노출 불변식)
                # 중립 안내 ErrorEvent 1프레임만 흘리고 종료한다. 부분 출력은 이미 흘러간 뒤다.
                yield serialize_sse_event(
                    ErrorEvent(message="답변을 생성하는 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요.")
                )

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        sse = StreamingResponse(
            generate(), media_type="text/event-stream", headers=headers
        )
        if set_cookie_uid is not None:
            sse.set_cookie(
                key=_COOKIE_NAME,
                value=set_cookie_uid,
                httponly=True,
                samesite="lax",
                path="/",
            )
        return sse

    @app.get("/ask/{tracking}")
    def retrieve_endpoint(tracking: str) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        reply = bundle.ask.retrieve(tracking)
        if reply is None:
            raise HTTPException(status_code=404, detail="알 수 없는 추적 토큰")
        return serialize_reply(reply)

    # ── 운영 면 인증 라우트 (T6.5 슬라이스 1) ───────────────────────────────

    @app.post("/login")
    def login(req: LoginRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """무비밀번호 로그인 — `user_id`를 세션에 박는다(ADR 0016 결정 2).

        `req.user_id`가 Registry에 실재하는 User여야 한다(없으면 401). 검사 출처는
        bundle이 보는 Registry(데모 6명). 유효하면 `request.session[_SESSION_USER_KEY]`에 저장.

        **SSO 모드(T7.1·ADR 0021 결정 4)**: `oidc_provider` 주입 시 이 무비밀번호 채널은
        403으로 거부한다 — SSO를 켰는데 신원 *선택*이 살아 있으면 SSO 증명이 무의미해지므로
        (선택 우회 차단). SSO 모드에선 `POST /login/sso`(신원 증명)만 쓴다.
        """
        if _sso_enabled:
            raise HTTPException(
                status_code=403, detail="SSO 모드 — POST /login/sso(신원 증명)를 사용하세요"
            )
        if req.user_id not in bundle.registry.user_ids():
            raise HTTPException(status_code=401, detail=f"미존재 사용자: {req.user_id!r}")
        request.session[_SESSION_USER_KEY] = req.user_id
        return {"ok": True, "user_id": req.user_id}

    @app.post("/login/sso")
    def login_sso(req: SsoLoginRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """SSO 로그인 — IdP가 *증명*한 신원만 세션에 박는다(T7.1·ADR 0021 결정 4·5).

        흐름(shape — tdd-engineer가 red→green으로 채운다):
          ① SSO 모드 가드 — `oidc_provider` 미주입이면 404(SSO 비활성·이 엔드포인트 없음).
          ② `oidc_provider.verify(req.id_token)` → `OidcClaims`. 검증 실패
             (`OidcVerificationError`)는 401(증명 실패).
          ③ `resolve_identity(claims, registry)` → registry user_id(verified email 매핑·
             email_verified 가드·0매칭 거부). 매핑 실패도 401(증명은 됐으나 우리 신원 아님).
          ④ 그 user_id를 *기존 세션 키*(`_SESSION_USER_KEY`)에 박는다 → 이후 `_session_identity`·
             운영 스코프·concur·빌더 OKF 커밋 author 전부 무변경 재사용(ADR 0021 결정 5 —
             신원 출처가 세션으로 격리돼 있어 박기 전 검증만 바뀐다).
        """
        if _oidc_provider is None:
            raise HTTPException(status_code=404, detail="SSO 비활성 — oidc_provider 미주입")
        try:
            claims = _oidc_provider.verify(req.id_token)
            user_id = resolve_identity(claims, bundle.registry)
        except OidcVerificationError as exc:
            raise HTTPException(status_code=401, detail=f"SSO 신원 증명 실패: {exc}")
        request.session[_SESSION_USER_KEY] = user_id
        return {"ok": True, "user_id": user_id}

    @app.post("/logout")
    def logout(request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """로그아웃 — 세션 클리어(ADR 0016 결정 2)."""
        request.session.clear()
        return {"ok": True}

    @app.get("/")
    def index() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_INDEX_HTML)

    @app.get("/inbox")
    def inbox_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_INBOX_HTML)

    # ── T6.5 슬라이스 2: 신원을 세션에서 읽는 운영 엔드포인트 ──────────────────

    @app.get("/inbox/cases")
    def inbox_cases(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """세션 owner의 처리함 케이스 조회 (ADR 0016 결정 3).

        path param 제거 — 세션 신원으로 자기 처리함만(남의 것 지목 표면 없음).
        """
        owner_id = _session_identity(request)
        cases = bundle.case_store.open_for_owner(owner_id)
        return [
            serialize_case(c, bundle.registry, bundle.published_index_store)
            for c in cases
        ]

    @app.get("/inbox/backup-reviews")
    def inbox_backup_reviews(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """세션 owner의 백업 검토 탭 조회 (ADR 0016 결정 3).

        path param 제거 — 세션 신원으로 자기 검토 탭만.
        """
        owner_id = _session_identity(request)
        if _review_store is None:
            return []
        items = _review_store.pending_for_owner(owner_id)
        return [serialize_review_item(it) for it in items]

    @app.get("/inbox/reeval")
    def inbox_reeval(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """세션 owner의 재평가 탭(세 번째 탭) 조회 (ADR 0019 결정 5·둘째 탭 GET 미러).

        path param 제거 — 세션 신원으로 자기 재평가 탭만. store 미주입이면 빈 목록
        (`/inbox/backup-reviews` review_store None 미러). question·reason은 serialize가
        subject에서 파생한다(audit_reader로 Answer 축 question 보강·없으면 폴백).
        """
        owner_id = _session_identity(request)
        if _reeval_store is None:
            return []
        items = _reeval_store.pending_for_owner(owner_id)
        return [
            serialize_reeval_item(it, bundle.registry, bundle.audit_reader)
            for it in items
        ]

    @app.post("/cases/{case_id}/concur")
    def concur(case_id: str, req: ConcurRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """1인칭 합의 표 — 인증 활성 시 by_owner를 세션에서, 미활성 시 body에서.

        스코프(인증 활성): 세션 owner가 그 case의 후보가 아니면 ValueError → 403.
        """
        if _auth_enabled:
            by_owner = _session_identity(request)
        else:
            by_owner = req.by_owner
        vote = ConcurOnPrimary(
            by_owner=by_owner,
            on_agent=req.on_agent,
            rationale=req.rationale,
        )
        try:
            outcome = bundle.consensus.concur(case_id, vote)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            if "후보 owner 아님" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        if isinstance(outcome, Deadlocked):
            case = bundle.case_store.get(case_id)
            if case is not None:
                bundle.ask.enqueue_deadlock(case, reason=outcome.reason)
        return serialize_outcome(outcome)

    @app.post("/inbox/cases/{case_id}/document")
    def inbox_fetch_document(  # pyright: ignore[reportUnusedFunction]
        case_id: str, req: FetchDocumentRequest, request: Request
    ) -> dict[str, Any]:
        """on-demand 문서 fetch — 연관 개념 클릭 시 owner 워커에서 문서 본문을 끌어온다.

        권한 두 축 중 *요청 측*(ADR 0028 §15 결정 E): 세션 owner가 *자기가 후보로 걸린
        다툼 케이스의 후보 문서만* fetch할 수 있다. 검증(인증 활성 시):
          ① case_id로 케이스를 찾는다(미존재 → 404).
          ② 세션 owner가 그 케이스 후보인지(`concur` 스코프 "후보 아니면 403"의 fetch판).
          ③ `agent_id`가 그 케이스 후보 중 하나인지(남의 OKF 무단 열람 차단).
        ②·③ 둘 다 통과해야 `FetchDocument`를 보낸다(불통이면 403). 통과 시 디스패처로
        fetch(결정 B/C) → 본문(또는 degradation 메시지)을 응답한다. **중앙 저장 0**(중계만,
        결정 E) — 본문은 디스패처 슬롯을 거쳐 이 응답으로 통과만 한다.

        읽기 측 권한(워커 자기 카드만·결정 D)은 워커가 따로 진다(이중 게이트).
        """
        from agent_org_network.transport import WebSocketDispatcher as _WSD

        case = bundle.case_store.get(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"미존재 케이스: {case_id!r}")
        if _auth_enabled:
            session_owner = _session_identity(request)  # 미로그인 → 401
            if not case.involves_owner(session_owner):
                raise HTTPException(
                    status_code=403,
                    detail=f"자기 케이스 후보만 문서를 열 수 있습니다(세션 {session_owner!r}).",
                )
        # agent_id가 그 케이스 후보인지 — 인증 무관 항상 검사(케이스 범위 한정).
        if req.agent_id not in case.candidate_ids():
            raise HTTPException(
                status_code=403,
                detail=f"케이스 후보가 아닌 카드입니다: {req.agent_id!r}",
            )
        if not isinstance(dispatcher, _WSD):
            # 분산 전송(WS 디스패처)이 아니면 owner 워커 연결이 없다 — degradation.
            return {"found": False, "available": False, "message": "추출 불가(분산 전송 비활성)"}
        result = dispatcher.fetch_document(req.agent_id, req.concept_id)
        if result.status == "offline":
            return {"found": False, "available": False, "message": "추출 불가(담당 워커 미연결)"}
        if result.status == "timeout":
            return {"found": False, "available": False, "message": "추출 불가(담당 워커 응답 없음)"}
        if not result.found:
            return {"found": False, "available": True, "message": "문서를 찾을 수 없습니다."}
        return {"found": True, "available": True, "content": result.content}

    @app.get("/manager/queue")
    def manager_queue(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """세션 manager_id의 큐 조회 (ADR 0016 결정 3).

        path param 제거 — 세션 신원의 큐만.
        """
        manager_id = _session_identity(request)
        if _manager_queue_store is None:
            return []
        items = _manager_queue_store.pending_for_manager(manager_id)
        return [serialize_manager_item(it) for it in items]

    @app.post("/manager/items/{item_id}/act")
    def manager_act(item_id: str, req: ManagerActionRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Manager 처분 — 인증 활성 시 by_manager를 세션에서, 미활성 시 body에서.

        스코프(인증 활성): 세션 신원 ≠ item.manager_id면 ValueError → 403.
        """
        if _auth_enabled:
            by_manager = _session_identity(request)
        else:
            by_manager = req.by_manager
        if _manager_queue_store is None:
            raise HTTPException(status_code=404, detail="Manager 큐가 비활성화되어 있습니다.")
        action = _parse_manager_action(req, by_manager)
        svc = ManagerQueueService(
            queue_store=_manager_queue_store,
            precedents=bundle.precedents,
            case_store=bundle.case_store,
        )
        try:
            resolved = svc.act(item_id, action)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            if "1인칭 위반" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        return serialize_manager_item(resolved)

    @app.post("/backup-reviews/{item_id}")
    def review_backup(item_id: str, req: BackupReviewRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """백업 답 검토 — 인증 활성 시 by_owner를 세션에서, 미활성 시 body에서.

        스코프(인증 활성): 세션 owner ≠ item.owner_id면 ValueError → 403.
        """
        if _auth_enabled:
            by_owner = _session_identity(request)
        else:
            by_owner = req.by_owner
        if _review_store is None or _review_service is None:
            raise HTTPException(status_code=404, detail="검토 기능이 비활성화되어 있습니다.")
        review = _parse_backup_review(req, by_owner)
        try:
            reviewed = _review_service.review(item_id, review)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            if "검토자" in msg and "다름" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        return serialize_review_item(reviewed)

    @app.post("/reeval/{item_id}/review")
    def review_reeval(item_id: str, req: ReevalReviewRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """재평가 처분(세 번째 탭) — 인증 활성 시 by_owner를 세션에서, 미활성 시 body에서.

        둘째 탭 `review_backup` 미러. `kind`→ReevalOutcome 빌드(keep|invalidate|supersede|
        acknowledge|reanswer·supersede는 new_primary 필수→없으면 400). `ReevalService.review`
        가 1인칭 강제(item.owner_id != by_owner면 ValueError→403)·미존재 item_id→ValueError→404.
        store/service 미주입이면 404(둘째 탭 None 미러). 전이≠기록 — 재검토 행위 audit은 호출자
        책임이나 여긴 둘째 탭과 동형으로 service 전이만(이번 범위).
        """
        if _auth_enabled:
            by_owner = _session_identity(request)
        else:
            by_owner = req.by_owner
        if _reeval_store is None or _reeval_service is None:
            raise HTTPException(status_code=404, detail="재평가 기능이 비활성화되어 있습니다.")
        try:
            outcome = _parse_reeval_outcome(req, by_owner)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            reviewed = _reeval_service.review(item_id, outcome)
        except ValueError as exc:
            msg = str(exc)
            if "미존재" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            if "1인칭 위반" in msg:
                raise HTTPException(status_code=403, detail=msg) from exc
            raise HTTPException(status_code=400, detail=msg) from exc
        return serialize_reeval_item(reviewed, bundle.registry, bundle.audit_reader)

    @app.get("/monitor")
    def monitor_logs(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """운영 모니터링 — 인증 활성 시 로그인 필요(ADR 0016 결정 5: 인증만)."""
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        if bundle.audit_reader is None:
            return []
        records = bundle.audit_reader.records()
        return [summarize_audit_record(i, r) for i, r in dedupe_audit_records(records)]

    # 정적 경로(/monitor/view)를 동적(/monitor/{index})보다 *먼저* 등록한다 —
    # 그러지 않으면 "view"가 {index}(int)에 잡혀 422가 난다(FastAPI 매칭 순서).
    @app.get("/monitor/view")
    def monitor_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_MONITOR_HTML)

    @app.get("/monitor/{index}")
    def monitor_detail(index: int, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """운영 모니터링 상세 — 인증 활성 시 로그인 필요(ADR 0016 결정 5)."""
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        if bundle.audit_reader is None:
            raise HTTPException(status_code=404, detail="모니터링 로그가 비활성화되어 있습니다.")
        record = bundle.audit_reader.record_at(index)
        if record is None:
            raise HTTPException(status_code=404, detail="알 수 없는 로그 인덱스")
        return record

    # ── T5.3: Org 그래프(운영자 면 — 레지스트리 순수 파생) ───────────────────────

    @app.get("/org/graph")
    def org_graph(request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Org 그래프 데이터 — Registry를 {nodes, edges}로 순수 파생(T5.3).

        모니터링과 같은 인증 결(인증 활성 시 로그인 필요, 세분 역할 없이 인증만 —
        ADR 0016 결정 5). 새 전이·기록 0(레지스트리 읽기 파생). 운영 면이라 내부값 노출 OK.
        """
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        return serialize_org_graph(bundle.registry)

    @app.get("/org/view")
    def org_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_ORG_HTML)

    # ── T5.3: Agent 빌더(Owner 면 — 카드 구성·검증·YAML 미리보기) ────────────────

    @app.post("/builder/validate")
    def builder_validate(req: BuilderValidateRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """빌더 카드 검증 — admission 통과면 YAML 미리보기, 실패면 사유(T5.3).

        라이브 등록 안 함(편집 채널 = git/PR, CONTEXT Maintainer) — YAML은 Owner가
        복사→커밋할 출력. **Owner 스코프(ADR 0016)**: 인증 활성 시 세션 신원 ≠ 카드
        `owner`면 403(자기 카드만 깎음 — 운영 스코프), 미로그인 401. 인증 OFF는 자유 구성.
        """
        if _auth_enabled:
            session_owner = _session_identity(request)  # 미로그인 401
            if req.owner != session_owner:
                raise HTTPException(
                    status_code=403,
                    detail=f"자기 카드만 구성할 수 있습니다(세션 {session_owner!r} ≠ owner {req.owner!r}).",
                )
        return validate_card_for_builder(req, bundle.registry)

    @app.post("/builder/okf/commit")
    def builder_okf_commit(req: BuilderOkfCommitRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """OKF 번들 파일을 owner author로 커밋한다(ADR 0018 결정 1·5).

        author는 세션 신원에서 채워진다(ADR 0016 위조 차단 — body 아님). Owner 스코프:
        세션 신원 ≠ 대상 카드 owner → 403, 미로그인 → 401, 카드 미존재 → 404,
        파일 없음/경로 탈출 → 400.
        """
        session_owner = _session_identity(request)  # 미로그인 → 401

        # agent_id 형식 검증(validate_card_for_builder와 대칭 — m3)
        if not req.agent_id or not req.agent_id.strip():
            raise HTTPException(status_code=400, detail="agent_id는 비어 있을 수 없습니다.")

        # 대상 카드 존재 및 owner 스코프 확인
        try:
            card = bundle.registry.get(req.agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"카드 미존재: {req.agent_id!r}")

        if card.owner != session_owner:
            raise HTTPException(
                status_code=403,
                detail=f"자기 번들만 커밋할 수 있습니다(세션 {session_owner!r} ≠ owner {card.owner!r}).",
            )

        # files 입력 검증 및 OkfFile 변환
        if not req.files:
            raise HTTPException(status_code=400, detail="커밋할 파일이 없습니다.")
        try:
            okf_files = tuple(
                OkfFile(path=f["path"], content=f["content"]) for f in req.files
            )
        except (KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"파일 형식 오류: {exc}") from exc

        commit_req = BuilderCommitRequest(
            agent_id=req.agent_id,
            owner=session_owner,
            files=okf_files,
            message=req.message,
        )
        try:
            result = commit_okf_bundle(commit_req, _git_gateway)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {"sha": result.sha, "agent_id": result.agent_id}

    @app.get("/builder")
    def builder_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_BUILDER_HTML)

    # ── 하위호환 path 라우트 (인증 OFF 환경 전용 — 데모/기존 테스트) ───────────────
    # **인증 ON(session_secret 주입)이면 이 path 가장 경로를 *등록하지 않는다*** — 그래야
    # `/inbox/{owner_id}` 같은 신원-지목 경로 자체가 존재하지 않아 세션 스코프 우회가
    # 구조적으로 불가능하다(ADR 0016 보안: 신원 출처를 세션으로 *옮긴다* — path/body에
    # 남겨두면 우회 표면이 된다). 인증 OFF(secret 미주입)는 데모/기존 테스트 전용 모드라
    # path param 가장을 허용한다(이 모드는 "데모용 로그인 가장"임을 명시 — ADR 0009).
    if not _auth_enabled:

        @app.get("/inbox/{owner_id}/backup-reviews")
        def inbox_backup_reviews_legacy(owner_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
            """하위호환(인증 OFF 전용): path param으로 owner 지정."""
            if _review_store is None:
                return []
            items = _review_store.pending_for_owner(owner_id)
            return [serialize_review_item(it) for it in items]

        @app.get("/inbox/{owner_id}/reeval")
        def inbox_reeval_legacy(owner_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
            """하위호환(인증 OFF 전용): path param으로 owner 지정(둘째 탭 레거시 미러)."""
            if _reeval_store is None:
                return []
            items = _reeval_store.pending_for_owner(owner_id)
            return [
                serialize_reeval_item(it, bundle.registry, bundle.audit_reader)
                for it in items
            ]

        @app.get("/inbox/{owner_id}")
        def inbox_cases_legacy(owner_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
            """하위호환(인증 OFF 전용): path param으로 owner 지정."""
            cases = bundle.case_store.open_for_owner(owner_id)
            return [
                serialize_case(c, bundle.registry, bundle.published_index_store)
                for c in cases
            ]

        @app.get("/manager/{manager_id}")
        def manager_queue_legacy(manager_id: str) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
            """하위호환(인증 OFF 전용): path param으로 manager 지정."""
            if _manager_queue_store is None:
                return []
            items = _manager_queue_store.pending_for_manager(manager_id)
            return [serialize_manager_item(it) for it in items]

    return app


# OPERATOR_SESSION_SECRET env 설정 시 인증 ON(프로덕션), 미설정 시 인증 OFF(데모).
# 프로덕션에서는 반드시 OPERATOR_SESSION_SECRET 환경변수를 설정할 것. 하드코딩 금지.
#
# 재평가(처리함 세 번째 탭) store·service 구성 + 데모 시드 — create_central_app과 동형
# (둘째 탭 미러). 인프로세스 데모 앱(web:app)도 스트리밍 /ask·다툼·백업과 함께 재평가
# 탭을 한 백엔드에서 보이게 한다. 실 OKF 커밋→StalenessPropagator 자동 적재는 후속.
_demo_reeval_store = InMemoryReevalStore()
_demo_reeval_service = ReevalService(_demo_reeval_store)
seed_demo_reeval_items(_demo_reeval_store)
app = create_app(
    session_secret=os.environ.get("OPERATOR_SESSION_SECRET"),
    reeval_store=_demo_reeval_store,
    reeval_service=_demo_reeval_service,
)
