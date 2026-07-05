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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, assert_never, cast

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
from agent_org_network.audit import InMemoryAuditLog, JsonlAuditLog, action_record
from agent_org_network.console import ConsoleFeed, stream_console_frames

if TYPE_CHECKING:
    from datetime import datetime as _DateTime

    from agent_org_network.agent_card import AgentCard
    from agent_org_network.answer_record import (
        AnswerCorrectionView,
        AnswerRecordStore,
        CorrectionStore,
        FeedbackStore,
        MonitoringItem,
    )
    from agent_org_network.audit import AuditReader
    from agent_org_network.presence import PresenceStatus
    from agent_org_network.sqlite_stores import SqliteRegistryJournal
from agent_org_network.conflict import (
    Agreed,
    ConcurOnPrimary,
    ConflictCase,
    ConsensusOutcome,
    Deadlocked,
    StillOpen,
)
from agent_org_network.demo import DEMO_OKF_ROOT, build_demo, seed_demo_reeval_items
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
    StalenessPropagator,
    SupersedePrecedent,
)
from agent_org_network.index_matcher import relevant_concepts
from agent_org_network.embedder_select import select_embedder
from agent_org_network.okf_dedup import classify_dedup_candidates
from agent_org_network.okf_authoring import (
    OkfAuthor,
    OkfDocumentDraft,
    OkfDraft,
    admit_okf,
    render_okf_markdown,
    run_authoring_pipeline,
    TextIngestor,
)
from agent_org_network.okf_index import build_knowledge_index_from_okf, parse_okf_document
from agent_org_network.registry import Registry
from agent_org_network.runtime import AgentRuntime
from agent_org_network.runtime_select import select_runtime
from agent_org_network.two_stage_router import (
    PublishedIndexStore,
    accept_published_index,
)
from agent_org_network.hitl import HitlToggleMap
from agent_org_network.session import SessionAskOrg, SessionStore
from agent_org_network.storage_select import (
    select_answer_record_store,
    select_correction_store,
    select_session_store,
    select_token_store,
)
from agent_org_network.token import TokenStore
from agent_org_network.user import User

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"
_INBOX_HTML = _WEB_DIR / "inbox.html"
_MONITOR_HTML = _WEB_DIR / "monitor.html"
_SUPERVISION_HTML = _WEB_DIR / "owner-monitor.html"
_CONSOLE_FEED_HTML = _WEB_DIR / "console-feed.html"
_ORG_HTML = _WEB_DIR / "org.html"
_BUILDER_HTML = _WEB_DIR / "builder.html"
_ADMIN_HTML = _WEB_DIR / "admin.html"

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


class CorrectionRequest(BaseModel):
    """POST /supervision/answers/{record_id}/correct 요청 바디(Phase 12 (A)·ADR 0033 결정 4).

    owner 로컬 감독 면(127.0.0.1 관례)의 정정 제출. `corrected_text`는 정정본, `rationale`은
    정정 사유(선택). `corrected_text`/`rationale`은 owner 스코핑 위반은 서비스가
    ValueError→403.

    `by_owner`(정정 주체 신원, `concur`/`ConcurRequest`와 대칭 — code-reviewer M-1):
    인증 활성(`_auth_enabled`) 시 **세션 신원을 강제**하고 이 필드는 무시한다(클라이언트
    자기보고 신뢰 금지 — 세션 신원이 오너 변경 후에도 항상 *지금* 로그인한 사용자를
    가리키므로, 구 owner UI 세션을 갖고 있어도 재로그인 없이는 남의 신원을 못 싣는다).
    미인증 로컬 관례(세션 미들웨어 없음)에서만 이 필드를 읽는다(하위호환) — 도메인
    `owner_of` 현재 owner 대조가 그 경우에도 최종 방어선.
    """

    by_owner: str = ""
    corrected_text: str
    rationale: str = ""


class FeedbackRequest(BaseModel):
    """POST /answer/{record_id}/feedback 요청 바디 — 질문자 좋음/싫음(계획 §10.5).

    `verdict`는 `good`|`bad` 2값(FeedbackVerdict와 같은 SSOT) — 잘못된 값은 pydantic이
    422로 거부. `comment`는 선택("싫음" 사유 — 담당자가 정정에 참고, good도 허용).
    """

    verdict: Literal["good", "bad"]
    comment: str = ""


