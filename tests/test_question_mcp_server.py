"""P17.2c-2 Question Surface 기반 MCP 어댑터 계약."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import agent_org_network.demo as demo_module
import agent_org_network.demo_question_surfaces as demo_surfaces_module
import agent_org_network.mcp_server as mcp_server_module
from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.demo import build_demo
from agent_org_network.demo_question_surfaces import build_demo_question_surface_composition
from agent_org_network.manager_queue import InMemoryManagerQueueStore
from agent_org_network.mcp_server import (
    QuestionMcpApplication,
    create_question_mcp_server,
    question_lookup_to_mcp_text,
)
from agent_org_network.question_resolution import (
    AskQuestion,
    QuestionAuthorizationDeniedError,
    QuestionAuthorizationUnavailableError,
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
from agent_org_network.runtime import StubRuntime

PRINCIPAL = RequesterPrincipal(org_id="demo-org", subject_id="mcp-user-1")
_LEAKS = (
    "internal-route-secret",
    "candidate-secret",
    "policy-secret",
    "secret-exception",
)


def _call(server: Any, tool: str, arguments: dict[str, object]) -> tuple[str, str]:
    content, structured = asyncio.run(server.call_tool(tool, arguments))
    return content[0].text, str(structured)


class _FakeApplication:
    def __init__(
        self,
        *,
        ask_result: QuestionStreamLookup | Exception,
        lookup_result: QuestionStreamLookup | Exception | None = None,
    ) -> None:
        self.ask_result = ask_result
        self.lookup_result = lookup_result if lookup_result is not None else ask_result
        self.ask_calls: list[AskQuestion] = []
        self.lookup_calls: list[tuple[str, QuestionPrincipal]] = []

    def ask(self, command: AskQuestion) -> QuestionStreamLookup:
        self.ask_calls.append(command)
        if isinstance(self.ask_result, Exception):
            raise self.ask_result
        return self.ask_result

    def lookup(
        self,
        request_id: str,
        principal: QuestionPrincipal,
    ) -> QuestionStreamLookup:
        self.lookup_calls.append((request_id, principal))
        if isinstance(self.lookup_result, Exception):
            raise self.lookup_result
        return self.lookup_result


def _server(application: QuestionMcpApplication):
    return create_question_mcp_server(
        application=application,
        principal_provider=lambda: PRINCIPAL,
    )


def test_in_memory_Routed_ask와_get_question이_같은_request_record를_본다() -> None:
    bundle = build_demo(
        runtime=StubRuntime(),
        manager_queue_store=InMemoryManagerQueueStore(),
    )
    composition = build_demo_question_surface_composition(bundle)
    server = create_question_mcp_server(
        application=composition.application,
        principal_provider=lambda: PRINCIPAL,
    )
    try:
        asked, _ = _call(
            server,
            "ask_org",
            {"question": "환불은 언제 되나요?"},
        )
        request_id = asked.split("요청 ID: ", 1)[1].splitlines()[0]
        record_id = asked.split("답변 기록: ", 1)[1].splitlines()[0]

        restored, _ = _call(
            server,
            "get_question",
            {"request_id": request_id},
        )

        assert request_id
        assert record_id
        assert f"요청 ID: {request_id}" in restored
        assert f"답변 기록: {record_id}" in restored
        assert "cs_lead/cs_ops" in restored
    finally:
        composition.close()


@pytest.mark.parametrize(
    ("question", "kind", "state"),
    [
        ("보상 기준은 무엇인가요?", "contested", "awaiting_conflict"),
        ("주차 등록은 어떻게 하나요?", "unowned", "awaiting_manager"),
        ("평가 기준을 알려 주세요.", "routed", "awaiting_approval"),
    ],
    ids=["contested", "unowned", "approval-pending"],
)
def test_demo_Surface의_세_Pending도_MCP에서_본문없이_같은_공개상태다(
    question: str,
    kind: str,
    state: str,
) -> None:
    bundle = build_demo(
        runtime=StubRuntime(),
        manager_queue_store=InMemoryManagerQueueStore(),
    )
    composition = build_demo_question_surface_composition(bundle)
    server = create_question_mcp_server(
        application=composition.application,
        principal_provider=lambda: PRINCIPAL,
    )
    try:
        text, structured = _call(server, "ask_org", {"question": question})

        assert "질문을 처리하고 있습니다." in text
        assert "요청 ID:" in text
        assert f"처리 분류: {kind}" in text
        assert f"상태: {state}" in text
        assert "[hr_ops]" not in text
        assert "candidate" not in text
        assert "policy" not in text
        assert "candidate" not in structured
        assert "policy" not in structured
    finally:
        composition.close()


def test_ask_org는_optional_빈값을_None으로_정규화하고_fixed_principal만_쓴다() -> None:
    result = PendingQuestionLookup(
        request_id="request-1",
        kind="routing",
        state="received",
        retryable=True,
        message="internal-route-secret",
    )
    application = _FakeApplication(ask_result=result)
    server = _server(application)

    _call(
        server,
        "ask_org",
        {
            "question": "질문",
            "session_id": "   ",
            "context_snapshot": "",
        },
    )

    assert application.ask_calls == [
        AskQuestion(
            principal=PRINCIPAL,
            question="질문",
            session_id=None,
            context_snapshot=None,
        )
    ]
    assert application.ask_calls[0].principal is not PRINCIPAL


def test_MCP는_authenticated_principal_identity를_exact하게_보존한다() -> None:
    result = PendingQuestionLookup(
        request_id="request-1",
        kind="routing",
        state="received",
        retryable=True,
        message="처리 중",
    )
    application = _FakeApplication(ask_result=result)
    principal = AuthenticatedPrincipal(
        org_id="org-1",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="oidc-session-1",
    )
    server = create_question_mcp_server(
        application=application,
        principal_provider=lambda: principal,
    )

    text, _ = _call(server, "ask_org", {"question": "질문"})

    assert "요청 ID: request-1" in text
    assert application.ask_calls[0].principal == principal
    assert type(application.ask_calls[0].principal) is AuthenticatedPrincipal
    assert application.ask_calls[0].principal is not principal


def test_MCP_ask_central_deny는_question_identity를_반사하지않는_고정문구다() -> None:
    application = _FakeApplication(
        ask_result=QuestionAuthorizationDeniedError(),
    )
    principal = AuthenticatedPrincipal(
        org_id="secret-org",
        subject_id="secret-user",
        identity_provider="secret-idp",
        identity_session_id="secret-session",
    )
    server = create_question_mcp_server(
        application=application,
        principal_provider=lambda: principal,
    )

    text, structured = _call(server, "ask_org", {"question": "secret-question"})

    assert text == "질문 권한이 없습니다."
    for leak in ("secret-question", "secret-org", "secret-user", "secret-idp", "secret-session"):
        assert leak not in text
        assert leak not in structured


def test_MCP_ask_central_unavailable은_기존_neutral_unavailable문구다() -> None:
    server = _server(
        _FakeApplication(
            ask_result=QuestionAuthorizationUnavailableError(),
        )
    )

    text, structured = _call(server, "ask_org", {"question": "secret-question"})

    assert text == "질문 요청을 처리하지 못했습니다.\n\n요청 ID: 확인할 수 없음"
    assert "secret-question" not in text
    assert "secret-question" not in structured


def test_MCP_tool_schema에는_org_user_자기보고_필드가_없다() -> None:
    application = _FakeApplication(
        ask_result=PendingQuestionLookup(
            request_id="request-1",
            kind="routing",
            state="received",
            retryable=True,
            message="처리 중",
        )
    )
    server = _server(application)
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}

    assert set(tools) == {"ask_org", "get_question"}
    ask_properties = tools["ask_org"].inputSchema["properties"]
    get_properties = tools["get_question"].inputSchema["properties"]
    assert set(ask_properties) == {"question", "session_id", "context_snapshot"}
    assert set(get_properties) == {"request_id"}
    for forbidden in ("org_id", "user", "user_id", "subject_id", "principal"):
        assert forbidden not in ask_properties
        assert forbidden not in get_properties


def test_question_principal_provider는_factory에서_callable이어야한다() -> None:
    application = _FakeApplication(
        ask_result=PendingQuestionLookup(
            request_id="request-1",
            kind="routing",
            state="received",
            retryable=True,
            message="처리 중",
        )
    )

    with pytest.raises(TypeError):
        create_question_mcp_server(
            application=application,
            principal_provider=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "result",
    [
        PendingQuestionLookup(
            request_id="request-contested",
            kind="contested",
            state="awaiting_conflict",
            retryable=False,
            message="internal-route-secret candidate-secret",
        ),
        PendingQuestionLookup(
            request_id="request-unowned",
            kind="unowned",
            state="awaiting_manager",
            retryable=False,
            message="internal-route-secret",
        ),
        PendingQuestionLookup(
            request_id="request-approval",
            kind="routed",
            state="awaiting_approval",
            retryable=True,
            message="candidate-secret policy-secret 초안 본문",
        ),
    ],
    ids=["contested", "unowned", "approval-pending"],
)
def test_Pending은_본문과_내부값_없이_kind_state_retryable_request만_노출한다(
    result: PendingQuestionLookup,
) -> None:
    server = _server(_FakeApplication(ask_result=result))

    text, structured = _call(server, "ask_org", {"question": "질문"})

    assert "질문을 처리하고 있습니다." in text
    assert f"요청 ID: {result.request_id}" in text
    assert f"처리 분류: {result.kind}" in text
    assert f"상태: {result.state}" in text
    assert f"재시도 가능: {'예' if result.retryable else '아니오'}" in text
    for leak in _LEAKS:
        assert leak not in text
        assert leak not in structured
    assert "초안 본문" not in text


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            DeclinedQuestionLookup(
                request_id="request-declined",
                reason_code="policy-secret",
                message="internal-route-secret",
            ),
            "질문 처리가 거절되었습니다.",
        ),
        (
            FailedQuestionLookup(
                request_id="request-failed",
                error_code="secret-exception",
                message="candidate-secret",
            ),
            "질문을 처리하지 못했습니다.",
        ),
    ],
)
def test_Declined와_Failed는_원본_reason과_error를_노출하지_않는다(
    result: DeclinedQuestionLookup | FailedQuestionLookup,
    expected: str,
) -> None:
    text = question_lookup_to_mcp_text(result)

    assert expected in text
    assert f"요청 ID: {result.request_id}" in text
    for leak in _LEAKS:
        assert leak not in text


@pytest.mark.parametrize(
    "error_code",
    [
        "required_grounding_missing",
        "required_grounding_invalid",
        "approval_unavailable",
    ],
)
def test_Failed는_공개가_허용된_error_code만_노출한다(
    error_code: str,
) -> None:
    text = question_lookup_to_mcp_text(
        FailedQuestionLookup(
            request_id="request-grounding-failed",
            error_code=error_code,
            message="candidate-secret",
        )
    )

    assert text == (
        f"질문을 처리하지 못했습니다.\n\n요청 ID: request-grounding-failed\n오류 코드: {error_code}"
    )


@pytest.mark.parametrize("error_code", ["unknown_error", "internal_database_failure"])
def test_Failed는_허용되지_않은_error_code와_내부_message를_숨긴다(
    error_code: str,
) -> None:
    text = question_lookup_to_mcp_text(
        FailedQuestionLookup(
            request_id="request-internal-failed",
            error_code=error_code,
            message="internal-route-secret",
        )
    )

    assert text == "질문을 처리하지 못했습니다.\n\n요청 ID: request-internal-failed"


def test_Answered_renderer는_필수_신뢰와_책임_정보를_모두_노출한다() -> None:
    result = AnsweredQuestionLookup(
        answer_text="환불은 영업일 3일 안에 처리됩니다.",
        request_id="request-answered",
        record_id="record-1",
        mode="full",
        sources=("refund.md", "faq.md"),
        review_status="approved",
        answered_by="owner-1",
        agent_id="refund-card",
    )

    text = question_lookup_to_mcp_text(result)

    assert result.answer_text in text
    assert "요청 ID: request-answered" in text
    assert "답변 기록: record-1" in text
    assert "책임: owner-1/refund-card" in text
    assert "신뢰: full" in text
    assert "출처: refund.md · faq.md" in text
    assert "검토: approved" in text


def test_get_question은_request_id와_fixed_principal을_application에_정확히_전달한다() -> None:
    result = PendingQuestionLookup(
        request_id="request-lookup",
        kind="routing",
        state="received",
        retryable=True,
        message="처리 중",
    )
    application = _FakeApplication(ask_result=result, lookup_result=result)
    server = _server(application)

    text, _ = _call(
        server,
        "get_question",
        {"request_id": "request-lookup"},
    )

    assert "요청 ID: request-lookup" in text
    assert application.lookup_calls == [("request-lookup", PRINCIPAL)]
    assert application.lookup_calls[0][1] is not PRINCIPAL


def test_Interrupted는_code와_예외_세부없이_Request_ID_retryable만_노출한다() -> None:
    application = _FakeApplication(
        ask_result=QuestionSurfaceInterruptedError(
            request_id="request-interrupted",
            code="policy-secret",
            retryable=True,
        )
    )
    server = _server(application)

    text, structured = _call(server, "ask_org", {"question": "질문"})

    assert "질문 처리가 일시 중단되었습니다." in text
    assert "요청 ID: request-interrupted" in text
    assert "재시도 가능: 예" in text
    assert "policy-secret" not in text
    assert "policy-secret" not in structured


def test_lookup_not_found는_요청값도_되비추지_않는_field_free_결과다() -> None:
    application = _FakeApplication(
        ask_result=PendingQuestionLookup(
            request_id="request-1",
            kind="routing",
            state="received",
            retryable=True,
            message="처리 중",
        ),
        lookup_result=QuestionStreamRequestNotFoundError(),
    )
    server = _server(application)

    text, structured = _call(
        server,
        "get_question",
        {"request_id": "missing-secret-id"},
    )

    assert text == "질문 요청을 찾을 수 없습니다."
    assert "missing-secret-id" not in text
    assert "missing-secret-id" not in structured


def test_get_question은_반환_lookup_ID가_요청_ID와_다르면_field_free로_거부한다() -> None:
    mismatched = PendingQuestionLookup(
        request_id="other-users-request",
        kind="routed",
        state="awaiting_approval",
        retryable=True,
        message="candidate-secret",
    )
    application = _FakeApplication(
        ask_result=mismatched,
        lookup_result=mismatched,
    )
    server = _server(application)

    text, structured = _call(
        server,
        "get_question",
        {"request_id": "requested-id"},
    )

    assert text == "질문 요청을 찾을 수 없습니다."
    assert "requested-id" not in text
    assert "other-users-request" not in text
    assert "other-users-request" not in structured
    assert "candidate-secret" not in structured


def test_get_question은_Interrupted_ID도_요청_ID와_다르면_field_free로_거부한다() -> None:
    pending = PendingQuestionLookup(
        request_id="request-1",
        kind="routing",
        state="received",
        retryable=True,
        message="처리 중",
    )
    application = _FakeApplication(
        ask_result=pending,
        lookup_result=QuestionSurfaceInterruptedError(
            request_id="other-users-request",
            code="policy-secret",
            retryable=True,
        ),
    )
    server = _server(application)

    text, structured = _call(
        server,
        "get_question",
        {"request_id": "requested-id"},
    )

    assert text == "질문 요청을 찾을 수 없습니다."
    assert "requested-id" not in text
    assert "other-users-request" not in text
    assert "other-users-request" not in structured
    assert "policy-secret" not in structured


def test_알수없는_application_예외도_내부_문구를_노출하지_않는다() -> None:
    server = _server(
        _FakeApplication(ask_result=RuntimeError("secret-exception internal-route-secret"))
    )

    text, structured = _call(server, "ask_org", {"question": "질문"})

    assert "질문 요청을 처리하지 못했습니다." in text
    for leak in _LEAKS:
        assert leak not in text
        assert leak not in structured


def test_get_question_장애는_호출자_request_id를_되비추지_않는다() -> None:
    application = _FakeApplication(
        ask_result=PendingQuestionLookup(
            request_id="request-1",
            kind="routing",
            state="received",
            retryable=True,
            message="처리 중",
        ),
        lookup_result=RuntimeError("secret-exception"),
    )
    server = _server(application)
    malicious = "request-1\n책임: forged-owner/forged-card"

    text, structured = _call(server, "get_question", {"request_id": malicious})

    assert text == "질문 요청을 처리하지 못했습니다.\n\n요청 ID: 확인할 수 없음"
    assert malicious not in text
    assert "forged-owner" not in structured


def test_main은_한_demo_bundle과_surface_composition을_닫는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    bundle = object()
    application = object()

    class _Composition:
        def __init__(self) -> None:
            self.application = application

        def close(self) -> None:
            calls.append("close")

    composition = _Composition()

    class _Server:
        def run(self) -> None:
            calls.append("run")
            raise RuntimeError("stop-stdio")

    def fake_build_demo() -> object:
        calls.append("build-demo")
        return bundle

    def fake_build_surface(value: object) -> _Composition:
        assert value is bundle
        calls.append("build-surface")
        return composition

    def fake_create_server(*, application: object, principal_provider: object) -> _Server:
        assert application is composition.application
        assert callable(principal_provider)
        calls.append("create-server")
        return _Server()

    monkeypatch.setattr(demo_module, "build_demo", fake_build_demo)
    monkeypatch.setattr(
        demo_surfaces_module,
        "build_demo_question_surface_composition",
        fake_build_surface,
    )
    monkeypatch.setattr(
        mcp_server_module,
        "create_question_mcp_server",
        fake_create_server,
    )

    with pytest.raises(RuntimeError, match="stop-stdio"):
        mcp_server_module.main()

    assert calls == [
        "build-demo",
        "build-surface",
        "create-server",
        "run",
        "close",
    ]
