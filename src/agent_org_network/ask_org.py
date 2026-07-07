import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, assert_never

from agent_org_network.audit import AuditEntry, AuditLog, Clock, default_clock
from agent_org_network.conflict import Candidate, ConflictCase, ConflictCaseStore
from agent_org_network.console import (
    AnswerSent as _AnswerSent,
    QuestionReceived as _QuestionReceived,
    RoutingDecisionRecorded as _RoutingDecisionRecorded,
)
from agent_org_network.decision import Contested, Routed, RoutingDecision, Unowned
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    DispatchOutcome,
    EscalatedToManager,
    LocalStreamingDispatcher,
    RuntimeDispatcher,
    WorkTicket,
)
from agent_org_network.router import RouterPort
from agent_org_network.runtime import AnswerMode
from agent_org_network.user import User

if TYPE_CHECKING:
    from datetime import datetime
    from agent_org_network.answer_record import AnswerRecordStore
    from agent_org_network.console import ConsoleEvent, ConsoleFeed
    from agent_org_network.grounding import GroundingSelector, GroundingSet
    from agent_org_network.hitl import HitlToggleMap
    from agent_org_network.manager_queue import ManagerQueueStore
    from agent_org_network.notify import Notifier
    from agent_org_network.presence import PresenceStatus
    from agent_org_network.review import BackupReview, BackupReviewItem, BackupReviewStore
    from agent_org_network.runtime import Answer

# 접지 텍스트 resolver seam(ADR 0037 결정 3) — agent_id 하나를 그 접지 본문
# 텍스트로 해소하는 함수. 실 KnowledgeStore 다중 조회 배선은 mcp-runtime
# 슬라이스 D 영역이라, 이번 슬라이스는 주입 함수 타입만 여기 선언한다
# (`assemble_grounding_text`가 이 타입의 `lookup` 인자를 받는다).
GroundingTextResolver = Callable[[str], str]


@dataclass(frozen=True)
class Answered:
    text: str
    answered_by: tuple[str, str]
    mode: AnswerMode
    sources: tuple[str, ...] = field(default_factory=tuple)
    # 답변 감사 단위(`AnswerRecord`) 식별자(Phase 12 (B)·ADR 0033 결정 4). 중앙 답변
    # 발신 시 적재된 레코드를 질문자가 나중에 재조회(풀 방식 정정 배지)할 때 쓰는 *불투명
    # 손잡이* 1개다 — `tracking`과 같은 결(내부 구조 미인코딩). 미배선(answer_record_store
    # 미주입)이면 None(하위호환 — 기존 즉답 경로는 record_id 없이 그대로 동작).
    record_id: str | None = None


# Pending kind 세 갈래로 확장(T6.3 슬라이스2). `dispatched`가 분산 전송의 비동기
# 결말을 사용자向으로 흡수한다 — DispatchOutcome의 `AwaitingWorker`(미회신)와
# `EscalatedToManager`(timeout/owner 부재)를 *둘 다* 하나로 모은다. 근거: 사용자
# 관점에선 "담당에게 보냈는데 아직 답이 없다(사람 손으로 넘어가는 중)"로 동일하고,
# 워커 미연결인지 Manager escalation인지는 *감춰야 할 내부값*이다(노출 불변식). kind를
# 둘로 쪼개면(dispatched/escalated) 그 내부 구분이 사용자에게 새어나간다. 그래서 최소화.
PendingKind = Literal["contested", "unowned", "dispatched"]


@dataclass(frozen=True)
class Pending:
    """담당이 사람 손에 있어 즉답이 없는 상태의 사용자向 투영.

    노출 불변식: `kind`+`message`(+ `dispatched`면 불투명 `tracking`)만 노출.
    manager_id·reason·ticket_id·owner·후보 목록 등 *조직 내부 구조*는 절대 싣지
    않는다(test_web `_LEAKY_KEYS`가 강제). `dispatched`는 분산 전송의 AwaitingWorker·
    EscalatedToManager를 함께 투영한 사용자 상태.

    `tracking`(ADR 0011 결정 6-5, 슬라이스2b): 답 *조회용 불투명 추적 토큰* 1개.
    워커가 나중에 회신한 답을 사용자가 받을 경로 — uuid4 hex라 owner_id·ticket_id·
    구조를 비추지 않는다(서버가 tracking→ticket 매핑을 따로 보관, 토큰 자체엔 내부값
    미인코딩). 노출 불변식의 *정밀화*이지 완화가 아니다(불투명 ID 1개 OK, 조직 구조 금지).
    `dispatched`에만 채워지고 contested/unowned는 None.
    """

    kind: PendingKind
    message: str
    tracking: str | None = None


OrgReply = Answered | Pending


# ── 노출 투영 헬퍼(SSOT) — 블로킹 /ask·SSE가 공유 ────────────────────────
#
# ADR 0031 결정 3: `meta`·`done`·`pending`의 페이로드 투영은 `serialize_reply`와 *같은
# 투영 규칙*을 공유한다. 두 경로(블로킹 /ask·스트리밍 SSE)가 노출 불변식을 *다르게* 흘릴
# 여지를 구조적으로 제거하려, `serialize_reply`가 Answered/Pending을 dict로 투영하던 로직을
# 여기 순수 헬퍼로 추출한다 — `serialize_reply`도 이 헬퍼를 쓰고, SSE 직렬화도 같은 헬퍼를
# 쓴다(노출 SSOT). 내부값(confidence·candidates·manager_id 등)은 어느 헬퍼도 싣지 않는다.


def project_answered_by(answered_by: tuple[str, str]) -> dict[str, str]:
    """담당(owner·agent_id) 투영 — Answered·MetaEvent가 공유(노출 불변식: 담당만)."""
    return {"owner": answered_by[0], "agent_id": answered_by[1]}


def project_answered(reply: "Answered") -> dict[str, "object"]:
    """Answered를 사용자向 dict로 투영한다(담당·mode·출처만 — 내부값 0).

    `record_id`(Phase 12 (B)·ADR 0033 결정 4)는 답변 감사 단위의 *불투명 손잡이* 1개로
    질문자가 답변 페이지에서 정정 배지를 풀(pull)로 조회할 때만 쓴다(`tracking`과 같은
    결 — 조직 내부 구조 미인코딩·불투명 ID라 leaky 아님). 미배선이면 None이라 실리지 않음.
    """
    body: dict[str, object] = {
        "type": "answered",
        "text": reply.text,
        "answered_by": project_answered_by(reply.answered_by),
        "mode": reply.mode,
        "sources": list(reply.sources),
    }
    if reply.record_id is not None:
        body["record_id"] = reply.record_id
    return body


def project_pending(reply: "Pending") -> dict[str, "object"]:
    """Pending을 사용자向 dict로 투영한다(kind·message·tracking?만 — 내부값 0)."""
    body: dict[str, object] = {
        "type": "pending",
        "kind": reply.kind,
        "message": reply.message,
    }
    if reply.tracking is not None:
        body["tracking"] = reply.tracking
    return body


def _answer_of(answered: "Answered") -> "Answer":
    """Answered(사용자向 투영)에서 audit용 runtime Answer를 복원한다(text·mode·sources).

    audit는 Delivered.answer(=runtime Answer)를 본다 — handle_stream이 완성 Answered를
    audit 엔트리에 싣기 위한 역투영(answered_by는 audit decision이 따로 들어 떨군다).
    """
    from agent_org_network.runtime import Answer as _Answer

    return _Answer(text=answered.text, sources=answered.sources, mode=answered.mode)


# 분산 전송 결말 → 사용자向 Pending 안내 문구. 둘 다 같은 `dispatched`로 모이되,
# 문구도 내부(워커 미연결 vs escalation)를 비추지 않는 중립 안내로 통일한다.
_DISPATCHED_MESSAGE = "담당에게 질문을 전달했어요. 답변이 준비되면 알림드릴게요."


# ── SSE 스트리밍 이벤트: AskEvent sealed sum(ADR 0031 결정 3) ─────────────
#
# `/ask` 스트리밍 한 프레임 — sealed sum("타입이 곧 상태", RoutingDecision·DispatchOutcome
# 정신). 각 이벤트는 SSE `event:` + `data:`(JSON) 한 프레임으로 직렬화된다. 이벤트 순서
# 불변: Routed 성공 `meta→token*→done` · Pending `pending` 단독 · 실패 `(meta?)→error`
# (한 스트림에 done과 pending 동시 불가·상호 배타).


