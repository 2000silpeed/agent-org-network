from __future__ import annotations
# pyright: reportPrivateUsage=false

import sqlite3
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Any, cast

import pytest

from agent_org_network.sqlite_durable_reciprocal_review import (
    migrate_sqlite_durable_reciprocal_review_ledger,
)
from agent_org_network.sqlite_reciprocal_review_human_disposition import (
    migrate_sqlite_reciprocal_review_human_disposition_v2,
)
from agent_org_network.sqlite_reciprocal_review_source_binding import (
    SourceBindingAdapter,
    SqliteReciprocalReviewSourceBindingError,
    create_sqlite_reciprocal_review_source_binding_intent_uow,
)
from agent_org_network.trusted_source_binding_authority import (
    _bootstrap_source_binding_wiring,
)
import test_sqlite_reciprocal_review_ai_mixed_disposition as v5_upstream
import test_sqlite_reciprocal_review_human_disposition as v2_upstream
from agent_org_network.sqlite_reciprocal_review_source_binding_v7 import (
    SourceBindingAuthorizationAuthority,
    create_sqlite_reciprocal_review_source_binding_v7_uow,
    migrate_sqlite_reciprocal_review_source_binding_v7,
    validate_sqlite_reciprocal_review_source_binding_v7,
)
import agent_org_network.sqlite_reciprocal_review_source_binding_v7 as v7_impl
import agent_org_network.sqlite_reciprocal_review_source_binding as v6_impl
from agent_org_network.reciprocal_review import CreateSourceBindingIntent, CreateSourceBindingIntentV7
from agent_org_network.reciprocal_review import (
    IntegrationEnforcementProfileV7,
    SourceBindingAuthorizationEnvelopeV7,
)
from agent_org_network.sqlite_reciprocal_review_source_binding import (
    migrate_sqlite_reciprocal_review_source_binding_v6,
)
from agent_org_network.sqlite_source_binding_worker_v7 import (
    FakeSourceBindingExecutor,
    PendingWorker,
    SourceBindingWorkerError,
    _dig as worker_digest,
    migrate_sqlite_source_binding_worker_v7,
)
import agent_org_network.sqlite_source_binding_worker_v7 as worker_impl


def test_v6_legacy_intent_writer_is_closed() -> None:
    with pytest.raises(SqliteReciprocalReviewSourceBindingError):
        create_sqlite_reciprocal_review_source_binding_intent_uow(
            ":memory:", adapter=cast(SourceBindingAdapter, object())
        )


def test_v7_requires_v2_and_installs_exact_empty_capability() -> None:
    connection = sqlite3.connect(":memory:")
    with pytest.raises(Exception):
        migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    migrate_sqlite_reciprocal_review_human_disposition_v2(connection)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    validate_sqlite_reciprocal_review_source_binding_v7(connection)
    assert connection.execute(
        "SELECT component_id FROM schema_component_manifests WHERE component_id=?",
        ("durable_reciprocal_review_source_binding_v7",),
    ).fetchone() == ("durable_reciprocal_review_source_binding_v7",)


def test_v7_public_command_rejects_authority_dto_injection() -> None:
    with pytest.raises(ValueError):
        CreateSourceBindingIntentV7.model_validate(
            {
                "receipt_id": "receipt",
                "audit_id": "audit",
                "outbox_id": "outbox",
                "idempotency_key": "key",
                "cycle_id": "cycle",
                "expected_upstream_revision": 1,
                "source_ref": "source",
                "authority_receipt": {"forged": "dto"},
            }
        )


def test_v7_is_additive_to_the_closed_v6_catalog() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_sqlite_durable_reciprocal_review_ledger(connection)
    migrate_sqlite_reciprocal_review_human_disposition_v2(connection)
    migrate_sqlite_reciprocal_review_source_binding_v6(connection)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    assert {
        row[0]
        for row in connection.execute(
            "SELECT component_id FROM schema_component_manifests "
            "WHERE component_id IN (?,?)",
            (
                "durable_reciprocal_review_source_binding_v6",
                "durable_reciprocal_review_source_binding_v7",
            ),
        )
    } == {
        "durable_reciprocal_review_source_binding_v6",
        "durable_reciprocal_review_source_binding_v7",
    }


def test_v7_uow_requires_central_authority_and_trusted_registries() -> None:
    with pytest.raises(ValueError):
        create_sqlite_reciprocal_review_source_binding_v7_uow(
            ":memory:",
            authority=cast(SourceBindingAuthorizationAuthority, object()),
                authority_capability=cast(Any, object()),
            trusted_integration_profiles={},
        )


def test_v7_rejects_issue_only_authority_without_bootstrap_capability() -> None:
    class IssueOnly:
        def issue_intent(self, **_: object) -> object:
            return object()

    with pytest.raises(ValueError):
        create_sqlite_reciprocal_review_source_binding_v7_uow(
            ":memory:",
            authority=cast(SourceBindingAuthorizationAuthority, IssueOnly()),
            authority_capability=cast(Any, object()),
            trusted_integration_profiles={"integration": _profile()},
        )


