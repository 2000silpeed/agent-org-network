"""Deterministic official-SDK contract tests for ADR 0074."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, cast

import httpx
import pytest

from agent_org_network.a2a_remote_runtime import (
    A2ACompletedText,
    A2AProtocolViolation,
    A2ARemoteRejected,
    A2ARemoteUnavailable,
    A2ARemoteRuntimeProfile,
)
from agent_org_network.a2a_sdk_adapter import A2ASdkInvocationAdapter, OobBearerCredential
from agent_org_network.owner_device_key_store import OwnerInstallationPublicBindingV1


_SECRET = "never-render-this-secret"
_ORIGIN = "https://a2a.example.com"
_ENDPOINT = f"{_ORIGIN}/a2a/v1"
_TS = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)


class _CredentialProvider:
    def resolve_bearer(self, credential_ref: str) -> OobBearerCredential | None:
        assert credential_ref == "keychain:a2a-remote"
        return OobBearerCredential(_SECRET)


class _PublicDns:
    def resolve(self, hostname: str) -> tuple[str, ...]:
        assert hostname == "a2a.example.com"
        return ("93.184.216.34",)


class _PrivateDns:
    def resolve(self, hostname: str) -> tuple[str, ...]:
        del hostname
        return ("127.0.0.1",)


class _Ipv6Dns:
    def resolve(self, hostname: str) -> tuple[str, ...]:
        assert hostname == "2606:2800:220:1:248:1893:25c8:1946"
        return ("2606:2800:220:1:248:1893:25c8:1946",)


def _card(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "name": "remote",
        "description": "strict test remote",
        "version": "1",
        "capabilities": {"streaming": False},
        "supportedInterfaces": [
            {
                "url": _ENDPOINT,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ],
        "securitySchemes": {"owner_bearer": {"httpAuthSecurityScheme": {"scheme": "bearer"}}},
        "securityRequirements": [{"schemes": {"owner_bearer": {}}}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }
    result.update(changes)
    return result


def _digest(card: dict[str, object]) -> str:
    canonical = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _profile(card: dict[str, object], **changes: object) -> A2ARemoteRuntimeProfile:
    values: dict[str, object] = {
        "binding": OwnerInstallationPublicBindingV1(
            central_origin="https://central.example.com",
            org_id="support",
            owner_user_id="owner_lee",
            agent_card_id="refund_runtime",
            agent_card_revision=4,
            agent_card_digest="a" * 64,
            device_key_thumbprint="A" * 43,
        ),
        "service_endpoint": _ENDPOINT,
        "remote_card_sha256": _digest(card),
        "credential_ref": "keychain:a2a-remote",
    }
    values.update(changes)
    return A2ARemoteRuntimeProfile.model_validate(values)


def _completed(*parts: dict[str, object], state: str = "TASK_STATE_COMPLETED") -> dict[str, object]:
    return {
        "task": {
            "id": "remote-task-id-must-not-leak",
            "contextId": "remote-context-id-must-not-leak",
            "status": {"state": state},
            "artifacts": [{"artifactId": "completed-answer", "parts": list(parts)}],
        }
    }


def _adapter(
    handler: httpx.MockTransport,
    *,
    resolver: _PublicDns | _PrivateDns | None = None,
) -> A2ASdkInvocationAdapter:
    return A2ASdkInvocationAdapter.for_test(
        credentials=_CredentialProvider(),
        resolver=resolver or _PublicDns(),
        transport=handler,
    )


def test_official_sdk_fetches_pinned_card_then_authenticated_rest_message_and_maps_ordered_text() -> None:
    card = _card()
    observed: list[tuple[str, str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            (
                request.method,
                str(request.url),
                request.headers.get("authorization"),
                request.headers.get("a2a-version"),
            )
        )
        if request.method == "GET":
            return httpx.Response(200, json=card)
        assert request.method == "POST"
        assert request.url == httpx.URL(f"{_ENDPOINT}/message:send")
        body: dict[str, Any] = json.loads(request.content)
        assert body["message"]["parts"] == [{"text": "환불 기한은?\n\n지난주 구매"}]
        assert body["configuration"]["acceptedOutputModes"] == ["text/plain"]
        assert body["configuration"].get("returnImmediately", False) is False
        return httpx.Response(200, json=_completed({"text": "7일"}, {"text": "입니다."}))

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="환불 기한은?", context="지난주 구매"
    )

    assert outcome == A2ACompletedText(text="7일입니다.")
    assert observed == [
        ("GET", f"{_ORIGIN}/.well-known/agent-card.json", None, "1.0"),
        ("POST", f"{_ENDPOINT}/message:send", f"Bearer {_SECRET}", "1.0"),
    ]


@pytest.mark.parametrize(
    ("card", "profile_changes"),
    [
        (_card(), {"remote_card_sha256": "b" * 64}),
        (_card(supportedInterfaces=[]), {}),
        (
            _card(
                supportedInterfaces=[
                    {
                        "url": _ENDPOINT,
                        "protocolBinding": "HTTP+JSON",
                        "protocolVersion": "0.3",
                    }
                ]
            ),
            {},
        ),
        (_card(securityRequirements=[]), {}),
        (_card(securitySchemes={"owner_bearer": {"httpAuthSecurityScheme": {"scheme": "basic"}}}), {}),
        (_card(defaultInputModes=["application/json"]), {}),
        (_card(defaultOutputModes=["application/json"]), {}),
        (
            _card(
                supportedInterfaces=[
                    {
                        "url": _ENDPOINT,
                        "protocolBinding": "HTTP+JSON",
                        "protocolVersion": "1.0",
                        "tenant": "remote-selected-tenant",
                    }
                ]
            ),
            {},
        ),
    ],
)
def test_card_digest_interface_version_and_auth_drift_fail_closed_before_credential_send(
    card: dict[str, object], profile_changes: dict[str, object]
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=card)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card, **profile_changes), question="질문", context=None
    )

    assert isinstance(outcome, A2AProtocolViolation)
    assert [request.method for request in calls] == ["GET"]
    assert _SECRET not in f"{outcome!r} {outcome!s}"


@pytest.mark.parametrize(
    "card",
    [
        _card(unexpectedV1Field="must fail"),
        _card(url=_ENDPOINT, preferredTransport="HTTP+JSON"),
    ],
)
def test_unknown_or_legacy_0_3_card_fields_fail_strict_protobuf_parse(
    card: dict[str, object],
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=card)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert outcome == A2AProtocolViolation()
    assert calls == ["GET"]


def test_redirect_and_private_dns_are_redacted_unavailable_without_credential_send() -> None:
    card = _card()
    calls: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.example.com/card"})

    redirected = _adapter(httpx.MockTransport(redirect)).invoke(
        profile=_profile(card), question="질문", context=None
    )
    private = _adapter(httpx.MockTransport(redirect), resolver=_PrivateDns()).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert isinstance(redirected, A2AProtocolViolation)
    assert isinstance(private, A2ARemoteUnavailable)
    assert [request.method for request in calls] == ["GET"]
    assert _SECRET not in f"{redirected!r} {private!r}"


@pytest.mark.parametrize("status_code", [300, 304, 307])
def test_any_card_redirect_status_is_rejected_even_without_location(status_code: int) -> None:
    card = _card()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code, json=card)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert outcome == A2AProtocolViolation()
    assert _SECRET not in f"{outcome!r} {outcome!s}"


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/jsonish"])
def test_card_requires_application_json_content_type(content_type: str | None) -> None:
    card = _card()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        headers = {} if content_type is None else {"content-type": content_type}
        return httpx.Response(200, content=json.dumps(card).encode(), headers=headers)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert outcome == A2AProtocolViolation()


def test_ipv6_service_path_keeps_bracketed_same_origin_card_and_pinned_post_urls() -> None:
    origin = "https://[2606:2800:220:1:248:1893:25c8:1946]"
    endpoint = f"{origin}/a2a/v1"
    card = _card(
        supportedInterfaces=[
            {
                "url": endpoint,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ]
    )
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json=card if request.method == "GET" else _completed({"text": "ok"}))

    adapter = A2ASdkInvocationAdapter.for_test(
        credentials=_CredentialProvider(),
        resolver=_Ipv6Dns(),
        transport=httpx.MockTransport(handler),
    )
    outcome = adapter.invoke(
        profile=_profile(card, service_endpoint=endpoint),
        question="질문",
        context=None,
    )

    assert outcome == A2ACompletedText(text="ok")
    assert urls == [f"{origin}/.well-known/agent-card.json", f"{endpoint}/message:send"]


_NONTERMINAL: dict[str, object] = _completed({"text": "still working"}, state="TASK_STATE_WORKING")
_RAW_PART: dict[str, object] = _completed({"raw": "not-text"})
_MULTIPLE_ARTIFACTS: dict[str, object] = {
    "task": {
        "id": "remote-task-id-must-not-leak",
        "contextId": "remote-context-id-must-not-leak",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [
            {"artifactId": "first", "parts": [{"text": "fine"}]},
            {"artifactId": "second", "parts": [{"text": "also"}]},
        ],
    }
}


@pytest.mark.parametrize("response", [_NONTERMINAL, _RAW_PART, _MULTIPLE_ARTIFACTS])
def test_nonterminal_artifact_or_nontext_result_is_sealed_protocol_violation(
    response: dict[str, object],
) -> None:
    card = _card()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=card if request.method == "GET" else response)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert isinstance(outcome, A2AProtocolViolation)
    assert _SECRET not in f"{outcome!r} {outcome!s}"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("TASK_STATE_REJECTED", A2ARemoteRejected()),
        ("TASK_STATE_FAILED", A2ARemoteRejected()),
        ("TASK_STATE_CANCELED", A2ARemoteRejected()),
        ("TASK_STATE_AUTH_REQUIRED", A2ARemoteUnavailable()),
        ("TASK_STATE_SUBMITTED", A2AProtocolViolation()),
        ("TASK_STATE_INPUT_REQUIRED", A2AProtocolViolation()),
    ],
)
def test_remote_terminal_and_pending_states_map_to_redacted_sealed_outcomes(
    state: str,
    expected: A2ARemoteRejected | A2ARemoteUnavailable | A2AProtocolViolation,
) -> None:
    card = _card()

    def handler(request: httpx.Request) -> httpx.Response:
        response = card if request.method == "GET" else _completed({"text": "ignored"}, state=state)
        return httpx.Response(200, json=response)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert outcome == expected
    assert _SECRET not in f"{outcome!r} {outcome!s}"


def test_post_http_4xx_is_remote_rejected_but_5xx_is_unavailable() -> None:
    card = _card()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403 if request.method == "POST" else 200,
            json=card if request.method == "GET" else {"error": {"message": "denied"}},
        )

    rejected = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )
    assert rejected == A2ARemoteRejected()
    assert _SECRET not in f"{rejected!r} {rejected!s}"

    def unavailable_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json=card if request.method == "GET" else {"error": {"message": "down"}},
        )

    unavailable = _adapter(httpx.MockTransport(unavailable_handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )
    assert unavailable == A2ARemoteUnavailable()


def test_post_requires_json_and_rejects_redirect_before_sdk_decode() -> None:
    card = _card()

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302 if request.method == "POST" else 200,
            json=card if request.method == "GET" else {"message": {}},
        )

    redirected = _adapter(httpx.MockTransport(redirect_handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )
    assert redirected == A2AProtocolViolation()

    def content_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=card)
        return httpx.Response(200, content=b"{}", headers={"content-type": "text/plain"})

    invalid_content_type = _adapter(httpx.MockTransport(content_handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )
    assert invalid_content_type == A2AProtocolViolation()


def test_oversize_completed_text_and_remote_identifiers_do_not_escape() -> None:
    card = _card()
    remote_task = "remote-task-id-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=card if request.method == "GET" else _completed({"text": "x" * 1_025}),
        )

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card, max_response_bytes=1_024), question="질문", context=None
    )

    rendered = f"{outcome!s} {outcome!r}"
    assert isinstance(outcome, A2AProtocolViolation)
    assert _SECRET not in rendered
    assert remote_task not in rendered
    assert _ENDPOINT not in rendered


def test_oversize_post_wire_body_is_rejected_before_sdk_json_decode() -> None:
    card = _card()
    small_result = json.dumps(_completed({"text": "small"}), separators=(",", ":")).encode()
    oversized_wire = small_result + (b" " * 1_024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=card)
        return httpx.Response(200, content=oversized_wire, headers={"content-type": "application/json"})

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card, max_response_bytes=1_024), question="질문", context=None
    )

    assert outcome == A2AProtocolViolation()


def test_repeated_sync_invocation_gets_a_fresh_http_client_event_loop() -> None:
    card = _card()
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json=card if request.method == "GET" else _completed({"text": "ok"}))

    adapter = _adapter(httpx.MockTransport(handler))
    profile = _profile(card)
    assert adapter.invoke(profile=profile, question="one", context=None) == A2ACompletedText(text="ok")
    assert adapter.invoke(profile=profile, question="two", context=None) == A2ACompletedText(text="ok")
    assert requests == ["GET", "POST", "GET", "POST"]


def test_direct_text_message_is_accepted_but_active_event_loop_fails_closed() -> None:
    card = _card()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=card)
        return httpx.Response(
            200,
            json={
                "message": {
                    "messageId": "reply",
                    "contextId": "remote-context",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "done"}],
                }
            },
        )

    adapter = _adapter(httpx.MockTransport(handler))
    profile = _profile(card)
    assert adapter.invoke(profile=profile, question="one", context=None) == A2ACompletedText(text="done")

    async def invoke_inside_loop() -> object:
        return adapter.invoke(profile=profile, question="must not start", context=None)

    assert asyncio.run(invoke_inside_loop()) == A2ARemoteUnavailable()
    assert calls == ["GET", "POST"]


@pytest.mark.parametrize(
    "message",
    [
        {
            "messageId": "reply",
            "contextId": "remote-context",
            "role": "ROLE_USER",
            "parts": [{"text": "done"}],
        },
        {
            "messageId": "reply",
            "contextId": "remote-context",
            "role": "ROLE_UNSPECIFIED",
            "parts": [{"text": "done"}],
        },
        {"messageId": "reply", "role": "ROLE_AGENT", "parts": [{"text": "done"}]},
        {
            "messageId": "reply",
            "contextId": "remote-context",
            "role": "ROLE_AGENT",
            "parts": [{"text": "done", "filename": "answer.txt"}],
        },
        {
            "messageId": "reply",
            "contextId": "remote-context",
            "role": "ROLE_AGENT",
            "parts": [{"text": "done", "mediaType": "application/json"}],
        },
        {
            "messageId": "reply",
            "contextId": "remote-context",
            "role": "ROLE_AGENT",
            "parts": [{"text": "done"}],
            "extensions": ["remote-extension"],
        },
        {
            "messageId": "reply",
            "contextId": "remote-context",
            "role": "ROLE_AGENT",
            "parts": [{"text": "   "}],
        },
    ],
)
def test_direct_message_requires_agent_role_and_plain_nonblank_text_only(
    message: dict[str, object],
) -> None:
    card = _card()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=card if request.method == "GET" else {"message": message})

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert outcome == A2AProtocolViolation()


@pytest.mark.parametrize(
    "artifact",
    [
        {"artifactId": "answer", "extensions": ["remote-extension"], "parts": [{"text": "done"}]},
        {
            "artifactId": "answer",
            "parts": [{"text": "done", "mediaType": "application/json"}],
        },
    ],
)
def test_completed_artifact_rejects_remote_metadata_and_file_attributes(
    artifact: dict[str, object],
) -> None:
    card = _card()
    response: dict[str, object] = {
        "task": {
            "id": "remote-task",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [artifact],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=card if request.method == "GET" else response)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert outcome == A2AProtocolViolation()


def test_named_text_artifact_with_plain_media_type_and_metadata_is_accepted_without_projection() -> None:
    card = _card()
    response: dict[str, object] = {
        "task": {
            "id": "remote-task",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "metadata": {"ignored": "task"},
            "history": [
                {
                    "messageId": "old",
                    "contextId": "remote-context",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "ignored history"}],
                }
            ],
            "artifacts": [
                {
                    "artifactId": "answer",
                    "name": "normal answer",
                    "description": "normal peer metadata",
                    "metadata": {"ignored": "artifact"},
                    "parts": [
                        {
                            "text": "accepted",
                            "mediaType": "text/plain",
                            "metadata": {"ignored": "part"},
                        }
                    ],
                }
            ],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=card if request.method == "GET" else response)

    outcome = _adapter(httpx.MockTransport(handler)).invoke(
        profile=_profile(card), question="질문", context=None
    )

    assert outcome == A2ACompletedText(text="accepted")


def test_default_httpx_transport_is_unavailable_until_a_pinned_transport_is_supplied() -> None:
    card = _card()
    adapter = A2ASdkInvocationAdapter(credentials=_CredentialProvider(), resolver=_PublicDns())

    outcome = adapter.invoke(profile=_profile(card), question="no egress", context=None)

    assert outcome == A2ARemoteUnavailable()


def test_test_only_seam_rejects_arbitrary_network_transport_and_async_client() -> None:
    with pytest.raises(TypeError):
        A2ASdkInvocationAdapter.for_test(
            credentials=_CredentialProvider(),
            resolver=_PublicDns(),
            transport=cast(Any, httpx.AsyncHTTPTransport()),
        )
    with pytest.raises(TypeError):
        unsafe_constructor = cast(Any, A2ASdkInvocationAdapter)
        unsafe_constructor(
            credentials=_CredentialProvider(),
            resolver=_PublicDns(),
            http_client=httpx.AsyncClient(),
        )
