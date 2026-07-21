"""P17.8 S3 Conflict/Manager MCP 신원 경계 회귀 계약."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from agent_org_network.approval import ApproverPrincipal
from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.conflict import Candidate, ConflictCase
from agent_org_network.manager_queue import ManagerItem
from agent_org_network.mcp_server import create_question_mcp_server
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConflictOperationsPrincipal,
    OwnerPrincipal,
    P17ConcurrenceResult,
)
from agent_org_network.p17_manager_disposition import (
    DeadlockManagerDispositionCommand,
    ManagerOperationsPrincipal,
    ManagerPrincipal,
    P17DeadlockManagerDispositionResult,
    P17ManagerDispositionCommand,
    P17ManagerDispositionResult,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    QuestionPrincipal,
    RequesterPrincipal,
)
from agent_org_network.question_stream_execution import (
    PendingQuestionLookup,
    QuestionStreamLookup,
)


def _call(server: Any, tool: str, arguments: dict[str, object]) -> str:
    content, _structured = asyncio.run(server.call_tool(tool, arguments))
    return content[0].text


class _QuestionApplication:
    def ask(self, command: AskQuestion) -> QuestionStreamLookup:
        return self.lookup("request-1", command.principal)

    def lookup(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamLookup:
        del principal
        return PendingQuestionLookup(
            request_id=request_id,
            kind="routing",
            state="received",
            retryable=True,
            message="internal",
        )


class _ConflictOperations:
    def __init__(self) -> None:
        self.calls = 0

    def pending_for(self, principal: ConflictOperationsPrincipal) -> list[ConflictCase]:
        del principal
        self.calls += 1
        return []

    def document(self, case_id: str, principal: ConflictOperationsPrincipal) -> ConflictCase:
        del case_id, principal
        self.calls += 1
        raise AssertionError("legacy principal must fail before operations")

    def concur(self, command: ConcurOnConflict) -> P17ConcurrenceResult:
        del command
        self.calls += 1
        raise AssertionError("legacy principal must fail before operations")


class _ManagerOperations:
    def __init__(self) -> None:
        self.calls = 0

    def pending_for(self, principal: ManagerOperationsPrincipal) -> list[ManagerItem]:
        del principal
        self.calls += 1
        return []

    def act(self, command: P17ManagerDispositionCommand) -> P17ManagerDispositionResult:
        del command
        self.calls += 1
        raise AssertionError("legacy principal must fail before operations")


class _DeadlockOperations:
    def __init__(self) -> None:
        self.calls = 0

    def act(
        self,
        command: DeadlockManagerDispositionCommand,
    ) -> P17DeadlockManagerDispositionResult:
        del command
        self.calls += 1
        raise AssertionError("legacy principal must fail before operations")


class _ManagerStore:
    def __init__(self) -> None:
        self.reads = 0

    def get(self, item_id: str) -> ManagerItem | None:
        del item_id
        self.reads += 1
        raise AssertionError("legacy principal must fail before store read")

    def enqueue(self, item: ManagerItem) -> None:
        del item
        raise AssertionError

    def pending_for_manager(self, manager_id: str) -> list[ManagerItem]:
        del manager_id
        raise AssertionError

    def get_by_case(self, case_id: str) -> ManagerItem | None:
        del case_id
        raise AssertionError

    def mark_resolved(self, item: ManagerItem) -> None:
        del item
        raise AssertionError


def test_conflict_mcp는_legacy_owner_provider를_고정_장애로_막는다() -> None:
    operations = _ConflictOperations()
    legacy_provider = cast(
        "Any",
        lambda: OwnerPrincipal(org_id="org-1", subject_id="owner-1"),
    )
    server = create_question_mcp_server(
        application=_QuestionApplication(),
        principal_provider=lambda: RequesterPrincipal(org_id="org-1", subject_id="requester-1"),
        conflict_operations=operations,
        conflict_principal_provider=legacy_provider,
    )

    assert _call(server, "list_conflicts", {}) == "다툼 처리 요청을 완료하지 못했습니다."
    assert (
        _call(
            server,
            "concur_conflict",
            {"case_id": "case-1", "expected_round": 1, "on_agent": "card-1"},
        )
        == "다툼 처리 요청을 완료하지 못했습니다."
    )
    assert operations.calls == 0


def test_manager_mcp는_legacy_manager_provider를_store_읽기_전_막는다() -> None:
    operations = _ManagerOperations()
    deadlock_operations = _DeadlockOperations()
    store = _ManagerStore()
    legacy_provider = cast(
        "Any",
        lambda: ManagerPrincipal(org_id="org-1", subject_id="manager-1"),
    )
    server = create_question_mcp_server(
        application=_QuestionApplication(),
        principal_provider=lambda: RequesterPrincipal(org_id="org-1", subject_id="requester-1"),
        manager_operations=operations,
        deadlock_manager_operations=deadlock_operations,
        manager_store=store,
        manager_principal_provider=legacy_provider,
    )

    assert _call(server, "list_manager_items", {}) == "Manager 처리 요청을 완료하지 못했습니다."
    assert (
        _call(
            server,
            "act_manager_item",
            {"item_id": "item-1", "action": "dismiss"},
        )
        == "Manager 처리 요청을 완료하지 못했습니다."
    )
    assert operations.calls == 0
    assert deadlock_operations.calls == 0
    assert store.reads == 0


def test_governance_mcp_schema에는_actor_org_role_자기보고가_없다() -> None:
    operations = _ConflictOperations()
    manager_operations = _ManagerOperations()
    server = create_question_mcp_server(
        application=_QuestionApplication(),
        principal_provider=lambda: RequesterPrincipal(org_id="org-1", subject_id="requester-1"),
        conflict_operations=operations,
        conflict_principal_provider=cast(
            "Any", lambda: ApproverPrincipal(org_id="x", subject_id="y")
        ),
        manager_operations=manager_operations,
        deadlock_manager_operations=_DeadlockOperations(),
        manager_store=_ManagerStore(),
        manager_principal_provider=cast(
            "Any", lambda: ApproverPrincipal(org_id="x", subject_id="y")
        ),
    )

    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in (
        "list_conflicts",
        "get_conflict",
        "concur_conflict",
        "list_manager_items",
        "act_manager_item",
    ):
        properties = tools[name].inputSchema.get("properties", {})
        assert not {"actor", "org", "org_id", "role", "roles", "principal"} & set(properties)


def test_get_conflict는_다른_case_id_반환을_본문_없이_거부한다() -> None:
    class _WrongCaseOperations(_ConflictOperations):
        def document(
            self,
            case_id: str,
            principal: ConflictOperationsPrincipal,
        ) -> ConflictCase:
            del case_id, principal
            return ConflictCase(
                case_id="case-other",
                intent="refund",
                question="다른 조직의 질문 본문",
                candidates=(Candidate(agent_id="card-1", owner="owner-1"),),
                opened_at=datetime(2026, 7, 15, tzinfo=UTC),
                request_id="request-other",
            )

    server = create_question_mcp_server(
        application=_QuestionApplication(),
        principal_provider=lambda: RequesterPrincipal(org_id="org-1", subject_id="requester-1"),
        conflict_operations=_WrongCaseOperations(),
        conflict_principal_provider=lambda: AuthenticatedPrincipal(
            org_id="org-1",
            subject_id="owner-1",
            identity_provider="oidc",
            identity_session_id="session-1",
        ),
    )

    text = _call(server, "get_conflict", {"case_id": "case-1"})

    assert text == "다툼 처리 요청을 완료하지 못했습니다."
    assert "다른 조직의 질문 본문" not in text