NOW = datetime(2026, 7, 20, tzinfo=UTC)
SHA = "a" * 64
KEY = b"central-authority-key"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _profile() -> IntegrationEnforcementProfileV7:
    return IntegrationEnforcementProfileV7(
        integration_id="trusted-source",
        profile_version="profile-v1",
        profile_digest="b" * 64,
        enforcement_plan_digest="e" * 64,
    )


class _CentralAuthority:
    def __init__(self, *, current: bool = True, mutation: str | None = None) -> None:
        self.current = current
        self.mutation = mutation
        self.calls: list[str] = []

    def issue_intent(
        self, *, cycle_id: str, source_ref: str, db_now: datetime
    ) -> SourceBindingAuthorizationEnvelopeV7:
        payload = {
            "format_version": "v7", "receipt_id": "authority-receipt",
            "key_id": "central-1",
            "org_id": "org",
            "source_ref": source_ref,
            "source_resource": {"org_id": "org", "resource_kind": "source", "resource_id": source_ref},
            "expected_source_revision": "source-revision-1",
            "revision_id": "revision",
            "content_digest": SHA,
            "data_classification": "internal",
            "boundary_snapshot_ref": "boundary",
            "boundary_digest": "b" * 64,
            "declassification_receipt_id": None,
            "declassification_receipt_digest": None, "declassification_expires_at": None,
            "action": "reciprocal_review.source_bind", "policy_version": "policy-v1",
            "policy_digest": "c" * 64,
            "principal_grant_digest": "d" * 64,
            "integration_id": "trusted-source",
            "integration_profile_version": "profile-v1",
            "integration_profile_digest": "b" * 64,
            "enforcement_plan_digest": "e" * 64,
            "every_read_mode": "gateway",
            "drift_action": "source_deny_reads",
            "drift_action_digest": "f" * 64,
            "drift_expires_at": db_now + timedelta(minutes=5),
            "issued_at": db_now - timedelta(milliseconds=1),
            "expires_at": db_now + timedelta(minutes=5),
            "payload_digest": "1" * 64,
            "intent_semantic_digest": "2" * 64,
        }
        if self.mutation == "classification":
            payload["data_classification"] = "restricted"
        if self.mutation == "boundary_snapshot":
            payload["boundary_snapshot_ref"] = "other-boundary"
        if self.mutation == "boundary_digest":
            payload["boundary_digest"] = "e" * 64
        if self.mutation == "expired":
            payload["expires_at"] = db_now
        if self.mutation == "untrusted_integration":
            payload["integration_id"] = "static-acl-scheduler"
        drift_expires_at = cast(datetime, payload["drift_expires_at"])
        preimage: dict[str, object] = {
            "org_id": "org", "source_resource": payload["source_resource"],
            "expected_source_revision": payload["expected_source_revision"], "revision_id": "revision",
            "content_digest": SHA, "data_classification": payload["data_classification"],
            "boundary_snapshot_ref": payload["boundary_snapshot_ref"], "boundary_digest": payload["boundary_digest"],
            "declassification_receipt_id": None, "policy_digest": "c" * 64,
            "principal_grant_digest": "d" * 64, "integration_profile": _profile().model_dump(mode="json"),
            "every_read_mode": "gateway", "drift_action": "source_deny_reads", "drift_action_digest": "f" * 64,
            "drift_expires_at": drift_expires_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        payload["intent_semantic_digest"] = hashlib.sha256(_canonical(preimage).encode()).hexdigest()
        payload["payload_digest"] = hashlib.sha256(
            _canonical({key: value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value for key, value in payload.items() if key != "payload_digest"}).encode()
        ).hexdigest()
        signature = hmac.new(
            KEY, _canonical({key: value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value for key, value in payload.items()}).encode(), hashlib.sha256
        ).hexdigest()
        return SourceBindingAuthorizationEnvelopeV7.model_validate(
            {**payload, "signature": signature}
        )



def _command(
    *, receipt: str = "v7-receipt", key: str = "v7-key", upstream_revision: int = 2
) -> CreateSourceBindingIntentV7:
    return CreateSourceBindingIntentV7(
        receipt_id=receipt,
        audit_id=f"audit-{receipt}",
        outbox_id=f"outbox-{receipt}",
        idempotency_key=key,
        cycle_id="cycle:revision",
        expected_upstream_revision=upstream_revision,
        source_ref="source:trusted",
    )


def _uow(
    path: Path,
    authority: _CentralAuthority,
    *,
    fault: Callable[[str], None] | None = None,
) -> Any:
    return create_sqlite_reciprocal_review_source_binding_v7_uow(
        path,
        authority=authority,
        authority_capability=_test_capability(authority, path),
        trusted_integration_profiles={"trusted-source": _profile()},
        fault_injector=fault,
    )


def _test_capability(authority: _CentralAuthority, path: Path) -> Any:
    wiring = _bootstrap_source_binding_wiring(
        issuer_registry={"central-1": KEY},
        resolver=lambda _receipt, purpose, _now: (authority.calls.append(purpose) or authority.current),
        database_wiring=path, source_wiring="source:trusted",
    )
    class _Authority:
        _state = "claimed"
    class _Handle:
        authority_capability: _Authority
        _source_binding_closed = False
        def __init__(self) -> None:
            self.authority_capability = _Authority()
    handle = _Handle()
    return wiring.open_for_bootstrap(handle, handle.authority_capability)


def test_source_binding_capability_fails_closed_when_its_bootstrap_lifecycle_changes() -> None:
    authority = _CentralAuthority()
    capability = _test_capability(authority, Path("/tmp/source-binding-target.sqlite"))
    assert capability is not None and capability.is_live()
    handle = capability._handle
    handle._source_binding_closed = True
    assert not capability.is_live()


def test_bootstrap_capability_rejects_an_unrelated_sqlite_target_before_write(
    tmp_path: Path,
) -> None:
    bound = tmp_path / "bound.sqlite"
    other = tmp_path / "other.sqlite"
    authority = _CentralAuthority()
    capability = _test_capability(authority, bound)
    with pytest.raises(ValueError):
        create_sqlite_reciprocal_review_source_binding_v7_uow(
            other,
            authority=authority,
            authority_capability=capability,
            trusted_integration_profiles={"trusted-source": _profile()},
        )
    assert not other.exists()


@pytest.mark.parametrize("target", (":memory:", "file:shared?mode=memory&cache=shared", "relative.sqlite"))
def test_bootstrap_wiring_rejects_nonpersistent_or_relative_sqlite_targets(target: str) -> None:
    wiring = _bootstrap_source_binding_wiring(
        issuer_registry={"central-1": KEY},
        resolver=lambda _receipt, _purpose, _now: True,
        database_wiring=target,
        source_wiring="source:trusted",
    )
    class _Authority:
        _state = "claimed"
    class _Handle:
        authority_capability: _Authority
        _source_binding_closed = False
        def __init__(self) -> None:
            self.authority_capability = _Authority()
    handle = _Handle()
    assert wiring.open_for_bootstrap(handle, handle.authority_capability) is None


def _counts(path: Path) -> tuple[int, int, int, int]:
    connection = sqlite3.connect(path)
    result = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "durable_reciprocal_review_source_binding_cycles_v7",
            "reciprocal_review_v7_binding_intents",
            "reciprocal_review_v7_binding_audit",
            "reciprocal_review_v7_binding_outbox",
        )
    )
    connection.close()
    return cast(tuple[int, int, int, int], result)


