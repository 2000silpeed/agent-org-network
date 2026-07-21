"""P17.8 S6 production authority capability adversarial contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any, cast

from agent_org_network.approval_operations import ApprovalOperationsApplication
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    WorkerBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.production_authority import (
    ProductionAuthorityCapability,
    ProductionIdentityResolver,
)
from agent_org_network.production_bootstrap import (
    ProductionCompositionRejected,
    ProductionDependencies,
    ProductionBootstrapHandle,
    ProductionBootstrapConfig,
    bootstrap_authorized_production,
)
from agent_org_network.question_stream_execution import QuestionStreamApplication
from agent_org_network.question_surface_composition import (
    AtomicQuestionCompletionStorage,
    QuestionSurfaceComposition,
    _bind_question_surface_production_authority,  # pyright: ignore[reportPrivateUsage]
    _issue_question_surface_production_contract_attestation,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.question_resolution import QuestionResolutionApplication
from agent_org_network.worker_authorization import (
    StrictSnapshotWorkerBindingSource,
    WorkerAuthorization,
)


class _NoopScheduler:
    def shutdown(self, *, wait: bool) -> None:
        assert wait is True


def _snapshot() -> AuthorityPolicySnapshot:
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "v1",
        "content_sha256": "pending",
        "subject_roles": [{"org_id": "acme", "subject_id": "alice", "roles": ["owner"]}],
        "role_permissions": [
            {
                "role": "owner",
                "actions": ["monitor.read", "worker.connect", "worker.submit"],
            }
        ],
        "route_rules": [],
        "worker_bindings": [
            {
                "org_id": "acme",
                "credential_id": "cred-1",
                "owner_subject_id": "alice",
                "connection_role": "primary",
                "generation": 1,
            }
        ],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    return AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="v1",
        content_sha256=document["content_sha256"],  # type: ignore[arg-type]
        subject_roles=(SubjectRoleBinding(org_id="acme", subject_id="alice", roles=("owner",)),),
        role_permissions=(
            RolePermission(
                role="owner", actions=("monitor.read", "worker.connect", "worker.submit")
            ),
        ),
        route_rules=(),
        worker_bindings=(
            WorkerBinding(
                org_id="acme",
                credential_id="cred-1",
                owner_subject_id="alice",
                connection_role="primary",
                generation=1,
            ),
        ),
    )


def _surface(
    *,
    authorizer: SnapshotCentralAuthorizer | None = None,
    resolver: ProductionIdentityResolver | None = None,
    operational: OperationalAuthorization | None = None,
) -> QuestionSurfaceComposition:
    resolution = QuestionResolutionApplication(
        requests=cast(Any, object()),
        router=cast(Any, object()),
        conflicts=cast(Any, object()),
        managers=cast(Any, object()),
        route_authority=cast(Any, object()),
        deadline_policy=cast(Any, object()),
        request_id_factory=lambda: "request-1",
        clock=cast(Any, object()),
        central_authorizer=authorizer,
    )
    surface = QuestionSurfaceComposition(
        application=QuestionStreamApplication(
            resolution=resolution,
            execution=cast(Any, object()),
            broker=cast(Any, object()),
            scheduler=cast(Any, _NoopScheduler()),
        ),
        storage=cast(AtomicQuestionCompletionStorage, object()),
        approval_operations=cast(ApprovalOperationsApplication, object()),
    )
    _issue_question_surface_production_contract_attestation(surface)
    if authorizer is not None or resolver is not None or operational is not None:
        assert authorizer is not None
        assert resolver is not None
        assert operational is not None
        assert _bind_question_surface_production_authority(
            surface,
            central_authorizer=authorizer,
            identity_resolver=resolver,
            operational_authorization=operational,
        )
    return surface


def _capability(
    *, surface: QuestionSurfaceComposition | None = None
) -> ProductionAuthorityCapability:
    snapshot = _snapshot()
    authorizer = SnapshotCentralAuthorizer(snapshot)
    source = StrictSnapshotWorkerBindingSource(snapshot)
    resolver = ProductionIdentityResolver(
        configured_org_id="acme",
        snapshot=snapshot,
        authorizer=authorizer,
        resolver=lambda _request: AuthenticatedPrincipal(
            org_id="acme",
            subject_id="alice",
            identity_provider="oidc",
            identity_session_id="session-1",
        ),
    )
    operational = OperationalAuthorization(configured_org_id="acme", central_authorizer=authorizer)
    return ProductionAuthorityCapability(
        configured_org_id="acme",
        snapshot=snapshot,
        authorizer=authorizer,
        identity_resolver=resolver,
        operational_authorization=operational,
        worker_authorization=WorkerAuthorization(
            configured_org_id="acme", central_authorizer=authorizer, binding_source=source
        ),
        worker_binding_source=source,
        question_surface=surface
        or _surface(authorizer=authorizer, resolver=resolver, operational=operational),
    )


def test_capability_claims_only_its_exact_question_surface_once() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]

    assert capability.is_coherent() is True
    assert capability.claim(surface) is True
    assert capability.claim(surface) is False


def test_capability_rejects_equivalent_but_distinct_authorizer_or_snapshot() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]
    replacement = SnapshotCentralAuthorizer(_snapshot())
    object.__setattr__(capability, "_authorizer", replacement)

    assert capability.is_coherent() is False
    assert capability.claim(surface) is False


def test_capability_rejects_other_surface_before_any_claim() -> None:
    capability = _capability()

    assert capability.claim(_surface()) is False


def test_capability_claim_is_a_single_winner_under_32_way_replay_race() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]

    def claim_once(_index: int) -> bool:
        return capability.claim(surface)

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(claim_once, range(32)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 31


def test_capability_rejects_qsc_without_factory_authority_binding() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]
    object.__setattr__(surface, "_production_authority_binding", None)

    assert capability.is_coherent() is False
    assert capability.claim(surface) is False


def test_capability_rejects_qsc_when_resolution_uses_different_authorizer() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]
    replacement = SnapshotCentralAuthorizer(_snapshot())
    object.__setattr__(surface.application._resolution, "_central_authorizer", replacement)  # pyright: ignore[reportPrivateUsage]

    assert capability.is_coherent() is False
    assert capability.claim(surface) is False


def test_claim_and_close_share_qsc_lifecycle_lock_and_leave_no_live_claim() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]
    start = Barrier(3)

    def claim() -> bool:
        start.wait()
        return capability.claim(surface)

    def close() -> None:
        start.wait()
        surface.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim)
        close_future = pool.submit(close)
        start.wait()
        claim_result = claim_future.result(timeout=2)
        close_future.result(timeout=2)

    assert surface._closed is True  # pyright: ignore[reportPrivateUsage]
    # claim이 먼저 이겨도 close가 같은 lock 안에서 capability를 revoke하므로,
    # 종료된 surface를 다시 live production capability로 쓸 수 없다.
    assert capability._state == "revoked"  # pyright: ignore[reportPrivateUsage]
    assert capability.claim(surface) is False
    # 두 호출 중 어느 쪽이 먼저 lifecycle lock을 얻는지는 비결정적이다. 어느
    # 순서든 종료 뒤 capability가 live claim으로 남지 않는 것이 계약이다.
    assert claim_result in (True, False)


def _environ() -> dict[str, str]:
    return {
        "AON_PRODUCTION_ORG_ID": "acme",
        "AON_PRODUCTION_DATABASE_DSN": "postgresql://aon@db.example.test/aon",
        "AON_PRODUCTION_OIDC_ISSUER": "https://identity.example.test/",
        "AON_PRODUCTION_OIDC_CLIENT_ID": "aon-production",
        "AON_PRODUCTION_OIDC_CLIENT_SECRET": "oidc-secret",
        "AON_PRODUCTION_SESSION_SECRET": "session-secret",
        "AON_PRODUCTION_AUTHORITY_POLICY_REF": "authority-policy-v1",
        "AON_PRODUCTION_PROVIDER": "openai",
        "AON_PRODUCTION_PROVIDER_CREDENTIAL": "provider-secret",
    }


class _Factory:
    def __init__(self, dependencies: ProductionDependencies) -> None:
        self._dependencies = dependencies

    def open(self, config: ProductionBootstrapConfig) -> ProductionDependencies:
        del config
        return self._dependencies


def test_authorized_bootstrap_requires_and_consumes_actual_capability() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]

    def composition_factory(*, production_style: bool) -> QuestionSurfaceComposition:
        assert production_style is True
        return surface

    dependencies = ProductionDependencies(
        composition_factory=composition_factory,
        close=lambda: None,
        authority_capability=capability,
    )

    first = bootstrap_authorized_production(
        environ=_environ(), dependency_factory=_Factory(dependencies)
    )
    second = bootstrap_authorized_production(
        environ=_environ(), dependency_factory=_Factory(dependencies)
    )

    assert type(first) is ProductionBootstrapHandle
    assert type(second) is ProductionCompositionRejected


def test_authorized_bootstrap_rejects_absent_capability_before_composition_factory() -> None:
    calls = 0

    def factory(*, production_style: bool) -> QuestionSurfaceComposition:
        nonlocal calls
        del production_style
        calls += 1
        return _surface()

    result = bootstrap_authorized_production(
        environ=_environ(),
        dependency_factory=_Factory(
            ProductionDependencies(composition_factory=factory, close=lambda: None)
        ),
    )

    assert type(result) is ProductionCompositionRejected
    assert calls == 0


def test_authorized_bootstrap_rejects_configured_org_mismatch_before_composition() -> None:
    capability = _capability()
    surface = capability._question_surface  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def factory(*, production_style: bool) -> QuestionSurfaceComposition:
        nonlocal calls
        assert production_style is True
        calls += 1
        return surface

    result = bootstrap_authorized_production(
        environ={**_environ(), "AON_PRODUCTION_ORG_ID": "other-org"},
        dependency_factory=_Factory(
            ProductionDependencies(
                composition_factory=factory,
                close=lambda: None,
                authority_capability=capability,
            )
        ),
    )

    assert type(result) is ProductionCompositionRejected
    assert calls == 0
