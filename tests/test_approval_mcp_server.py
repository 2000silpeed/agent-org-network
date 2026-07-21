"""P17.6b S5 MCP Approval 운영 어댑터 계약."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent_org_network.approval import (
    AnswerCandidate,
    ApprovalPendingSummary,
    ApproverPrincipal,
)
from agent_org_network.approval_operations import (
    ApprovalAnswered,
    ApprovalDeclined,
    ApprovalPendingDetail,
    ApprovalReassigned,
    ApproveIntent,
    ApproveWithEditIntent,
    ManualApprovalReassignmentTarget,
    RejectIntent,
)
from agent_org_network.mcp_server import ApprovalMcpOperations, create_question_mcp_server
from agent_org_network.p17_manager_disposition import TerminalPublished
from agent_org_network.question_resolution import AskQuestion, QuestionPrincipal, RequesterPrincipal
from agent_org_network.question_stream_execution import (
    PendingQuestionLookup,
    QuestionStreamLookup,
)

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
REQUESTER = RequesterPrincipal(org_id="org-1", subject_id="requester-1")
APPROVER = ApproverPrincipal(org_id="org-1", subject_id="approver-1")


def _call(server: Any, tool: str, arguments: dict[str, object]) -> tuple[str, str]:
    content, structured = asyncio.run(server.call_tool(tool, arguments))
    return content[0].text, str(structured)


class _QuestionApplication:
    def ask(self, command: AskQuestion) -> QuestionStreamLookup:
        return PendingQuestionLookup(
            request_id="request-question",
            kind="routing",
            state="received",
            retryable=True,
            message="internal-message",
        )

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
            message="internal-message",
        )


class _ApprovalOperations:
    def __init__(self) -> None:
        self.principals: list[ApproverPrincipal] = []
        self.decisions: list[tuple[str, object]] = []
        self.reassignments: list[tuple[str, object]] = []

    def pending_for(self, principal: ApproverPrincipal) -> list[ApprovalPendingSummary]:
        self.principals.append(principal)
        return [
            ApprovalPendingSummary(
                item_id="approval-1",
                request_id="request-1",
                approval_round=1,
                assigned_at=NOW,
                due_at=NOW + timedelta(hours=1),
            )
        ]

    def detail(
        self,
        item_id: str,
        principal: ApproverPrincipal,
    ) -> ApprovalPendingDetail:
        assert item_id == "approval-1"
        self.principals.append(principal)
        return ApprovalPendingDetail(
            item_id="approval-1",
            request_id="request-1",
            approval_round=1,
            assigned_at=NOW,
            due_at=NOW + timedelta(hours=1),
            question="환불은 언제 되나요?",
            draft_id="draft-secret",
            candidate=AnswerCandidate(
                text="영업일 3일 안에 처리됩니다.",
                sources=("private-source.md",),
                mode="draft_only",
            ),
        )

    def decide(
        self,
        item_id: str,
        principal: ApproverPrincipal,
        intent: object,
    ) -> ApprovalAnswered | ApprovalDeclined:
        self.principals.append(principal)
        self.decisions.append((item_id, intent))
        if isinstance(intent, RejectIntent):
            return ApprovalDeclined(
                item_id="approval-1",
                approval_round=1,
                request_id="request-1",
                reason_code=intent.reason_code,
                delivery=TerminalPublished(),
            )
        action = "approve_with_edit" if isinstance(intent, ApproveWithEditIntent) else "approve"
        return ApprovalAnswered(
            item_id="approval-1",
            approval_round=1,
            request_id="request-1",
            record_id="record-1",
            action=action,
            delivery=TerminalPublished(),
        )

    def reassign(
        self,
        item_id: str,
        principal: ApproverPrincipal,
        target: object,
    ) -> ApprovalReassigned:
        self.principals.append(principal)
        self.reassignments.append((item_id, target))
        return ApprovalReassigned(
            predecessor_item_id="approval-1",
            successor_item_id="approval-2",
            request_id="request-1",
            approval_round=2,
            due_at=NOW + timedelta(hours=2),
            reason="reassigned",
        )


def _server(
    operations: ApprovalMcpOperations | None = None,
    provider: Callable[[], ApproverPrincipal] | None = None,
):
    return create_question_mcp_server(
        application=_QuestionApplication(),
        principal_provider=lambda: REQUESTER,
        approval_operations=operations,
        approver_principal_provider=provider,
    )


def test_provider가_없으면_operations가_있어도_기존_두_도구만_유지한다() -> None:
    tools = {tool.name for tool in asyncio.run(_server(_ApprovalOperations()).list_tools())}

    assert tools == {"ask_org", "get_question"}


def test_provider가_있으면_operations도_필수다() -> None:
    with pytest.raises(ValueError):
        _server(provider=lambda: APPROVER)


def test_non_callable_provider는_승인_도구를_등록하기_전에_조립을_거부한다() -> None:
    non_callable_provider: Any = object()

    with pytest.raises(ValueError, match="callable"):
        create_question_mcp_server(
            application=_QuestionApplication(),
            principal_provider=lambda: REQUESTER,
            approval_operations=_ApprovalOperations(),
            approver_principal_provider=non_callable_provider,
        )


def test_승인_MCP_여섯_도구의_schema에는_신원_자기보고가_없다() -> None:
    provider_calls = 0

    def provider() -> ApproverPrincipal:
        nonlocal provider_calls
        provider_calls += 1
        return APPROVER

    server = _server(_ApprovalOperations(), provider)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert provider_calls == 0
    assert set(tools) == {
        "ask_org",
        "get_question",
        "list_approvals",
        "get_approval",
        "approve",
        "approve_with_edit",
        "reject",
        "reassign_approval",
    }
    assert set(tools["list_approvals"].inputSchema["properties"]) == set()
    assert set(tools["get_approval"].inputSchema["properties"]) == {"item_id"}
    assert set(tools["approve"].inputSchema["properties"]) == {"item_id"}
    assert set(tools["approve_with_edit"].inputSchema["properties"]) == {
        "item_id",
        "edited_text",
    }
    assert set(tools["reject"].inputSchema["properties"]) == {"item_id", "reason_code"}
    assert set(tools["reassign_approval"].inputSchema["properties"]) == {
        "item_id",
        "approver_id",
    }
    for tool in tools.values():
        properties = tool.inputSchema.get("properties", {})
        for forbidden in ("org", "org_id", "actor", "user", "user_id", "principal"):
            assert forbidden not in properties


def test_목록은_본문없이_요약만_노출하고_상세만_질문과_후보를_노출한다() -> None:
    server = _server(_ApprovalOperations(), lambda: APPROVER)

    listing, listing_structured = _call(server, "list_approvals", {})
    detail, detail_structured = _call(server, "get_approval", {"item_id": "approval-1"})

    assert "approval-1" in listing
    assert "request-1" in listing
    assert "환불은 언제 되나요?" not in listing
    assert "영업일 3일 안에 처리됩니다." not in listing
    assert "private-source.md" not in listing
    assert "draft-secret" not in listing
    assert "환불은 언제 되나요?" not in listing_structured
    assert "영업일 3일 안에 처리됩니다." not in listing_structured

    assert "approval-1" in detail
    assert "request-1" in detail
    assert "환불은 언제 되나요?" in detail
    assert "영업일 3일 안에 처리됩니다." in detail
    assert "draft_only" in detail
    assert "draft-secret" not in detail
    assert "private-source.md" not in detail
    assert "환불은 언제 되나요?" in detail_structured


def test_처분_도구는_server_side_principal의_strict_copy와_actor_free_intent만_넘긴다() -> None:
    operations = _ApprovalOperations()
    server = _server(operations, lambda: APPROVER)

    approve_text, approve_structured = _call(server, "approve", {"item_id": "approval-1"})
    edited_text, edited_structured = _call(
        server,
        "approve_with_edit",
        {"item_id": "approval-1", "edited_text": "수정 후보 secret"},
    )
    reject_text, reject_structured = _call(
        server,
        "reject",
        {"item_id": "approval-1", "reason_code": "reject-secret"},
    )
    reassign_text, reassign_structured = _call(
        server,
        "reassign_approval",
        {"item_id": "approval-1", "approver_id": "next-approver-secret"},
    )

    assert operations.decisions == [
        ("approval-1", ApproveIntent()),
        ("approval-1", ApproveWithEditIntent(edited_text="수정 후보 secret")),
        ("approval-1", RejectIntent(reason_code="reject-secret")),
    ]
    assert operations.reassignments == [
        (
            "approval-1",
            ManualApprovalReassignmentTarget(approver_id="next-approver-secret"),
        )
    ]
    assert operations.principals
    assert all(
        principal == APPROVER and principal is not APPROVER for principal in operations.principals
    )
    rendered = (
        approve_text,
        approve_structured,
        edited_text,
        edited_structured,
        reject_text,
        reject_structured,
        reassign_text,
        reassign_structured,
    )
    for value in rendered:
        assert "수정 후보 secret" not in value
        assert "reject-secret" not in value
        assert "next-approver-secret" not in value
        assert "영업일 3일 안에 처리됩니다." not in value
    assert "record-1" in approve_text
    assert "record-1" in edited_text
    assert "반려 처리가 완료되었습니다." in reject_text


def test_목록과_상세도_server_side_principal의_strict_copy만_쓴다() -> None:
    operations = _ApprovalOperations()
    server = _server(operations, lambda: APPROVER)

    _call(server, "list_approvals", {})
    _call(server, "get_approval", {"item_id": "approval-1"})

    assert operations.principals == [APPROVER, APPROVER]
    assert all(principal is not APPROVER for principal in operations.principals)


def test_get_approval은_다른_item_id_반환을_본문_없이_거부한다() -> None:
    class _WrongItemOperations(_ApprovalOperations):
        def detail(
            self,
            item_id: str,
            principal: ApproverPrincipal,
        ) -> ApprovalPendingDetail:
            del item_id, principal
            return ApprovalPendingDetail(
                item_id="approval-other",
                request_id="request-other",
                approval_round=1,
                assigned_at=NOW,
                due_at=NOW + timedelta(hours=1),
                question="다른 승인 항목의 질문 본문",
                draft_id="draft-other",
                candidate=AnswerCandidate(
                    text="다른 승인 항목의 후보 답변",
                    sources=(),
                    mode="draft_only",
                ),
            )

    text, structured = _call(
        _server(_WrongItemOperations(), lambda: APPROVER),
        "get_approval",
        {"item_id": "approval-1"},
    )

    assert text == "승인 항목을 찾을 수 없거나 조회하지 못했습니다."
    assert "다른 승인 항목" not in text
    assert "다른 승인 항목" not in structured


def test_ApproverPrincipal_subclass는_operations_호출_전에_거부한다() -> None:
    class _ForgedPrincipal(ApproverPrincipal):
        pass

    operations = _ApprovalOperations()
    server = _server(
        operations,
        lambda: _ForgedPrincipal(org_id="org-secret", subject_id="actor-secret"),
    )

    text, structured = _call(server, "list_approvals", {})

    assert operations.principals == []
    assert "org-secret" not in text
    assert "actor-secret" not in text
    assert "org-secret" not in structured
    assert "actor-secret" not in structured


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_approval", {"item_id": "item-secret"}),
        ("approve", {"item_id": "item-secret"}),
        (
            "approve_with_edit",
            {"item_id": "item-secret", "edited_text": "edited-secret"},
        ),
        ("reject", {"item_id": "item-secret", "reason_code": "reason-secret"}),
        (
            "reassign_approval",
            {"item_id": "item-secret", "approver_id": "approver-secret"},
        ),
    ],
)
def test_승인_도구_장애는_예외와_입력값을_반사하지_않는다(
    tool: str,
    arguments: dict[str, object],
) -> None:
    class _FailingOperations(_ApprovalOperations):
        def detail(self, item_id: str, principal: ApproverPrincipal) -> ApprovalPendingDetail:
            raise RuntimeError(f"exception-secret {item_id} {principal.subject_id}")

        def decide(
            self,
            item_id: str,
            principal: ApproverPrincipal,
            intent: object,
        ) -> ApprovalAnswered:
            raise RuntimeError(f"exception-secret {item_id} {principal.subject_id} {intent!r}")

        def reassign(
            self,
            item_id: str,
            principal: ApproverPrincipal,
            target: object,
        ) -> ApprovalReassigned:
            raise RuntimeError(f"exception-secret {item_id} {principal.subject_id} {target!r}")

    text, structured = _call(
        _server(_FailingOperations(), lambda: APPROVER),
        tool,
        arguments,
    )

    reflected = " ".join(str(value) for value in arguments.values())
    for secret in ("exception-secret", "approver-1", *reflected.split()):
        assert secret not in text
        assert secret not in structured


def test_provider_예외도_세부를_반사하지_않는다() -> None:
    def broken_provider() -> ApproverPrincipal:
        raise RuntimeError("provider-secret actor-secret")

    text, structured = _call(
        _server(_ApprovalOperations(), broken_provider),
        "list_approvals",
        {},
    )

    assert "provider-secret" not in text
    assert "actor-secret" not in text
    assert "provider-secret" not in structured
    assert "actor-secret" not in structured


def test_목록_operations_예외도_세부를_반사하지_않는다() -> None:
    class _FailingListOperations(_ApprovalOperations):
        def pending_for(self, principal: ApproverPrincipal) -> list[ApprovalPendingSummary]:
            raise RuntimeError(f"list-secret {principal.org_id} {principal.subject_id}")

    text, structured = _call(
        _server(_FailingListOperations(), lambda: APPROVER),
        "list_approvals",
        {},
    )

    assert text == "승인 처리함을 불러오지 못했습니다."
    for secret in ("list-secret", "org-1", "approver-1"):
        assert secret not in text
        assert secret not in structured
