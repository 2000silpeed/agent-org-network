"""ADR 0070 deterministic negative gates for the thin Question User artifact."""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path
from typing import Any, cast
import pytest
from fastapi.testclient import TestClient

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.oidc import OidcClaims
from agent_org_network.question_user_mcp import (
    HttpsQuestionGatewayClient,
    QUESTION_USER_TOOL_MANIFEST,
    PkceLoopbackPairing,
    QuestionMcpProfile,
    QuestionUserMcpUnavailable,
    create_question_user_mcp,
)
from agent_org_network.central_question_gateway import create_https_question_gateway
from agent_org_network.question_resolution import AskQuestion
from pydantic import SecretStr


class _Gateway:
    def ask_org(self, question: str) -> str:
        return "accepted: " + question

    def get_question(self, request_id: str) -> str:
        return "request: " + request_id


class _Credentials:
    def __init__(self) -> None:
        self.token: SecretStr | None = None

    def put(self, token: SecretStr) -> str:
        self.token = token
        return "opaque-keychain-ref"

    def get(self, reference: str) -> SecretStr:
        assert reference == "opaque-keychain-ref" and self.token is not None
        return self.token

    def delete(self, reference: str) -> None:
        return None


class _GatewayResponse:
    def __init__(self, response: Any, url: str) -> None:
        self._response, self._url = response, url
        self.status = response.status_code

    def geturl(self) -> str:
        return self._url

    def read(self, amount: int = -1) -> bytes:
        raw = self._response.content
        return raw if amount < 0 else raw[:amount]


def _profile() -> QuestionMcpProfile:
    return QuestionMcpProfile(
        gateway_url="https://gateway.example.test",
        authorization_url="https://id.example.test/authorize",
        token_url="https://id.example.test/token",
        client_id="question-user-mcp",
    )


def _exchange(*, code: SecretStr, redirect_uri: str, code_verifier: str) -> SecretStr:
    del code, redirect_uri, code_verifier
    return SecretStr("id-token")


def test_production_manifest_is_exact_and_has_no_identity_arguments() -> None:
    server = create_question_user_mcp(gateway=_Gateway())
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == QUESTION_USER_TOOL_MANIFEST
    schemas = {tool.name: tool.inputSchema for tool in tools}
    assert set(schemas["ask_org"]["properties"]) == {"question"}
    assert set(schemas["get_question"]["properties"]) == {"request_id"}
    assert "user_id" not in str(schemas)
    assert not {"feedback", "approval", "manager", "worker", "card", "registry"} & {
        tool.name for tool in tools
    }


@pytest.mark.parametrize("field", ["gateway_url", "authorization_url", "token_url"])
def test_http_endpoint_is_rejected(field: str) -> None:
    values = _profile().model_dump()
    values[field] = "http://127.0.0.1:8080"
    with pytest.raises((QuestionUserMcpUnavailable, ValueError)):
        QuestionMcpProfile.model_validate(values)


def test_pkce_state_is_single_use_and_only_opaque_ref_persists() -> None:
    pairing = PkceLoopbackPairing(_profile(), random=iter(("s" * 32, "v" * 32)).__next__)
    started = pairing.begin(43123)
    assert started.redirect_uri == "http://127.0.0.1:43123/callback"
    credentials = _Credentials()
    reference = pairing.consume_callback(
        state=started.state,
        code=SecretStr("authorization-code"),
        exchange=_exchange,
        store=credentials,
    )
    assert reference == "opaque-keychain-ref"
    assert "authorization-code" not in pairing.__dict__.values()
    assert "v" * 32 not in pairing.__dict__.values()
    with pytest.raises(QuestionUserMcpUnavailable):
        pairing.consume_callback(
            state=started.state,
            code=SecretStr("replay"),
            exchange=_exchange,
            store=credentials,
        )


_ALICE = AuthenticatedPrincipal(
    org_id="org-1", subject_id="alice", identity_provider="oidc", identity_session_id="session-a"
)
_BOB = AuthenticatedPrincipal(
    org_id="org-1", subject_id="bob", identity_provider="oidc", identity_session_id="session-b"
)


class _Oidc:
    def __init__(self, claims: OidcClaims | Exception) -> None:
        self._claims = claims

    def verify(self, id_token: str) -> OidcClaims:
        if isinstance(self._claims, Exception):
            raise self._claims
        assert id_token == "paired-token"
        return self._claims


class _Principals:
    def __init__(self, principal: AuthenticatedPrincipal | Exception) -> None:
        self._principal = principal

    def resolve(self, claims: OidcClaims) -> AuthenticatedPrincipal:
        del claims
        if isinstance(self._principal, Exception):
            raise self._principal
        return self._principal


class _RequestOwners:
    def __init__(self, resource: ResourceRef | Exception | None) -> None:
        self._resource = resource

    def resolve_question_owner(self, request_id: str) -> ResourceRef | None:
        if isinstance(self._resource, Exception):
            raise self._resource
        return self._resource


class _Authority:
    def __init__(self) -> None:
        self.resources: list[tuple[str, ResourceRef]] = []

    def authorize(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef
    ) -> AuthorizationGrant:
        self.resources.append((action, resource))
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,  # type: ignore[arg-type]
            resource=resource,
            roles=("requester",),
            policy_version="v1",
            policy_digest="a" * 64,
        )

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: str,
        resource: ResourceRef,
    ) -> bool:
        return (
            grant.subject_id == principal.subject_id
            and grant.action == action
            and grant.resource == resource
            and (action == "question.create" or resource.owner_subject_id == principal.subject_id)
        )