@dataclass(frozen=True)
class MetaEvent:
    """Routed 스트림 시작 시 1회 — 담당·*초기 추정* mode·출처(노출 투영 후).

    `mode`는 초기 추정(보통 full)이고, *최종 권위*는 `DoneEvent.mode`다(Approval 게이트·
    backup 하향은 완성 답에 적용되므로). 답 전체에 붙는 신뢰 메타라 델타(token)엔 안 싣는다.
    """

    answered_by: tuple[str, str]
    mode: AnswerMode
    sources: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TokenEvent:
    """델타마다 N회 — 텍스트 델타만(answered_by·mode·sources 미포함·노출 불변식)."""

    text: str


@dataclass(frozen=True)
class DoneEvent:
    """Routed 스트림 종료 시 1회 — Approval 게이트 적용 후 *최종 권위* mode·출처."""

    mode: AnswerMode
    sources: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PendingEvent:
    """Contested/Unowned/dispatched 1회 후 종료(비스트림) — kind·message·tracking?만."""

    kind: PendingKind
    message: str
    tracking: str | None = None


@dataclass(frozen=True)
class ErrorEvent:
    """런타임 실패·timeout 1회 후 종료 — *중립 안내만*(내부 예외·스택 0)."""

    message: str


AskEvent = MetaEvent | TokenEvent | DoneEvent | PendingEvent | ErrorEvent


def serialize_sse_event(event: AskEvent) -> str:
    """AskEvent를 SSE 프레임 문자열(`event: <type>\\ndata: <json>\\n\\n`)로 직렬화한다(순수).

    노출 투영 SSOT(ADR 0031 결정 3): meta·done·pending 페이로드는 `serialize_reply`와 같은
    투영 헬퍼(`project_answered_by`·`project_pending`)를 거친다 — 두 경로가 노출 불변식을
    다르게 흘릴 여지 0. token은 텍스트 델타만, error는 중립 안내만(내부값 0). match+assert_never로
    5종 망라 — 새 이벤트 타입 추가 시 컴파일 강제(serialize_reply·_project_outcome 정신).
    """
    import json

    name: str
    data: dict[str, object]
    match event:
        case MetaEvent():
            name = "meta"
            data = {
                "answered_by": project_answered_by(event.answered_by),
                "mode": event.mode,
                "sources": list(event.sources),
            }
        case TokenEvent():
            name = "token"
            data = {"text": event.text}
        case DoneEvent():
            name = "done"
            data = {"mode": event.mode, "sources": list(event.sources)}
        case PendingEvent():
            name = "pending"
            data = {"kind": event.kind, "message": event.message}
            if event.tracking is not None:
                data["tracking"] = event.tracking
        case ErrorEvent():
            name = "error"
            data = {"message": event.message}
        case _ as never:
            assert_never(never)
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {name}\ndata: {payload}\n\n"


