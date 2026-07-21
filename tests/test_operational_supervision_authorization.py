"""P17.8 S4.2 감독·스코어카드의 중앙 Authority 경계 회귀."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from fastapi import Request
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.answer_record import InMemoryAnswerRecordStore, InMemoryCorrectionStore
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    CentralAuthorizer,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app


def _principal(subject_id: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id="session-1",
    )


def _snapshot(*, roles: dict[str, tuple[str, ...]]) -> AuthorityPolicySnapshot:
    permissions = (
        RolePermission(
            role="owner",
            actions=("supervision.read", "supervision.correct", "scorecard.read"),
        ),
        RolePermission(role="operator", actions=("scorecard.read",)),
    )
    bindings = tuple(
        SubjectRoleBinding(org_id="acme", subject_id=subject_id, roles=cast(Any, subject_roles))
        for subject_id, subject_roles in roles.items()
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test-policy",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json") for binding in bindings],
        "role_permissions": [permission.model_dump(mode="json") for permission in permissions],
        "route_rules": [],
        "worker_bindings": [],
    }
    digest = canonical_policy_digest(document)
    document["content_sha256"] = digest
    return AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="test-policy",
        content_sha256=digest,
        subject_roles=bindings,
        role_permissions=permissions,
        route_rules=(),
        worker_bindings=(),
    )


def _response(client: TestClient, method: str, url: str, **kwargs: object) -> Response:
    http: Any = client
    return cast(Response, getattr(http, method)(url, **kwargs))


def _app(
    *,
    subject_id: str = "cs_lead",
    roles: dict[str, tuple[str, ...]] | None = None,
    authorizer: CentralAuthorizer | None = None,
    resolver: Callable[[Request], AuthenticatedPrincipal] | None = None,
    records: InMemoryAnswerRecordStore | None = None,
    corrections: InMemoryCorrectionStore | None = None,
) -> tuple[TestClient, InMemoryAnswerRecordStore, InMemoryCorrectionStore]:
    answer_records = records if records is not None else InMemoryAnswerRecordStore()
    correction_store = corrections if corrections is not None else InMemoryCorrectionStore()
    actual_authorizer = authorizer or SnapshotCentralAuthorizer(
        _snapshot(roles={subject_id: ("owner",)} if roles is None else roles)
    )
    operational = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=actual_authorizer
    )

    def _default_resolver(_request: Request) -> AuthenticatedPrincipal:
        return _principal(subject_id)

    actual_resolver = resolver or _default_resolver
    app = create_app(
        runtime=StubRuntime(),
        answer_record_store=answer_records,
        correction_store=correction_store,
        governance_principal_resolver=actual_resolver,
        operational_authorization=operational,
    )
    return TestClient(app), answer_records, correction_store


def _seed_answer(client: TestClient) -> dict[str, object]:
    response = _response(client, "post", "/ask", json={"question": "계약 변경 가능?"})
    assert response.status_code == 200
    return cast(dict[str, object], response.json())


def test_supervision_read_requires_role_and_current_owner_without_record_leak() -> None:
    client, _, _ = _app(subject_id="not_owner", roles={"not_owner": ("owner",)})

    denied = _response(client, "get", "/supervision/answers", params={"agent_id": "cs_ops"})

    assert denied.status_code == 503
    assert "not_owner" not in denied.text

    client, _, _ = _app(subject_id="cs_lead", roles={})
    role_denied = _response(client, "get", "/supervision/answers", params={"agent_id": "cs_ops"})
    assert role_denied.status_code == 503


def test_supervision_read_rechecks_current_owner_before_return() -> None:
    """첫 grant 뒤 owner가 바뀌면 이미 읽은 감독 값도 반환하지 않는다."""

    class _TransfersOnFirstAuthorize:
        def __init__(self, registry_holder: dict[str, object]) -> None:
            self._delegate = SnapshotCentralAuthorizer(_snapshot(roles={"legal_lead": ("owner",)}))
            self._registry_holder = registry_holder
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            result = self._delegate.authorize(principal, cast(Any, action), resource)
            self._calls += 1
            if self._calls == 1:
                registry = cast(Any, self._registry_holder["registry"])
                card = registry.get("contract_ops")
                registry.replace_card(card.model_copy(update={"owner": "hr_lead"}))
            return result

        def verify(
            self,
            grant: object,
            principal: AuthenticatedPrincipal,
            action: object,
            resource: ResourceRef,
        ) -> bool:
            return self._delegate.verify(cast(Any, grant), principal, cast(Any, action), resource)

    for path in ("/supervision/answers", "/supervision/presence/{agent_id}"):
        registry_holder: dict[str, object] = {}
        authorizer = cast(CentralAuthorizer, _TransfersOnFirstAuthorize(registry_holder))
        client, _, _ = _app(subject_id="legal_lead", authorizer=authorizer)
        import inspect

        app_any: Any = client.app
        endpoint: Any = next(
            route.endpoint for route in app_any.routes if getattr(route, "path", "") == path
        )
        authorize = inspect.getclosurevars(endpoint).nonlocals["_authorize_operational_card"]
        current_card = inspect.getclosurevars(authorize).nonlocals["_current_agent_card"]
        registry_holder["registry"] = (
            inspect.getclosurevars(current_card).nonlocals["bundle"].registry
        )
        if path == "/supervision/answers":
            _seed_answer(client)
            response = _response(client, "get", path, params={"agent_id": "contract_ops"})
        else:
            response = _response(client, "get", "/supervision/presence/contract_ops")
        assert response.status_code == 503


def test_central_correction_is_principal_first_and_ignores_body_actor() -> None:
    client, _, corrections = _app(subject_id="legal_lead")
    answer = _seed_answer(client)
    record_id = cast(str, answer["record_id"])

    response = _response(
        client,
        "post",
        f"/supervision/answers/{record_id}/correct",
        json={"by_owner": "attacker", "corrected_text": "수정"},
    )

    assert response.status_code == 503
    events = corrections.for_record(record_id)
    assert events == []


def test_correction_denial_and_owner_transfer_before_submit_leave_zero_writes() -> None:
    client, _records, corrections = _app(subject_id="not_owner", roles={"not_owner": ("owner",)})
    answer = _seed_answer(client)
    record_id = cast(str, answer["record_id"])
    denied = _response(
        client,
        "post",
        f"/supervision/answers/{record_id}/correct",
        json={"corrected_text": "몰래 수정"},
    )
    assert denied.status_code == 503
    assert corrections.for_record(record_id) == []

    registry_holder: dict[str, object] = {}

    class _TransfersOnFirstAuthorize:
        def __init__(self) -> None:
            self._delegate = SnapshotCentralAuthorizer(_snapshot(roles={"legal_lead": ("owner",)}))
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            result = self._delegate.authorize(principal, cast(Any, action), resource)
            self._calls += 1
            if self._calls == 1:
                registry = registry_holder["registry"]
                card = cast(Any, registry).get("contract_ops")
                cast(Any, registry).replace_card(card.model_copy(update={"owner": "hr_lead"}))
            return result

        def verify(
            self,
            grant: object,
            principal: AuthenticatedPrincipal,
            action: object,
            resource: ResourceRef,
        ) -> bool:
            return self._delegate.verify(cast(Any, grant), principal, cast(Any, action), resource)

    transfer_authorizer = cast(CentralAuthorizer, _TransfersOnFirstAuthorize())
    client, _records, corrections = _app(subject_id="legal_lead", authorizer=transfer_authorizer)
    import inspect

    app_any: Any = client.app
    endpoint: Any = next(
        route.endpoint
        for route in app_any.routes
        if getattr(route, "path", "") == "/supervision/answers/{record_id}/correct"
    )
    current_card = inspect.getclosurevars(endpoint).nonlocals["_current_agent_card"]
    registry_holder["registry"] = inspect.getclosurevars(current_card).nonlocals["bundle"].registry
    answer = _seed_answer(client)
    record_id = cast(str, answer["record_id"])
    transferred = _response(
        client,
        "post",
        f"/supervision/answers/{record_id}/correct",
        json={"corrected_text": "늦은 수정"},
    )
    assert transferred.status_code == 503
    assert corrections.for_record(record_id) == []


def test_scorecard_collection_filters_other_owners_and_operator_can_read_all() -> None:
    client, _, _ = _app(subject_id="cs_lead", roles={"cs_lead": ("owner",)})

    own = _response(client, "get", "/supervision/scorecard", params={"owner_id": "hr_lead"})
    collection = _response(client, "get", "/admin/scorecards")

    assert own.status_code == 503
    assert collection.status_code == 503

    operator, _, _ = _app(subject_id="ops", roles={"ops": ("operator",)})
    all_cards = _response(operator, "get", "/admin/scorecards")
    assert all_cards.status_code == 503


def test_scorecards_reauthorize_after_compute_before_serializing() -> None:
    """계산 사이 owner가 전이되면 self는 404, collection에는 항목을 넣지 않는다."""

    class _TransfersCsCardOnFirstAuthorize:
        def __init__(
            self, roles: dict[str, tuple[str, ...]], registry_holder: dict[str, object]
        ) -> None:
            self._delegate = SnapshotCentralAuthorizer(_snapshot(roles=roles))
            self._registry_holder = registry_holder
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            result = self._delegate.authorize(principal, cast(Any, action), resource)
            self._calls += 1
            if self._calls == 1:
                registry = cast(Any, self._registry_holder["registry"])
                card = registry.get("cs_ops")
                registry.replace_card(card.model_copy(update={"owner": "hr_lead"}))
            return result

        def verify(
            self,
            grant: object,
            principal: AuthenticatedPrincipal,
            action: object,
            resource: ResourceRef,
        ) -> bool:
            return self._delegate.verify(cast(Any, grant), principal, cast(Any, action), resource)

    def _client_after_transfer(subject_id: str, roles: dict[str, tuple[str, ...]]) -> TestClient:
        registry_holder: dict[str, object] = {}
        authorizer = cast(
            CentralAuthorizer, _TransfersCsCardOnFirstAuthorize(roles, registry_holder)
        )
        client, _, _ = _app(subject_id=subject_id, roles=roles, authorizer=authorizer)
        import inspect

        app_any: Any = client.app
        endpoint: Any = next(
            route.endpoint
            for route in app_any.routes
            if getattr(route, "path", "") == "/supervision/scorecard"
        )
        authorize_owner = inspect.getclosurevars(endpoint).nonlocals["_authorize_scorecard_owner"]
        authorize_card = inspect.getclosurevars(authorize_owner).nonlocals[
            "_authorize_operational_card"
        ]
        current_card = inspect.getclosurevars(authorize_card).nonlocals["_current_agent_card"]
        registry_holder["registry"] = (
            inspect.getclosurevars(current_card).nonlocals["bundle"].registry
        )
        return client

    self_client = _client_after_transfer("cs_lead", {"cs_lead": ("owner",)})
    self_response = _response(self_client, "get", "/supervision/scorecard")
    assert self_response.status_code == 503

    collection_client = _client_after_transfer("ops", {"ops": ("operator",)})
    collection_response = _response(collection_client, "get", "/admin/scorecards")
    assert collection_response.status_code == 503


def test_authorizer_failure_is_neutral_503_without_principal_or_cause() -> None:
    class _Explodes:
        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            raise RuntimeError("secret upstream policy detail")

        def verify(
            self,
            grant: object,
            principal: AuthenticatedPrincipal,
            action: object,
            resource: ResourceRef,
        ) -> bool:
            return False

    client, _, _ = _app(authorizer=cast(CentralAuthorizer, _Explodes()))
    response = _response(client, "get", "/supervision/answers", params={"agent_id": "cs_ops"})
    assert response.status_code == 503
    assert "secret" not in response.text
    assert "cs_lead" not in response.text


def test_central_raw_correction_is_unavailable_before_principal_or_body() -> None:
    calls = 0

    def resolver(_request: Request) -> AuthenticatedPrincipal:
        nonlocal calls
        calls += 1
        return _principal("legal_lead")

    client, _, _ = _app(subject_id="legal_lead", resolver=resolver)
    response = _response(
        client,
        "post",
        "/supervision/answers/no-such-record/correct",
        content=b"{bad json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 503
    assert calls == 0