def _race_create(_: int, path: Path) -> Any:
    return _uow(path, _CentralAuthority()).create(_command())


@pytest.mark.parametrize("lane", ("v2", "v5"))
def test_v7_real_binding_ready_creates_only_pending_with_central_receipt(
    tmp_path: Path, lane: str
) -> None:
    path = tmp_path / f"{lane}.sqlite"
    if lane == "v2":
        v2_upstream._seed(path)
        v2_upstream.create_sqlite_reciprocal_review_human_disposition_uow(
            path, trusted_human_authority_keys={"human": b"human-key"}, trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW
        ).submit(v2_upstream._principal(), v2_upstream._command())
    else:
        v5_upstream._seed(path)
        v5_upstream._v5_uow(path).submit(v5_upstream._v5_principal(path), v5_upstream._v5_command())
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    connection.close()
    authority = _CentralAuthority()
    result = _uow(path, authority).create(_command(upstream_revision=2 if lane == "v2" else 3))
    assert (result.org_id, result.cycle_state, result.binding_generation) == ("org", "binding_pending", 1)
    assert _counts(path) == (1, 1, 1, 1)
    assert authority.calls == ["intent_create"]
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT state_kind,upstream_kind,upstream_revision FROM durable_reciprocal_review_source_binding_cycles_v7").fetchone() == ("binding_pending", lane, 2 if lane == "v2" else 3)
    connection.close()


