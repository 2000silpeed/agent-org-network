"""중앙 MCP 서버 진입점 — legacy AskOrg와 P17 Question Surface 어댑터.

ADR 0006: 중앙=단일 MCP 서버, `ask_org`가 1급 진입점. 여러 클라이언트(Claude
Desktop·IDE 등)가 같은 백엔드를 본다. 결정적 결과: 일반 MCP 클라이언트에선 담당·
승인 같은 신뢰 표식이 *우리 UI가 아니라 텍스트로* 노출된다(내용 보존). 그래서 도구
결과는 사람이 읽는 한국어 텍스트에 담당·신뢰 상태(mode)·출처를 박는다(불변식 "답엔
항상 담당·신뢰 상태가 붙는다").

노출 규율은 web의 `serialize_reply`와 같다 — `OrgReply`(Answered | Pending)에서만
투영하고 confidence·candidates·escalated_to·manager_id·reason·ticket_id 등 조직 내부값은
절대 싣지 않는다. 다른 점은 출력 형식뿐이다(web은 JSON dict, MCP는 텍스트). 내부값이
새지 않는 안전성은 구조적이다 — Answered/Pending에 그 필드 자체가 없다.

비즈니스 로직 없음: `ask_org` 도구는 `ask.handle(question, User(...))`를 호출하고
`reply_to_mcp_text`로 텍스트만 투영한다. 미아 없음·Authority 중앙·전이≠기록은 `handle`이
이미 보장한다(MCP는 표현층).

`create_mcp_server`는 기존 AskOrg·feedback 호환 factory로 유지한다. 새
`create_question_mcp_server`는 Question Surface application 하나만 받아 Request-first
`ask_org`·`get_question`을 제공한다. 실 stdio `main`은 단일 프로세스 데모 composition
하나를 쓰지만 web 프로세스와 state 공유를 보장하지 않는다.
"""

from collections.abc import Callable, Mapping
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, assert_never, cast

from mcp.server.fastmcp import FastMCP

from agent_org_network.ask_org import Answered, AskOrg, OrgReply, Pending
from agent_org_network.central_authority import Action, AuthenticatedPrincipal, ResourceRef
from agent_org_network.approval import ApprovalPendingSummary, ApproverPrincipal
from agent_org_network.approval_operations import (
    ApprovalAnswered,
    ApprovalDecisionIntent,
    ApprovalDeclined,
    ApprovalOperationsDecision,
    ApprovalOperationsPrincipal,
    ApprovalPendingDetail,
    ApprovalReassigned,
    ApproveIntent,
    ApproveWithEditIntent,
    ManualApprovalReassignmentTarget,
    RejectIntent,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    QuestionAuthorizationDeniedError,
    QuestionPrincipal,
    RequesterPrincipal,
)
from agent_org_network.question_stream_execution import (
    AnsweredQuestionLookup,
    DeclinedQuestionLookup,
    FailedQuestionLookup,
    PendingQuestionLookup,
    QuestionStreamLookup,
    QuestionStreamRequestNotFoundError,
    QuestionSurfaceInterruptedError,
)
from agent_org_network.user import User
from agent_org_network.conflict import ConflictCase
from agent_org_network.manager_queue import (
    FromDeadlock,
    FromUnowned,
    ManagerItem,
    ManagerQueueStore,
)
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConflictDispositionError,
    ConflictOperationsPrincipal,
    P17ConcurrenceResult,
)
from agent_org_network.p17_manager_disposition import (
    AssignDeadlockedOwner,
    AssignUnownedOwner,
    DeadlockManagerDispositionCommand,
    DismissDeadlocked,
    DismissUnowned,
    ManagerDispositionError,
    ManagerOperationsPrincipal,
    P17DeadlockManagerDispositionResult,
    P17ManagerDispositionCommand,
    P17ManagerDispositionResult,
)
from agent_org_network.operational_authorization import (
    OPERATIONAL_ACTION_MANIFEST,
    OperationalAction,
    OperationalAuthorization,
    OperationalAuthorizationOutcome,
)
from agent_org_network.operational_application import (
    OperationalApplication,
    OperationalDeniedError,
    OperationalNotFoundError,
    OperationalUnavailableError,
)
from agent_org_network.admin_registry import CardCandidate
from agent_org_network.authoring_application import AuthoringApplication, AuthoringMutation
from agent_org_network.agent_card import AgentCard

if TYPE_CHECKING:
    from agent_org_network.answer_record import AnswerRecordStore, FeedbackStore

# 사용자 신원은 서버 *설정값*이지 도구 파라미터가 아니다(ADR 0009 연결점). walking
# skeleton이라 익명 guest로 고정한다 — 도구가 user를 받게 두면 누구든 남을 가장할 수
# 있으므로 막는다(T6.5에서 실 인증 주체로 대체할 자리). web의 `_WEB_USER`와 같은 정신.
_DEFAULT_MCP_USER_ID = "mcp_guest"

_QUESTION_PENDING_TEXT = "질문을 처리하고 있습니다."
_QUESTION_DECLINED_TEXT = "질문 처리가 거절되었습니다."
_QUESTION_FAILED_TEXT = "질문을 처리하지 못했습니다."
_QUESTION_INTERRUPTED_TEXT = "질문 처리가 일시 중단되었습니다."
_QUESTION_NOT_FOUND_TEXT = "질문 요청을 찾을 수 없습니다."
_QUESTION_UNAVAILABLE_TEXT = "질문 요청을 처리하지 못했습니다."
_QUESTION_FORBIDDEN_TEXT = "질문 권한이 없습니다."

# 별도 저작 MCP의 닫힌 server-side action matrix. raw document와 owner/principal은
# 도구 입력이 아니라 caller/provider에서만 결정한다. ``commit_author_bundle`` 같은
# 불투명 OkfFile bundle commit 도구는 구조화된 BuilderDraft admission이 생길 때까지
# 의도적으로 등록하지 않는다.
MCP_AUTHORING_TOOL_ACTIONS: Mapping[str, OperationalAction] = MappingProxyType(
    {
        "get_author_index": "author.read",
        "publish_authoring": "author.publish",
    }
)

_APPROVAL_LIST_UNAVAILABLE_TEXT = "승인 처리함을 불러오지 못했습니다."
_APPROVAL_DETAIL_UNAVAILABLE_TEXT = "승인 항목을 찾을 수 없거나 조회하지 못했습니다."
_APPROVAL_DECISION_UNAVAILABLE_TEXT = "승인 처리를 완료하지 못했습니다."
_APPROVAL_REASSIGNMENT_UNAVAILABLE_TEXT = "승인 재지정을 완료하지 못했습니다."
_CONFLICT_UNAVAILABLE_TEXT = "다툼 처리 요청을 완료하지 못했습니다."
_MANAGER_UNAVAILABLE_TEXT = "Manager 처리 요청을 완료하지 못했습니다."

