"""P17.8 운영 MCP 매트릭스 계약.

질문 MCP와 운영 MCP는 별도 factory다. 전자는 운영 도구를 절대 묶지 않고, 후자는
공유 OperationalApplication을 통해서만 정해진 P0 도구를 연다.
"""

from __future__ import annotations

import asyncio
from typing import cast

from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.manager_queue import ManagerQueueStore
from agent_org_network.mcp_server import (
    ApprovalMcpOperations,
    ConflictMcpOperations,
    DeadlockManagerMcpOperations,
    MCP_OPERATIONAL_TOOL_ACTIONS,
    ManagerMcpOperations,
    OperationalMcpAuthorizationContract,
    QUESTION_GOVERNANCE_MCP_TOOL_ACTIONS,
    QuestionMcpApplication,
    create_question_mcp_server,
    validate_operational_mcp_registration,
)
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.question_resolution import QuestionPrincipal, RequesterPrincipal
from agent_org_network.question_stream_execution import PendingQuestionLookup, QuestionStreamLookup


class _QuestionApplication:
    def ask(self, command: object) -> QuestionStreamLookup:
        del command
        return self._result()

    def lookup(self, request_id: str, principal: QuestionPrincipal) -> QuestionStreamLookup:
        del principal
        return PendingQuestionLookup(
            request_id=request_id,
            kind="routing",
            state="received",
            retryable=True,
            message="처리 중",
        )

    @staticmethod
    def _result() -> QuestionStreamLookup:
        return PendingQuestionLookup(
            request_id="request-1",
            kind="routing",
            state="received",
            retryable=True,
            message="처리 중",
        )


def test_question_mcp의_전체_도구는_닫힌_action_매트릭스와_같다() -> None:
    """선택 provider를 모두 주입해도 S4 운영 도구가 몰래 열리지 않는다."""
    server = create_question_mcp_server(
        application=cast(QuestionMcpApplication, _QuestionApplication()),
        principal_provider=lambda: RequesterPrincipal(org_id="org-1", subject_id="requester-1"),
        approval_operations=cast(ApprovalMcpOperations, object()),
        approver_principal_provider=lambda: AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="approver-1",
            identity_provider="oidc",
            identity_session_id="session-1",
        ),
        conflict_operations=cast(ConflictMcpOperations, object()),
        conflict_principal_provider=lambda: AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="owner-1",
            identity_provider="oidc",
            identity_session_id="session-1",
        ),
        manager_operations=cast(ManagerMcpOperations, object()),
        deadlock_manager_operations=cast(DeadlockManagerMcpOperations, object()),
        manager_store=cast(ManagerQueueStore, object()),
        manager_principal_provider=lambda: AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="manager-1",
            identity_provider="oidc",
            identity_session_id="session-1",
        ),
    )

    names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert names == set(QUESTION_GOVERNANCE_MCP_TOOL_ACTIONS)
    assert MCP_OPERATIONAL_TOOL_ACTIONS == {
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


def test_운영_mcp_tool_이름만으로는_registration을_열수없다() -> None:
    """권한 wrapper 없는 action metadata는 operational MCP capability가 아니다."""
    try:
        validate_operational_mcp_registration(
            tool_actions={"get_monitor": "monitor.read"},
            authorization_contract=None,
        )
    except ValueError as error:
        assert str(error) == "운영 MCP provider에는 authorization contract가 필요합니다."
    else:  # pragma: no cover - fail-closed contract assertion
        raise AssertionError("bare operational MCP registration must be rejected")


def test_운영_mcp_registration은_runtime_OperationalAuthorization_contract를_요구한다() -> None:
    boundary = OperationalAuthorization(configured_org_id="org-1", central_authorizer=None)
    contract = OperationalMcpAuthorizationContract(
        authorization=boundary,
        principal_provider=lambda: AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="operator-1",
            identity_provider="oidc",
            identity_session_id="session-1",
        ),
    )

    registered = validate_operational_mcp_registration(
        tool_actions={"get_monitor": "monitor.read"},
        authorization_contract=contract,
    )

    assert registered == {"get_monitor": "monitor.read"}