@pytest.mark.parametrize("mutation", ("classification", "boundary_snapshot", "boundary_digest", "expired"))
def test_v7_invalid_central_receipt_is_write_zero(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / f"{mutation}.sqlite"
    v2_upstream._seed(path)
    v2_upstream.create_sqlite_reciprocal_review_human_disposition_uow(path, trusted_human_authority_keys={"human": b"human-key"}, trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW).submit(v2_upstream._principal(), v2_upstream._command())
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    connection.close()
    with pytest.raises(Exception):
        _uow(path, _CentralAuthority(mutation=mutation)).create(_command())
    assert _counts(path) == (0, 0, 0, 0)


def test_v7_replay_rechecks_current_authority_and_conflict_is_write_zero(tmp_path: Path) -> None:
    path = tmp_path / "replay.sqlite"
    v2_upstream._seed(path)
    v2_upstream.create_sqlite_reciprocal_review_human_disposition_uow(path, trusted_human_authority_keys={"human": b"human-key"}, trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW).submit(v2_upstream._principal(), v2_upstream._command())
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    connection.close()
    authority = _CentralAuthority()
    uow = _uow(path, authority)
    first = uow.create(_command())
    assert uow.create(_command()) == first
    with pytest.raises(Exception):
        _uow(path, _CentralAuthority(current=False)).create(_command())
    with pytest.raises(Exception):
        uow.create(_command(receipt="other", key="v7-key"))
    assert _counts(path) == (1, 1, 1, 1)


@pytest.mark.parametrize("point", ("after_pending", "after_intent", "after_audit", "after_outbox"))
def test_v7_each_write_step_rolls_back(tmp_path: Path, point: str) -> None:
    path = tmp_path / f"{point}.sqlite"
    v2_upstream._seed(path)
    v2_upstream.create_sqlite_reciprocal_review_human_disposition_uow(path, trusted_human_authority_keys={"human": b"human-key"}, trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW).submit(v2_upstream._principal(), v2_upstream._command())
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    connection.close()
    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError(point)
    with pytest.raises(RuntimeError):
        _uow(path, _CentralAuthority(), fault=fault).create(_command())
    assert _counts(path) == (0, 0, 0, 0)


def test_v7_32_same_race_converges_and_different_key_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    v2_upstream._seed(path)
    v2_upstream.create_sqlite_reciprocal_review_human_disposition_uow(path, trusted_human_authority_keys={"human": b"human-key"}, trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW).submit(v2_upstream._principal(), v2_upstream._command())
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    connection.close()
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(_race_create, index, path) for index in range(32)]
        results = [future.result() for future in futures]
    assert {result.command_digest for result in results} == {results[0].command_digest}
    assert _counts(path) == (1, 1, 1, 1)
    with pytest.raises(Exception):
        _uow(path, _CentralAuthority()).create(_command(receipt="other", key="other-key"))
    assert _counts(path) == (1, 1, 1, 1)


@pytest.mark.parametrize("mode", ("static_acl", "scheduler_only", "no_every_read", "untrusted"))
def test_v7_noncontinuous_or_untrusted_integration_is_write_zero(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / f"integration-{mode}.sqlite"
    v2_upstream._seed(path)
    v2_upstream.create_sqlite_reciprocal_review_human_disposition_uow(path, trusted_human_authority_keys={"human": b"human-key"}, trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW).submit(v2_upstream._principal(), v2_upstream._command())
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    connection.close()
    if mode == "untrusted":
        with pytest.raises(Exception):
            _uow(path, _CentralAuthority(mutation="untrusted_integration")).create(_command())
    else:
        field = {
            "static_acl": "continuous_enforcement",
            "scheduler_only": "worker_fencing",
            "no_every_read": "every_read_attestation",
        }[mode]
        with pytest.raises(ValueError):
            IntegrationEnforcementProfileV7.model_validate(
                {"integration_id": "trusted-source", "profile_digest": "b" * 64, field: False}
            )
    assert _counts(path) == (0, 0, 0, 0)


def _ready_v7(path: Path) -> None:
    v2_upstream._seed(path)
    v2_upstream.create_sqlite_reciprocal_review_human_disposition_uow(path, trusted_human_authority_keys={"human": b"human-key"}, trusted_ai_execution_keys={"ai": b"ai-key"}, clock=lambda: NOW).submit(v2_upstream._principal(), v2_upstream._command())
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_source_binding_v7(connection)
    connection.close()
    _uow(path, _CentralAuthority()).create(_command())


def _ready_source_binding_worker(path: Path, executor: FakeSourceBindingExecutor) -> PendingWorker:
    _ready_v7(path)
    connection = sqlite3.connect(path)
    migrate_sqlite_source_binding_worker_v7(connection)
    connection.close()
    capability = _test_capability(_CentralAuthority(), path)
    assert capability is not None
    return PendingWorker(path, executor, capability)


def _worker_evidence(path: Path) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    connection = sqlite3.connect(path)
    attempts = connection.execute(
        "SELECT count(*) FROM source_binding_v7_apply_attempts"
    ).fetchone()[0]
    attempt_classes = tuple(
        row[0]
        for row in connection.execute(
            "SELECT classification FROM source_binding_v7_apply_attempts ORDER BY attempt_no"
        )
    )
    readback_classes = tuple(
        row[0]
        for row in connection.execute(
            "SELECT classification FROM source_binding_v7_readbacks ORDER BY attempt_no"
        )
    )
    connection.close()
    return attempts, attempt_classes, readback_classes


def _expire_worker_lease(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE source_binding_v7_worker_leases SET expires_at='2000-01-01T00:00:00.000Z'"
    )
    connection.commit()
    connection.close()


def test_v7_worker_duplicate_scanner_delivery_converges_to_one_semantic_mutation_and_immutable_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate-scanner.sqlite"
    executor = FakeSourceBindingExecutor()
    worker = _ready_source_binding_worker(path, executor)
    first_epoch, first_token = worker.claim(
        org_id="org", intent_id="v7-receipt", worker_id="scanner-1"
    )
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="scanner-1", epoch=first_epoch, token=first_token) == "observed"
    _expire_worker_lease(path)
    second_epoch, second_token = worker.claim(
        org_id="org", intent_id="v7-receipt", worker_id="scanner-2"
    )
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="scanner-2", epoch=second_epoch, token=second_token) == "observed"

    assert executor.calls == 2
    assert len(executor._operations) == 1
    assert type(executor.last_request) is worker_impl.SourceBindingOperationRequest
    assert cast(Any, executor.last_request.verified_authorization).receipt_id == "authority-receipt"
    assert _worker_evidence(path) == (2, ("dispatched", "dispatched"), ("observed", "observed"))
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT state_kind FROM durable_reciprocal_review_source_binding_cycles_v7"
    ).fetchone() == ("binding_pending",)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE source_binding_v7_apply_attempts SET classification='observed'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE source_binding_v7_readbacks SET classification='uncertain'")
    connection.close()