# `create_question_mcp_server`가 현재 중앙 Authority action으로 여는 도구의 닫힌
# 매트릭스다. P0 운영 MCP는 아래 별도 factory로만 노출한다. P0 밖의 운영·관리·저작
# application은 아직 MCP provider로 열지 않는다. action 이름만 추가해 권한 확인을
# 우회할 수 없게 question과 operational 표면을 분리한다.
QUESTION_GOVERNANCE_MCP_TOOL_ACTIONS: Mapping[str, Action] = MappingProxyType(
    {
        "ask_org": "question.create",
        "get_question": "question.read",
        "list_approvals": "approval.list",
        "get_approval": "approval.read",
        "approve": "approval.decide",
        "approve_with_edit": "approval.decide",
        "reject": "approval.decide",
        "reassign_approval": "approval.reassign",
        "list_conflicts": "conflict.list",
        "get_conflict": "conflict.document.read",
        "concur_conflict": "conflict.concur",
        "list_manager_items": "manager.list",
        "act_manager_item": "manager.act",
    }
)
MCP_OPERATIONAL_TOOL_ACTIONS: Mapping[str, OperationalAction] = MappingProxyType(
    {
        "get_monitor": "monitor.read",
        "get_audit_record": "audit.read",
        "get_org_graph": "org_graph.read",
        "get_session": "session.end",
        "end_session": "session.end",
        "get_hitl": "hitl.read",
        "set_hitl": "hitl.write",
        "list_cards": "card.read",
        "get_card": "card.read",
        "register_card": "card.register",
        "transfer_card_owner": "card.transfer_owner",
    }
)


class OperationalMcpAuthorizationContract:
    """운영 MCP handler가 호출할 server-side principal·sealed grant 경계다.

    이 계약은 도구 인수에서 org/actor를 받지 않는다. 별도 provider를 등록할 때
    handler는 현재 authoritative ``ResourceRef``를 만든 뒤 ``authorize``를 호출해야
    하며, 결과가 ``allowed``일 때만 query 결과를 투영하거나 mutation을 수행할 수 있다.
    P0 factory는 이 계약을 우회해 저장소를 직접 부르지 않고
    ``OperationalApplication``의 같은 권한 경계를 사용한다.
    """

    def __init__(
        self,
        *,
        authorization: OperationalAuthorization,
        principal_provider: Callable[[], AuthenticatedPrincipal],
    ) -> None:
        if type(authorization) is not OperationalAuthorization:
            raise TypeError("운영 MCP authorization은 OperationalAuthorization이어야 합니다.")
        if not callable(principal_provider):
            raise TypeError("운영 MCP principal provider는 callable이어야 합니다.")
        self._authorization = authorization
        self._principal_provider = principal_provider

    def authorize(
        self,
        *,
        action: OperationalAction,
        resource: ResourceRef,
    ) -> OperationalAuthorizationOutcome:
        """현재 서버 principal과 리소스로 exact OperationalAuthorization을 실행한다."""
        try:
            raw_principal = self._principal_provider()
            if type(raw_principal) is not AuthenticatedPrincipal:
                return "denied"
            principal = AuthenticatedPrincipal.model_validate(raw_principal, strict=True)
        except Exception:
            return "unavailable"
        return self._authorization.authorize(principal, action, resource)


def validate_operational_mcp_registration(
    *,
    tool_actions: object,
    authorization_contract: OperationalMcpAuthorizationContract | None,
) -> Mapping[str, OperationalAction]:
    """운영 MCP provider 등록 전의 fail-closed capability gate다.

    별도 provider는 도구마다 이 함수를 통과한 action과 동일한
    ``OperationalMcpAuthorizationContract``의 ``authorize``를 handler의 저장소
    read/write 직전에 사용해야 한다. contract 없이 도구 이름/action만 선언한 등록은
    여기서 거부한다. P0 factory의 실제 등록은 ``MCP_OPERATIONAL_TOOL_ACTIONS``를
    유일한 이름/action 원천으로 삼고, handler는 ``OperationalApplication``만 호출한다.
    """
    if not isinstance(tool_actions, Mapping):
        raise TypeError("운영 MCP tool action은 mapping이어야 합니다.")
    canonical: dict[str, OperationalAction] = {}
    raw_actions = cast(Mapping[object, object], tool_actions)
    for name, action in raw_actions.items():
        if (
            type(name) is not str
            or not name.strip()
            or type(action) is not str
            or action not in OPERATIONAL_ACTION_MANIFEST
        ):
            raise ValueError("운영 MCP tool action이 유효하지 않습니다.")
        canonical[name] = action
    if canonical and type(authorization_contract) is not OperationalMcpAuthorizationContract:
        raise ValueError("운영 MCP provider에는 authorization contract가 필요합니다.")
    if not canonical and authorization_contract is not None:
        raise ValueError("운영 도구 없이 authorization contract를 등록할 수 없습니다.")
    return MappingProxyType(canonical)


def _canonical_operational_principal(raw: object) -> AuthenticatedPrincipal:
    if type(raw) is not AuthenticatedPrincipal:
        raise TypeError("운영 MCP principal type mismatch")
    return AuthenticatedPrincipal.model_validate(raw, strict=True)


