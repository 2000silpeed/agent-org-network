"""P17.8 S4.5 저작/OKF 표면의 중앙 Authority 경계 회귀."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorityPolicySnapshot,
    CentralAuthorizer,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.git_gateway import FakeGitGateway
from agent_org_network.okf_authoring import FakeAuthor, OkfDocumentDraft
from agent_org_network.agent_card import AgentCard
from agent_org_network.authoring_application import AuthoringApplication, AuthoringMutation
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.operational_application import (
    MutationApprovalProvider,
    OperationalMutationApproval,
)
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app


_AUTHOR_ACTIONS = ("author.read", "author.write", "author.publish")


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id="cs_lead",
        identity_provider="oidc",
        identity_session_id="session-1",
    )


def _snapshot(actions: tuple[str, ...]) -> AuthorityPolicySnapshot:
    binding = SubjectRoleBinding(org_id="acme", subject_id="cs_lead", roles=("owner",))
    effective_actions = actions or ("question.create",)
    permission = RolePermission(role="owner", actions=cast(Any, effective_actions))
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test-policy",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    digest = canonical_policy_digest(document)
    return AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="test-policy",
        content_sha256=digest,
        subject_roles=(binding,),
        role_permissions=(permission,),
        route_rules=(),
        worker_bindings=(),
    )


def _client(
    *,
    actions: tuple[str, ...] = _AUTHOR_ACTIONS,
    authorizer: CentralAuthorizer | None = None,
    resolver: Callable[[Request], AuthenticatedPrincipal] | None = None,
    gateway: FakeGitGateway | None = None,
    author: FakeAuthor | None = None,
    mutation_approval: MutationApprovalProvider | None = None,
) -> tuple[TestClient, FakeGitGateway]:
    actual_gateway = gateway or FakeGitGateway()
    authority = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=authorizer or SnapshotCentralAuthorizer(_snapshot(actions)),
    )

    def default_resolver(_request: Request) -> AuthenticatedPrincipal:
        return _principal()

    return (
        TestClient(
            create_app(
                runtime=StubRuntime(),
                git_gateway=actual_gateway,
                governance_principal_resolver=resolver or default_resolver,
                operational_authorization=authority,
                operational_mutation_approval=mutation_approval
                or (
                    lambda _principal, _action, _resource, digest, fingerprint: (
                        OperationalMutationApproval(
                            outcome="allowed",
                            evidence_id="human-approval-1",
                            command_digest=digest,
                            resource_fingerprint=fingerprint,
                        )
                    )
                ),
                author=author,
            )
        ),
        actual_gateway,
    )


def _response(client: TestClient, method: str, url: str, **kwargs: object) -> Response:
    return cast(Response, getattr(cast(Any, client), method)(url, **kwargs))


def _concept() -> dict[str, object]:
    return {
        "concept_id": "refund-window",
        "disposition": "approved",
        "title": "환불 기간",
        "core_question": "언제까지 환불되나요?",
        "body": "결제일로부터 7일 이내입니다.",
        "domain": "환불",
    }


def _author() -> FakeAuthor:
    document = OkfDocumentDraft(
        concept_id="refund-window",
        title="환불 기간",
        core_question="언제까지 환불되나요?",
        body="결제일로부터 7일 이내입니다.",
        domain="환불",
    )
    return FakeAuthor(split_result=(document,), derive_result=(document,), link_result=())


class _DenyAuthorizationCall:
    """한 요청의 마지막 재검증 지점을 결정론적으로 revoke하는 fake."""

    def __init__(self, *, deny_at: int) -> None:
        self._delegate = SnapshotCentralAuthorizer(_snapshot(_AUTHOR_ACTIONS))
        self._deny_at = deny_at
        self.calls = 0

    def reset(self, *, deny_at: int) -> None:
        self._deny_at = deny_at
        self.calls = 0

    def authorize(
        self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> object:
        self.calls += 1
        if self.calls == self._deny_at:
            return AuthorizationDenied(kind="not_found_or_denied")
        return self._delegate.authorize(principal, action, resource)

    def verify(self, grant: object, principal: object, action: object, resource: object) -> bool:
        return self._delegate.verify(
            cast(Any, grant), cast(Any, principal), action, cast(Any, resource)
        )


def test_central_raw_author_endpoints_are_unavailable_before_legacy_gateway_use() -> None:
    denied, gateway = _client(actions=())
    assert _response(denied, "get", "/author/index/cs_ops").status_code == 503
    assert (
        _response(
            denied, "post", "/author/run", json={"agent_id": "cs_ops", "document": "환불 문서"}
        ).status_code
        == 503
    )
    assert (
        _response(
            denied, "post", "/author/publish", json={"agent_id": "cs_ops", "concepts": [_concept()]}
        ).status_code
        == 503
    )
    try:
        gateway.head_sha("cs_ops")
    except ValueError:
        pass
    else:  # pragma: no cover - denial must not create a commit
        raise AssertionError("denied publish wrote an OKF commit")


def test_http_replayed_approval_evidence_cannot_publish_changed_body() -> None:
    saved: OperationalMutationApproval | None = None

    def replay(
        _principal: AuthenticatedPrincipal,
        _action: object,
        _resource: object,
        digest: str,
        fingerprint: str,
    ) -> OperationalMutationApproval:
        nonlocal saved
        if saved is None:
            saved = OperationalMutationApproval(
                outcome="allowed",
                evidence_id="human-once",
                command_digest=digest,
                resource_fingerprint=fingerprint,
            )
        return saved

    client, gateway = _client(mutation_approval=replay)
    first = _response(
        client, "post", "/author/publish", json={"agent_id": "cs_ops", "concepts": [_concept()]}
    )
    changed = _concept()
    changed["body"] = "바뀐 본문은 승인받지 않았습니다."
    second = _response(
        client, "post", "/author/publish", json={"agent_id": "cs_ops", "concepts": [changed]}
    )

    assert first.status_code == 503
    assert second.status_code == 503
    with pytest.raises(ValueError):
        gateway.head_sha("cs_ops")


def test_author_mutation_audit_allowlists_callback_detail_and_never_persists_raw_values() -> None:
    client, _gateway = _client()
    app_any: Any = client.app
    application = cast(AuthoringApplication | None, app_any.state.authoring_application)
    assert application is not None

    def malicious_writer(_card: AgentCard) -> tuple[str, AuthoringMutation]:
        return (
            "written",
            AuthoringMutation(
                resource_id="cs_ops",
                detail={
                    "operation": "publish",
                    "raw_body": "customer secret answer",
                    "change_ref": "git://private/ref",
                    "secret": "do-not-audit",
                },
            ),
        )

    result = application.mutate(
        _principal(),
        "cs_ops",
        malicious_writer,
        channel="test",
        command={"operation": "publish", "body_sha256": "safe-digest"},
    )

    assert result == "written"
    application_any: Any = application
    audit_log: Any = application_any._audit_log
    detail: dict[str, Any] = audit_log.records()[-1]["action"]
    assert detail["operation"] == "publish"
    assert detail["channel"] == "test"
    assert detail["outcome"] == "succeeded"
    assert "approval_evidence_id" in detail and "approval_command_digest" in detail
    assert not {"raw_body", "change_ref", "secret"} & set(detail)


def test_central_raw_author_write_is_unavailable_before_principal_or_body_lookup() -> None:
    calls: list[str] = []

    def resolver(_request: Request) -> AuthenticatedPrincipal:
        calls.append("resolved")
        return _principal()

    denied, _ = _client(actions=(), resolver=resolver)

    # raw legacy central route는 principal/body/card lookup보다 먼저 닫힌다.
    response = _response(denied, "put", "/author/concept/cs_ops/x", json={})
    assert response.status_code == 503
    assert calls == []


def test_author_publish_reauthorizes_immediately_before_git_mutation() -> None:
    class DenySecondAuthorization:
        def __init__(self) -> None:
            self._delegate = SnapshotCentralAuthorizer(_snapshot(_AUTHOR_ACTIONS))
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            self._calls += 1
            if self._calls == 2:
                return AuthorizationDenied(kind="not_found_or_denied")
            return self._delegate.authorize(principal, action, resource)

        def verify(
            self, grant: object, principal: object, action: object, resource: object
        ) -> bool:
            return self._delegate.verify(
                cast(Any, grant), cast(Any, principal), action, cast(Any, resource)
            )

    authorizer = DenySecondAuthorization()
    client, gateway = _client(authorizer=cast(CentralAuthorizer, authorizer))

    response = _response(
        client,
        "post",
        "/author/publish",
        json={"agent_id": "cs_ops", "concepts": [_concept()]},
    )

    assert response.status_code == 503
    assert authorizer._calls == 0  # pyright: ignore[reportPrivateUsage]
    try:
        gateway.head_sha("cs_ops")
    except ValueError:
        pass
    else:  # pragma: no cover - reauthorization failure must leave git untouched
        raise AssertionError("revoked publish wrote an OKF commit")


def test_partial_author_composition_is_503_before_body_or_card_lookup() -> None:
    app = create_app(
        runtime=StubRuntime(),
        governance_principal_resolver=lambda _request: _principal(),
    )
    client = TestClient(app)

    assert _response(client, "post", "/author/run", content=b"{").status_code == 503
    assert _response(client, "get", "/author/index/missing-card").status_code == 503


def test_legacy_builder_bundle_is_unavailable_in_central_mode_before_body_or_write() -> None:
    """임의 OkfFile은 policy/domain이 바뀌어도 재admission할 수 없어 중앙에서 닫힌다."""
    gateway = FakeGitGateway()
    client, _ = _client(gateway=gateway)

    # 유효한 author.publish 정책과 현재 owner가 있어도 opaque bundle은 구조화된 domain
    # admission 전 중앙 write 경계에 들어갈 수 없다. malformed body도 읽지 않는다.
    response = _response(client, "post", "/builder/okf/commit", content=b"{")

    assert response.status_code == 503
    try:
        gateway.head_sha("cs_ops")
    except ValueError:
        pass
    else:  # pragma: no cover - unavailable must leave Git untouched
        raise AssertionError("central legacy builder commit wrote an OKF bundle")


def test_legacy_builder_bundle_stays_write_zero_when_policy_or_domain_shrinks() -> None:
    """정책 revoke와 카드 domain 축소가 경합해도 opaque bundle은 Git에 도달하지 않는다."""
    gateway = FakeGitGateway()
    client, _ = _client(actions=(), gateway=gateway)

    response = _response(
        client,
        "post",
        "/builder/okf/commit",
        json={
            "agent_id": "cs_ops",
            "files": [{"path": "refund.md", "content": "# 환불\n본문"}],
            "message": "should-not-write",
        },
    )

    assert response.status_code == 503
    try:
        gateway.head_sha("cs_ops")
    except ValueError:
        pass
    else:  # pragma: no cover - revoked/changed policy must leave Git untouched
        raise AssertionError("central legacy builder commit bypassed policy/domain admission")


def test_author_run_rechecks_current_grant_before_returning_transient_dto() -> None:
    authorizer = _DenyAuthorizationCall(deny_at=2)
    client, _ = _client(authorizer=cast(CentralAuthorizer, authorizer), author=_author())

    response = _response(
        client,
        "post",
        "/author/run",
        json={"agent_id": "cs_ops", "document": "환불 문서"},
    )

    assert response.status_code == 503
    assert authorizer.calls == 0
    assert "concepts" not in response.json()


def test_author_publish_revoke_after_commit_does_not_write_central_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AON_ROUTER", "index")
    authorizer = _DenyAuthorizationCall(deny_at=3)
    client, gateway = _client(authorizer=cast(CentralAuthorizer, authorizer))

    response = _response(
        client,
        "post",
        "/author/publish",
        json={"agent_id": "cs_ops", "concepts": [_concept()]},
    )

    assert response.status_code == 503
    assert authorizer.calls == 0
    with pytest.raises(ValueError):
        gateway.head_sha("cs_ops")
    index = _response(client, "get", "/author/index/cs_ops")
    assert index.status_code == 503


@pytest.mark.parametrize(
    ("method", "url", "payload"),
    [
        ("put", "/author/concept/cs_ops/refund-window", {"title": "바뀌면 안 되는 제목"}),
        ("delete", "/author/concept/cs_ops/refund-window", None),
    ],
)
def test_concept_mutation_revoke_after_commit_does_not_replace_central_index(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    url: str,
    payload: dict[str, object] | None,
) -> None:
    monkeypatch.setenv("AON_ROUTER", "index")
    authorizer = _DenyAuthorizationCall(deny_at=99)
    client, gateway = _client(authorizer=cast(CentralAuthorizer, authorizer))

    kwargs: dict[str, object] = {} if payload is None else {"json": payload}
    response = _response(client, method, url, **kwargs)
    assert response.status_code == 503
    assert authorizer.calls == 0
    with pytest.raises(ValueError):
        gateway.head_sha("cs_ops")