def test_v7_worker_timeout_then_late_result_keeps_pending_with_deterministic_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "timeout-late.sqlite"
    executor = FakeSourceBindingExecutor()
    executor.timeout = True
    executor.late = {"source_revision": "source-revision-1", "late": True}
    worker = _ready_source_binding_worker(path, executor)
    first_epoch, first_token = worker.claim(
        org_id="org", intent_id="v7-receipt", worker_id="scanner-1"
    )
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="scanner-1", epoch=first_epoch, token=first_token) == "uncertain"
    executor.timeout = False
    _expire_worker_lease(path)
    second_epoch, second_token = worker.claim(org_id="org", intent_id="v7-receipt", worker_id="scanner-2")
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="scanner-2", epoch=second_epoch, token=second_token) == "observed"

    assert executor.calls == 2
    assert len(executor._operations) == 1
    assert _worker_evidence(path) == (2, ("dispatched", "dispatched"), ("uncertain", "observed"))
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT state_kind FROM durable_reciprocal_review_source_binding_cycles_v7"
    ).fetchone() == ("binding_pending",)
    connection.close()


def test_v7_worker_source_cas_conflict_and_stale_lease_fence_leave_source_unmutated(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cas-and-fence.sqlite"
    executor = FakeSourceBindingExecutor(source_revision="source-revision-2")
    worker = _ready_source_binding_worker(path, executor)
    cas_epoch, cas_token = worker.claim(
        org_id="org", intent_id="v7-receipt", worker_id="scanner-cas"
    )
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="scanner-cas", epoch=cas_epoch, token=cas_token) == "uncertain"
    _expire_worker_lease(path)
    stale_epoch, stale_token = worker.claim(
        org_id="org", intent_id="v7-receipt", worker_id="scanner-stale"
    )
    _expire_worker_lease(path)
    worker.claim(org_id="org", intent_id="v7-receipt", worker_id="scanner-current")
    with pytest.raises(SourceBindingWorkerError, match="stale lease"):
        worker.execute(org_id="org", intent_id="v7-receipt", worker_id="scanner-stale", epoch=stale_epoch, token=stale_token)

    assert executor.calls == 1
    assert executor._operations == {}
    assert _worker_evidence(path) == (1, ("dispatched",), ("uncertain",))


def test_v7_worker_restart_reclaims_persisted_outbox_and_idempotently_completes_delivery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart-recovery.sqlite"
    executor = FakeSourceBindingExecutor()
    worker = _ready_source_binding_worker(path, executor)
    connection = sqlite3.connect(path)
    semantic_digest = connection.execute(
        "SELECT command_digest FROM reciprocal_review_v7_binding_intents WHERE org_id='org' AND receipt_id='v7-receipt'"
    ).fetchone()[0]
    operation = worker_digest({"intent": "v7-receipt", "semantic": semantic_digest})
    connection.execute(
        "INSERT INTO source_binding_v7_worker_outbox VALUES('org','v7-receipt',?,NULL)",
        (operation,),
    )
    connection.commit()
    connection.close()
    assert worker.recover(worker_id="restarted-scanner") == (("org", "v7-receipt"),)
    assert worker.recover(worker_id="restarted-scanner") == ()
    assert executor.calls == 1
    assert len(executor._operations) == 1
    assert _worker_evidence(path) == (1, ("dispatched",), ("observed",))
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT delivered_at IS NOT NULL FROM source_binding_v7_worker_outbox"
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT state_kind FROM durable_reciprocal_review_source_binding_cycles_v7"
    ).fetchone() == ("binding_pending",)
    connection.close()


def test_v7_worker_32_way_claim_reclaim_and_duplicate_delivery_has_one_fenced_semantic_effect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker-race.sqlite"
    executor = FakeSourceBindingExecutor()
    worker = _ready_source_binding_worker(path, executor)

    def claim(worker_id: str) -> tuple[str, int, str] | None:
        try:
            epoch, token = worker.claim(org_id="org", intent_id="v7-receipt", worker_id=worker_id)
            return worker_id, epoch, token
        except SourceBindingWorkerError:
            return None

    with ThreadPoolExecutor(max_workers=32) as pool:
        first_claims = list(pool.map(claim, (f"scanner-{index}" for index in range(32))))
    first = next(item for item in first_claims if item is not None)
    assert sum(item is not None for item in first_claims) == 1

    def deliver(_: int) -> str:
        try:
            return worker.execute(org_id="org", intent_id="v7-receipt", worker_id=first[0], epoch=first[1], token=first[2])
        except SourceBindingWorkerError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=32) as pool:
        first_delivery = list(pool.map(deliver, range(32)))
    assert first_delivery.count("observed") == 1
    assert first_delivery.count("rejected") == 31
    _expire_worker_lease(path)
    with ThreadPoolExecutor(max_workers=32) as pool:
        reclaimed = list(pool.map(claim, (f"reclaimer-{index}" for index in range(32))))
    second = next(item for item in reclaimed if item is not None)
    assert sum(item is not None for item in reclaimed) == 1
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id=second[0], epoch=second[1], token=second[2]) == "observed"
    assert executor.calls == 2
    assert len(executor._operations) == 1