class _Application:
    def __init__(self) -> None:
        self.commands: list[AskQuestion] = []
        self.lookups: list[tuple[str, AuthenticatedPrincipal]] = []

    def ask(self, command: AskQuestion) -> object:
        self.commands.append(command)
        return "asked"

    def lookup(self, request_id: str, principal: AuthenticatedPrincipal) -> object:
        self.lookups.append((request_id, principal))
        return "looked-up"


def _claims() -> OidcClaims:
    return OidcClaims(sub="subject", email="alice@example.test", email_verified=True, iss="https://id.example.test", aud="aon")


def _gateway_client(
    *,
    principal: AuthenticatedPrincipal | Exception = _ALICE,
    claims: OidcClaims | Exception | None = None,
    owner: ResourceRef | Exception | None = None,
) -> tuple[Any, _Authority, _Application]:
    authority, application = _Authority(), _Application()
    app = create_https_question_gateway(
        application=application,
        oidc=_Oidc(_claims() if claims is None else claims),
        principals=_Principals(principal),
        request_owners=_RequestOwners(
            ResourceRef(org_id="org-1", kind="question", resource_id="request-1", owner_subject_id="alice")
            if owner is None else owner
        ),
        authority=authority,
        render=_render,
    )
    # Starlette's TestClient is currently untyped in this dependency set.
    return cast(Any, TestClient(app)), authority, application


def _headers() -> dict[str, str]:
    return {"authorization": "Bearer paired-token"}


def _render(result: object) -> str:
    return str(result)


def test_https_gateway_uses_verified_current_principal_for_question_create() -> None:
    client, authority, application = _gateway_client()

    response = client.post("/ask_org", json={"question": "질문"}, headers=_headers())

    assert response.status_code == 200
    assert response.json() == {"text": "asked"}
    assert application.commands[0].principal == _ALICE
    assert authority.resources == [("question.create", ResourceRef(org_id="org-1", kind="question"))]


def test_https_gateway_reads_only_current_users_authoritative_request() -> None:
    client, authority, application = _gateway_client()

    response = client.post("/get_question", json={"request_id": "request-1"}, headers=_headers())

    assert response.status_code == 200
    assert response.json() == {"text": "looked-up"}
    assert application.lookups == [("request-1", _ALICE)]
    assert authority.resources == [
        ("question.read", ResourceRef(org_id="org-1", kind="question", resource_id="request-1", owner_subject_id="alice"))
    ]


def test_remote_question_client_reaches_central_gateway_without_client_identity() -> None:
    client, _, application = _gateway_client()
    credentials = _Credentials()
    credentials.put(SecretStr("paired-token"))

    def open_remote(request: Any) -> _GatewayResponse:
        url = request.full_url
        response = client.post(
            url.removeprefix("https://gateway.example.test"),
            content=request.data,
            headers=dict(request.headers),
        )
        return _GatewayResponse(response, url)

    remote = HttpsQuestionGatewayClient(
        _profile().model_copy(update={"credential_ref": "opaque-keychain-ref"}),
        credentials,
        opener=open_remote,
    )

    assert remote.ask_org("원격 질문") == "asked"
    assert remote.get_question("request-1") == "looked-up"
    assert application.commands[0].principal == _ALICE
    assert application.lookups == [("request-1", _ALICE)]


def test_https_gateway_rejects_cross_user_read_using_authoritative_request_owner() -> None:
    client, authority, application = _gateway_client(principal=_BOB)

    response = client.post("/get_question", json={"request_id": "request-1"}, headers=_headers())

    assert response.status_code == 403
    assert application.lookups == []
    assert authority.resources == [
        ("question.read", ResourceRef(org_id="org-1", kind="question", resource_id="request-1", owner_subject_id="alice"))
    ]


@pytest.mark.parametrize(
    ("claims", "principal", "owner"),
    [
        (RuntimeError("token=paired-token"), _ALICE, None),
        (None, RuntimeError("registry email=alice@example.test"), None),
        (None, _ALICE, RuntimeError("request ownership failure")),
    ],
)
def test_https_gateway_maps_claim_and_resolver_failures_to_opaque_401(
    claims: OidcClaims | Exception | None,
    principal: AuthenticatedPrincipal | Exception,
    owner: ResourceRef | Exception | None,
) -> None:
    client, _, _ = _gateway_client(claims=claims, principal=principal, owner=owner)
    endpoint = "/get_question" if owner is not None else "/ask_org"
    payload = {"request_id": "request-1"} if owner is not None else {"question": "질문"}

    response = client.post(endpoint, json=payload, headers=_headers())

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}
    assert "paired-token" not in response.text
    assert "alice@example.test" not in response.text


def test_question_user_artifact_has_no_identity_env_or_forbidden_central_imports() -> None:
    source = Path("src/agent_org_network/question_user_mcp.py").read_text(encoding="utf-8")
    imports = {
        name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for name in (
            ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            + ([node.module] if isinstance(node, ast.ImportFrom) and node.module is not None else [])
        )
    }

    assert not {
        "agent_org_network.mcp_server",
        "agent_org_network.central_question_gateway",
        "agent_org_network.central_authority",
        "agent_org_network.oidc",
        "agent_org_network.question_resolution",
        "agent_org_network.question_stream_execution",
        "agent_org_network.sqlite_production_registry_users",
        "fastapi",
    } & imports
    assert "os.environ" not in source
    assert "getenv(" not in source
    assert "user_id" not in source[source.index("def main"):]
    assert "feedback" not in QUESTION_USER_TOOL_MANIFEST
    assert not {"approval", "manager", "worker", "card", "registry", "author"} & QUESTION_USER_TOOL_MANIFEST