def create_operational_mcp_server(
    *,
    application: OperationalApplication,
    principal_provider: Callable[[], AuthenticatedPrincipal],
) -> FastMCP:
    """권한-우선 운영 MCP 서버(P17.8 S4 P0)다.

    HTTP adapter와 같은 ``OperationalApplication``만 호출한다. 도구 인수에는 조직,
    actor, owner가 없으며, application이 현재 Registry/Session에서 ResourceRef를 다시
    구성하고 mutation 직전 재인가·승인 증거 확인까지 수행한다.
    """
    if type(application) is not OperationalApplication:
        raise TypeError("운영 MCP application은 OperationalApplication이어야 합니다.")
    if not callable(principal_provider):
        raise TypeError("운영 MCP principal provider는 callable이어야 합니다.")
    mcp = FastMCP("Agent Org Network — 운영")
    registered_tool_actions: dict[str, OperationalAction] = {}

    def operational_tool(
        *, name: str, action: OperationalAction, description: str
    ) -> Callable[[Callable[..., str]], Any]:
        """닫힌 action 매트릭스의 도구만 한 번씩 실제 MCP에 등록한다.

        이 guard가 metadata만으로 인가한다는 뜻은 아니다. 각 handler는 shared
        OperationalApplication을 호출하며, 그 application이 current ResourceRef,
        sealed grant, 승인과 감사 precondition을 적용한다.
        """
        if name in registered_tool_actions or MCP_OPERATIONAL_TOOL_ACTIONS.get(name) != action:
            raise RuntimeError("운영 MCP 도구 등록 매트릭스가 일치하지 않습니다.")
        registered_tool_actions[name] = action
        return mcp.tool(name=name, description=description)

    def registered_action(name: str) -> OperationalAction:
        action = registered_tool_actions.get(name)
        if action is None or MCP_OPERATIONAL_TOOL_ACTIONS.get(name) != action:
            raise RuntimeError("운영 MCP 도구 등록 매트릭스가 일치하지 않습니다.")
        return action

    def principal() -> AuthenticatedPrincipal:
        return _canonical_operational_principal(principal_provider())

    def safe(call: Callable[[], str]) -> str:
        try:
            return call()
        except OperationalUnavailableError:
            return "운영 권한 또는 승인 상태를 일시적으로 확인할 수 없습니다."
        except (OperationalDeniedError, OperationalNotFoundError):
            return "운영 대상을 찾을 수 없거나 권한이 없습니다."
        except Exception:
            return "운영 요청을 처리하지 못했습니다."

    @operational_tool(
        name="get_monitor",
        action="monitor.read",
        description="현재 권한으로 조직 운영 모니터링 요약을 조회합니다.",
    )
    def get_monitor() -> str:  # pyright: ignore[reportUnusedFunction]
        return safe(
            lambda: (
                "운영 모니터링\n\n"
                + str(application.monitor(principal(), action=registered_action("get_monitor")))
            )
        )

    @operational_tool(
        name="get_audit_record",
        action="audit.read",
        description="감사 인덱스의 상세 기록을 현재 권한으로 조회합니다.",
    )
    def get_audit_record(index: int) -> str:  # pyright: ignore[reportUnusedFunction]
        return safe(
            lambda: (
                "감사 기록\n\n"
                + str(
                    application.audit_detail(
                        principal(), index, action=registered_action("get_audit_record")
                    )
                )
            )
        )

    @operational_tool(
        name="get_org_graph",
        action="org_graph.read",
        description="현재 권한으로 조직 카드 그래프를 조회합니다.",
    )
    def get_org_graph() -> str:  # pyright: ignore[reportUnusedFunction]
        return safe(
            lambda: (
                "조직 그래프\n\n"
                + str(application.graph(principal(), action=registered_action("get_org_graph")))
            )
        )

    @operational_tool(
        name="get_session",
        action="session.end",
        description="세션의 현재 상태를 현재 권한으로 조회합니다.",
    )
    def get_session(session_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        def run() -> str:
            session = application.session(
                principal(), session_id, action=registered_action("get_session")
            )
            return f"세션 ID: {session.session_id}\n상태: {session.status}"

        return safe(run)

    @operational_tool(
        name="end_session",
        action="session.end",
        description="승인 증거가 있는 경우에만 세션을 종료합니다.",
    )
    def end_session(session_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        def run() -> str:
            session = application.end_session(
                principal(),
                session_id,
                channel="mcp",
                action=registered_action("end_session"),
            )
            return f"세션 종료 완료: {session.session_id}\n상태: {session.status}"

        return safe(run)

    @operational_tool(
        name="get_hitl",
        action="hitl.read",
        description="현재 카드 owner 기준 HITL 상태를 조회합니다.",
    )
    def get_hitl(agent_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return safe(
            lambda: (
                f"HITL 상태: {str(application.hitl(principal(), agent_id, action=registered_action('get_hitl'))).lower()}"
            )
        )

    @operational_tool(
        name="set_hitl",
        action="hitl.write",
        description="승인 증거가 있는 경우에만 현재 카드의 HITL 상태를 변경합니다.",
    )
    def set_hitl(agent_id: str, on: bool) -> str:  # pyright: ignore[reportUnusedFunction]
        return safe(
            lambda: (
                f"HITL 변경 완료: {str(application.set_hitl(principal(), agent_id, on, channel='mcp', action=registered_action('set_hitl'))).lower()}"
            )
        )

    @operational_tool(
        name="list_cards",
        action="card.read",
        description="권한이 있는 Agent Card 목록을 조회합니다.",
    )
    def list_cards() -> str:  # pyright: ignore[reportUnusedFunction]
        def run() -> str:
            cards = application.list_cards(principal(), action=registered_action("list_cards"))
            return "\n".join(
                ["Agent Card 목록"]
                + [f"ID: {card.agent_id} | Owner: {card.owner} | 팀: {card.team}" for card in cards]
            )

        return safe(run)

    @operational_tool(
        name="get_card",
        action="card.read",
        description="한 Agent Card의 현재 운영 메타데이터를 조회합니다.",
    )
    def get_card(agent_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        def run() -> str:
            card = application.card(principal(), agent_id, action=registered_action("get_card"))
            return f"Agent Card: {card.agent_id}\nOwner: {card.owner}\n팀: {card.team}"

        return safe(run)

    @operational_tool(
        name="register_card",
        action="card.register",
        description="승인 증거가 있는 경우에만 새 Agent Card를 admission을 거쳐 등록합니다.",
    )
    def register_card(  # pyright: ignore[reportUnusedFunction]
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str],
        last_reviewed_at: str,
        maintainer: str | None = None,
    ) -> str:
        def run() -> str:
            card = application.register_card(
                principal(),
                CardCandidate(
                    agent_id=agent_id,
                    owner=owner,
                    team=team,
                    summary=summary,
                    domains=list(domains),
                    last_reviewed_at=last_reviewed_at,
                    maintainer=maintainer,
                ),
                channel="mcp",
                action=registered_action("register_card"),
            )
            return f"Agent Card 등록 완료: {card.agent_id}\nOwner: {card.owner}"

        return safe(run)

    @operational_tool(
        name="transfer_card_owner",
        action="card.transfer_owner",
        description="승인 증거가 있는 경우에만 Agent Card의 현재 owner를 이전합니다.",
    )
    def transfer_card_owner(agent_id: str, new_owner: str) -> str:  # pyright: ignore[reportUnusedFunction]
        def run() -> str:
            result = application.transfer_card_owner(
                principal(),
                agent_id,
                new_owner,
                channel="mcp",
                action=registered_action("transfer_card_owner"),
            )
            return f"Agent Card owner 이전 완료: {result.agent_id}\n이전: {result.from_owner} → {result.to_owner}"

        return safe(run)

    if registered_tool_actions != dict(MCP_OPERATIONAL_TOOL_ACTIONS):
        raise RuntimeError("운영 MCP 도구 등록 매트릭스가 일치하지 않습니다.")
    return mcp


def create_authoring_mcp_server(
    *,
    application: AuthoringApplication,
    principal_provider: Callable[[], AuthenticatedPrincipal],
    read_index: Callable[[AgentCard], str],
    publish_owner_side: Callable[[AgentCard, str], tuple[str, AuthoringMutation]],
    accept_after_git: Callable[[str, AgentCard], str],
) -> FastMCP:
    """raw/staged 자료를 저장하지 않는 저작 MCP adapter.

    모든 도구는 별도 ``AuthoringApplication``만 통해 current ResourceRef와 sealed grant를
    확인한다. publish callback은 Git write만 하고, application의 post-write reauthorize
    뒤에 index 수용을 하려면 callback 내부가 아닌 application caller 측 post hook을 써야
    한다. 이 factory는 raw storage·조직·principal 입력을 MCP 인자로 받지 않는다. 임의
    OkfFile bundle을 바로 Git에 넣는 ``commit_author_bundle``은 현재 domain 재admission
    계약이 없어 노출하지 않는다. LLM 뒤 mandatory re-admit 포트를 아직 typed result로
    강제할 수 없는 ``run_authoring``도 공개하지 않는다. HTTP의 구조화된 run 경로는 별도
    admission 계약을 유지한다.
    """
    if type(application) is not AuthoringApplication or not callable(principal_provider):
        raise TypeError("저작 MCP에는 AuthoringApplication과 principal provider가 필요합니다.")
    if not all(
        callable(callback) for callback in (read_index, publish_owner_side, accept_after_git)
    ):
        raise TypeError("저작 MCP owner-side adapter가 필요합니다.")
    mcp = FastMCP("Agent Org Network — 저작")
    registered: dict[str, OperationalAction] = {}

    def tool(
        name: str, action: OperationalAction, description: str
    ) -> Callable[[Callable[..., str]], Any]:
        if MCP_AUTHORING_TOOL_ACTIONS.get(name) != action or name in registered:
            raise RuntimeError("저작 MCP 등록 매트릭스가 일치하지 않습니다.")
        registered[name] = action
        return mcp.tool(name=name, description=description)

    def principal() -> AuthenticatedPrincipal:
        return _canonical_operational_principal(principal_provider())

    def safe(call: Callable[[], str]) -> str:
        try:
            return call()
        except OperationalUnavailableError:
            return "저작 권한 또는 승인 상태를 일시적으로 확인할 수 없습니다."
        except (OperationalDeniedError, OperationalNotFoundError):
            return "저작 대상을 찾을 수 없거나 권한이 없습니다."
        except Exception:
            return "저작 요청을 처리하지 못했습니다."

    @tool("get_author_index", "author.read", "현재 권한으로 게시된 저작 목차를 조회합니다.")
    def get_author_index(agent_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return safe(lambda: application.query(principal(), agent_id, read_index))

    @tool("publish_authoring", "author.publish", "사람 승인 증적이 있는 저작 변경을 게시합니다.")
    def publish_authoring(agent_id: str, change_ref: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return safe(
            lambda: application.mutate(
                principal(),
                agent_id,
                lambda card: publish_owner_side(card, change_ref),
                channel="mcp",
                command={
                    "operation": "publish_authoring",
                    "agent_id": agent_id,
                    "change_ref_sha256": sha256(change_ref.encode("utf-8")).hexdigest(),
                },
                after_write=accept_after_git,
            )
        )

    if registered != dict(MCP_AUTHORING_TOOL_ACTIONS):
        raise RuntimeError("저작 MCP 등록 매트릭스가 일치하지 않습니다.")
    return mcp


class QuestionMcpApplication(Protocol):
    """MCP가 허용받는 P17 Question Surface의 최소 application 포트."""

    def ask(self, command: AskQuestion) -> QuestionStreamLookup: ...

    def lookup(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamLookup: ...


class ApprovalMcpOperations(Protocol):
    """MCP가 호출하는 Approval 운영 application의 최소 포트."""

    def pending_for(self, principal: ApproverPrincipal) -> list[ApprovalPendingSummary]: ...

    def detail(
        self,
        item_id: str,
        principal: ApproverPrincipal,
    ) -> ApprovalPendingDetail: ...

    def decide(
        self,
        item_id: str,
        principal: ApproverPrincipal,
        intent: ApprovalDecisionIntent,
    ) -> ApprovalOperationsDecision: ...

    def reassign(
        self,
        item_id: str,
        principal: ApproverPrincipal,
        target: ManualApprovalReassignmentTarget,
    ) -> ApprovalReassigned: ...


class ConflictMcpOperations(Protocol):
    def pending_for(self, principal: ConflictOperationsPrincipal) -> list[ConflictCase]: ...

    def document(self, case_id: str, principal: ConflictOperationsPrincipal) -> ConflictCase: ...

    def concur(self, command: ConcurOnConflict) -> P17ConcurrenceResult: ...


class ManagerMcpOperations(Protocol):
    def pending_for(self, principal: ManagerOperationsPrincipal) -> list[ManagerItem]: ...

    def act(self, command: P17ManagerDispositionCommand) -> P17ManagerDispositionResult: ...


class DeadlockManagerMcpOperations(Protocol):
    def act(
        self,
        command: DeadlockManagerDispositionCommand,
    ) -> P17DeadlockManagerDispositionResult: ...


def question_lookup_to_mcp_text(raw: QuestionStreamLookup) -> str:
    """canonical lookup DTO만 중립적인 MCP 사용자 텍스트로 투영한다."""
    result = _canonical_question_lookup(raw)
    match result:
        case AnsweredQuestionLookup():
            sources = " · ".join(result.sources) if result.sources else "(없음)"
            return (
                f"{result.answer_text}\n\n"
                f"요청 ID: {result.request_id}\n"
                f"답변 기록: {result.record_id}\n"
                f"책임: {result.answered_by}/{result.agent_id}\n"
                f"신뢰: {result.mode}\n"
                f"출처: {sources}\n"
                f"검토: {result.review_status}"
            )
        case PendingQuestionLookup():
            retryable = "예" if result.retryable else "아니오"
            return (
                f"{_QUESTION_PENDING_TEXT}\n\n"
                f"요청 ID: {result.request_id}\n"
                f"처리 분류: {result.kind}\n"
                f"상태: {result.state}\n"
                f"재시도 가능: {retryable}"
            )
        case DeclinedQuestionLookup():
            return f"{_QUESTION_DECLINED_TEXT}\n\n요청 ID: {result.request_id}"
        case FailedQuestionLookup():
            body = f"{_QUESTION_FAILED_TEXT}\n\n요청 ID: {result.request_id}"
            if result.error_code in (
                "required_grounding_missing",
                "required_grounding_invalid",
                "approval_unavailable",
            ):
                body = f"{body}\n오류 코드: {result.error_code}"
            return body
        case _ as never:
            assert_never(never)


def create_question_mcp_server(
    *,
    application: QuestionMcpApplication,
    principal_provider: Callable[[], QuestionPrincipal],
    approval_operations: ApprovalMcpOperations | None = None,
    approver_principal_provider: Callable[[], ApprovalOperationsPrincipal] | None = None,
    conflict_operations: ConflictMcpOperations | None = None,
    conflict_principal_provider: Callable[[], AuthenticatedPrincipal] | None = None,
    manager_operations: ManagerMcpOperations | None = None,
    deadlock_manager_operations: DeadlockManagerMcpOperations | None = None,
    manager_store: ManagerQueueStore | None = None,
    manager_principal_provider: Callable[[], AuthenticatedPrincipal] | None = None,
) -> FastMCP:
    """P17 질문 도구와 선택적인 Approval 운영 도구를 한 MCP 서버로 노출한다.

    조직·사용자 신원은 server-side provider만 정하며 도구 인자가 아니다. 이 factory는
    Store·Finalization·demo state를 만들지 않고 주입된 application만 호출한다. Approval
    principal provider가 없으면 operations 주입 여부와 무관하게 기존 질문 도구 두 개만
    유지한다. provider가 있으면 operations도 반드시 함께 있어야 한다.
    """
    if not callable(principal_provider):
        raise TypeError("Question MCP principal provider는 callable이어야 합니다.")
    if approver_principal_provider is not None:
        if not callable(approver_principal_provider):
            raise ValueError("Approval MCP principal provider는 callable이어야 합니다.")
        if approval_operations is None:
            raise ValueError("Approval MCP에는 operations와 principal provider가 함께 필요합니다.")
    if conflict_principal_provider is not None:
        if not callable(conflict_principal_provider):
            raise ValueError("Conflict MCP principal provider는 callable이어야 합니다.")
        if conflict_operations is None:
            raise ValueError("Conflict MCP에는 operations와 principal provider가 함께 필요합니다.")
    if manager_principal_provider is not None:
        if not callable(manager_principal_provider):
            raise ValueError("Manager MCP principal provider는 callable이어야 합니다.")
        if (
            manager_operations is None
            or deadlock_manager_operations is None
            or manager_store is None
        ):
            raise ValueError(
                "Manager MCP에는 두 disposition application·Store·principal provider가 필요합니다."
            )
    mcp = FastMCP("Agent Org Network — Request-first 조직에 묻기")

    @mcp.tool(
        name="ask_org",
        description=(
            "회사 조직에 질문을 접수하고 현재 사용자 결과와 요청 ID를 돌려줍니다. "
            "session_id와 context_snapshot은 선택 입력입니다."
        ),
    )
    def ask_org(  # pyright: ignore[reportUnusedFunction]
        question: str,
        session_id: str = "",
        context_snapshot: str = "",
    ) -> str:
        try:
            principal = _canonical_principal(principal_provider())
            result = application.ask(
                AskQuestion(
                    principal=principal,
                    question=question,
                    session_id=_optional_text(session_id),
                    context_snapshot=_optional_text(context_snapshot),
                )
            )
            return question_lookup_to_mcp_text(result)
        except QuestionAuthorizationDeniedError:
            return _QUESTION_FORBIDDEN_TEXT
        except QuestionSurfaceInterruptedError as error:
            return _interrupted_to_mcp_text(error)
        except QuestionStreamRequestNotFoundError:
            return _QUESTION_NOT_FOUND_TEXT
        except Exception:
            return f"{_QUESTION_UNAVAILABLE_TEXT}\n\n요청 ID: 확인할 수 없음"

    @mcp.tool(
        name="get_question",
        description="요청 ID로 본인이 접수한 질문의 canonical 현재 결과를 조회합니다.",
    )
    def get_question(request_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            principal = _canonical_principal(principal_provider())
            result = _canonical_question_lookup(application.lookup(request_id, principal))
            if result.request_id != request_id:
                raise QuestionStreamRequestNotFoundError()
            return question_lookup_to_mcp_text(result)
        except QuestionSurfaceInterruptedError as error:
            if error.request_id != request_id:
                return _QUESTION_NOT_FOUND_TEXT
            return _interrupted_to_mcp_text(error)
        except QuestionStreamRequestNotFoundError:
            return _QUESTION_NOT_FOUND_TEXT
        except Exception:
            # 도구 입력은 신뢰 경계 밖이다. 장애 응답에 원문을 되비추면 개행·제어문자로
            # 가짜 책임/상태 줄을 삽입할 수 있으므로 field-free 중립 결과만 반환한다.
            return f"{_QUESTION_UNAVAILABLE_TEXT}\n\n요청 ID: 확인할 수 없음"

    if approver_principal_provider is not None:
        assert approval_operations is not None
        _register_approval_tools(
            mcp,
            approval_operations,
            approver_principal_provider,
        )
    if conflict_principal_provider is not None:
        assert conflict_operations is not None
        _register_conflict_tools(mcp, conflict_operations, conflict_principal_provider)
    if manager_principal_provider is not None:
        assert manager_operations is not None
        assert deadlock_manager_operations is not None
        assert manager_store is not None
        _register_manager_tools(
            mcp,
            manager_operations,
            deadlock_manager_operations,
            manager_store,
            manager_principal_provider,
        )

    return mcp


def _register_conflict_tools(
    mcp: FastMCP,
    operations: ConflictMcpOperations,
    principal_provider: Callable[[], AuthenticatedPrincipal],
) -> None:
    @mcp.tool(
        name="list_conflicts",
        description="현재 인증된 후보 Owner에게 배정된 request-aware 다툼을 조회합니다.",
    )
    def list_conflicts() -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            principal = _canonical_conflict_principal(principal_provider())
            cases = operations.pending_for(principal)
            if type(cases) is not list:
                raise TypeError
            canonical = tuple(_canonical_conflict_case(case) for case in cases)
            if not canonical:
                return "대기 중인 다툼이 없습니다."
            return "\n\n".join(
                f"케이스 ID: {case.case_id}\n의도: {case.intent}\n합의 차수: {case.concurrence_round}"
                for case in canonical
            )
        except Exception:
            return _CONFLICT_UNAVAILABLE_TEXT

    @mcp.tool(
        name="get_conflict",
        description="현재 후보 Owner 권한으로 다툼 질문과 후보를 조회합니다.",
    )
    def get_conflict(case_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            canonical_case_id = _required_governance_input(case_id)
            principal = _canonical_conflict_principal(principal_provider())
            case = _canonical_conflict_case(operations.document(canonical_case_id, principal))
            if case.case_id != canonical_case_id:
                raise TypeError
            candidates = " · ".join(candidate.agent_id for candidate in case.candidates)
            return (
                f"케이스 ID: {case.case_id}\n질문: {case.question}\n"
                f"후보: {candidates}\n합의 차수: {case.concurrence_round}"
            )
        except Exception:
            return _CONFLICT_UNAVAILABLE_TEXT

    @mcp.tool(
        name="concur_conflict",
        description="현재 인증된 후보 Owner의 합의 표를 제출합니다.",
    )
    def concur_conflict(  # pyright: ignore[reportUnusedFunction]
        case_id: str,
        expected_round: int,
        on_agent: str,
        stance: Literal["withdraw", "keep_as_complement"] = "withdraw",
        rationale: str = "",
    ) -> str:
        try:
            principal = _canonical_conflict_principal(principal_provider())
            outcome = operations.concur(
                ConcurOnConflict(
                    principal=principal,
                    case_id=_required_governance_input(case_id),
                    expected_round=expected_round,
                    on_agent=_required_governance_input(on_agent),
                    stance=stance,
                    rationale=rationale,
                )
            )
            return (
                f"다툼 처분: {outcome.kind}\n요청 ID: {outcome.request_id}\n"
                f"케이스 ID: {outcome.case_id}"
            )
        except ConflictDispositionError:
            return _CONFLICT_UNAVAILABLE_TEXT
        except Exception:
            return _CONFLICT_UNAVAILABLE_TEXT


def _register_manager_tools(
    mcp: FastMCP,
    operations: ManagerMcpOperations,
    deadlock_operations: DeadlockManagerMcpOperations,
    manager_store: ManagerQueueStore,
    principal_provider: Callable[[], AuthenticatedPrincipal],
) -> None:
    @mcp.tool(
        name="list_manager_items",
        description="현재 인증된 Manager에게 배정된 request-aware 항목을 조회합니다.",
    )
    def list_manager_items() -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            principal = _canonical_manager_principal(principal_provider())
            items = operations.pending_for(principal)
            if type(items) is not list:
                raise TypeError
            canonical = tuple(_canonical_manager_item(item) for item in items)
            if not canonical:
                return "대기 중인 Manager 항목이 없습니다."
            return "\n\n".join(
                f"항목 ID: {item.item_id}\n요청 ID: {item.request_id}" for item in canonical
            )
        except Exception:
            return _MANAGER_UNAVAILABLE_TEXT

    @mcp.tool(
        name="act_manager_item",
        description="현재 인증된 Manager가 담당 지정 또는 명시적 거절을 수행합니다.",
    )
    def act_manager_item(  # pyright: ignore[reportUnusedFunction]
        item_id: str,
        action: Literal["assign_owner", "dismiss"],
        agent_id: str = "",
        rationale: str = "",
    ) -> str:
        try:
            canonical_item_id = _required_governance_input(item_id)
            principal = _canonical_manager_principal(principal_provider())
            raw_item = manager_store.get(canonical_item_id)
            item = _canonical_manager_item(raw_item)
            if isinstance(item.source, FromUnowned):
                command: P17ManagerDispositionCommand = (
                    AssignUnownedOwner(
                        principal=principal,
                        item_id=canonical_item_id,
                        agent_id=_required_governance_input(agent_id),
                        rationale=rationale,
                    )
                    if action == "assign_owner"
                    else DismissUnowned(
                        principal=principal,
                        item_id=canonical_item_id,
                        rationale=rationale,
                    )
                )
                outcome = operations.act(command)
            elif isinstance(item.source, FromDeadlock):
                deadlock_command: DeadlockManagerDispositionCommand = (
                    AssignDeadlockedOwner(
                        principal=principal,
                        item_id=canonical_item_id,
                        agent_id=_required_governance_input(agent_id),
                        rationale=rationale,
                    )
                    if action == "assign_owner"
                    else DismissDeadlocked(
                        principal=principal,
                        item_id=canonical_item_id,
                        rationale=rationale,
                    )
                )
                outcome = deadlock_operations.act(deadlock_command)
            else:
                raise ValueError
            return f"Manager 처분: {outcome.kind}\n요청 ID: {outcome.request_id}"
        except ManagerDispositionError:
            return _MANAGER_UNAVAILABLE_TEXT
        except Exception:
            return _MANAGER_UNAVAILABLE_TEXT


def _canonical_conflict_principal(raw: object) -> ConflictOperationsPrincipal:
    if type(raw) is not AuthenticatedPrincipal:
        raise TypeError
    assert isinstance(raw, AuthenticatedPrincipal)
    return AuthenticatedPrincipal.model_validate(raw, strict=True)


def _canonical_manager_principal(raw: object) -> ManagerOperationsPrincipal:
    if type(raw) is not AuthenticatedPrincipal:
        raise TypeError
    assert isinstance(raw, AuthenticatedPrincipal)
    return AuthenticatedPrincipal.model_validate(raw, strict=True)


def _canonical_conflict_case(raw: object) -> ConflictCase:
    if type(raw) is not ConflictCase:
        raise TypeError
    assert isinstance(raw, ConflictCase)
    return raw


def _canonical_manager_item(raw: object) -> ManagerItem:
    if type(raw) is not ManagerItem:
        raise TypeError
    assert isinstance(raw, ManagerItem)
    return raw


def _required_governance_input(value: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError
    return value


def _register_approval_tools(
    mcp: FastMCP,
    operations: ApprovalMcpOperations,
    principal_provider: Callable[[], ApprovalOperationsPrincipal],
) -> None:
    """인증된 승인자용 여섯 도구를 actor-free schema로 등록한다."""

    @mcp.tool(
        name="list_approvals",
        description="현재 인증된 승인자에게 배정된 승인 대기 항목을 조회합니다.",
    )
    def list_approvals() -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            principal = _canonical_approver_principal(principal_provider())
            pending_for = cast(
                Callable[[ApprovalOperationsPrincipal], list[ApprovalPendingSummary]],
                operations.pending_for,
            )
            summaries = _canonical_approval_summaries(pending_for(principal))
            return _approval_summaries_to_mcp_text(summaries)
        except Exception:
            return _APPROVAL_LIST_UNAVAILABLE_TEXT

    @mcp.tool(
        name="get_approval",
        description="승인 항목의 질문과 후보 답을 현재 지정 승인자 권한으로 조회합니다.",
    )
    def get_approval(item_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            canonical_item_id = _required_approval_input(item_id)
            principal = _canonical_approver_principal(principal_provider())
            detail_call = cast(
                Callable[[str, ApprovalOperationsPrincipal], ApprovalPendingDetail],
                operations.detail,
            )
            detail = _canonical_approval_detail(detail_call(canonical_item_id, principal))
            if detail.item_id != canonical_item_id:
                raise TypeError
            return _approval_detail_to_mcp_text(detail)
        except Exception:
            return _APPROVAL_DETAIL_UNAVAILABLE_TEXT

    @mcp.tool(
        name="approve",
        description="현재 지정된 승인 항목을 승인합니다.",
    )
    def approve(item_id: str) -> str:  # pyright: ignore[reportUnusedFunction]
        return _decide_approval_to_mcp_text(
            operations,
            principal_provider,
            item_id=item_id,
            intent=ApproveIntent(),
        )

    @mcp.tool(
        name="approve_with_edit",
        description="후보 답 본문을 수정한 뒤 승인합니다.",
    )
    def approve_with_edit(  # pyright: ignore[reportUnusedFunction]
        item_id: str,
        edited_text: str,
    ) -> str:
        try:
            intent: ApprovalDecisionIntent = ApproveWithEditIntent(edited_text=edited_text)
        except Exception:
            return _APPROVAL_DECISION_UNAVAILABLE_TEXT
        return _decide_approval_to_mcp_text(
            operations,
            principal_provider,
            item_id=item_id,
            intent=intent,
        )

    @mcp.tool(
        name="reject",
        description="현재 지정된 승인 항목을 명시적으로 반려합니다.",
    )
    def reject(item_id: str, reason_code: str) -> str:  # pyright: ignore[reportUnusedFunction]
        try:
            intent: ApprovalDecisionIntent = RejectIntent(reason_code=reason_code)
        except Exception:
            return _APPROVAL_DECISION_UNAVAILABLE_TEXT
        return _decide_approval_to_mcp_text(
            operations,
            principal_provider,
            item_id=item_id,
            intent=intent,
        )

    @mcp.tool(
        name="reassign_approval",
        description="현재 승인 항목을 중앙 권한 검증을 거쳐 다른 승인자에게 재지정합니다.",
    )
    def reassign_approval(  # pyright: ignore[reportUnusedFunction]
        item_id: str,
        approver_id: str,
    ) -> str:
        try:
            canonical_item_id = _required_approval_input(item_id)
            principal = _canonical_approver_principal(principal_provider())
            target = ManualApprovalReassignmentTarget(approver_id=approver_id)
            reassign = cast(
                Callable[
                    [str, ApprovalOperationsPrincipal, ManualApprovalReassignmentTarget],
                    ApprovalReassigned,
                ],
                operations.reassign,
            )
            result = _canonical_approval_reassignment(
                reassign(canonical_item_id, principal, target)
            )
            return _approval_reassignment_to_mcp_text(result)
        except Exception:
            return _APPROVAL_REASSIGNMENT_UNAVAILABLE_TEXT


def _decide_approval_to_mcp_text(
    operations: ApprovalMcpOperations,
    principal_provider: Callable[[], ApprovalOperationsPrincipal],
    *,
    item_id: str,
    intent: ApprovalDecisionIntent,
) -> str:
    try:
        canonical_item_id = _required_approval_input(item_id)
        principal = _canonical_approver_principal(principal_provider())
        decide = cast(
            Callable[
                [str, ApprovalOperationsPrincipal, ApprovalDecisionIntent],
                ApprovalOperationsDecision,
            ],
            operations.decide,
        )
        result = _canonical_approval_decision(decide(canonical_item_id, principal, intent))
        return _approval_decision_to_mcp_text(result)
    except Exception:
        return _APPROVAL_DECISION_UNAVAILABLE_TEXT


def _required_approval_input(value: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("Approval MCP 입력이 비어 있습니다.")
    return value


def _canonical_approver_principal(raw: object) -> ApprovalOperationsPrincipal:
    if type(raw) not in (ApproverPrincipal, AuthenticatedPrincipal):
        raise ValueError("ApproverPrincipal type mismatch")
    assert isinstance(raw, (ApproverPrincipal, AuthenticatedPrincipal))
    return type(raw).model_validate(
        raw.model_dump(mode="python", round_trip=True),
        strict=True,
    )


def _canonical_approval_summaries(raw: object) -> tuple[ApprovalPendingSummary, ...]:
    if type(raw) is not list:
        raise TypeError("Approval summary list type mismatch")
    summaries: list[ApprovalPendingSummary] = []
    for value in cast(list[object], raw):
        if type(value) is not ApprovalPendingSummary:
            raise TypeError("ApprovalPendingSummary type mismatch")
        assert isinstance(value, ApprovalPendingSummary)
        summaries.append(
            ApprovalPendingSummary.model_validate(
                value.model_dump(mode="python", round_trip=True),
                strict=True,
            )
        )
    return tuple(summaries)


def _canonical_approval_detail(raw: object) -> ApprovalPendingDetail:
    if type(raw) is not ApprovalPendingDetail:
        raise TypeError("ApprovalPendingDetail type mismatch")
    assert isinstance(raw, ApprovalPendingDetail)
    return ApprovalPendingDetail.model_validate(
        raw.model_dump(mode="python", round_trip=True),
        strict=True,
    )


def _canonical_approval_decision(raw: object) -> ApprovalOperationsDecision:
    if type(raw) is ApprovalAnswered:
        assert isinstance(raw, ApprovalAnswered)
        return ApprovalAnswered.model_validate(
            raw.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    if type(raw) is ApprovalDeclined:
        assert isinstance(raw, ApprovalDeclined)
        return ApprovalDeclined.model_validate(
            raw.model_dump(mode="python", round_trip=True),
            strict=True,
        )
    raise TypeError("Approval decision type mismatch")


def _canonical_approval_reassignment(raw: object) -> ApprovalReassigned:
    if type(raw) is not ApprovalReassigned:
        raise TypeError("ApprovalReassigned type mismatch")
    assert isinstance(raw, ApprovalReassigned)
    return ApprovalReassigned.model_validate(
        raw.model_dump(mode="python", round_trip=True),
        strict=True,
    )


def _approval_summaries_to_mcp_text(
    summaries: tuple[ApprovalPendingSummary, ...],
) -> str:
    if not summaries:
        return "대기 중인 승인 항목이 없습니다."
    blocks: list[str] = []
    for index, summary in enumerate(summaries, start=1):
        blocks.append(
            f"{index}. 항목 ID: {summary.item_id}\n"
            f"요청 ID: {summary.request_id}\n"
            f"승인 차수: {summary.approval_round}\n"
            f"배정 시각: {summary.assigned_at.isoformat()}\n"
            f"처리 기한: {summary.due_at.isoformat()}"
        )
    return "승인 대기 항목\n\n" + "\n\n".join(blocks)


def _approval_detail_to_mcp_text(detail: ApprovalPendingDetail) -> str:
    return (
        "승인 항목 상세\n\n"
        f"항목 ID: {detail.item_id}\n"
        f"요청 ID: {detail.request_id}\n"
        f"승인 차수: {detail.approval_round}\n"
        f"배정 시각: {detail.assigned_at.isoformat()}\n"
        f"처리 기한: {detail.due_at.isoformat()}\n"
        f"후보 모드: {detail.candidate.mode}\n\n"
        f"질문\n{detail.question}\n\n"
        f"후보 답\n{detail.candidate.text}"
    )


def _approval_decision_to_mcp_text(result: ApprovalOperationsDecision) -> str:
    if isinstance(result, ApprovalAnswered):
        return (
            "승인 처리가 완료되었습니다.\n\n"
            f"항목 ID: {result.item_id}\n"
            f"요청 ID: {result.request_id}\n"
            f"승인 차수: {result.approval_round}\n"
            f"답변 기록: {result.record_id}\n"
            f"처분: {result.action}\n"
            f"전달 상태: {result.delivery.kind}"
        )
    return (
        "반려 처리가 완료되었습니다.\n\n"
        f"항목 ID: {result.item_id}\n"
        f"요청 ID: {result.request_id}\n"
        f"승인 차수: {result.approval_round}\n"
        f"전달 상태: {result.delivery.kind}"
    )


def _approval_reassignment_to_mcp_text(result: ApprovalReassigned) -> str:
    return (
        "승인 재지정이 완료되었습니다.\n\n"
        f"이전 항목 ID: {result.predecessor_item_id}\n"
        f"새 항목 ID: {result.successor_item_id}\n"
        f"요청 ID: {result.request_id}\n"
        f"승인 차수: {result.approval_round}\n"
        f"처리 기한: {result.due_at.isoformat()}"
    )


def _optional_text(value: str) -> str | None:
    return value if value.strip() else None


def _canonical_principal(raw: object) -> QuestionPrincipal:
    if type(raw) is RequesterPrincipal:
        return RequesterPrincipal.model_validate(
            {"org_id": raw.org_id, "subject_id": raw.subject_id},
            strict=True,
        )
    if type(raw) is AuthenticatedPrincipal:
        return AuthenticatedPrincipal.model_validate(
            {
                "org_id": raw.org_id,
                "subject_id": raw.subject_id,
                "identity_provider": raw.identity_provider,
                "identity_session_id": raw.identity_session_id,
            },
            strict=True,
        )
    raise ValueError("QuestionPrincipal type mismatch")


def _canonical_question_lookup(raw: object) -> QuestionStreamLookup:
    if type(raw) is AnsweredQuestionLookup:
        return AnsweredQuestionLookup.model_validate(
            {
                "answer_text": raw.answer_text,
                "request_id": raw.request_id,
                "record_id": raw.record_id,
                "mode": raw.mode,
                "sources": raw.sources,
                "review_status": raw.review_status,
                "answered_by": raw.answered_by,
                "agent_id": raw.agent_id,
            },
            strict=True,
        )
    if type(raw) is PendingQuestionLookup:
        return PendingQuestionLookup.model_validate(
            {
                "request_id": raw.request_id,
                "kind": raw.kind,
                "state": raw.state,
                "retryable": raw.retryable,
                "message": raw.message,
            },
            strict=True,
        )
    if type(raw) is DeclinedQuestionLookup:
        return DeclinedQuestionLookup.model_validate(
            {
                "request_id": raw.request_id,
                "reason_code": raw.reason_code,
                "message": raw.message,
            },
            strict=True,
        )
    if type(raw) is FailedQuestionLookup:
        return FailedQuestionLookup.model_validate(
            {
                "request_id": raw.request_id,
                "error_code": raw.error_code,
                "message": raw.message,
            },
            strict=True,
        )
    raise TypeError("지원하지 않는 Question Surface lookup type")


def _interrupted_to_mcp_text(error: QuestionSurfaceInterruptedError) -> str:
    request_id = error.request_id
    if not request_id.strip() or type(error.retryable) is not bool:
        raise ValueError("QuestionSurfaceInterruptedError shape mismatch")
    retryable = "예" if error.retryable else "아니오"
    return f"{_QUESTION_INTERRUPTED_TEXT}\n\n요청 ID: {request_id}\n재시도 가능: {retryable}"


def reply_to_mcp_text(reply: OrgReply) -> str:
    """OrgReply를 MCP 클라이언트가 텍스트로 읽을 사용자向 답으로 투영한다(내부값 미포함).

    순수 함수다 — SDK·IO 없이 결정론으로 테스트한다(이 모듈의 노출 규율 핵심).
    web의 `serialize_reply`와 같은 경계: `OrgReply`에서만 투영하므로 조직 내부값은
    구조적으로 새지 않는다(Answered/Pending에 필드 자체가 없다). 다른 점은 형식뿐
    (dict가 아니라 사람이 읽는 텍스트).

    Answered → 답 본문 + 담당(owner/agent_id)·신뢰 상태(mode)·출처 메타 라인. `mode`는
    full/draft_only/backup을 *그대로* 노출한다 — 본디 사용자에게 알려야 할 신뢰값이다
    (ADR 0012 결정 4, web과 동일). 출처가 없으면 "(없음)"으로 표기한다.

    `record_id`(계획 §10.4): 답변 감사 단위의 *불투명 손잡이*(uuid4 hex — owner_id·
    ticket_id·구조를 비추지 않으므로 `tracking`과 같은 결·leak 아님)를 "피드백 참조" 라인
    으로 덧붙인다. MCP 질문자는 이 참조로 `submit_feedback` 도구에 좋음/싫음을 건다. 값이
    None(answer_record_store 미배선)이면 라인을 생략한다(하위호환 — 기존 텍스트 그대로).

    Pending → kind별 중립 안내(`message`). `dispatched`면 답 회수용 *불투명 추적 토큰*
    1개를 안내에 덧붙인다(ADR 0011 결정 6-5 — 토큰은 uuid4 hex라 owner_id·ticket_id·구조를
    비추지 않으므로 노출 OK). contested/unowned는 tracking이 None이라 토큰 안내가 없다.

    match+assert_never로 OrgReply(Answered | Pending) sealed sum을 망라한다.
    """
    match reply:
        case Answered():
            owner, agent_id = reply.answered_by
            sources = " · ".join(reply.sources) if reply.sources else "(없음)"
            meta = f"담당: {owner}/{agent_id} · 신뢰: {reply.mode} · 출처: {sources}"
            body = f"{reply.text}\n\n{meta}"
            if reply.record_id is not None:
                body = f"{body}\n피드백 참조: {reply.record_id}"
            return body
        case Pending():
            # 답 회수용 불투명 추적 토큰(dispatched에만 존재, ADR 0011 결정 6-5). 사용자/
            # 클라이언트가 이 토큰으로 나중에 답을 회수한다 — 토큰은 uuid4 hex라
            # owner_id·ticket_id·구조를 비추지 않는다(노출 불변식의 정밀화). contested/
            # unowned는 tracking이 None이라 토큰 안내를 생략한다.
            if reply.tracking is not None:
                return f"{reply.message}\n\n추적 토큰: {reply.tracking}"
            return reply.message
        case _ as never:
            assert_never(never)


def create_mcp_server(
    ask: AskOrg,
    *,
    user_id: str = _DEFAULT_MCP_USER_ID,
    feedback_store: "FeedbackStore | None" = None,
    answer_record_store: "AnswerRecordStore | None" = None,
) -> FastMCP:
    """AskOrg 핸들러를 `ask_org` 도구로 노출하는 FastMCP 서버를 조립한다.

    도구 본문은 `ask.handle(question, User(id=user_id))`를 호출하고 `reply_to_mcp_text`로
    텍스트를 투영한다 — 비즈니스 로직은 전부 `handle`이 진다(MCP는 표현층). `user_id`는
    서버 *설정값*이라 도구 파라미터가 아니다(ADR 0009 연결점). 기본은 익명 guest이고
    도구는 question만 받는다 — 누구도 남을 가장할 수 없다(인증은 T6.5에서 실 주체로 대체).

    `feedback_store`·`answer_record_store`(계획 §10.4): 주입 시 `submit_feedback` 도구를
    추가로 등록한다. 웹(`POST /answer/{record_id}/feedback`)과 **같은 인스턴스**를
    물려야 MCP 질문자 피드백이 담당자 감독 면(`monitoring_for_owner`)에 도달한다 —
    조립은 `create_central_app`이 `select_feedback_store()`/`select_answer_record_store()`로
    고른 스토어를 web·dispatcher·MCP 삼면에 같이 물린다(단, 현 시연 진입점 `main()`은
    web과 별개 프로세스라 조립 관례상 각자 store를 잡는다 — 그 한계는 tasks에 기록).
    미주입이면 도구 자체가 등록되지 않는다(하위호환 — 기존 `ask_org`만 있는 서버 그대로).

    결정론 테스트는 `create_mcp_server(build_demo(runtime=StubRuntime()).ask)`로 만들어
    `await server.call_tool("ask_org", {...})`(in-memory)로 호출한다 — 실 stdio·실 claude·
    실 네트워크 0.
    """
    mcp = FastMCP("Agent Org Network — 조직에 묻기")

    @mcp.tool(
        name="ask_org",
        description=(
            "회사 조직에 질문하면 담당이 답합니다. 질문을 분류해 담당 영역으로 라우팅하고, "
            "담당의 답을 담당·신뢰 상태·출처와 함께 돌려줍니다. 담당이 정해지지 않았거나 "
            "(다툼) 아직 없으면(미배정) 처리 안내를, 담당에게 전달됐지만 답이 준비 중이면 "
            "답 회수용 추적 토큰을 돌려줍니다."
        ),
    )
    def ask_org(question: str) -> str:  # pyright: ignore[reportUnusedFunction]
        reply = ask.handle(question, User(id=user_id))
        return reply_to_mcp_text(reply)

    if feedback_store is not None and answer_record_store is not None:
        _register_submit_feedback(mcp, feedback_store, answer_record_store, user_id=user_id)

    return mcp


def _register_submit_feedback(
    mcp: FastMCP,
    feedback_store: "FeedbackStore",
    answer_record_store: "AnswerRecordStore",
    *,
    user_id: str,
) -> None:
    """`submit_feedback` 도구를 등록한다(계획 §10.4 — 질문자 좋음/싫음).

    `verdict`는 `Literal["good","bad"]`이라 잘못된 값은 MCP 스키마 단(입력 검증)에서
    거부된다(`FeedbackVerdict`와 같은 SSOT). `submitted_by`는 도구 파라미터가 아니라
    서버 설정 `user_id`(=mcp_guest) — `ask_org`가 신원을 파라미터로 안 받는 것과 같은
    규율(누구도 남을 가장 못 함, ADR 0009). 미존재 record_id면 거부 안내 텍스트를
    돌려준다(MCP 도구 관례 — 예외 대신 사람이 읽는 결과 텍스트로 실패를 알린다).
    """
    from agent_org_network.answer_record import AnswerFeedback, default_clock

    @mcp.tool(
        name="submit_feedback",
        description=(
            "받은 답에 좋음/싫음 피드백을 남깁니다. 답의 '피드백 참조' 값을 record_id로 "
            "넣으세요. '싫음'은 담당자에게 전달돼 정정 기회가 됩니다. 코멘트(선택)에 사유를 "
            "적으면 담당자가 정정에 참고합니다."
        ),
    )
    def submit_feedback(  # pyright: ignore[reportUnusedFunction]
        record_id: str, verdict: Literal["good", "bad"], comment: str = ""
    ) -> str:
        if answer_record_store.get(record_id) is None:
            return f"알 수 없는 답변 참조입니다: {record_id} — 피드백을 남기지 못했어요."
        feedback_store.upsert(
            AnswerFeedback(
                record_id=record_id,
                verdict=verdict,
                comment=comment,
                submitted_by=user_id,
                submitted_at=default_clock(),
            )
        )
        label = "좋음" if verdict == "good" else "싫음"
        return f"피드백({label})을 접수했어요. 참조: {record_id}"


def main() -> None:
    """한 P17 데모 composition으로 stdio MCP 서버를 기동한다.

    `build_demo` 한 벌을 `build_demo_question_surface_composition`에 그대로 넘겨 이
    프로세스 안의 두 도구가 같은 Request/Finalization state를 본다. 이는 standalone
    단일 프로세스 수동 데모일 뿐, 별도 web 프로세스와 state를 공유한다는 보장은 없다.
    새 factory에는 legacy feedback 도구를 당기지 않는다.
    """
    from agent_org_network.demo import build_demo
    from agent_org_network.demo_question_surfaces import (
        DEMO_ORG_ID,
        build_demo_question_surface_composition,
    )

    bundle = build_demo()
    composition = build_demo_question_surface_composition(bundle)
    try:
        server = create_question_mcp_server(
            application=composition.application,
            principal_provider=lambda: RequesterPrincipal(
                org_id=DEMO_ORG_ID,
                subject_id=_DEFAULT_MCP_USER_ID,
            ),
        )
        server.run()
    finally:
        composition.close()


if __name__ == "__main__":
    main()