def test_v7_worker_invalid_authority_before_or_just_before_executor_never_calls_source(
    tmp_path: Path,
) -> None:
    before_path = tmp_path / "authority-before.sqlite"
    before_executor = FakeSourceBindingExecutor()
    before = _ready_source_binding_worker(before_path, before_executor)
    before_epoch, before_token = before.claim(org_id="org", intent_id="v7-receipt", worker_id="before")
    before_handle = cast(Any, before.authority_capability._handle)  # pyright: ignore[reportPrivateUsage]
    before_handle._source_binding_closed = True
    with pytest.raises(SourceBindingWorkerError, match="authority capability unavailable"):
        before.execute(org_id="org", intent_id="v7-receipt", worker_id="before", epoch=before_epoch, token=before_token)
    assert before_executor.calls == 0
    assert _worker_evidence(before_path) == (0, (), ())

    just_before_path = tmp_path / "authority-just-before.sqlite"
    just_before_executor = FakeSourceBindingExecutor()
    just_before = _ready_source_binding_worker(just_before_path, just_before_executor)
    just_before_epoch, just_before_token = just_before.claim(org_id="org", intent_id="v7-receipt", worker_id="just-before")
    just_before_handle = cast(Any, just_before.authority_capability._handle)  # pyright: ignore[reportPrivateUsage]
    just_before.before_apply = lambda: setattr(just_before_handle, "_source_binding_closed", True)
    assert just_before.execute(org_id="org", intent_id="v7-receipt", worker_id="just-before", epoch=just_before_epoch, token=just_before_token) == "uncertain"
    assert just_before_executor.calls == 0
    assert _worker_evidence(just_before_path) == (1, ("dispatched",), ("uncertain",))
    connection = sqlite3.connect(just_before_path)
    assert connection.execute("SELECT delivered_at FROM source_binding_v7_worker_outbox").fetchone() == (None,)
    connection.close()
    just_before.before_apply = None
    just_before_handle._source_binding_closed = False
    _expire_worker_lease(just_before_path)
    assert just_before.recover(worker_id="recovery") == (("org", "v7-receipt"),)
    assert just_before.recover(worker_id="recovery") == ()
    assert just_before_executor.calls == 1
    assert _worker_evidence(just_before_path) == (2, ("dispatched", "dispatched"), ("uncertain", "observed"))


@pytest.mark.parametrize(
    ("point", "expected"),
    (
        ("after_lease", (0, 0, 0, 0)),
        ("after_attempt", (1, 0, 0, 0)),
        ("after_outbox", (1, 0, 0, 0)),
        ("after_observation", (1, 1, 0, 1)),
        ("after_delivery", (1, 1, 0, 1)),
    ),
)
def test_v7_worker_faults_roll_back_their_atomic_evidence_phase(
    tmp_path: Path, point: str, expected: tuple[int, int, int, int]
) -> None:
    path = tmp_path / f"fault-{point}.sqlite"
    executor = FakeSourceBindingExecutor()
    worker = _ready_source_binding_worker(path, executor)
    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError(actual)
    worker.fault_injector = fault
    if point == "after_lease":
        with pytest.raises(RuntimeError, match=point):
            worker.claim(org_id="org", intent_id="v7-receipt", worker_id="fault")
    else:
        epoch, token = worker.claim(org_id="org", intent_id="v7-receipt", worker_id="fault")
        with pytest.raises(RuntimeError, match=point):
            worker.execute(org_id="org", intent_id="v7-receipt", worker_id="fault", epoch=epoch, token=token)
    connection = sqlite3.connect(path)
    actual = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "source_binding_v7_worker_leases",
            "source_binding_v7_apply_attempts",
            "source_binding_v7_readbacks",
            "source_binding_v7_worker_outbox",
        )
    )
    connection.close()
    assert actual == expected