class AskOrg:
    """사용자 질문을 라우팅하고 담당 답을 *디스패처 경유*로 수집해 OrgReply로 투영한다.

    답 획득 경로(T6.3 슬라이스2): 동기 `AgentRuntime.answer` 직접 호출이 아니라
    `RuntimeDispatcher.dispatch→poll`을 본다. poll 결과 `DispatchOutcome`을 `OrgReply`로
    매핑한다 — Delivered→Answered(mode 보존), AwaitingWorker·EscalatedToManager→
    Pending(kind="dispatched"). escalation/미회신을 동기 Answer로 *위장하지 않는다*
    (ADR 0011 ②). in-process 데모/테스트는 `LocalRuntimeDispatcher`(즉시 Delivered)를
    주입해 Routed→Answered가 한 호출에 끝난다.
    """

    def __init__(
        self,
        router: RouterPort,
        dispatcher: RuntimeDispatcher,
        audit_log: AuditLog,
        clock: Clock = default_clock,
        case_store: ConflictCaseStore | None = None,
        review_store: "BackupReviewStore | None" = None,
        manager_queue_store: "ManagerQueueStore | None" = None,
        manager_of: "Callable[[str], str | None] | None" = None,
        manager_root: str = "root",
        notifier: "Notifier | None" = None,
        hitl_toggles: "HitlToggleMap | None" = None,
        console_feed: "ConsoleFeed | None" = None,
        answer_record_store: "AnswerRecordStore | None" = None,
        presence_of: "Callable[[str], PresenceStatus] | None" = None,
        grounding_selector: "GroundingSelector | None" = None,
        grounding_resolver: "GroundingTextResolver | None" = None,
    ) -> None:
        # [Minor 2] manager_root 기본값 "root" 주의: 데모의 실제 root는 "root_manager".
        # manager_queue_store 를 주입할 때 manager_root 도 함께 주입하지 않으면
        # escalation 이 존재하지 않는 "root" 큐로 귀속돼 Manager 화면에 표시되지 않는다.
        # manager_queue_store 주입 시 manager_root 도 주입 권장(build_demo 참고).
        self._router = router
        self._dispatcher = dispatcher
        self._audit = audit_log
        self._clock = clock
        self._case_store = case_store
        # 백업 답 검토 저장소(ADR 0012 결정 7). retrieve 덧씌움이 ticket_id로 조회해
        # 검토 결과를 우선 투영한다. 미주입이면 None(하위호환 — 검토 루프 없이 동작).
        self._review_store = review_store
        # Manager 큐(T5.2 ADR 0014): 미해소 escalation 보관소. 미주입이면 하위호환.
        self._manager_queue_store = manager_queue_store
        # owner → manager 콜백(Registry.get_user(owner).manager). 미주입이면 None.
        self._manager_of: Callable[[str], str | None] | None = manager_of
        # root User id — manager가 없는 escalation의 최종 수신자(미아 없음).
        # 기본값 "root"는 하위호환용이며 실제 root id 와 다를 수 있다.
        # manager_queue_store 주입 시 manager_root 도 반드시 주입할 것.
        self._manager_root = manager_root
        # 실시간 push 통지(T7.4·ADR 0022 결정 4) — 처리함/큐 적재 직후 push 발화. 미주입이면
        # 기존 동작(하위호환·게이트 보존 — push는 pull을 *추가*하지 대체하지 않는다). MVP 슬라이스
        # 발화 지점은 ConflictCase open(Contested arm)·Manager escalation 적재(나머지 확장은 후속).
        self._notifier = notifier
        # HITL 런타임 토글(T9.3(b)·ADR 0025) — 콘솔이 set, `_apply_approval_gate`가 read.
        # 미주입이면 기존 동작(카드 approval_when만 봄) 100% 보존(하위호환).
        self._hitl_toggles = hitl_toggles
        # 운영자 콘솔 관전 피드(T9.2(c)·ADR 0024) — handle이 질문 인입·라우팅 결정·답 확정
        # 시점에 ConsoleEvent를 emit한다(관전 미러). 미주입이면 발화 0(하위호환 — 기존 동작
        # 100% 무변경). emit 실패는 흡수한다(관전이 본 흐름을 못 깨는 계약 — 전이≠기록 정신).
        self._console_feed = console_feed
        # 답변 감사 단위 적재(Phase 12 (B)·ADR 0033 결정 4) — 중앙이 낸 답이 확정될 때마다
        # `AnswerRecord`를 append한다(담당자 모니터링·질문자 정정 배지의 데이터 원천). 미주입
        # 이면 적재 안 함(하위호환 — 기존 경로는 record_id 없이 그대로). `presence_of`는 그
        # 답의 담당 owner가 오프라인인지 조회하는 콜백(디스패처 프레즌스 추적기 위임 —
        # 프레즌스는 owner PC 연결 단위라 owner 키로 조회해야 한다·크로스머신 시연 실결함
        # 4호 정정) — 오프라인 자동발신(`full`)이면 `needs_correction_review=True`로 실어
        # 담당자 검토 필터에 노출한다(`resolve_mode_with_presence(..., return_flag=True)`
        # 정신을 적재 지점에서 재현).
        self._answer_record_store = answer_record_store
        self._presence_of = presence_of
        # co-grounding 하위호환 게이트(ADR 0037 결정 5·슬라이스 C·ADR 0038 결정 4·슬라이스 C):
        # 둘 다 주입됐을 때만 Contested arm이 "답+합의 병행"으로, Routed arm이 "co-grounded
        # 답(ConflictCase 없음)"으로 진화한다. 하나라도 미주입이면 기존 Pending/단일 접지
        # 동작 100% 보존(하위호환 — context/knowledge_store/propagator 옵셔널 주입 선례와
        # 동형). selector가 GroundingSet 대신 None을 반환해도 같은 폴백(어느 arm에 co-grounding을
        # 적용할지는 주입된 selector 정책 소관 — 프로덕션 기본 `ContestedGroundingSelector`는
        # Routed엔 항상 None을 돌려 Routed 행동이 그대로다).
        self._grounding_selector = grounding_selector
        self._grounding_resolver = grounding_resolver
        # 답 회수용 불투명 추적 토큰 → WorkTicket 매핑(ADR 0011 결정 6-5). 서버가
        # ticket을 보관하고 사용자엔 *불투명 토큰만* 준다 — 사용자向 응답에 ticket_id·
        # owner 등 내부 구조가 새지 않게 한다(노출 불변식). 토큰은 uuid4 hex(미인코딩).
        # MVP는 정리 없음(프로세스 수명 = 토큰 수명) — TTL/GC·다중 인스턴스 공유는 후속.
        self._tracking: dict[str, WorkTicket] = {}
        # 비동기 절차 상관키 보관소 — tracking → (user_id, question, decision)
        # retrieve가 Delivered 첫 관측 시 answered 엔트리를 기록하는 데 쓴다.
        self._pending_audit: dict[str, tuple[str, str, "Routed"]] = {}
        # 이미 answered 엔트리를 기록한 tracking 집합 — 멱등 가드.
        self._answered_recorded: set[str] = set()
        # tracking → 그 답에 적재된 record_id(Phase 12 (B)). 분산 회신(retrieve)은 여러 번
        # poll될 수 있어, 첫 Delivered에 record_id를 이 맵에 고정하고 이후 재조회는 같은
        # record_id를 답에 실어 준다(질문자가 새로고침해도 정정 배지 조회 손잡이가 안정).
        self._answer_record_ids: dict[str, str] = {}

    def _project_outcome(
        self,
        outcome: DispatchOutcome,
        primary_owner: str,
        primary_agent_id: str,
        tracking: str | None,
    ) -> OrgReply:
        """DispatchOutcome을 사용자向 OrgReply로 *투영*한다(표현 경계).

        Delivered→Answered(mode 보존, owner·agent_id 부착). AwaitingWorker·
        EscalatedToManager→Pending(kind="dispatched", 불투명 `tracking` 동반) — 사용자엔
        둘을 구분 않고 중립 안내만. manager_id/reason 등 기계값은 Pending에 싣지 않는다
        (노출 불변식). `tracking`은 답 회수용 불투명 토큰 1개로, 호출자가 만들어 넘긴다
        (handle은 새 토큰·매핑 저장, retrieve는 기존 토큰 유지). audit 기록은 별개다 —
        handle이 outcome *원형*을 AuditEntry로 넘긴다. match+assert_never로 망라.
        """
        match outcome:
            case Delivered():
                return Answered(
                    text=outcome.answer.text,
                    answered_by=(primary_owner, primary_agent_id),
                    mode=outcome.answer.mode,
                    sources=outcome.answer.sources,
                )
            case AwaitingWorker() | EscalatedToManager():
                return Pending(
                    kind="dispatched",
                    message=_DISPATCHED_MESSAGE,
                    tracking=tracking,
                )
            case _ as never:
                assert_never(never)

    def _record_answer(
        self, reply: OrgReply, question: str, session_id: str | None
    ) -> OrgReply:
        """확정된 `Answered`를 `AnswerRecord`로 적재하고 record_id를 그 답에 실어 돌려준다.

        중앙 답변 발신 지점(전이 ≠ 기록의 "기록" 축·ADR 0033 결정 4): 이 함수가 답 확정
        마다 정확히 append-only 레코드를 하나 남긴다 — 담당자 모니터링(`monitoring_for_owner`)
        과 질문자 정정 배지(`view_answer_with_correction`)의 원천. `Answered`가 아니거나 store
        미주입이면 그대로 통과(하위호환·발화 0).

        `needs_correction_review`(오프라인 자동발신 사후교정 플래그): `presence_of`가 그 답의
        담당 owner를 offline으로 보고하고 최종 mode가 `full`(자동 발신)이면 True — 담당자가
        복귀 후 "검토 필요" 필터에서 본다. `resolve_mode_with_presence(..., return_flag=True)`가
        S4에서 정한 판정을 *적재 시점*에 재현한다(온라인이거나 draft_only/backup이면 False).
        프레즌스는 owner(담당자 PC 연결) 키로 기록되므로(`transport.py`의 `observe_connect`)
        조회도 owner 키를 써야 한다 — agent_id로 조회하면 트래커에 없는 키라 항상 offline
        오탐이 난다(크로스머신 시연 실결함 4호).

        record_id는 uuid4 hex(내부 구조 미인코딩·불투명 손잡이) — 질문자가 이 토큰으로 자기
        답변 페이지에서 정정을 조회한다.
        """
        if self._answer_record_store is None or not isinstance(reply, Answered):
            return reply
        from agent_org_network.answer_record import AnswerRecord

        owner, agent_id = reply.answered_by
        needs_review = False
        if self._presence_of is not None and reply.mode == "full":
            needs_review = self._presence_of(owner) == "offline"
        record_id = uuid.uuid4().hex
        self._answer_record_store.add(
            AnswerRecord(
                record_id=record_id,
                question=question,
                answer_text=reply.text,
                answered_by=owner,
                agent_id=agent_id,
                mode=reply.mode,
                session_id=session_id,
                answered_at=self._clock(),
                needs_correction_review=needs_review,
            )
        )
        import dataclasses

        return dataclasses.replace(reply, record_id=record_id)

    def _select_grounding_set(self, decision: "RoutingDecision") -> "GroundingSet | None":
        """co-grounding 하위호환 게이트(ADR 0037 결정 5·ADR 0038 결정 4) — selector+resolver
        둘 다 주입됐고 selector가 `GroundingSet`을 반환할 때만 값을 돌려준다.

        파라미터가 `Contested` → `RoutingDecision`으로 넓혀졌다(ADR 0038 슬라이스 C) —
        Contested arm뿐 아니라 Routed(Delivered) arm에서도 호출된다. 엣지-소싱 Routed 전용
        selector는 `Routed`만, `ContestedGroundingSelector`는 `Contested`만 매칭해 처분이
        배타이므로 어느 arm에서 호출해도 안전하다(selector 자신이 타입 판별 — 합성 selector가
        둘을 순서대로 시도해 첫 non-None을 돌린다·`grounding.py` 참고). 하나라도 미주입이거나
        selector가 `None`을 반환하면 `None` — 호출자(handle·handle_stream)는 이걸 "기존
        Pending/단일 접지 폴백" 신호로 쓴다(회귀 0의 결정 지점).
        """
        if self._grounding_selector is None or self._grounding_resolver is None:
            return None
        return self._grounding_selector.select(decision)

    def _open_conflict_case(self, decision: Contested, question: str) -> ConflictCase | None:
        """Contested 부수효과(ConflictCase open·push 통지)를 수행한다(멱등·기존 handle과 동형).

        `handle`(co-grounded 답 경로·기존 Pending 경로)·`_handle_contested_stream`이
        공유한다 — 답+합의 병행이든 아니든 이 부수효과는 *항상 그대로*(ADR 0037 결정 5:
        "동시에 ConflictCase도 그대로 연다"). 이미 열려 있으면(`open_for_intent` 중복
        가드) None을 반환하고 아무것도 하지 않는다.
        """
        if self._case_store is None or self._case_store.open_for_intent(decision.intent) is not None:
            return None
        case = ConflictCase(
            intent=decision.intent,
            question=question,
            candidates=tuple(
                Candidate(agent_id=c.agent_id, owner=c.owner) for c in decision.candidates
            ),
            opened_at=self._clock(),
        )
        self._case_store.open_case(case)
        self._push_conflict_notification(case)
        return case

    def _merged_grounding_sources(self, grounding_set: "GroundingSet") -> tuple[str, ...]:
        """GroundingSet primary+supporting 전 카드의 `knowledge_sources`(출처 레이블)를

        합집합·순서 결정론(primary 먼저·그다음 supporting 순서)·중복 제거해 병합한다
        (ADR 0037 결정 2·6 — "supporting의 knowledge_sources를 primary의 것과 병합해
        sources에 투영. agent_id·confidence·candidate는 절대 안 실림"). 코드베이스의
        기존 "순서 보존 중복 제거" 관용구(`dict.fromkeys`, ask_org.py `_push_conflict_
        notification`·conflict.py 참고)를 재사용한다. 레이블(str)만 다루므로 새 노출
        표면 0 — 과소노출(primary만)을 정합으로 교정할 뿐이다.
        """
        all_labels = (
            label
            for card in (grounding_set.primary, *grounding_set.supporting)
            for label in card.knowledge_sources
        )
        return tuple(dict.fromkeys(all_labels))

    def _dispatch_co_grounded_answer(
        self, question: str, grounding_set: "GroundingSet", context: str | None
    ) -> tuple[Answered, DispatchOutcome]:
        """co-grounded 답을 조립·dispatch해 `Answered`(answered_by=primary)로 투영한다.

        `assemble_grounding_text`(grounding.py, ADR 0037 결정 3 재사용)로 GroundingSet의
        각 agent_id를 `_grounding_resolver`(주입 seam)로 해소·병합한 뒤, primary 카드로
        dispatch한다. 실 KnowledgeStore 다중 조회·크로스머신 조립은 이 함수 밖(슬라이스 D) —
        여기선 결정론 resolver(StubKnowledgeStore/fake)만 소비한다.

        Contested·Routed 양 arm 공용(ADR 0037 Contested 답+합의 병행 + ADR 0038 Routed 엣지
        co-grounding). Approval 게이트·`_record_answer`는 **호출자 책임** — Contested arm은
        `requires_approval` 축이 없어 게이트를 안 걸고, Routed arm은 호출자가 `_apply_approval_gate`를
        건다(이 헬퍼는 게이트를 적용하지 않는다). `DispatchOutcome` 원형도 함께 돌려줘 audit이
        절차 사실을 정직하게 남긴다(Contested면 decision은 여전히 Contested — ADR 0037 결정 5-a).

        `sources`는 primary 카드에서만 파생되는 런타임 기본 투영을 **덮어써**
        `_merged_grounding_sources`(primary+supporting 합집합)로 교체한다(code-reviewer
        Minor 1 수정 — ADR 0037 결정 2·6: "supporting의 knowledge_sources를 primary의
        것과 병합". 접지 본문은 이미 A+B에서 병합되는데 sources 레이블만 primary 단일로
        새던 간극을 닫는다).
        """
        import dataclasses

        from agent_org_network.grounding import assemble_grounding_text

        assert self._grounding_resolver is not None
        grounding_text = assemble_grounding_text(grounding_set, self._grounding_resolver)
        primary = grounding_set.primary
        merged_sources = self._merged_grounding_sources(grounding_set)
        ticket = self._dispatcher.dispatch(
            question, primary, context=context, grounding=grounding_text
        )
        outcome = self._dispatcher.poll(ticket)
        reply = self._project_outcome(outcome, primary.owner, primary.agent_id, None)
        if isinstance(reply, Answered):
            return dataclasses.replace(reply, sources=merged_sources), outcome
        # 로컬 즉답 디스패처(LocalRuntimeDispatcher/LocalStreamingDispatcher)는 항상
        # Delivered이므로 여기 도달은 분산 디스패처 주입 시에만 이론상 가능하다(슬라이스 D
        # 영역 — 실 분산 co-grounding 배선은 이번 범위 밖). 방어적으로 중립 Answered로
        # 감싸 계약을 지킨다(assert_never 대신 — Contested arm은 Pending을 이미 밀어냈으므로
        # 여기서 또 Pending을 내면 사용자向 계약이 흔들린다).
        return (
            Answered(
                text="",
                answered_by=(primary.owner, primary.agent_id),
                mode="full",
                sources=merged_sources,
            ),
            outcome,
        )

    def _apply_approval_gate(self, reply: OrgReply, decision: Routed) -> OrgReply:
        """Approval 게이트를 사용자向 답에 강제 반영한다(T2.5, ADR 0012 mode 강제 패턴).

        `decision.requires_approval`이면 *라우팅 결정*이 답을 `mode="draft_only"`로 내린다 —
        워커 자기보고가 아니라 라우팅이 강제(디스패처가 backup을 강제 하향하는 정신).
        `dataclasses.replace`로 mode만 갈아끼운 새 Answered를 돌려준다(frozen 보존).

        HITL 런타임 토글 반영(T9.3(b)·ADR 0025): `hitl_toggles` 주입 시 카드 approval
        정책(`decision.requires_approval`)과 그 에이전트의 HITL on/off 토글을
        `resolve_mode`(OR 결합·backup 보존·under-claim 단조성)로 함께 반영한다 —
        운영자 토글이 신뢰 게이트를 조이는 것이지 Authority(누가 담당인지) 선언이 아니다.
        미주입이면 기존 동작(카드 approval_when만 봄) 100% 보존(하위호환).

        적용 범위:
          - Answered(=Delivered 투영)에만 적용한다. Pending(dispatched, 아직 답 없음)엔 무관 —
            답 자체가 없으니 mode도 없다(답이 회신될 때 retrieve가 같은 게이트를 다시 적용).
          - mode 우선순위(ADR 0012): `backup`은 draft_only로 *덮지 않는다*(backup이 더 강한
            하향 — owner 미검토 답이라 승인 대기보다 약한 신뢰가 맞다). `full`→`draft_only`만
            격상. `draft_only`는 그대로(이미 게이트).
          - `hitl_toggles` 미주입이고 `requires_approval`이 False면 reply 그대로.

        collaborators는 여기서 다루지 않는다 — Answered에 싣지 않으므로(노출 불변식: 담당·
        승인·출처만, collaborator는 조직 내부 협업 구조). audit이 decision 원형으로 보관한다.

        주의(T2.5 경계): 이건 *게이트 표시*(draft_only로 보이기)까지다. 실 승인 행위(사람이
        draft를 승인해 full로 풀기)는 T5.2 Manager 큐 영역 — 여기선 승인 *상태만* 노출한다.
        """
        import dataclasses

        if not isinstance(reply, Answered):
            return reply

        if self._hitl_toggles is None:
            if not decision.requires_approval:
                return reply
            if reply.mode == "backup":
                return reply
            return dataclasses.replace(reply, mode="draft_only")

        from agent_org_network.hitl import resolve_mode

        hitl_on = self._hitl_toggles.is_on(decision.primary.agent_id)
        new_mode = resolve_mode(
            requires_approval=decision.requires_approval,
            hitl_on=hitl_on,
            current_mode=reply.mode,
        )
        if new_mode == reply.mode:
            return reply
        return dataclasses.replace(reply, mode=new_mode)

    def retrieve(self, tracking: str) -> OrgReply | None:
        """불투명 추적 토큰으로 답을 *조회*한다(ADR 0011 결정 6-5, pull 한정).

        워커가 나중에 회신하면 사용자가 이 토큰으로 답을 가져온다 — 디스패처 `poll`을
        재노출한 것(새 도메인 아님). 보관한 ticket을 복원해 poll하고, Delivered면
        Answered로, 아직이면 Pending(dispatched, 같은 토큰 유지)로 투영한다. 모르는
        토큰이면 None(존재하지 않는 추적은 조회 실패). 푸시(SSE/WS)는 범위 밖.

        검토 store 덧씌움(ADR 0012 결정 7-3): poll 결과가 Delivered(backup 답)인 경우,
        그 ticket의 BackupReviewItem을 *먼저 조회*해 reviewed면 검토 결과를 우선 투영한다.
          - CorrectBackup: 정정 text + mode=full(owner 실답으로 신뢰 복원).
          - ApproveBackup: 큐의 원 text + mode=full(승인으로 신뢰 복원).
          - DismissBackup: 큐의 원 text + mode=backup 유지(정정 없음·mode 그대로).
          - pending_review(미검토): 큐의 poll 결과 그대로(mode=backup 유지).
        큐 도메인 무변경(덮어쓰지 않고 읽기만 덧씌움) — 큐 멱등·단조성 보존.

        EscalatedToManager 종착도 사용자엔 `dispatched` 유지가 *의도*다 — 워커 미연결과
        Manager escalation의 구분은 감출 내부값(노출 불변식, ADR 0011 결정 4). escalation의
        사용자 통지(무한 조회 대신)는 별도 후속(Manager 큐 T5.2 연계).

        Approval 게이트(T2.5)는 여기 retrieve(dispatched 회신) 경로에선 *아직* 강제하지
        않는다 — `_tracking`은 `tracking→WorkTicket`만 보관하고 `requires_approval`(라우팅
        결정)을 들지 않아서다. 즉답(Delivered) 경로의 Approval은 `handle`이 강제하고(데모는
        `LocalRuntimeDispatcher`라 PRD §7 시나리오 2가 즉답으로 시연됨), 분산 회신 경로의
        Approval 강제는 후속(tracking→requires_approval 보관 자리 추가가 선결).
        """
        ticket = self._tracking.get(tracking)
        if ticket is None:
            return None
        outcome = self._dispatcher.poll(ticket)
        # answered 엔트리 기록 — Delivered를 처음 관측할 때 1회만(멱등).
        # 검토 store 덧씌움과 독립: 감사 사실(Delivered 도착)은 검토 오버레이와 무관하다.
        if (
            isinstance(outcome, Delivered)
            and tracking not in self._answered_recorded
            and tracking in self._pending_audit
        ):
            user_id, question, decision = self._pending_audit[tracking]
            self._audit.record(
                AuditEntry(
                    timestamp=self._clock(),
                    user_id=user_id,
                    question=question,
                    intent=decision.intent,
                    decision=decision,
                    dispatch_outcome=outcome,
                    tracking=tracking,
                )
            )
            # 관전 피드(T9.2(c)): 분산 회신 경로의 답 확정 emit — 이 Delivered 첫 관측
            # 멱등 지점(answered 기록과 같은 자리)에서 정확히 1회. answered_by는 ticket의
            # agent_id(회신 담당). mode는 큐 종착 Delivered.answer.mode(게이트 미적용 원형 —
            # retrieve는 Approval 게이트를 아직 안 거는 자리라 큐 mode를 그대로 관전에 실음).
            self._emit_console(
                _AnswerSent(
                    ticket_id=ticket.ticket_id,
                    answered_by=ticket.agent_id,
                    mode=outcome.answer.mode,
                    at=self._clock(),
                )
            )
            self._answered_recorded.add(tracking)
        # 검토 store 덧씌움: Delivered backup 답에 검토 결과를 우선 투영(큐 무변경).
        reply: OrgReply
        if isinstance(outcome, Delivered) and self._review_store is not None:
            reviewed = self._review_store.get_by_ticket(ticket.ticket_id)
            if reviewed is not None and reviewed.status == "reviewed":
                reply = self._project_review_outcome(reviewed, ticket, tracking)
                return self._record_answer_for_tracking(reply, tracking)
        reply = self._project_outcome(outcome, ticket.owner_id, ticket.agent_id, tracking)
        return self._record_answer_for_tracking(reply, tracking)

    def _record_answer_for_tracking(self, reply: OrgReply, tracking: str) -> OrgReply:
        """분산 회신(retrieve)에서 확정된 Answered를 tracking별로 멱등 적재한다.

        `handle`의 즉답 경로(`_record_answer`)와 달리 여기선 record_id를 tracking에 고정한다
        — retrieve가 여러 번 호출돼도 첫 적재의 record_id를 재사용해(멱등) 질문자 정정 배지
        조회 손잡이가 안정된다. `_pending_audit`가 그 답의 질문·질문자 세션을 보관한다.
        프레즌스 조회는 owner 키(`_record_answer`와 동일 — 트래커가 owner 단위로 기록).
        """
        if self._answer_record_store is None or not isinstance(reply, Answered):
            return reply
        existing = self._answer_record_ids.get(tracking)
        if existing is not None:
            import dataclasses

            return dataclasses.replace(reply, record_id=existing)
        from agent_org_network.answer_record import AnswerRecord

        audit_ctx = self._pending_audit.get(tracking)
        session_id = audit_ctx[0] if audit_ctx is not None else None
        question = audit_ctx[1] if audit_ctx is not None else ""
        owner, agent_id = reply.answered_by
        needs_review = False
        if self._presence_of is not None and reply.mode == "full":
            needs_review = self._presence_of(owner) == "offline"
        record_id = uuid.uuid4().hex
        self._answer_record_store.add(
            AnswerRecord(
                record_id=record_id,
                question=question,
                answer_text=reply.text,
                answered_by=owner,
                agent_id=agent_id,
                mode=reply.mode,
                session_id=session_id,
                answered_at=self._clock(),
                needs_correction_review=needs_review,
            )
        )
        self._answer_record_ids[tracking] = record_id
        import dataclasses

        return dataclasses.replace(reply, record_id=record_id)

    def _project_review_outcome(
        self, reviewed_item: "BackupReviewItem", ticket: WorkTicket, tracking: str
    ) -> OrgReply:
        """BackupReviewItem(reviewed)을 사용자向 Answered로 투영한다(검토 덧씌움).

        CorrectBackup → 정정 text + mode=full(owner가 직접 발행한 실답 — 신뢰 복원).
        ApproveBackup → 큐의 원 backup text + mode=full(owner 검토 완료 — 신뢰 복원).
        DismissBackup → 큐의 원 backup text + mode=backup(정정 없음 — mode 유지).
        answered_by는 어느 경우든 owner(책임 불변, 정정도 owner가 발행).
        노출 불변식: 검토 내부 구조(item_id·review_service·store 존재)는 밖으로 새지 않음.
        match+assert_never로 BackupReview sealed sum을 망라 — _serialize_backup_review(web.py)와 대칭.
        reviewed인데 review 필드가 None인 경우는 구조적으로 불가하므로 명시 방어.
        """
        from typing import assert_never as _assert_never

        from agent_org_network.review import ApproveBackup, CorrectBackup, DismissBackup

        review = reviewed_item.review
        if review is None:
            # reviewed 상태인데 review 필드가 None — 구조적 불변식 위반(발생 불가).
            # 방어적으로 큐 poll 결과로 폴백한다.
            outcome = self._dispatcher.poll(ticket)
            return self._project_outcome(outcome, ticket.owner_id, ticket.agent_id, tracking)
        match review:
            case CorrectBackup():
                return Answered(
                    text=review.corrected_text,
                    answered_by=(ticket.owner_id, ticket.agent_id),
                    mode="full",
                    sources=review.sources,
                )
            case ApproveBackup():
                return Answered(
                    text=reviewed_item.backup_answer_text,
                    answered_by=(ticket.owner_id, ticket.agent_id),
                    mode="full",
                )
            case DismissBackup():
                return Answered(
                    text=reviewed_item.backup_answer_text,
                    answered_by=(ticket.owner_id, ticket.agent_id),
                    mode="backup",
                )
            case _ as never:
                _assert_never(never)

    def record_review(self, item_id: str, review: "BackupReview") -> None:
        """검토 전이를 적용한다(전이만 — 검토 기록은 BackupReviewStore.history가 담당).

        BackupReviewService를 통해 상태 전이만 수행한다. audit에는 *아무것도 남기지 않는다* —
        검토 기록은 BackupReviewStore.history(append-only 전이 보관소)가 책임지고, audit은
        질문→라우팅→디스패치→답의 절차 전용이다(전이≠기록, ADR 0012 결정 7).
        review_store 미주입이면 no-op(하위호환). 1인칭 검증은 BackupReviewService가 담당.
        """
        from agent_org_network.review import BackupReviewService

        if self._review_store is None:
            return
        svc = BackupReviewService(self._review_store)
        svc.review(item_id, review)

    def _push_manager_notification(self, manager_id: str, subject_ref: str, now: "datetime") -> None:
        """Manager 큐 적재 직후 manager에게 push 통지를 1회 쏜다(T7.4·ADR 0022 결정 4).

        `notifier` 미주입이면 *아무것도 안 한다*(하위호환·게이트 보존). 비None이면
        `Notification(kind="manager_escalated")` 1회 push — `_push_conflict_notification`과
        동형. `manager_id`가 빈 문자열이면 push 안 함(미귀속 가드 — 처리함 pull이 떠받침).
        """
        if self._notifier is None:
            return
        if not manager_id:
            return
        from agent_org_network.notify import Notification

        self._notifier.notify(
            Notification(
                recipient_id=manager_id,
                kind="manager_escalated",
                subject_ref=subject_ref,
                created_at=now,
            )
        )

    def _enqueue_unowned(self, decision: Unowned, question: str) -> None:
        """Unowned → Manager 큐 적재(T5.2). 미주입이면 no-op(하위호환)."""
        if self._manager_queue_store is None:
            return
        from agent_org_network.manager_queue import (
            FromUnowned,
            ManagerItem,
            manager_id_for_unowned,
        )

        mid = manager_id_for_unowned(decision)
        source = FromUnowned(decision=decision, question=question)
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=self._clock(),
        )
        self._manager_queue_store.enqueue(item)
        self._push_manager_notification(mid, item.item_id, item.created_at)

    def _enqueue_dispatch(self, outcome: EscalatedToManager) -> None:
        """EscalatedToManager → Manager 큐 적재(T5.2). 미주입이면 no-op."""
        if self._manager_queue_store is None:
            return
        from agent_org_network.manager_queue import (
            FromDispatch,
            ManagerItem,
            manager_id_for_dispatch,
        )

        mid = manager_id_for_dispatch(outcome, root=self._manager_root)
        source = FromDispatch(outcome=outcome)
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=self._clock(),
        )
        self._manager_queue_store.enqueue(item)
        self._push_manager_notification(mid, item.item_id, item.created_at)

    def enqueue_deadlock(self, case: ConflictCase, reason: str = "") -> None:
        """Deadlocked → Manager 큐 적재(T5.2). web/concur 엔드포인트가 호출한다.

        미주입이면 no-op(하위호환). manager_of 콜백으로 첫 후보 owner의 manager를 찾는다.
        같은 case_id 로 이미 큐에 있으면 재적재하지 않는다(중복 방지 — [Major 1]).
        """
        if self._manager_queue_store is None:
            return
        from agent_org_network.manager_queue import (
            FromDeadlock,
            ManagerItem,
            manager_id_for_deadlock,
        )

        # [Major 1] 중복 방지: 같은 ConflictCase 가 이미 큐에 있으면 no-op.
        # open_for_intent 로 Contested 중복을 막는 것과 대칭.
        if self._manager_queue_store.get_by_case(case.case_id) is not None:
            return

        def _no_manager(uid: str) -> str | None:
            return None

        manager_of_fn = self._manager_of if self._manager_of is not None else _no_manager
        mid = manager_id_for_deadlock(case, manager_of=manager_of_fn, root=self._manager_root)
        source = FromDeadlock(case=case, reason=reason)
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=self._clock(),
        )
        self._manager_queue_store.enqueue(item)
        self._push_manager_notification(mid, item.item_id, item.created_at)

    def _emit_console(self, event: "ConsoleEvent") -> None:
        """관전 피드에 사건을 1건 emit한다 — 실패는 흡수(관전이 본 흐름을 못 깬다).

        `console_feed` 미주입이면 no-op(하위호환·발화 0). 주입 시 emit하되 어떤 예외도
        삼킨다(try/except) — 관전 미러는 유실 허용이고, emit 실패가 질문 처리(본 흐름)를
        깨서는 안 된다(전이≠기록 정신·notifier push 흡수와 동형).
        """
        if self._console_feed is None:
            return
        try:
            self._console_feed.emit(event)
        except Exception:
            pass

    def handle(self, question: str, user: User, *, context: str | None = None) -> OrgReply:
        # 관전 피드(T9.2(c)): 질문 인입을 즉시 emit한다(라우팅 전 — "들어온" 사건).
        self._emit_console(
            _QuestionReceived(
                question=question,
                session_id=user.id,
                at=self._clock(),
            )
        )
        decision = self._router.route(question)
        # 관전 피드: 라우팅 결정을 emit한다(Routed/Contested/Unowned 원형 재사용).
        self._emit_console(_RoutingDecisionRecorded(decision=decision, at=self._clock()))

        # 디스패치 절차의 결말 — Routed일 때만 채워지고(dispatch→poll), Contested/
        # Unowned는 디스패치를 안 하므로 None. audit이 이 원형에서 answer를 파생하고
        # escalation 대상(manager_id·reason)까지 기록한다(2b 선결, ADR 0011).
        outcome: DispatchOutcome | None = None
        # 비동기 절차의 상관키 — 비동기 Routed 분기에서만 세팅, 그 외 None.
        tracking_token: str | None = None
        match decision:
            case Routed():
                # Routed co-grounding(ADR 0038 결정 4·슬라이스 C): selector+resolver가 둘
                # 다 주입되고 selector가(예: 엣지-소싱 Routed 전용 selector) 합의-소싱
                # `ComplementEdge` 이웃을 골랐을 때만 co-grounded 답을 낸다. Contested가
                # 아니라 이미 라우팅된 질문이므로 **ConflictCase는 열지 않는다**(다툼 아님). Approval 게이트·
                # `_record_answer`·audit은 일반 Routed와 *동일 적용*(co-grounding이 게이트를
                # 우회하지 않는다). 하나라도 미주입/None이면 기존 단일 접지 Routed로 폴백
                # (하위호환 게이트 — 프로덕션 기본 `ContestedGroundingSelector`는 Routed에
                # 항상 None을 돌려 회귀 0).
                grounding_set = self._select_grounding_set(decision)
                if grounding_set is not None:
                    reply, outcome = self._dispatch_co_grounded_answer(
                        question, grounding_set, context
                    )
                    reply = self._apply_approval_gate(reply, decision)
                    reply = self._record_answer(reply, question, user.id)
                    if isinstance(outcome, EscalatedToManager):
                        self._enqueue_dispatch(outcome)
                else:
                    ticket = self._dispatcher.dispatch(
                        question, decision.primary, context=context
                    )
                    outcome = self._dispatcher.poll(ticket)
                    # 미회신(AwaitingWorker/EscalatedToManager)이면 답 회수용 불투명 토큰을
                    # 발급해 ticket을 서버에 보관한다 — 사용자가 나중에 retrieve로 답을 가져올
                    # 길(6-5). Delivered(즉답)면 토큰 불요(None). 토큰은 ticket_id와 *분리된*
                    # 별도 uuid4 hex라 사용자向에 ticket_id조차 노출되지 않는다(노출 불변식).
                    tracking: str | None = None
                    if not isinstance(outcome, Delivered):
                        tracking = uuid.uuid4().hex
                        self._tracking[tracking] = ticket
                        # 모니터 answered 기록을 위해 상관키·맥락을 보관한다.
                        # retrieve가 Delivered를 처음 관측할 때 answered 엔트리를 기록한다.
                        self._pending_audit[tracking] = (user.id, question, decision)
                        tracking_token = tracking
                    reply = self._project_outcome(
                        outcome,
                        decision.primary.owner,
                        decision.primary.agent_id,
                        tracking,
                    )
                    # Approval 게이트 강제(T2.5, ADR 0012 mode 강제 패턴): Routed에 Approval이
                    # 붙었으면 *라우팅 결정*이 답을 draft_only로 내린다 — 워커 자기보고가 아니라
                    # 라우팅이 강제한다(디스패처가 backup을 강제 하향하듯). Delivered(실 답이
                    # 도착)에만 적용하고, 아직 답이 없는 Pending(dispatched)엔 무관(답 자체가
                    # 없으니 mode도 없음). mode 우선순위(ADR 0012): backup이 더 강한 하향이라
                    # draft_only로 *덮지 않는다* — backup 답은 owner 미검토라 승인 대기보다 약한
                    # 신뢰가 맞다. full→draft_only만 격상(게이트 표시), backup은 보존.
                    reply = self._apply_approval_gate(reply, decision)
                    # 답변 감사 단위 적재(Phase 12 (B)·ADR 0033 결정 4) — 즉답(Delivered) 경로에서
                    # Answered가 확정된 직후. 오프라인 자동발신이면 needs_correction_review=True.
                    # 분산 회신(Pending dispatched)은 아직 답이 없어 여기선 적재 안 하고 retrieve가
                    # Delivered 첫 관측 시 적재한다(retrieve 참조).
                    reply = self._record_answer(reply, question, user.id)
                    # EscalatedToManager면 Manager 큐에도 적재(T5.2 ADR 0014).
                    if isinstance(outcome, EscalatedToManager):
                        self._enqueue_dispatch(outcome)
            case Contested():
                # co-grounding 답+합의 병행(ADR 0037 결정 5·슬라이스 C): selector+resolver가
                # 둘 다 주입되고 selector가 GroundingSet을 골랐을 때만 co-grounded 답을
                # 낸다. 하나라도 미주입/None이면 기존 Pending(kind="contested") 폴백 —
                # 하위호환 게이트(회귀 0).
                grounding_set = self._select_grounding_set(decision)
                if grounding_set is not None:
                    reply, outcome = self._dispatch_co_grounded_answer(
                        question, grounding_set, context
                    )
                    reply = self._record_answer(reply, question, user.id)
                else:
                    reply = Pending(
                        kind="contested",
                        message="담당을 확인하고 있어요. 정해지면 답변드릴게요.",
                    )
                # ConflictCase는 co-grounded 답이든 기존 Pending이든 *항상 그대로 연다*
                # (ADR 0037 결정 5 — "동시에 ConflictCase도 그대로 연다", 미아 없음 안전망 보존).
                self._open_conflict_case(decision, question)
            case Unowned():
                reply = Pending(
                    kind="unowned",
                    message="아직 담당이 없어 매니저에게 전달했어요. 답변되면 알림드릴게요.",
                )
                self._enqueue_unowned(decision, question)

        # 관전 피드(T9.2(c)): 답 확정 시점 emit. 동기 즉답(Delivered) 경로는 여기서 답이
        # 확정되므로 AnswerSent를 emit한다(reply가 Answered = Delivered 투영·게이트 반영 후
        # 최종 mode). 분산 회신(Pending dispatched)은 아직 답이 없어 여기선 emit 안 하고,
        # retrieve의 Delivered 첫 관측(멱등 지점)에서 emit한다.
        if isinstance(reply, Answered):
            self._emit_console(
                _AnswerSent(
                    ticket_id=tracking_token or "",
                    answered_by=reply.answered_by[1],
                    mode=reply.mode,
                    at=self._clock(),
                )
            )

        self._audit.record(
            AuditEntry(
                timestamp=self._clock(),
                user_id=user.id,
                question=question,
                intent=decision.intent,
                decision=decision,
                dispatch_outcome=outcome,
                tracking=tracking_token,
            )
        )
        return reply

    def handle_stream(
        self, question: str, user: User, *, context: str | None = None
    ) -> Iterator[AskEvent]:
        """질문을 스트리밍으로 처리한다 — `Iterator[AskEvent]`(ADR 0031 결정 2·3).

        `handle`의 *형제 메서드*(Routed/Contested co-grounding 하위호환 게이트는 `handle`과
        대칭 진화 — 두 메서드가 같은 `_select_grounding_set` 결정 지점을 공유). decide↔answer
        분리·audit-once:
          1. decide: `route(question)` 정확히 1회(맨 질문만 — 맥락은 dispatch로만).
          2. 분기(sealed sum match):
             - Contested/Unowned: 기존 `handle`의 그 분기 부수효과를 *그대로 1회*
               (ConflictCase open·push 통지·Manager 큐 적재) 후 **단일 pending 이벤트**를
               yield하고 종료(Pending 비스트림). audit 1회 후 종료. co-grounding 활성(selector
               가 GroundingSet을 고름)이면 Contested도 meta→token*→done(ConflictCase는
               side-effect로 그대로 열림 — `_stream_co_grounded_contested`).
             - Routed: `dispatch_stream`으로 meta → token*(델타) → 완성 Answer 확정 →
               Approval 게이트(_apply_approval_gate)를 완성 답에 적용 → audit 1회 → done.
               co-grounding 활성(엣지-소싱 Routed 전용 selector 등이 GroundingSet을 고름)이면
               ConflictCase 없이 co-grounded meta→token*→done(`_stream_co_grounded_routed`).
          3. audit-once: 스트림을 다 흘린 뒤 done 직전 `self._audit.record`를 정확히 1회.

        `handle`/`handle_stream`은 상호 배타(한 질문은 둘 중 하나·web 엔드포인트가 가름)라
        이중 audit 구조적 불가. 비스트림 디스패처(`LocalStreamingDispatcher` 아님)면 Routed도
        블로킹 폴백으로 흘린다(아래 — answer 1델타).
        """
        decision = self._router.route(question)

        match decision:
            case Routed():
                # Routed co-grounding 스트림(ADR 0038 결정 4·슬라이스 C): handle과 같은
                # 하위호환 게이트 — selector+resolver 둘 다 주입되고 selector가 GroundingSet을
                # 골랐을 때만 meta→token*→done(co-grounded 답)을 흘린다. ConflictCase는 열지
                # 않는다(Contested가 아니다). 아니면 기존 `_stream_routed`(단일 접지) 폴백.
                grounding_set = self._select_grounding_set(decision)
                if grounding_set is not None:
                    yield from self._stream_co_grounded_routed(
                        question, user, decision, grounding_set, context
                    )
                else:
                    yield from self._stream_routed(question, user, decision, context)
            case Contested():
                # co-grounding 답+합의 병행(ADR 0037 결정 5-c·슬라이스 C): handle과 같은
                # 하위호환 게이트 — selector+resolver 둘 다 주입되고 selector가 GroundingSet을
                # 골랐을 때만 meta→token→done(co-grounded 답)을 흘린다. 아니면 기존 단일
                # PendingEvent 폴백(회귀 0).
                grounding_set = self._select_grounding_set(decision)
                if grounding_set is not None:
                    yield from self._stream_co_grounded_contested(
                        question, user, decision, grounding_set, context
                    )
                else:
                    yield self._handle_contested_stream(question, decision)
                    self._audit_simple(question, user, decision)
            case Unowned():
                yield self._handle_unowned_stream(question, decision)
                self._audit_simple(question, user, decision)

    def _stream_routed(
        self,
        question: str,
        user: User,
        decision: Routed,
        context: str | None,
    ) -> Iterator[AskEvent]:
        """Routed 스트림 분기 — meta → token* → (게이트·audit) → done."""
        card = decision.primary

        # meta: 담당·초기 mode(런타임 초기·보통 full)·sources 투영. done의 mode가 최종 권위.
        yield MetaEvent(
            answered_by=(card.owner, card.agent_id),
            mode="full",
            sources=tuple(card.knowledge_sources),
        )

        # 스트리밍 디스패처면 델타를 흘리고 완성 답을 확정, 아니면 블로킹 폴백.
        completed: Answered
        if isinstance(self._dispatcher, LocalStreamingDispatcher):
            stream = self._dispatcher.dispatch_stream(question, card, context=context)
            for chunk in stream:
                yield TokenEvent(text=chunk.text_delta)
            answer = stream.completed
            completed = Answered(
                text=answer.text,
                answered_by=(card.owner, card.agent_id),
                mode=answer.mode,
                sources=answer.sources,
            )
        else:
            ticket = self._dispatcher.dispatch(question, card, context=context)
            outcome = self._dispatcher.poll(ticket)
            reply = self._project_outcome(outcome, card.owner, card.agent_id, None)
            if isinstance(reply, Answered):
                completed = reply
                yield TokenEvent(text=reply.text)
            else:
                # 비스트림 디스패처가 Delivered가 아니면(미회신/escalation) pending으로 종착.
                # 로컬 스트리밍 경로는 항상 Delivered라 여긴 분산 폴백 자리(이번 범위 밖 보호).
                # reply는 Answered가 아니므로 Pending으로 좁혀진다(_project_outcome 반환).
                self._audit_routed(question, user, decision, outcome, None)
                yield PendingEvent(
                    kind=reply.kind, message=reply.message, tracking=reply.tracking
                )
                return

        # Approval 게이트를 *완성 답*에 적용(델타마다 아님) — done의 mode가 최종 권위.
        gated = self._apply_approval_gate(completed, decision)
        final = gated if isinstance(gated, Answered) else completed

        # audit-once: 스트림 다 흘린 뒤 done 직전 정확히 1회.
        delivered = Delivered(
            ticket=WorkTicket(
                owner_id=card.owner,
                agent_id=card.agent_id,
                question=question,
                enqueued_at=self._clock(),
            ),
            answer=_answer_of(final),
        )
        self._audit_routed(question, user, decision, delivered, None)

        yield DoneEvent(mode=final.mode, sources=final.sources)

    def _handle_contested_stream(self, question: str, decision: Contested) -> PendingEvent:
        """Contested 부수효과(ConflictCase open·push 통지)를 1회 수행하고 pending 이벤트를 만든다.

        기존 `handle`의 Contested arm과 동형 부수효과 — 같은 책임을 한 곳에서.
        """
        self._open_conflict_case(decision, question)
        return PendingEvent(
            kind="contested",
            message="담당을 확인하고 있어요. 정해지면 답변드릴게요.",
        )

    def _stream_co_grounded_contested(
        self,
        question: str,
        user: User,
        decision: Contested,
        grounding_set: "GroundingSet",
        context: str | None,
    ) -> Iterator[AskEvent]:
        """Contested co-grounding 답 스트림 — meta→token*→done(ADR 0037 결정 5-c).

        `_stream_routed`의 Contested 대칭형: 사용자向은 `done` 하나만(§117 배타 보존) —
        ConflictCase open은 side-effect일 뿐 스트림 프레임이 아니다. Approval 게이트는
        적용하지 않는다(Contested엔 `requires_approval` 축이 없다). audit은 decision
        (Contested 원형)을 그대로 남긴다(5-a — 답이 나가도 Routed로 위장하지 않는다).
        """
        primary = grounding_set.primary

        # sources는 primary 단일이 아니라 GroundingSet 전 카드의 병합(ADR 0037 결정 2·6·
        # code-reviewer Minor 1 수정) — `_dispatch_co_grounded_answer`가 돌려주는 완성
        # Answered와 *같은* 병합값을 meta에도 싣는다(비스트림 Answered.sources·done.sources와
        # 3자 정합 — meta는 초기 추정이지만 sources는 접지 결정 시점에 이미 확정돼 있다).
        merged_sources = self._merged_grounding_sources(grounding_set)
        yield MetaEvent(
            answered_by=(primary.owner, primary.agent_id),
            mode="full",
            sources=merged_sources,
        )

        reply, outcome = self._dispatch_co_grounded_answer(question, grounding_set, context)
        yield TokenEvent(text=reply.text)

        recorded = self._record_answer(reply, question, user.id)
        final = recorded if isinstance(recorded, Answered) else reply

        # ConflictCase는 side-effect로 그대로 연다(사용자向 프레임 아님 — 결정 5-c).
        self._open_conflict_case(decision, question)

        self._audit.record(
            AuditEntry(
                timestamp=self._clock(),
                user_id=user.id,
                question=question,
                intent=decision.intent,
                decision=decision,
                dispatch_outcome=outcome,
                tracking=None,
            )
        )

        yield DoneEvent(mode=final.mode, sources=final.sources)

    def _stream_co_grounded_routed(
        self,
        question: str,
        user: User,
        decision: Routed,
        grounding_set: "GroundingSet",
        context: str | None,
    ) -> Iterator[AskEvent]:
        """Routed co-grounded 답 스트림 — meta→token*→done(ADR 0038 결정 4·슬라이스 C).

        `_stream_co_grounded_contested`의 Routed 대칭형이나 **ConflictCase는 열지 않는다**
        (다툼이 아니라 이미 라우팅된 질문 — Contested가 아니다). Approval 게이트는 Routed
        전용 축이라 완성 답에 적용한다(`_stream_routed`와 동형 — done의 mode가 최종 권위).
        `_stream_routed`가 `_record_answer`를 호출하지 않는 것과 대칭으로(스트리밍 Routed
        경로는 아직 record_id 미적재 — 기존 gap 그대로 보존, 이 슬라이스가 새로 도입한
        비대칭 아님) 여기서도 호출하지 않는다. audit은 decision(Routed 원형) 그대로 남긴다.
        """
        primary = grounding_set.primary

        # sources는 primary 단일이 아니라 GroundingSet 전 카드의 병합(ADR 0037 결정 2·6
        # 재사용) — 완성 Answered·done과 3자 정합.
        merged_sources = self._merged_grounding_sources(grounding_set)
        yield MetaEvent(
            answered_by=(primary.owner, primary.agent_id),
            mode="full",
            sources=merged_sources,
        )

        reply, outcome = self._dispatch_co_grounded_answer(question, grounding_set, context)
        yield TokenEvent(text=reply.text)

        # Approval 게이트를 *완성 답*에 적용(델타마다 아님) — done의 mode가 최종 권위
        # (`_stream_routed`와 동형).
        gated = self._apply_approval_gate(reply, decision)
        final = gated if isinstance(gated, Answered) else reply

        # EscalatedToManager면 Manager 큐에도 적재(T5.2 ADR 0014·`_stream_routed` 비스트림
        # 폴백과 동형 — 이 슬라이스의 결정론 테스트는 항상 Delivered라 실도달은 후속).
        if isinstance(outcome, EscalatedToManager):
            self._enqueue_dispatch(outcome)

        self._audit_routed(question, user, decision, outcome, None)

        yield DoneEvent(mode=final.mode, sources=final.sources)

    def _handle_unowned_stream(self, question: str, decision: Unowned) -> PendingEvent:
        """Unowned 부수효과(Manager 큐 적재)를 1회 수행하고 pending 이벤트를 만든다."""
        self._enqueue_unowned(decision, question)
        return PendingEvent(
            kind="unowned",
            message="아직 담당이 없어 매니저에게 전달했어요. 답변되면 알림드릴게요.",
        )

    def _audit_simple(self, question: str, user: User, decision: Contested | Unowned) -> None:
        """Contested/Unowned는 디스패치 없음 — outcome None으로 audit 1회(기존 handle과 동형)."""
        self._audit.record(
            AuditEntry(
                timestamp=self._clock(),
                user_id=user.id,
                question=question,
                intent=decision.intent,
                decision=decision,
                dispatch_outcome=None,
                tracking=None,
            )
        )

    def _audit_routed(
        self,
        question: str,
        user: User,
        decision: Routed,
        outcome: DispatchOutcome,
        tracking: str | None,
    ) -> None:
        """Routed 스트림 종료 시 audit 1회(기존 handle 끝 record와 동형·지점만 이동)."""
        self._audit.record(
            AuditEntry(
                timestamp=self._clock(),
                user_id=user.id,
                question=question,
                intent=decision.intent,
                decision=decision,
                dispatch_outcome=outcome,
                tracking=tracking,
            )
        )

    def _push_conflict_notification(self, case: ConflictCase) -> None:
        """ConflictCase open 직후 후보 owner들에게 push 통지를 쏜다(T7.4·ADR 0022 결정 4).

        `notifier` 미주입이면 *아무것도 안 한다*(하위호환·게이트 보존 — 기존 호출은 notifier를
        주입하지 않아 이 본문이 실행되지 않는다). 비None이면 각 후보 owner에게
        `Notification(kind="conflict_opened", subject_ref=case.case_id)`를 `notifier.notify`로
        push한다 — 후보 owner가 처리함 pull로 알기 전에 push로도 알린다(push는 pull을 *추가*하지
        대체하지 않는다·미아 없음은 pull이 떠받친다·결정 6). 멱등은 Notifier가 담당
        (`(recipient, kind, subject_ref)` 중복 발송 차단 — 같은 case에 두 번째 질문이 와도 한 번).

        **현재는 발화 자리만(미구현 통과 stub)** — 실 Notification 구성·notify 호출 본문은
        tdd-engineer가 red→green으로 채운다(슬라이스 3). `commit_okf_bundle(..., propagator=None)`
        의 옵셔널 발화와 동형.
        """
        if self._notifier is None:
            return
        from agent_org_network.notify import Notification

        now = self._clock()
        candidate_owners = dict.fromkeys(c.owner for c in case.candidates)
        for owner_id in candidate_owners:
            self._notifier.notify(
                Notification(
                    recipient_id=owner_id,
                    kind="conflict_opened",
                    subject_ref=case.case_id,
                    created_at=now,
                )
            )