class AdminCardRegisterRequest(BaseModel):
    """POST /admin/cards 요청 바디 — 신규 Agent Card 라이브 등록(ADR 0034 결정 1).

    `BuilderValidateRequest`와 같은 카드 필드를 받는다(폼→카드 후보 미러). 핸들러가
    `CardCandidate`로 변환해 admission 관문(`admit_card`)에 그대로 태운다 — 우회 등록
    API 없음(ADR 0023 계승). 통과 시 라이브 Registry에 즉시 반영 + 감사 로그 append.
    권한류 필드(`can_answer` 등)는 카드 under-claim 자기보고일 뿐(ADR 0004) — Authority
    SSOT는 여전히 중앙.
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


class AdminOwnerChangeRequest(BaseModel):
    """POST /admin/cards/{agent_id}/owner 요청 바디 — 오너 변경 전이(ADR 0034 결정 2).

    `new_owner`만 받는다 — 나머지 카드 값은 현재 라이브 카드에서 읽어 새 owner로 갈아끼운
    카드 후보를 구성한다(frozen 값 교체·agent_id 불변). 재-admission(새 owner 실재·참조
    무결성) 통과 시 스위치 + 구 owner 워커 토큰 revoke를 같은 임계 구역에서 실행한다.
    """

    new_owner: str


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


def serialize_monitoring_item(item: "MonitoringItem") -> dict[str, Any]:
    """담당자 모니터링 한 건을 화면向 dict로 변환한다(Phase 12 (A)·owner 로컬 감독 면).

    owner 자기 에이전트 감독 면이라 질문·답 본문·발신 mode·검토 필요 표식·정정 이력·
    답변 피드백을 그대로 노출한다(채팅 OrgReply 노출 불변식의 반대 — 여긴 담당자가
    답을 감독하는 면. 질문자 신원 `submitted_by`도 감독 면이라 leak 아님, 계획 §10.3).
    """
    return {
        "record_id": item.record.record_id,
        "question": item.record.question,
        "answer_text": item.record.answer_text,
        "agent_id": item.record.agent_id,
        "answered_by": item.record.answered_by,
        "mode": item.record.mode,
        "answered_at": item.record.answered_at.isoformat(),
        "needs_correction_review": item.needs_correction_review,
        "corrections": [
            {
                "corrected_text": ev.corrected_text,
                "by_owner": ev.by_owner,
                "rationale": ev.rationale,
                "corrected_at": ev.corrected_at.isoformat(),
            }
            for ev in item.corrections
        ],
        "feedback": (
            {
                "verdict": item.feedback.verdict,
                "comment": item.feedback.comment,
                "submitted_by": item.feedback.submitted_by,
                "submitted_at": item.feedback.submitted_at.isoformat(),
            }
            if item.feedback is not None
            else None
        ),
    }


def serialize_correction_view(view: "AnswerCorrectionView") -> dict[str, Any]:
    """질문자向 정정 배지 투영을 dict로 변환한다(풀 방식·원문+정정본 보존)."""
    return {
        "record_id": view.record_id,
        "original_text": view.original_text,
        "has_correction": view.has_correction,
        "corrected_text": view.corrected_text,
        "corrected_at": view.corrected_at.isoformat() if view.corrected_at is not None else None,
    }


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


# ── OKF 저작면(owner측 — ADR 0030 결정 2) 요청 바디·데모 author ──────────────
# 이 절의 라우트(/author/run·/author/publish)는 **owner측 저작면**이다(ADR 0030 결정 2).
# 카드 빌더(/builder/*·중앙)와 별개. 단일머신 데모는 in-process 디제너레이트(ADR 0030 §3·
# 워커=중앙 박스)로 같은 프로세스에서 돌되 **데이터 경계는 보존**한다 — raw 문서·staged
# 초안·LLM 토큰은 어떤 중앙 store에도 들어가지 않고, 중앙에 publish되는 것은 *목차*
# (KnowledgeIndex — concept_id·title·core_question·domain만·본문 0)뿐이다(§비소유 논거).


class AuthorRunRequest(BaseModel):
    """POST /author/run 요청 바디 — raw 문서 → staged 개념 초안(ADR 0030 결정 2·4).

    `agent_id`(어느 카드의 OKF인가)·`document`(owner가 붙여넣은 raw 텍스트). raw 문서는
    요청→응답 transient다 — 어떤 중앙 store에도 저장하지 않는다(비소유·결정 2).
    """

    agent_id: str
    document: str


class AuthorConceptDisposition(BaseModel):
    """승인된 개념 1건의 처분 — /author/publish의 concepts 원소.

    `disposition`: "approved"(원안 그대로)·"edited"(수정 필드 반영)·"rejected"(배포 제외).
    edited 시 title/core_question/body 수정본을 실으면 그 필드를 덮어쓴다(미지정은 보존).
    """

    concept_id: str
    disposition: Literal["approved", "edited", "rejected"]
    title: str | None = None
    core_question: str | None = None
    body: str | None = None
    domain: str | None = None
    type: str | None = None


class AuthorPublishRequest(BaseModel):
    """POST /author/publish 요청 바디 — 승인 개념 → owner git 커밋 + 목차 publish.

    `agent_id`·`concepts`(개념별 처분). rejected는 제외하고, edited는 수정 필드를 반영해
    `OkfDraft`를 구성한다. 커밋은 owner git(commit_okf_bundle), 중앙에는 *목차만* publish한다.
    """

    agent_id: str
    concepts: list[AuthorConceptDisposition]


def _make_default_author() -> OkfAuthor:
    """프로덕션 기본 OKF 저작자 — owner OAuth 실 추출(중앙 토큰 0·실사용 가짜 0).

    `author_select.select_author`(env `AON_AUTHOR` 시임·`runtime_select` 대칭)에 위임한다 —
    미설정 기본은 `LlmAuthor(AnthropicSdkTransport())`(인프로세스 anthropic SDK·지연 import·
    미설치면 명확한 SystemExit), `claude-code`면 `claude -p` 구독 위임. **어느 분기도 데모
    더블이 아니다** — env 토글은 *실* transport 사이만 고른다. 모델은 `AON_AUTHOR_MODEL`
    또는 `author_select.DEFAULT_AUTHOR_MODEL`. 결정론 테스트는 `create_app(author=FakeAuthor)`
    주입으로 이 경로 자체를 안 탄다.
    """
    from agent_org_network.author_select import select_author

    return select_author()


class AuthorConceptEditRequest(BaseModel):
    """PUT /author/concept/{agent_id}/{concept_id} 요청 바디 — 개념 편집(부분 덮어쓰기).

    모든 필드 선택(미지정=기존 값 보존). 핸들러가 현재 개념을 먼저 읽어 미지정 필드를 머지한
    뒤 admit_okf로 domain 권한을 재검증한다(over-claim 거부·Authority 중앙). concept_id는
    path에서 받으므로 body에 두지 않는다(고정 — 편집은 같은 파일 덮어쓰기).
    """

    title: str | None = None
    core_question: str | None = None
    body: str | None = None
    domain: str | None = None
    type: str | None = None


class AuthorDedupConcept(BaseModel):
    """POST /author/dedup 요청의 신규 staged 개념 1건(ADR 0032 §C 252~271행).

    `/author/run` 응답 concepts 원소와 같은 모양(concept_id·title·core_question·body·
    domain·type). 임베딩 입력 텍스트는 이 본문이라 owner측에서만 계산된다.
    """

    concept_id: str
    title: str
    core_question: str
    body: str
    domain: str
    type: str | None = None


class AuthorDedupRequest(BaseModel):
    """POST /author/dedup/{agent_id} 요청 — 신규 추출 staged 개념 vs 게시 라이브러리 near-dup.

    `concepts`: 이번 /author/run이 낸 미게시 staged 개념(아직 commit 안 됨). 핸들러가 owner측
    게시 라이브러리 전체를 읽어 pairwise cosine으로 near-dup 후보를 분류한다(ADR 0032 결정 C).
    """

    concepts: list[AuthorDedupConcept]


# near-dup 임계값(ADR 0032 OQ-5·결정 C3 — 주입 정책값·하드코딩 분산 금지). e5 instruct
# prefix 전제 cosine. OQ-5 갱신 시 이 *한 곳*만 바뀐다.
DEDUP_TAU_HIGH = 0.88  # 이상이면 auto_suggest(거의 동일·자동 병합 후보 제안)
DEDUP_TAU_LOW = 0.70  # [LOW, HIGH)이면 similar("비슷한 개념" 표시만)


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


# ── 운영자 콘솔 명령 요청 바디 (T9.2(b)·T9.3(b)·T9.5(c)) ───────────────────


class HitlToggleRequest(BaseModel):
    """POST /console/hitl/{agent_id} 요청 바디 — HITL 토글 set(ADR 0025)."""

    on: bool


class TokenIssueRequest(BaseModel):
    """POST /console/tokens 요청 바디 — 워커 admission 토큰 발급(ADR 0026).

    `owner_id`는 Registry에 실재하는 User여야 한다(없으면 404). `role`은
    `primary`|`backup`(ADR 0012 등급).
    """

    owner_id: str
    role: Literal["primary", "backup"] = "primary"


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
    author: OkfAuthor | None = None,
    token_store: TokenStore | None = None,
    hitl_toggles: HitlToggleMap | None = None,
    console_feed: "ConsoleFeed | None" = None,
    answer_record_store: "AnswerRecordStore | None" = None,
    correction_store: "CorrectionStore | None" = None,
    feedback_store: "FeedbackStore | None" = None,
    presence_of: "Callable[[str], PresenceStatus] | None" = None,
    registry_journal: "SqliteRegistryJournal | None" = None,
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
    `select_session_store()`(`storage_select.py`)가 고른다 — `AON_DB`(SQLite 파일 경로)
    env 설정 시 `SqliteSessionStore(path)`(durable, T9.8), 미설정 시 기존
    `InMemorySessionStore()`(하위호환 — 기존 동작 무변경). 명시 주입이 항상 env보다 우선.
    `author`(T11.7d 실 배선·ADR 0030 S1): `/author/run`이 쓰는 OKF 저작 포트. **미주입이면
    프로덕션 기본 = `LlmAuthor`(owner OAuth 인프로세스 anthropic SDK 실 추출·중앙 토큰 0)**를
    *지연* 생성한다(`_make_default_author` — anthropic 미설치면 명확한 SystemExit). 결정론
    테스트는 `FakeAuthor` 주입으로 실 LLM·실 네트워크를 막는다(`git_gateway`와 같은 seam).

    reeval Precedent 축 라이브 배선(T11.7e minor-1·ADR 0030 S4): `dispatcher`가
    `WebSocketDispatcher`이고 `reeval_store`가 주입돼 있으면, `build_demo` 완료 후 실
    `StalenessPropagator`(`bundle.precedents`·`bundle.audit_reader`·그 `reeval_store`·
    `bundle.registry` 기반 `owner_of`)를 구성해 `dispatcher.bind_propagator`로 사후 주입한다
    (`bind_published_index`와 대칭인 닭-달걀 해소 — 디스패처가 `build_demo`보다 먼저 만들어져
    생성자 시점엔 실 precedents가 없다). 미주입 조합(reeval_store 없음 또는 dispatcher가
    WebSocketDispatcher 아님)이면 배선하지 않아 기존 동작(하위호환·발화 0) 그대로다.

    `token_store`(T9.2(b)·T9.5(c)·ADR 0026): 워커 admission 토큰 포트. 콘솔 `/console/tokens*`
    라우트가 이 인스턴스를 발급/조회/revoke한다. 미주입이면 `select_token_store()`가 고른다
    (`AON_DB` 설정 시 `SqliteTokenStore(path)` durable, 미설정 시 기존 `InMemoryTokenStore()`
    — 하위호환. `session_store`와 같은 `AON_DB` 경로면 한 DB 파일을 공유한다 — 세션은
    `sessions`/`session_turns` 테이블, 토큰은 `tokens` 테이블이라 이름 충돌 없음).
    `create_central_app`은 이 인스턴스를 `WebSocketDispatcher(token_store=)`에도
    같이 물려 콘솔 발급 토큰으로 워커가 실제 register되게 한다(단일 원천).
    `hitl_toggles`(T9.2(b)·T9.3(b)·ADR 0025): HITL 런타임 토글 맵. 콘솔 `/console/hitl/{agent_id}`
    라우트가 set/get하고, `build_demo`가 만드는 `AskOrg`에 *같은 인스턴스*로 전달돼 다음 답의
    mode에 반영된다. 미주입이면 `HitlToggleMap()` 내부 생성(하위호환 — 기존 동작 100% 보존).
    `feedback_store`(계획 §10 — 답변 피드백): 질문자 좋음/싫음 저장 포트.
    `POST /answer/{record_id}/feedback`이 upsert하고, `monitoring_for_owner`가 조인해
    "검토 필요" 판정에 bad 피드백 축을 더한다(§10.3 OR 조인). **미주입이면 `/answer/
    {record_id}/feedback`이 503**(정정 서비스 미배선과 동형)이고 `monitoring_for_owner`
    호출도 `feedback_store=None`으로 나가 기존 판정 100% 보존(하위호환).
    """
    from starlette.middleware.sessions import SessionMiddleware

    app = FastAPI(title="Agent Org Network — 채팅·처리함(데모)")

    # SessionMiddleware 부착 (T6.5 슬라이스 1 — ADR 0016 결정 1).
    # session_secret 주입 시에만 붙인다(미주입이면 인증 없이 동작 — 하위호환).
    _auth_enabled = session_secret is not None
    if _auth_enabled:
        app.add_middleware(SessionMiddleware, secret_key=session_secret)

    # 콘솔 seam(T9.2(b)·T9.3(b)·T9.5(c)): 미주입이면 `select_token_store()`로 결정
    # (`AON_DB` 설정 시 SqliteTokenStore, 미설정 시 기존 InMemoryTokenStore — 하위호환).
    _token_store: TokenStore = token_store if token_store is not None else select_token_store()
    _hitl_toggles: HitlToggleMap = (
        hitl_toggles if hitl_toggles is not None else HitlToggleMap()
    )
    # 콘솔 관전 피드(T9.2(c)·ADR 0024): 미주입이면 이 앱이 한 인스턴스를 만들어 AskOrg
    # (build_demo 경유)·SSE 라우트(GET /console/feed)에 *같이* 물린다(단일 원천 — 질문
    # 처리가 emit하는 그 피드를 관전 스트림이 구독). `create_central_app`은 dispatcher에도
    # 같은 인스턴스를 물려 워커 연결/종료 사건까지 한 피드로 모은다(아래 배선).
    _console_feed: ConsoleFeed = console_feed if console_feed is not None else ConsoleFeed()
    # GET /console/hitl/{agent_id}가 "명시 set된 적 있는가"를 구분해 카드 시드 해석값을
    # 낼 수 있게 하는 보조 집합(HitlToggleMap.is_on 자체는 미설정을 False로 다뤄 카드
    # approval_when이 있는 카드도 시드 전엔 off로 보이는 걸 막는다).
    _hitl_toggles_explicit: set[str] = set()

    # 담당자 감독 배선(Phase 12 (A)(B)·ADR 0033 결정 4): 답변 감사 단위·정정 이벤트 저장소.
    # 미주입이면 `select_answer_record_store()`/`select_correction_store()`로 결정
    # (`AON_DB` 설정 시 `SqliteAnswerRecordStore`/`SqliteCorrectionStore` durable,
    # 미설정 시 기존 InMemory — 하위호환) — `build_demo`(→AskOrg 적재)·감독 라우트가
    # *같은 인스턴스*를 본다(단일 원천). `presence_of`는 답의 담당 프레즌스 조회 콜백
    # (오프라인 자동발신 → 검토 필요 표식) — 미주입이면 항상 offline 미간주
    # (needs_correction_review는 그 조건이 성립 못 해 False). `create_central_app`이
    # 실 `_presence_tracker.status`를 물린다.
    _answer_record_store: AnswerRecordStore = (
        answer_record_store
        if answer_record_store is not None
        else select_answer_record_store()
    )
    _correction_store: CorrectionStore = (
        correction_store if correction_store is not None else select_correction_store()
    )
    # 답변 피드백 스토어(계획 §10) — 완전 옵셔널 배선(정정 서비스와 동형): 미주입이면
    # `None`을 그대로 유지한다(강제 생성 없음). `/answer/{record_id}/feedback`이 503,
    # `monitoring_for_owner` 호출도 `feedback_store=None`으로 나가 기존 판정 보존.
    _feedback_store: "FeedbackStore | None" = feedback_store

    bundle = build_demo(
        runtime=runtime,
        dispatcher=dispatcher,
        review_store=review_store,
        manager_queue_store=manager_queue_store,
        audit_log=audit_log,
        hitl_toggles=_hitl_toggles,
        console_feed=_console_feed,
        answer_record_store=_answer_record_store,
        presence_of=presence_of,
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
    # reeval 인덱스-수용 훅 라이브 배선(T11.7e minor-1·ADR 0030 S4): 실 `StalenessPropagator`를
    # `build_demo` 완료 *후* 구성해 디스패처에 사후 주입한다(`bind_propagator` — 위 published
    # index bind와 대칭인 닭-달걀 해소). `create_central_app`이 디스패처를 이 함수 호출보다
    # 먼저 만들어야 하므로 생성자 시점엔 실 `precedents`가 없다 — 여기서 그 시점 문제를 푼다.
    # `bundle.precedents`(build_demo가 실제로 판례를 채우는 그 store — 빈 새 통 아님)와
    # `bundle.audit_reader`(build_demo가 만든 audit 인스턴스 — `create_app`이 build_demo에
    # 넘긴 `audit_log`와 같은 것이라 `/ask`가 쓰는 바로 그 로그, Answer 축 정합)를 그대로
    # 물린다. `owner_of`는 `bundle.registry.get(agent_id).owner`(미등록 agent_id는 방어적으로
    # None — reeval.py "manager_of 정신"). `reeval_store`가 주입돼 있을 때만 구성한다 —
    # 미주입(`_ws_demo_app`류 기존 WebSocketDispatcher 단위 테스트)이면 배선하지 않아 기존
    # 동작(하위호환·발화 0) 그대로 보존된다.
    if (
        isinstance(dispatcher, WebSocketDispatcher)
        and reeval_store is not None
        and bundle.audit_reader is not None
    ):
        def _owner_of(agent_id: str) -> str | None:
            try:
                return bundle.registry.get(agent_id).owner
            except KeyError:
                return None

        propagator = StalenessPropagator(
            precedents=bundle.precedents,
            audit_reader=bundle.audit_reader,
            reeval_store=reeval_store,
            owner_of=_owner_of,
        )
        dispatcher.bind_propagator(propagator)
    # T9.1(d): 세션 층 래퍼 — AskOrg를 *수정하지 않고* 감싸기로 세션을 붙인다.
    # /ask 엔드포인트만 교체. retrieve·dispatched·mcp_server는 이번 스코프 밖.
    # Phase 9 쿠키 세션 seam: 주입 store가 있으면 그것을, 없으면 `select_session_store()`로
    # 결정(`AON_DB` 설정 시 SqliteSessionStore, 미설정 시 기존 InMemorySessionStore — 하위호환).
    _session_store: SessionStore = session_store if session_store is not None else select_session_store()
    # app.state 노출 — 어떤 store가 실제로 선택됐는지(InMemory/Sqlite) 배선을 관찰하는
    # seam(FastAPI 표준 확장점). 라우트는 여전히 클로저 변수 `_session_store`/`_token_store`를
    # 쓴다(무변경) — 이 노출은 순수 관찰용.
    app.state.session_store = _session_store
    app.state.token_store = _token_store
    _session_ask = SessionAskOrg(ask=bundle.ask, session_store=_session_store)
    _review_store = review_store
    _review_service = review_service
    _reeval_store = reeval_store
    _reeval_service = reeval_service
    _manager_queue_store = manager_queue_store
    # 정정 상태기계(Phase 12 (A)·ADR 0033 결정 4): `submit_correction`이 원 레코드 불변으로
    # `CorrectionEvent`를 append하고 정정 지식을 재평가 큐에 얹는다. reeval 적재 대상은
    # bundle이 항상 구성하는 reeval_store(주입 없으면 그것) — 정정→판례/지식 갱신 고리.
    from agent_org_network.answer_record import CorrectionService as _CorrectionService

    _correction_reeval_store = (
        reeval_store if reeval_store is not None else bundle.reeval_store
    )
    # 정정 판정 교체(Phase 12 3라운드·ADR 0034 결정 3): `submit_correction`이 "현재 카드
    # owner" 기준으로 정정 권한을 판정하도록 `owner_of`(레지스트리 조회)를 주입한다. 오너
    # 변경 후 새 owner가 과거 답을 정정 가능·구 owner는 거부. 카드가 사라지면 None → 정정
    # 불가(판정 원천 부재). 과거 `AnswerRecord.answered_by`는 불변(전이 ≠ 기록).
    def _card_owner_of(agent_id: str) -> str | None:
        try:
            return bundle.registry.get(agent_id).owner
        except KeyError:
            return None

    _correction_service = (
        _CorrectionService(
            _answer_record_store,
            _correction_store,
            _correction_reeval_store,
            owner_of=_card_owner_of,
        )
        if _correction_reeval_store is not None
        else None
    )
    # 관리 UI 서비스(Phase 12 3라운드·ADR 0034 결정 1·2): 라이브 카드 등록 + 오너 변경
    # 전이. 같은 `bundle.registry`를 Router가 라이브로 읽으므로(라우터는 매 route마다
    # `all_cards()` — 재색인 불요) 등록·전이가 다음 라우팅에 즉시 잡힌다. 감사는
    # `bundle.audit`(등록·전이 이력의 append-only 진실 — YAML git 추적을 대신), 오너 변경
    # 시 구 owner 토큰 revoke는 `_token_store`(콘솔 발급과 같은 인스턴스), 구 owner 워커 WS
    # 세션 끊기는 `WebSocketDispatcher.disconnect`(등급별 — presence offline·in-flight
    # re-queue). 디스패처가 WS가 아니면(로컬/데모) disconnect 미배선(no-op·하위호환).
    #
    # 카드 durable 저널(Phase 12 SQLite 확장·ADR 0034 결정 1 "AON_DB 영속"): 미주입이면
    # `select_registry_journal()`로 결정(`AON_DB` 설정 시 `SqliteRegistryJournal(path)`,
    # 미설정 시 `None` — 하위호환·기존 InMemory Registry 그대로). 저널이 있으면 (1) 이
    # 함수 호출 시점에 기존 저널 항목을 `bundle.registry`(하드코딩/YAML 시드 완료 상태)
    # 위에 리플레이해 이전 프로세스의 라이브 등록·오너 변경을 복원하고(admission 경유 —
    # 무효 항목은 스킵), (2) `AdminRegistryService(journal_sink=)`로 물려 *이후* 등록·
    # 오너 변경도 저널에 계속 append되게 한다.
    from agent_org_network.admin_registry import (
        AdminRegistryService as _AdminRegistryService,
        replay_registry_journal as _replay_registry_journal,
    )
    from agent_org_network.storage_select import select_registry_journal

    _admin_disconnect_owner: "Callable[[str], None] | None" = None
    if isinstance(dispatcher, WebSocketDispatcher):
        _ws_dispatcher = dispatcher

        def _disconnect_owner_all_roles(owner_id: str) -> None:
            # 구 owner 워커의 모든 등급(primary/backup) 연결을 끊는다 — 등록 레지스트리에서
            # 제거(다음 push 차단)·presence offline 도출·in-flight claimed 작업 re-queue.
            # 이미 열린 소켓 자체는 토큰 revoke 후 재인증 실패로 닫히지만, 여기서 명시적
            # disconnect로 즉시 presence·큐 정리를 유발한다(ADR 0034 결정 2 부작용 축).
            _ws_dispatcher.disconnect(owner_id, "primary")
            _ws_dispatcher.disconnect(owner_id, "backup")

        _admin_disconnect_owner = _disconnect_owner_all_roles

    _registry_journal = (
        registry_journal if registry_journal is not None else select_registry_journal()
    )
    if _registry_journal is not None:
        _replay_registry_journal(_registry_journal, bundle.registry)

    _admin_registry_service = _AdminRegistryService(
        bundle.registry,
        audit_sink=bundle.audit,
        token_store=_token_store,
        disconnect_owner=_admin_disconnect_owner,
        journal_sink=_registry_journal,
    )
    # app.state 노출 — 어떤 store가 실제로 선택됐는지(InMemory/Sqlite) 배선을 관찰하는
    # seam(`session_store`/`token_store`와 같은 패턴). 라우트는 클로저 변수를 그대로 쓴다.
    app.state.answer_record_store = _answer_record_store
    app.state.correction_store = _correction_store
    app.state.feedback_store = _feedback_store
    app.state.registry_journal = _registry_journal
    _git_gateway: GitGateway = git_gateway if git_gateway is not None else FakeGitGateway()
    # OKF 저작자(/author/run) — 주입(테스트=FakeAuthor)이면 그대로, 미주입(프로덕션)이면 실
    # `LlmAuthor`를 *지연* 생성한다. `runtime_select` 대칭: anthropic SDK는 선택 extra라 첫
    # 저작 호출 시점에야 import한다(미설치 owner는 /author를 안 치는 한 무접촉·core 의존 0).
    # 1-요소 holder로 클로저 안에서 1회 초기화한다(앱당 1개 author 재사용).
    _author_holder: list[OkfAuthor | None] = [author]

    def _get_author() -> OkfAuthor:
        existing = _author_holder[0]
        if existing is not None:
            return existing
        built = _make_default_author()  # anthropic 미설치면 여기서 명확한 SystemExit
        _author_holder[0] = built
        return built
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

    # ── 담당자 감독(Supervised Answering) 면 — Phase 12 (A)·ADR 0033 결정 4 ────
    #
    # owner 로컬 감독 면(127.0.0.1 관례·owner_web 검토면과 대칭). 자기 에이전트의 Answer
    # Record 목록·검토 필요 필터·정정 제출·프레즌스 배지. owner 스코핑은 agent_id(조회)와
    # CorrectionService 1인칭 대조(정정)가 떠받친다. 질문자 정정 배지는 별 라우트(/answer/…).

    from agent_org_network.answer_record import (
        monitoring_for_owner as _monitoring_for_owner,
        view_answer_with_correction as _view_answer_with_correction,
    )

    @app.get("/supervision")
    def supervision_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_SUPERVISION_HTML)

    @app.get("/supervision/answers")
    def supervision_answers(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, needs_review: bool = False
    ) -> list[dict[str, Any]]:
        # 자기 에이전트의 답 목록(최신순)·정정 이력 투영. needs_review=true면 검토 필요만.
        # owner 스코핑: agent_id로만 스코핑(담당자는 자기 에이전트 것만 — 도메인 코어 계약).
        items = _monitoring_for_owner(
            _answer_record_store,
            _correction_store,
            agent_id=agent_id,
            feedback_store=_feedback_store,
        )
        items = sorted(items, key=lambda it: it.record.answered_at, reverse=True)
        if needs_review:
            items = [it for it in items if it.needs_correction_review]
        return [serialize_monitoring_item(it) for it in items]

    @app.post("/supervision/answers/{record_id}/correct")
    def supervision_correct(  # pyright: ignore[reportUnusedFunction]
        record_id: str, req: CorrectionRequest, request: Request
    ) -> dict[str, Any]:
        # 정정 제출 — 미배선(reeval 부재)이면 503, 미존재 레코드면 404, 남의 에이전트
        # 정정(1인칭 위반)이면 403. 원 레코드는 불변으로 CorrectionEvent만 append된다.
        #
        # by_owner 출처(`concur`와 대칭·code-reviewer M-1): 인증 활성 시 세션 신원을
        # 강제(클라이언트 자기보고 무시) — 오너 변경 후 새 owner 세션이 정정할 수 있고
        # 구 owner 세션은 여전히 자기 신원(구 owner)으로만 보내므로 도메인 판정에서
        # 403이 난다. 미인증 로컬 관례에선 body의 by_owner를 읽되, 도메인 `owner_of`
        # 현재 owner 대조가 최종 방어선(위조돼도 현재 owner가 아니면 거부).
        if _correction_service is None:
            raise HTTPException(status_code=503, detail="정정 서비스가 배선되지 않았습니다")
        by_owner = _session_identity(request) if _auth_enabled else req.by_owner
        try:
            event = _correction_service.submit_correction(
                record_id=record_id,
                by_owner=by_owner,
                corrected_text=req.corrected_text,
                rationale=req.rationale,
            )
        except ValueError as exc:
            # 미존재 레코드 vs 1인칭 위반을 메시지로 가른다(도메인이 두 경우 다 ValueError).
            if "미존재" in str(exc):
                raise HTTPException(status_code=404, detail=str(exc))
            raise HTTPException(status_code=403, detail=str(exc))
        return {"submitted": True, "event_id": event.event_id, "record_id": record_id}

    @app.get("/supervision/presence/{agent_id}")
    def supervision_presence(agent_id: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        # 자기 워커 프레즌스 배지(온라인/오프라인). 미배선이면 offline 기본(안전측·미관측 정신).
        status: "PresenceStatus" = presence_of(agent_id) if presence_of is not None else "offline"
        return {"agent_id": agent_id, "status": status}

    @app.get("/answer/{record_id}/correction")
    def answer_correction(record_id: str) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        # 질문자 정정 배지(풀 방식·ADR 0033 결정 4) — 원문 + (있으면) 정정 배지·정정본.
        # 노출 불변식: 정정 주체(by_owner)·사유 등 감독 내부값은 싣지 않는다(원문·정정본만).
        view = _view_answer_with_correction(
            _answer_record_store, _correction_store, record_id=record_id
        )
        if view is None:
            raise HTTPException(status_code=404, detail="알 수 없는 답변 레코드")
        return serialize_correction_view(view)

    @app.post("/answer/{record_id}/feedback")
    def answer_feedback(  # pyright: ignore[reportUnusedFunction]
        record_id: str, req: FeedbackRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        # 질문자 측 답변 피드백 제출(계획 §10.5) — 세션 신원 불요. submitted_by는 /ask와
        # 같은 익명 쿠키(`_COOKIE_NAME`)에서 읽는다(없으면 새로 발급 — /ask와 동형).
        # 검증 순서: 미배선(feedback_store 없음)이면 503 → 미존재 record면 404 →
        # verdict enum은 pydantic이 이미 422로 거부(FeedbackRequest 필드 타입).
        # 노출 불변식: 응답엔 접수 확인(submitted·record_id·verdict)만 싣는다 — bad
        # 피드백이 어느 owner/agent로 갔는지·내부 판정은 절대 노출하지 않는다.
        if _feedback_store is None:
            raise HTTPException(status_code=503, detail="피드백 서비스가 배선되지 않았습니다")
        if _answer_record_store.get(record_id) is None:
            raise HTTPException(status_code=404, detail="알 수 없는 답변 레코드")

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
        from agent_org_network.answer_record import (
            AnswerFeedback as _AnswerFeedback,
            default_clock as _default_clock,
        )

        _feedback_store.upsert(
            _AnswerFeedback(
                record_id=record_id,
                verdict=req.verdict,
                comment=req.comment,
                submitted_by=uid,
                submitted_at=_default_clock(),
            )
        )
        return {"submitted": True, "record_id": record_id, "verdict": req.verdict}

    # ── 관리 UI 라우트 (Phase 12 3라운드·ADR 0034) ──────────────────────────
    #
    # 신규 카드 등록 + 오너 변경 전이. 운영자 면이라 세션 신원을 요구한다(인증 활성 시
    # 미로그인 401·ADR 0034 결정 4). 역할(root/운영자) 구분은 아직 없다 — 실 SSO 활성 시
    # `resolve_identity` 위에 운영자 role 클레임 검사를 얹는 게 강화 지점(ADR 0016/0021).
    # 우회 등록 API 없음: 둘 다 `admit_card` 관문을 통과해야 라이브에 들어간다(ADR 0023).

    from agent_org_network.admin_registry import (
        AdmissionError as _AdmissionError,
        CardCandidate as _CardCandidate,
        DuplicateCardError as _DuplicateCardError,
        UnknownCardError as _UnknownCardError,
    )

    @app.get("/admin")
    def admin_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        return FileResponse(_ADMIN_HTML)

    @app.get("/admin/cards")
    def admin_list_cards(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """등록된 카드 목록(운영 면) — 오너 변경 폼의 대상 선택 원천.

        운영 면이라 내부값(agent_id·owner·team·domains) 노출 OK(채팅 OrgReply 불변식의
        반대·`serialize_org_graph` 결). 인증 활성 시 미로그인 401.
        """
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        return [
            {
                "agent_id": c.agent_id,
                "owner": c.owner,
                "team": c.team,
                "domains": list(c.domains),
            }
            for c in sorted(bundle.registry.all_cards(), key=lambda c: c.agent_id)
        ]

    @app.post("/admin/cards")
    def admin_register_card(  # pyright: ignore[reportUnusedFunction]
        req: AdminCardRegisterRequest, request: Request
    ) -> dict[str, Any]:
        """신규 카드 라이브 등록 — admission 통과 즉시 반영(ADR 0034 결정 1).

        무효 카드는 422(사유 목록)·중복 agent_id는 409·미로그인 401. 우회 없이
        `admit_card` 관문을 통과해야 라이브에 들어간다("무효 카드 등록 금지" 불변식).
        """
        operator = _session_identity(request) if _auth_enabled else "operator"
        candidate = _CardCandidate(
            agent_id=req.agent_id,
            owner=req.owner,
            team=req.team,
            summary=req.summary,
            domains=list(req.domains),
            last_reviewed_at=req.last_reviewed_at,
            maintainer=req.maintainer,
            can_answer=list(req.can_answer),
            cannot_answer=list(req.cannot_answer),
            approval_when=list(req.approval_when),
            collaborate_when=list(req.collaborate_when),
            knowledge_sources=list(req.knowledge_sources),
            trust_labels=list(req.trust_labels),
        )
        try:
            card = _admin_registry_service.register_card(candidate, by=operator)
        except _AdmissionError as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.errors})
        except _DuplicateCardError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"registered": True, "agent_id": card.agent_id, "owner": card.owner}

    @app.post("/admin/cards/{agent_id}/owner")
    def admin_change_owner(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, req: AdminOwnerChangeRequest, request: Request
    ) -> dict[str, Any]:
        """오너 변경 전이 — 재-admission + 스위치 + 구 owner 토큰 revoke(ADR 0034 결정 2).

        현재 카드 값을 읽어 새 owner로 갈아끼운 후보를 구성하고(agent_id 불변·frozen 값
        교체) 재-admission을 태운다. 무효 새 owner는 422·미존재 카드는 404·미로그인 401.
        스위치와 revoke는 도메인 서비스가 같은 임계 구역에서 실행(owner 격리 보안 계약).
        """
        operator = _session_identity(request) if _auth_enabled else "operator"
        try:
            current = bundle.registry.get(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"미존재 agent_id: {agent_id!r}")
        candidate = _CardCandidate(
            agent_id=current.agent_id,
            owner=req.new_owner,
            team=current.team,
            summary=current.summary,
            domains=list(current.domains),
            last_reviewed_at=current.last_reviewed_at.isoformat(),
            maintainer=current.maintainer,
            can_answer=list(current.can_answer),
            cannot_answer=list(current.cannot_answer),
            approval_when=list(current.approval_when),
            collaborate_when=list(current.collaborate_when),
            knowledge_sources=list(current.knowledge_sources),
            trust_labels=list(current.trust_labels),
        )
        try:
            result = _admin_registry_service.transfer_ownership(candidate, by=operator)
        except _AdmissionError as exc:
            raise HTTPException(status_code=422, detail={"errors": exc.errors})
        except _UnknownCardError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "transferred": True,
            "agent_id": result.agent_id,
            "from_owner": result.from_owner,
            "to_owner": result.to_owner,
            "revoked_token_ids": result.revoked_token_ids,
            "audit_index": result.audit_index,
        }

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
        # 전이 ≠ 기록 — 전이(review_store)는 위 service가 했고, 검토 *행위*의 절차 기록은
        # 호출자(여기)가 audit에 남긴다(ADR 0012 결정 7). 실패(4xx) 경로는 행위 미발생이라
        # 기록하지 않는다 — service 성공 뒤에만 append(append-only·기존 인덱스 불변).
        if bundle.audit is not None:
            from datetime import UTC, datetime

            bundle.audit.record_action(
                action_record(
                    timestamp=datetime.now(UTC),
                    action=f"backup_review.{req.type}",
                    subject_id=item_id,
                    by=by_owner,
                )
            )
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
        # 전이 ≠ 기록 — 재평가 처분 *행위*의 절차 기록(둘째 탭 review_backup과 동형).
        # 실패(4xx)는 행위 미발생이라 기록 없음 — service 성공 뒤에만 append.
        if bundle.audit is not None:
            from datetime import UTC, datetime

            bundle.audit.record_action(
                action_record(
                    timestamp=datetime.now(UTC),
                    action=f"reeval.{req.kind}",
                    subject_id=item_id,
                    by=by_owner,
                )
            )
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

    # ── 운영자 콘솔 POST 명령 (T9.2(b)·T9.3(b)·T9.5(c)) ─────────────────────
    #
    # 각 명령은 도메인 서비스(SessionStore.end·HitlToggleMap.set·TokenStore)를 부르는
    # 얇은 어댑터다. 운영자 인증은 /monitor·/org/graph와 같은 결(인증 활성 시 로그인
    # 필요, 세분 역할 없이 인증만 — ADR 0016 결정 5). 인증 OFF면 기존 데모 관용대로 통과.

    def _console_now() -> "_DateTime":
        from datetime import UTC, datetime

        return datetime.now(UTC)

    @app.post("/console/sessions/{session_id}/end")
    def console_end_session(session_id: str, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """운영자 세션 종료(ADR 0024 결정 4) — `SessionStore.end`를 그대로 부른다."""
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        ended = _session_store.end(session_id)
        if ended is None:
            raise HTTPException(status_code=404, detail=f"미존재 세션: {session_id!r}")
        return {
            "session_id": ended.session_id,
            "user_id": ended.user_id,
            "status": ended.status,
        }

    @app.get("/console/hitl/{agent_id}")
    def console_get_hitl(agent_id: str, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """HITL 토글 현재 상태 조회 — 카드 시드 포함 해석값(ADR 0025).

        토글맵에 명시 set이 없으면 카드 approval_when 시드(`seed_from_card`)로 해석한다
        (`HitlToggleMap.is_on` 자체 기본값 False와 달리, 여기선 *카드 정책 시드*를 우선
        반영해 운영자가 "이 카드는 원래 어떤 기본값인가"를 볼 수 있게 한다). 카드 미존재
        시 404.
        """
        try:
            card = bundle.registry.get(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"미존재 agent_id: {agent_id!r}")
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        from agent_org_network.hitl import seed_from_card

        on = _hitl_toggles.is_on(agent_id) if agent_id in _hitl_toggles_explicit else seed_from_card(card)
        return {"agent_id": agent_id, "on": on}

    @app.post("/console/hitl/{agent_id}")
    def console_set_hitl(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, req: HitlToggleRequest, request: Request
    ) -> dict[str, Any]:
        """HITL 토글 set — `HitlToggleMap.set`을 그대로 부른다(ADR 0025). 카드 미존재 404."""
        try:
            bundle.registry.get(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"미존재 agent_id: {agent_id!r}")
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        _hitl_toggles.set(agent_id, req.on)
        _hitl_toggles_explicit.add(agent_id)
        return {"agent_id": agent_id, "on": req.on}

    @app.post("/console/tokens")
    def console_issue_token(req: TokenIssueRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """워커 admission 토큰 발급 — 평문 raw token은 이 응답 1회만(ADR 0026).

        `owner_id`는 Registry 실재 User여야 한다(없으면 404). 저장은 해시만
        (`TokenStore.issue` 계약) — 이후 `GET /console/tokens`는 절대 평문을 다시 보여주지
        않는다.
        """
        if req.owner_id not in bundle.registry.user_ids():
            raise HTTPException(status_code=404, detail=f"미존재 owner_id: {req.owner_id!r}")
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        raw, token = _token_store.issue(req.owner_id, req.role, now=_console_now())
        return {
            "token_id": token.token_id,
            "owner_id": token.owner_id,
            "role": token.role,
            "token": raw,  # 평문 — 이 응답 1회만
            "issued_at": token.issued_at.isoformat(),
            "expires_at": token.expires_at.isoformat() if token.expires_at is not None else None,
        }

    @app.get("/console/tokens")
    def console_list_tokens(request: Request) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """활성 토큰 목록 — token_hash·평문 미노출(운영 면이어도 비밀은 렌더 규율)."""
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        return [
            {
                "token_id": t.token_id,
                "owner_id": t.owner_id,
                "role": t.role,
                "issued_at": t.issued_at.isoformat(),
                "expires_at": t.expires_at.isoformat() if t.expires_at is not None else None,
            }
            for t in _token_store.list_active(now=_console_now())
        ]

    @app.post("/console/tokens/{token_id}/revoke")
    def console_revoke_token(token_id: str, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """토큰 취소(revoke) — append-only(삭제 X), 미존재는 404."""
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401
        revoked = _token_store.revoke(token_id, now=_console_now())
        if revoked is None:
            raise HTTPException(status_code=404, detail=f"미존재 token_id: {token_id!r}")
        return {"token_id": revoked.token_id, "revoked": revoked.revoked}

    # ── 운영자 콘솔 관전 SSE 피드 (T9.2(c)·ADR 0024) ────────────────────────
    #
    # `/ask/stream`(사용자 답 SSE)과 같은 패턴이되 *운영 면 관전*이다 — 질문 인입·라우팅
    # 결정·답 확정·워커 연결/종료 사건을 실시간으로 흘린다. 인증은 /monitor와 같은 결
    # (인증 활성 시 로그인 401). 연결 종료 시 finally로 반드시 unsubscribe(누수 방지).

    @app.get("/console/feed")
    def console_feed_stream(request: Request) -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        """관전 피드 SSE 스트림 — 구독→큐 pop→SSE 프레임 yield, 종료 시 unsubscribe.

        `/ask/stream` 패턴(StreamingResponse·text/event-stream). `_console_feed`에 subscribe해
        큐에서 이벤트를 꺼내 `serialize_console_sse`로 프레임을 흘린다. 이벤트가 없으면
        주기적으로 keep-alive 코멘트 프레임(`: keep-alive`)을 흘려 프록시 idle 타임아웃을
        막는다(15s류). 클라이언트가 끊기면 generator가 GeneratorExit로 종료되고 finally가
        반드시 unsubscribe한다(느린/떠난 구독자 큐 누수 방지). 운영 면 인증은 /monitor 결.
        """
        if _auth_enabled:
            _session_identity(request)  # 미로그인 401

        # 프로덕션은 무한 스트림(`stop`은 항상 False) — 연결이 끊기면 StreamingResponse가
        # 제너레이터를 GeneratorExit로 닫고, `stream_console_frames`의 finally가 unsubscribe
        # 한다. 프레임 로직(구독→pop→프레임·프라이밍·keep-alive)은 console 모듈이 소유해
        # 결정론 단위 테스트가 유한 `stop`으로 검증한다(TestClient 무한 스트림 우회).
        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            stream_console_frames(_console_feed, stop=lambda: False),
            media_type="text/event-stream",
            headers=headers,
        )

    @app.get("/console/view")
    def console_feed_page() -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        """관전 화면 서빙 — EventSource로 /console/feed를 구독하는 정적 HTML."""
        return FileResponse(_CONSOLE_FEED_HTML)

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

    # ── OKF 저작면(owner측 — ADR 0030 결정 2·4) 라우트 2개 ─────────────────────
    # 저작은 **owner측**이다(ADR 0030 결정 2 — 중앙은 raw·초안·LLM 토큰 0·*목차만*).
    # 카드 빌더(/builder/*·중앙)와 별개. 단일머신 데모는 in-process 디제너레이트(ADR 0030
    # §3·워커=중앙 박스)로 같은 프로세스에서 돌되 **데이터 경계는 보존**한다:
    #   - raw 문서·staged 초안은 어떤 중앙 store에도 안 들어간다(요청→응답 transient·owner측).
    #   - 중앙 published_index_store에 publish되는 것은 *목차*(KnowledgeIndex)뿐이다 —
    #     concept_id·title·core_question·domain만·본문 0(§비소유 논거). 이게 이 작업의 핵심.

    def _author_scoped_card(agent_id: str, request: Request) -> "AgentCard":
        """저작 라우트의 owner 스코프 가드(/builder/okf/commit과 같은 규칙).

        미로그인 → 401(인증 활성 시)·미존재 카드 → 404·세션 신원 ≠ card.owner → 403.
        인증 OFF면 owner 스코프를 강제하지 않는다(데모/기존 테스트 — 자기 카드만 가정).
        """
        session_owner: str | None = None
        if _auth_enabled:
            session_owner = _session_identity(request)  # 미로그인 → 401
        if not agent_id or not agent_id.strip():
            raise HTTPException(status_code=400, detail="agent_id는 비어 있을 수 없습니다.")
        try:
            card = bundle.registry.get(agent_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"카드 미존재: {agent_id!r}")
        if _auth_enabled and card.owner != session_owner:
            raise HTTPException(
                status_code=403,
                detail=f"자기 OKF만 저작할 수 있습니다(세션 {session_owner!r} ≠ owner {card.owner!r}).",
            )
        return card

    @app.post("/author/run")
    def author_run(req: AuthorRunRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """raw 문서 → staged 개념 초안(owner측·transient·중앙 store 0·ADR 0030 결정 2·4).

        파이프라인: TextIngestor → run_authoring_pipeline(실 `LlmAuthor`) → admit_okf(over-claim
        필터). 프로덕션 author는 owner OAuth 인프로세스 anthropic SDK로 split/derive/link를 실
        추출한다(중앙 토큰 0·`_make_default_author`). 결정론 테스트는 `create_app(author=...)`로
        `FakeAuthor`를 주입해 실 LLM·실 네트워크를 막는다(T11.7d·ADR 0030 S1).

        **불변식 가드(비소유)**: raw 문서·staged 초안을 어떤 중앙 store에도 저장하지 않는다 —
        이 핸들러는 published_index_store·case_store 등 어떤 store에도 *쓰지 않는다*(읽기만·
        카드 스코프). 산출은 응답으로만 owner에게 돌아간다(요청→응답 transient).

        **노출 불변식**: 실 LLM 호출 실패·타임아웃 시 내부 예외·스택을 절대 노출하지 않는다 —
        `/ask` 스트림 ErrorEvent와 같은 정신으로 502 + 중립 메시지만 돌려준다(권한 가드의
        401/403/404 HTTPException은 try 밖이라 그대로 보존된다).
        """
        card = _author_scoped_card(req.agent_id, request)

        ingestor = TextIngestor()
        sources = ingestor.ingest([(f"{req.agent_id}-src", req.document)])
        try:
            author = _get_author()
            # 카드 권한 domain을 split에 힌트로 주입 — LLM이 개념 domain을 owner 권한
            # 라벨(예: 환불·보상)로 정렬하게 한다(매칭률↑). 강제 아님 — over-claim은
            # admit_okf가 정상 drop한다(ADR 0030 비소유·권한 중앙 선언 보존).
            authored = run_authoring_pipeline(
                req.agent_id, sources, author, tuple(card.domains)
            )
        except Exception as exc:  # 실 LLM 호출·파싱·타임아웃 실패 — 내부 예외·스택 미노출
            raise HTTPException(
                status_code=502,
                detail="저작 추출 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요.",
            ) from exc
        result = admit_okf(authored.draft, card)

        kept_ids = {doc.concept_id for doc in result.admitted.documents}
        concepts = [
            {
                "concept_id": doc.concept_id,
                "title": doc.title,
                "core_question": doc.core_question,
                "domain": doc.domain,
                "body": doc.body,
                "type": doc.type,
                "in_domain": doc.concept_id in kept_ids,
                # 실제 커밋되는 OKF 마크다운(프론트매터+본문) — owner가 OKF 형식을 눈으로 확인.
                "okf_markdown": render_okf_markdown(doc),
            }
            for doc in authored.draft.documents
        ]
        dropped = [
            {"concept_id": cid, "reason": "over-claim(권한 밖 domain) — admit_okf가 떨굼"}
            for cid in result.dropped_concepts
        ]
        stages = [
            {"key": "ingest", "label": "① 인제스트", "state": "done"},
            {"key": "split", "label": "② 개념 분할", "state": "done"},
            {"key": "derive", "label": "③ core_question 정련", "state": "done"},
            {"key": "link", "label": "④ 관계 도출", "state": "done"},
            {"key": "index", "label": "⑤ 목차(승인 후 publish)", "state": "pending"},
        ]
        return {"stages": stages, "concepts": concepts, "dropped": dropped}

    @app.post("/author/publish")
    def author_publish(req: AuthorPublishRequest, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """승인 개념 → owner git 커밋 + 목차만 중앙 publish(owner측·ADR 0030 결정 2·3).

        disposition 적용: rejected 제외·edited는 수정 필드 반영 → OkfDraft 구성 →
        admit_okf(over-claim 재필터) → render_okf_markdown → commit_okf_bundle(owner git·
        /builder/okf/commit과 같은 게이트웨이) → 커밋된 번들에서 **목차(KnowledgeIndex) 도출
        → 중앙 published_index_store에 publish(목차만·내용 0)**.

        **불변식 가드(비소유)**: 중앙 store에는 *목차*(KnowledgeIndex)만 넣는다 —
        accept_published_index가 받는 것은 build_index_from_admitted가 만든 KnowledgeIndex
        (concept_id·title·core_question·domain·type만·본문 0)다. raw 본문·LLM 토큰은 중앙에
        안 간다(비소유). 목차 도출용 마크다운 직렬화는 *격리 임시 디렉터리*에 쓰고 버린다 —
        owner OKF 본체·중앙 어디에도 본문이 남지 않는다. rejected 개념은 커밋·publish 0.
        """
        card = _author_scoped_card(req.agent_id, request)
        session_owner = card.owner if not _auth_enabled else _session_identity(request)

        # disposition 적용: rejected 제외·edited 수정 필드 반영 → OkfDocumentDraft 구성
        docs: list[OkfDocumentDraft] = []
        for c in req.concepts:
            if c.disposition == "rejected":
                continue  # 거부분 — 커밋·publish 0
            try:
                docs.append(
                    OkfDocumentDraft(
                        concept_id=c.concept_id,
                        title=c.title or c.concept_id,
                        body=c.body or "(본문 없음)",
                        core_question=c.core_question or c.concept_id,
                        domain=c.domain or "",
                        type=c.type,
                    )
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"개념 형식 오류: {exc}") from exc

        if not docs:
            raise HTTPException(status_code=400, detail="배포할 승인 개념이 없습니다(전부 거부됨).")

        try:
            draft = OkfDraft(agent_id=req.agent_id, documents=tuple(docs))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"초안 구성 오류: {exc}") from exc

        # over-claim 재필터(저작측 admission — 권한 밖 domain 떨굼)
        result = admit_okf(draft, card)
        if not result.admitted.documents:
            raise HTTPException(
                status_code=400,
                detail="권한 안(under-claim) 개념이 없습니다 — 전부 over-claim으로 떨궈졌습니다.",
            )

        # owner git 커밋(/builder/okf/commit과 같은 게이트웨이·author=세션 신원)
        okf_files = tuple(
            OkfFile(path=f"{doc.concept_id}.md", content=render_okf_markdown(doc))
            for doc in result.admitted.documents
        )
        commit_req = BuilderCommitRequest(
            agent_id=req.agent_id,
            owner=session_owner,
            files=okf_files,
            message=f"OKF 저작 publish: {req.agent_id}",
        )
        try:
            commit_result = commit_okf_bundle(commit_req, _git_gateway)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 커밋된 번들 전체에서 *목차*(KnowledgeIndex) 도출 → 중앙에 목차만 publish.
        # ADR 0032 결정 B1: extract_snapshot으로 그 커밋 시점의 OKF 번들 *전체*(이전+이번 누적)를
        # 임시 디렉터리에 추출 → build_knowledge_index_from_okf로 전체 glob 도출.
        # 중앙 store에 들어가는 객체는 KnowledgeIndex(목차)뿐 — 비소유 보장.
        import tempfile
        from datetime import UTC, datetime

        generated_at = datetime.now(UTC)
        with tempfile.TemporaryDirectory() as tmp:
            okf_root = Path(tmp)
            agent_dest = okf_root / req.agent_id
            _git_gateway.extract_snapshot(commit_result.sha, req.agent_id, agent_dest)
            index = build_knowledge_index_from_okf(card, okf_root, generated_at=generated_at)
        # (임시 디렉터리는 with 블록 종료 시 삭제 — 본문 마크다운은 디스크에 안 남는다)

        published: dict[str, Any] | None = None
        if bundle.published_index_store is not None:
            # 중앙 수용 경로 재사용 — accept_published_index가 스코핑→필터→put(staleness).
            # propagator 옵션은 이번 범위 밖(주입 시 reeval 인덱스-수용 훅·ADR 0030 S4).
            accept_published_index(
                session_owner, index, bundle.registry, bundle.published_index_store
            )
            published = {
                "agent_id": index.agent_id,
                "concept_count": len(index.concepts),
                "generated_at": generated_at.isoformat(),
            }

        return {
            "committed": {
                "sha": commit_result.sha,
                "files": [f.path for f in okf_files],
            },
            "published": published,
            "dropped": list(result.dropped_concepts),
        }

    @app.get("/author/index/{agent_id}")
    def author_index(agent_id: str, request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """owner의 published 목차(KnowledgeIndex) 조회 — 중앙 store의 *목차만* 노출.

        owner 스코프 가드(`_author_scoped_card`): 미로그인 401·미존재 404·타인 403.

        **불변식 가드(비소유)**: 중앙 published_index_store에는 목차(KnowledgeIndex)뿐이라
        반환도 *목차만*이다 — Concept은 본문 필드 자체가 없다(id·label·core_question·domain·
        type만). raw 문서·staged 초안·LLM 토큰은 store에 안 들어가므로 여기서 노출할 길이 없다.
        미게시 카드(store None·get None)는 빈 목차(미아 아님)로 응답한다.
        """
        _author_scoped_card(agent_id, request)

        empty: dict[str, Any] = {"agent_id": agent_id, "generated_at": None, "concepts": []}
        if bundle.published_index_store is None:
            return empty
        index = bundle.published_index_store.get(agent_id)
        if index is None:
            return empty
        return {
            "agent_id": index.agent_id,
            "generated_at": index.generated_at.isoformat(),
            "concepts": [
                {
                    "id": c.id,
                    "label": c.label,
                    "core_question": c.core_question,
                    "domain": c.domain,
                    "type": c.type,
                }
                for c in index.concepts
            ],
        }

    def _validate_concept_id(concept_id: str) -> str:
        """concept_id가 순수 파일명 컴포넌트인지 검증(traversal 방어 — 400).

        저작면 단일 권위 `validate_safe_path_component`(agent_card.py) 재사용 — 구분자·`..`·
        절대경로·빈 값 거부. path 파라미터로 들어온 stem이 okf_root/{agent_id} 밖을 못 가리키게
        한다(GET/PUT/DELETE concept 공통 1차 방어).
        """
        from agent_org_network.agent_card import validate_safe_path_component

        try:
            return validate_safe_path_component(concept_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"concept_id 형식 오류: {exc}") from exc

    def _read_concept_doc(
        card: "AgentCard", concept_id: str
    ) -> "OkfDocumentDraft | None":
        """owner 게이트웨이 번들에서 한 개념의 OKF 본문을 읽어 OkfDocumentDraft로 역파싱한다.

        head_sha → extract_snapshot → {concept_id}.md 읽기 → _parse_frontmatter로 프론트매터
        파싱(render_okf_markdown 규약 역parse: title→title·description→core_question·
        tags[0]→domain·type→type·`---` 이후 본문→body). 파일/커밋 없으면 None.

        **owner 자기 조회**(익명 /ask 아님 — owner는 자기 OKF 소유자라 본문 노출은 비소유 위반
        아님). 임시 디렉터리는 with 블록 종료 시 삭제(본문 디스크 잔존 없음).
        """
        import tempfile

        try:
            head = _git_gateway.head_sha(card.agent_id)
        except (ValueError, KeyError):
            return None
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / card.agent_id
            _git_gateway.extract_snapshot(head, card.agent_id, dest)
            md_path = dest / f"{concept_id}.md"
            if not md_path.is_file():
                return None
            text = md_path.read_text(encoding="utf-8")
        # render_okf_markdown 규약 역parse: title→title·description→core_question·
        # tags[0]→domain·type→type·`---` 이후 본문→body(parse_okf_document 단일 권위).
        front, body = parse_okf_document(text)
        title = str(front.get("title", "") or "")
        core_question = str(front.get("description", "") or "")
        raw_tags: object = front.get("tags", [])
        domain = ""
        if isinstance(raw_tags, list) and raw_tags:
            first_tag: object = cast("list[object]", raw_tags)[0]
            domain = str(first_tag)
        raw_type = front.get("type")
        concept_type = str(raw_type) if raw_type is not None else None
        return OkfDocumentDraft(
            concept_id=concept_id,
            title=title or concept_id,
            body=body or "(본문 없음)",
            core_question=core_question or concept_id,
            domain=domain,
            type=concept_type,
        )

    def _read_all_concept_docs(card: "AgentCard") -> "list[OkfDocumentDraft]":
        """owner 게이트웨이 번들의 게시 개념 *전체*를 OkfDocumentDraft 리스트로 읽는다.

        `_read_concept_doc`(단일)의 디렉터리 버전: head_sha → extract_snapshot → 디렉터리
        `*.md` glob → 각 파일을 같은 역parse 규약(parse_okf_document)으로 OkfDocumentDraft로
        변환. 파일명 정렬(결정론·okf_index 도출 규칙과 같은 결).

        게시 인덱스 없음(커밋/번들 없음)이면 빈 리스트 → near-dup 후보 0(미아 아님·ADR 0032
        §C 278행). **owner 자기 조회**(읽기 전용·임시 디렉터리는 with 종료 시 삭제).

        `index.md`(번들 메타·type="index")는 실제 개념이 아니므로 제외한다(`LibraryPanel`이
        같은 메타를 화면에서 숨기는 것과 같은 결 — 비교 대상에 끼면 의미 없는 후보가 생긴다).
        """
        import tempfile

        try:
            head = _git_gateway.head_sha(card.agent_id)
        except (ValueError, KeyError):
            return []
        docs: list[OkfDocumentDraft] = []
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / card.agent_id
            _git_gateway.extract_snapshot(head, card.agent_id, dest)
            for md_path in sorted(dest.glob("*.md")):
                text = md_path.read_text(encoding="utf-8")
                front, body = parse_okf_document(text)
                title = str(front.get("title", "") or "")
                core_question = str(front.get("description", "") or "")
                raw_tags: object = front.get("tags", [])
                domain = ""
                if isinstance(raw_tags, list) and raw_tags:
                    first_tag: object = cast("list[object]", raw_tags)[0]
                    domain = str(first_tag)
                raw_type = front.get("type")
                concept_type = str(raw_type) if raw_type is not None else None
                if concept_type == "index":
                    continue
                concept_id = md_path.stem
                docs.append(
                    OkfDocumentDraft(
                        concept_id=concept_id,
                        title=title or concept_id,
                        body=body or "(본문 없음)",
                        core_question=core_question or concept_id,
                        domain=domain,
                        type=concept_type,
                    )
                )
        return docs

    def _rederive_and_accept_index(
        card: "AgentCard", sha: str, session_owner: str
    ) -> "dict[str, Any] | None":
        """커밋 직후 번들 전체에서 목차 재도출 → 중앙 publish(ADR 0032 B1 재사용).

        extract_snapshot(sha)로 그 커밋 시점 OKF 번들 전체를 임시 디렉터리에 추출 →
        build_knowledge_index_from_okf로 전체 glob 도출 → accept_published_index(스코핑→
        필터→put·staleness). store None이면 None. /author/publish의 재도출과 *동일 경로*다.
        """
        import tempfile
        from datetime import UTC, datetime

        generated_at = datetime.now(UTC)
        with tempfile.TemporaryDirectory() as tmp:
            okf_root = Path(tmp)
            agent_dest = okf_root / card.agent_id
            _git_gateway.extract_snapshot(sha, card.agent_id, agent_dest)
            index = build_knowledge_index_from_okf(card, okf_root, generated_at=generated_at)
        if bundle.published_index_store is None:
            return None
        accept_published_index(
            session_owner, index, bundle.registry, bundle.published_index_store
        )
        return {
            "agent_id": index.agent_id,
            "concept_count": len(index.concepts),
            "generated_at": generated_at.isoformat(),
        }

    @app.get("/author/concept/{agent_id}/{concept_id}")
    def author_concept_detail(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, concept_id: str, request: Request
    ) -> dict[str, Any]:
        """owner의 게시 개념 1건을 *본문 포함* 상세 조회(ADR 0032 OQ-3 — 편집 전 본문 확보).

        owner 스코프 가드(미로그인 401·미존재 카드 404·타인 403)·concept_id traversal 방어(400).
        게이트웨이 번들에서 그 개념 OKF 본문을 읽어 {concept_id, title, core_question, domain,
        body, type}로 반환한다. 개념 파일 없으면 404.

        **owner 자기 조회**: 본문 노출은 비소유 위반이 아니다 — owner는 자기 OKF 소유자이고,
        이 라우트는 _author_scoped_card로 *자기 카드*만 통과시킨다(타 owner 403).
        """
        card = _author_scoped_card(agent_id, request)
        cid = _validate_concept_id(concept_id)
        doc = _read_concept_doc(card, cid)
        if doc is None:
            raise HTTPException(status_code=404, detail=f"개념 미존재: {cid!r}")
        return {
            "concept_id": doc.concept_id,
            "title": doc.title,
            "core_question": doc.core_question,
            "domain": doc.domain,
            "body": doc.body,
            "type": doc.type,
        }

    @app.put("/author/concept/{agent_id}/{concept_id}")
    def author_concept_edit(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        concept_id: str,
        req: AuthorConceptEditRequest,
        request: Request,
    ) -> dict[str, Any]:
        """owner의 게시 개념 1건을 편집(부분 덮어쓰기·ADR 0032 OQ-3).

        절차: owner 스코프 가드 → 현재 개념 읽기(없으면 404) → 미지정 필드 머지 →
        OkfDocumentDraft(concept_id 고정) → admit_okf(domain 권한 재검증·over-claim 400) →
        render_okf_markdown → commit_okf_bundle(같은 {concept_id}.md 덮어쓰기) → 인덱스 재도출
        → accept_published_index. 응답: 갱신 개념 + published concept_count.

        **Authority 중앙**: admit_okf로 domain∈card.domains 재검증 — 편집이 권한을 못 넓힌다.
        """
        card = _author_scoped_card(agent_id, request)
        session_owner = card.owner if not _auth_enabled else _session_identity(request)
        cid = _validate_concept_id(concept_id)

        current = _read_concept_doc(card, cid)
        if current is None:
            raise HTTPException(status_code=404, detail=f"개념 미존재: {cid!r}")

        # 미지정 필드는 기존 값 보존(부분 덮어쓰기 머지). type는 명시 None 구분 없이 미지정 보존.
        merged_type = req.type if req.type is not None else current.type
        try:
            edited = OkfDocumentDraft(
                concept_id=cid,
                title=req.title if req.title is not None else current.title,
                body=req.body if req.body is not None else current.body,
                core_question=(
                    req.core_question
                    if req.core_question is not None
                    else current.core_question
                ),
                domain=req.domain if req.domain is not None else current.domain,
                type=merged_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"개념 형식 오류: {exc}") from exc

        # over-claim 재필터(Authority 중앙 — 편집 domain이 권한 밖이면 떨궈 400)
        result = admit_okf(OkfDraft(agent_id=agent_id, documents=(edited,)), card)
        if not result.admitted.documents:
            raise HTTPException(
                status_code=400,
                detail="권한 밖 domain입니다 — 편집이 over-claim으로 거부되었습니다.",
            )
        admitted_doc = result.admitted.documents[0]

        commit_req = BuilderCommitRequest(
            agent_id=agent_id,
            owner=session_owner,
            files=(OkfFile(path=f"{cid}.md", content=render_okf_markdown(admitted_doc)),),
            message=f"OKF 개념 편집: {agent_id}/{cid}",
        )
        try:
            commit_result = commit_okf_bundle(commit_req, _git_gateway)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        published = _rederive_and_accept_index(card, commit_result.sha, session_owner)
        return {
            "concept": {
                "concept_id": admitted_doc.concept_id,
                "title": admitted_doc.title,
                "core_question": admitted_doc.core_question,
                "domain": admitted_doc.domain,
                "body": admitted_doc.body,
                "type": admitted_doc.type,
            },
            "committed": {"sha": commit_result.sha},
            "published": published,
        }

    @app.delete("/author/concept/{agent_id}/{concept_id}")
    def author_concept_delete(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, concept_id: str, request: Request
    ) -> dict[str, Any]:
        """owner의 게시 개념 1건을 삭제(ADR 0032 OQ-3·결정 B3 물리 삭제 커밋).

        절차: owner 스코프 가드 → commit_okf_bundle(removed_paths=("{cid}.md",)·files=())로
        삭제 커밋 → 인덱스 재도출(그 개념 빠진 목차) → accept_published_index. 응답: 삭제 confirm
        + 남은 concept_count. 마지막 개념 삭제로 빈 번들이면 빈 인덱스(0 후보→Unowned→
        escalation·미아 없음 보존).

        **중앙 비소유**: 중앙은 삭제를 따로 모른다 — 완전 인덱스 교체가 곧 삭제 반영(결정 B3).
        """
        card = _author_scoped_card(agent_id, request)
        session_owner = card.owner if not _auth_enabled else _session_identity(request)
        cid = _validate_concept_id(concept_id)

        commit_req = BuilderCommitRequest(
            agent_id=agent_id,
            owner=session_owner,
            files=(),
            removed_paths=(f"{cid}.md",),
            message=f"OKF 개념 삭제: {agent_id}/{cid}",
        )
        try:
            commit_result = commit_okf_bundle(commit_req, _git_gateway)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        published = _rederive_and_accept_index(card, commit_result.sha, session_owner)
        return {
            "deleted": {"concept_id": cid},
            "committed": {"sha": commit_result.sha},
            "published": published,
        }

    @app.post("/author/dedup/{agent_id}")
    def author_dedup(  # pyright: ignore[reportUnusedFunction]
        agent_id: str, req: AuthorDedupRequest, request: Request
    ) -> dict[str, Any]:
        """신규 staged 개념 vs 게시 라이브러리 near-dup 후보 탐지(ADR 0032 결정 C·탐지 전용).

        **읽기 전용** — 중앙 store 무변경·owner git 무변경. 임베딩·cosine·후보 분류가 전부
        owner 프로세스에서 돈다(중앙 비소유). 응답은 concept_id·유사도·등급뿐(본문 0).

        절차(ADR 0032 §C 273~289행):
          owner 스코프 가드(401/404/403) → 게시 라이브러리 전체 읽기(`_read_all_concept_docs`·
          없으면 빈 리스트→후보 0) → `embed_text = title\\ncore_question\\nbody` 합성 →
          `select_embedder()`(env AON_EMBEDDER·미설정/demo면 None=비활성) → new/existing 양쪽
          임베딩 → `classify_dedup_candidates(τ_high·τ_low 주입)` → {"candidates": [...]}.

        임베더가 `None`(운영 기본·비활성)이면 임베딩을 건너뛰고 빈 후보를 낸다(extra 미설치
        owner 무영향). 병합 *실행*은 이 라우트가 안 한다 — owner가 후보를 보고 확정하면
        프론트가 기존 PUT(병합 본문)/DELETE(버릴 개념)로 처분한다(ADR 0032 결정 C4·301행).
        """
        card = _author_scoped_card(agent_id, request)

        def _embed_text(title: str, core_question: str, body: str) -> str:
            return f"{title}\n{core_question}\n{body}"

        embedder = select_embedder()
        if embedder is None:
            # 운영 기본(비활성) — 임베딩 의존성 없이 빈 후보로 통과(미아 아님).
            return {"candidates": []}

        existing_docs = _read_all_concept_docs(card)
        new_texts = [
            _embed_text(c.title, c.core_question, c.body) for c in req.concepts
        ]
        existing_texts = [
            _embed_text(d.title, d.core_question, d.body) for d in existing_docs
        ]
        new_vecs = embedder.embed(new_texts)
        existing_vecs = embedder.embed(existing_texts)
        candidates = classify_dedup_candidates(
            new_concepts=list(zip([c.concept_id for c in req.concepts], new_vecs)),
            existing_concepts=list(
                zip([d.concept_id for d in existing_docs], existing_vecs)
            ),
            tau_high=DEDUP_TAU_HIGH,
            tau_low=DEDUP_TAU_LOW,
        )
        return {
            "candidates": [
                {
                    "new_concept_id": c.new_concept_id,
                    "existing_concept_id": c.existing_concept_id,
                    "similarity": c.similarity,
                    "grade": c.grade,
                }
                for c in candidates
            ]
        }

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


def seed_gateway_from_disk(
    gateway: GitGateway, registry: Registry, okf_root: str | Path
) -> None:
    """디스크 `okf/{agent_id}/*.md` 베이스라인을 게이트웨이에 카드별 1커밋으로 시드한다.

    저작→답변 루프(ADR 0018 결정 4)의 단일 진실원천을 게이트웨이로 모은다 — 답변 런타임이
    커밋 스냅샷 모드로 접지할 때 *시드(기존 디스크 OKF)+저작(publish 누적)* 둘 다 닿게 하려고
    publish가 쓰는 그 게이트웨이를 디스크 베이스라인으로 먼저 채운다. 시드 = 같은 okf/ 파일이라
    기존 디스크-접지 답변과 동일 내용(회귀 0).

    규약: `okf_root/{agent_id}/`의 마크다운 파일들을 `OkfFile(path=상대경로)`로 모아
    `commit_okf_bundle`(author=card.owner)로 1커밋. okf 디렉터리가 없는 카드는 건너뛴다
    (커밋 없음 → 답변은 working tree 직독 폴백·하위호환). 실 git이 아니라 주입 게이트웨이
    (데모는 `FakeGitGateway`)에 커밋하므로 부작용·비결정 0.
    """
    root = Path(okf_root)
    for card in registry.all_cards():
        bundle_dir = root / card.agent_id
        if not bundle_dir.is_dir():
            continue
        files = tuple(
            OkfFile(
                path=str(md.relative_to(bundle_dir)),
                content=md.read_text(encoding="utf-8"),
            )
            for md in sorted(bundle_dir.rglob("*.md"))
        )
        if not files:
            continue
        commit_okf_bundle(
            BuilderCommitRequest(
                agent_id=card.agent_id,
                owner=card.owner,
                files=files,
                message=f"seed OKF baseline: {card.agent_id}",
            ),
            gateway,
        )


# OPERATOR_SESSION_SECRET env 설정 시 인증 ON(프로덕션), 미설정 시 인증 OFF(데모).
# 프로덕션에서는 반드시 OPERATOR_SESSION_SECRET 환경변수를 설정할 것. 하드코딩 금지.
#
# 재평가(처리함 세 번째 탭) store·service 구성 + 데모 시드 — create_central_app과 동형
# (둘째 탭 미러). 인프로세스 데모 앱(web:app)도 스트리밍 /ask·다툼·백업과 함께 재평가
# 탭을 한 백엔드에서 보이게 한다. 실 OKF 커밋→StalenessPropagator 자동 적재는 후속.
_demo_reeval_store = InMemoryReevalStore()
_demo_reeval_service = ReevalService(_demo_reeval_store)
seed_demo_reeval_items(_demo_reeval_store)

# 저작→답변 루프 단일 진실원천 = 게이트웨이(ADR 0018 결정 4). publish가 커밋하는 *그*
# 게이트웨이를 답변 런타임의 스냅샷 접지원으로도 쓴다 — 그래야 디스크 시드(기존 OKF)와
# 저작(publish 누적)이 *한 원천*으로 답변에 닿는다. 절차:
#   ① 게이트웨이 먼저 생성 → ② 디스크 okf/ 베이스라인을 카드별 1커밋으로 시드(seed_gateway_from_disk)
#   → ③ claude-code 분기 답변 런타임을 그 게이트웨이로 snapshot 모드 연결(select_runtime)
#   → ④ 같은 게이트웨이를 create_app에 주입(publish가 시드 위에 누적 커밋).
# `AON_PROVIDER` 설정 시(owner OAuth 공급자)는 게이트웨이가 그 분기에서 *무시*되고 런타임의
# okf_root 접지를 유지한다(snapshot 모드는 claude-code 전용 — select_runtime이 분기 격리).
_demo_gateway = FakeGitGateway()
# build_demo의 registry로 카드 목록을 얻는다(런타임은 안 부르고 .registry만 읽음 — 부작용 0).
seed_gateway_from_disk(_demo_gateway, build_demo().registry, DEMO_OKF_ROOT)
# 답 생성 런타임을 owner `AON_PROVIDER`로 고른다(worker와 공유 `select_runtime`). 미설정이면
# `ClaudeCodeRuntime`(기존 build_demo 기본·게이트·데모 행위 불변·무회귀)에 `_demo_gateway`를
# snapshot 모드로 연결 — 답변이 시드+저작 커밋 번들을 cwd로 접지한다. `AON_PROVIDER=claude-api`면
# owner OAuth 인프로세스 anthropic SDK 스트리밍 — `/ask/stream`에 실 토큰 델타가 흐른다(중앙 토큰 0).
app = create_app(
    runtime=select_runtime(DEMO_OKF_ROOT, git_gateway=_demo_gateway),
    session_secret=os.environ.get("OPERATOR_SESSION_SECRET"),
    reeval_store=_demo_reeval_store,
    reeval_service=_demo_reeval_service,
    git_gateway=_demo_gateway,
)