@pytest.mark.parametrize("surface", ("lease", "attempt", "observation", "outbox", "upstream"))
def test_v7_worker_strict_catalog_and_semantic_tamper_fails_on_next_entry(
    tmp_path: Path, surface: str
) -> None:
    path = tmp_path / f"tamper-{surface}.sqlite"
    executor = FakeSourceBindingExecutor()
    worker = _ready_source_binding_worker(path, executor)
    epoch, token = worker.claim(org_id="org", intent_id="v7-receipt", worker_id="tamper")
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="tamper", epoch=epoch, token=token) == "observed"
    connection = sqlite3.connect(path)
    if surface == "lease":
        connection.execute("UPDATE source_binding_v7_worker_leases SET token_hash='broken'")
    elif surface == "attempt":
        trigger = "source_binding_v7_apply_attempts_no_update"
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("UPDATE source_binding_v7_apply_attempts SET operation_digest='broken'")
        connection.execute(worker_impl._T[trigger])
    elif surface == "observation":
        trigger = "source_binding_v7_readbacks_no_update"
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("UPDATE source_binding_v7_readbacks SET observation_digest='broken'")
        connection.execute(worker_impl._T[trigger])
    elif surface == "outbox":
        connection.execute("UPDATE source_binding_v7_worker_outbox SET delivery_key='broken'")
    else:
        connection.execute("DROP TRIGGER durable_reciprocal_review_cycles_v2_legal_update")
        connection.execute("UPDATE durable_reciprocal_review_cycles_v2 SET cycle_revision=99")
        connection.execute(v2_upstream.human_disposition_module._TRIGGERS["durable_reciprocal_review_cycles_v2_legal_update"])
    connection.commit()
    connection.close()
    with pytest.raises(Exception):
        worker.claim(org_id="org", intent_id="v7-receipt", worker_id="next")


def test_v7_worker_executor_exception_is_uncertain_pending_and_has_no_bound_read_or_promotion_surface(
    tmp_path: Path,
) -> None:
    path = tmp_path / "executor-exception.sqlite"
    executor = FakeSourceBindingExecutor()
    worker = _ready_source_binding_worker(path, executor)
    epoch, token = worker.claim(org_id="org", intent_id="v7-receipt", worker_id="exception")
    executor.apply = lambda _request, _fence: (_ for _ in ()).throw(RuntimeError("executor exploded"))  # type: ignore[method-assign]
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="exception", epoch=epoch, token=token) == "uncertain"
    assert _worker_evidence(path) == (1, ("dispatched",), ("uncertain",))
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT state_kind FROM durable_reciprocal_review_source_binding_cycles_v7").fetchone() == ("binding_pending",)
    connection.close()
    assert not any(hasattr(worker_impl, name) for name in ("Bound", "read", "authorize_read", "proposal", "evaluate", "promotion"))


def test_v7_worker_rejects_caller_supplied_fake_authority_capability(tmp_path: Path) -> None:
    path = tmp_path / "fake-capability.sqlite"
    _ready_v7(path)
    connection = sqlite3.connect(path)
    migrate_sqlite_source_binding_worker_v7(connection)
    connection.close()
    with pytest.raises(ValueError, match="live bootstrap"):
        PendingWorker(path, FakeSourceBindingExecutor(), cast(Any, object()))


def test_v7_worker_rejects_arbitrary_executor_protocol_implementation(tmp_path: Path) -> None:
    path = tmp_path / "fake-executor.sqlite"
    _ready_v7(path)
    connection = sqlite3.connect(path)
    migrate_sqlite_source_binding_worker_v7(connection)
    connection.close()
    class CallerSuppliedExecutor:
        def apply(self, _request: object, _lease_fence: int) -> object:
            return {"forged": True}
    capability = _test_capability(_CentralAuthority(), path)
    assert capability is not None
    with pytest.raises(ValueError, match="FakeSourceBindingExecutor"):
        PendingWorker(path, cast(Any, CallerSuppliedExecutor()), capability)


def test_v7_worker_timeout_observation_stays_recoverable_until_recovery_observes_success(
    tmp_path: Path,
) -> None:
    path = tmp_path / "timeout-recoverable.sqlite"
    executor = FakeSourceBindingExecutor()
    executor.timeout = True
    worker = _ready_source_binding_worker(path, executor)
    epoch, token = worker.claim(org_id="org", intent_id="v7-receipt", worker_id="timeout")
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="timeout", epoch=epoch, token=token) == "uncertain"
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT delivered_at FROM source_binding_v7_worker_outbox").fetchone() == (None,)
    connection.close()
    executor.timeout = False
    _expire_worker_lease(path)
    assert worker.recover(worker_id="recovery") == (("org", "v7-receipt"),)
    assert worker.recover(worker_id="recovery") == ()
    assert executor.calls == 2
    assert len(executor._operations) == 1
    assert _worker_evidence(path) == (2, ("dispatched", "dispatched"), ("uncertain", "observed"))


def test_v7_worker_preapply_reclaim_fences_old_worker_before_external_call(tmp_path: Path) -> None:
    path = tmp_path / "preapply-fence.sqlite"
    executor = FakeSourceBindingExecutor()
    worker = _ready_source_binding_worker(path, executor)
    old_epoch, old_token = worker.claim(org_id="org", intent_id="v7-receipt", worker_id="old")

    def reclaim() -> None:
        _expire_worker_lease(path)
        worker.claim(org_id="org", intent_id="v7-receipt", worker_id="new")

    worker.before_apply = reclaim
    assert worker.execute(org_id="org", intent_id="v7-receipt", worker_id="old", epoch=old_epoch, token=old_token) == "uncertain"
    assert executor.calls == 0
    assert executor._operations == {}
    assert _worker_evidence(path) == (1, ("dispatched",), ("uncertain",))


@pytest.mark.parametrize("revoke", ("current", "source", "policy"))
def test_v7_worker_authority_revocation_blocks_claim_and_external_effect(
    tmp_path: Path, revoke: str
) -> None:
    path = tmp_path / f"authority-{revoke}.sqlite"
    executor = FakeSourceBindingExecutor()
    _ready_v7(path)
    connection = sqlite3.connect(path)
    migrate_sqlite_source_binding_worker_v7(connection)
    connection.close()
    wiring = _bootstrap_source_binding_wiring(
        issuer_registry={"central-1": KEY},
        resolver=lambda receipt, _purpose, _now: not (
            revoke == "current"
            or (revoke == "source" and receipt.source_ref == "source:trusted")
            or (revoke == "policy" and receipt.policy_digest == "c" * 64)
        ),
        database_wiring=path,
        source_wiring="source:trusted",
    )
    class _Authority:
        _state = "claimed"
    class _Handle:
        _source_binding_closed = False
        def __init__(self) -> None:
            self.authority_capability = _Authority()
    handle = _Handle()
    capability = wiring.open_for_bootstrap(handle, handle.authority_capability)
    assert capability is not None
    worker = PendingWorker(path, executor, capability)
    with pytest.raises(SourceBindingWorkerError, match="authorization denied"):
        worker.claim(org_id="org", intent_id="v7-receipt", worker_id="revoked")
    assert executor.calls == 0
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM source_binding_v7_worker_leases").fetchone() == (0,)
    connection.close()

@pytest.mark.parametrize(
    ("table", "trigger", "statement"),
    (
        ("reciprocal_review_v7_binding_intents", "reciprocal_review_v7_binding_intents_no_update", "UPDATE reciprocal_review_v7_binding_intents SET source_ref='source:forged'"),
        ("durable_reciprocal_review_source_binding_cycles_v7", "durable_reciprocal_review_source_binding_cycles_v7_no_update", "UPDATE durable_reciprocal_review_source_binding_cycles_v7 SET upstream_revision=9"),
        ("reciprocal_review_v7_binding_intents", "reciprocal_review_v7_binding_intents_no_update", "UPDATE reciprocal_review_v7_binding_intents SET authorization_json='{}'"),
        ("reciprocal_review_v7_binding_intents", "reciprocal_review_v7_binding_intents_no_update", "UPDATE reciprocal_review_v7_binding_intents SET authorization_digest='f' || substr(authorization_digest,2)"),
        ("reciprocal_review_v7_binding_intents", "reciprocal_review_v7_binding_intents_no_update", "UPDATE reciprocal_review_v7_binding_intents SET integration_profile_json='{}'"),
        ("reciprocal_review_v7_binding_audit", "reciprocal_review_v7_binding_audit_no_update", "UPDATE reciprocal_review_v7_binding_audit SET event_digest='f' || substr(event_digest,2)"),
        ("reciprocal_review_v7_binding_outbox", "reciprocal_review_v7_binding_outbox_no_update", "UPDATE reciprocal_review_v7_binding_outbox SET payload_digest='f' || substr(payload_digest,2)"),
        ("durable_reciprocal_review_source_binding_cycles_v7", "durable_reciprocal_review_source_binding_cycles_v7_no_delete", "DELETE FROM durable_reciprocal_review_source_binding_cycles_v7"),
    ),
)
def test_v7_trigger_restored_semantic_tamper_fails_on_next_entry(
    tmp_path: Path, table: str, trigger: str, statement: str
) -> None:
    path = tmp_path / f"tamper-{table}-{trigger}.sqlite"
    _ready_v7(path)
    connection = sqlite3.connect(path)
    connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(statement)
    connection.execute(v7_impl._T[trigger])
    connection.commit()
    connection.close()
    before = _counts(path)
    with pytest.raises(Exception):
        _uow(path, _CentralAuthority()).create(_command(receipt="new", key="new"))
    assert _counts(path) == before


def test_v6_legacy_evidence_has_no_pending_replay_bound_or_exposure_surface(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    v2_upstream._seed(path)
    connection = sqlite3.connect(path)
    migrate_sqlite_reciprocal_review_human_disposition_v2(connection)
    migrate_sqlite_reciprocal_review_source_binding_v6(connection)
    connection.close()
    with pytest.raises(SqliteReciprocalReviewSourceBindingError):
        create_sqlite_reciprocal_review_source_binding_intent_uow(
            path, adapter=cast(SourceBindingAdapter, object())
        )
    with pytest.raises(SqliteReciprocalReviewSourceBindingError):
        v6_impl._Uow(path, cast(SourceBindingAdapter, object()), {}, {}, None).create(
            cast(CreateSourceBindingIntent, object())
        )
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT count(*) FROM durable_reciprocal_review_source_binding_cycles_v6"
    ).fetchone() == (0,)
    connection.close()
    assert not any(
        hasattr(v7_impl, name)
        for name in ("apply", "bind", "expose", "read_source", "evaluate", "promote")
    )
