from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest
from pydantic import SecretStr, ValidationError

from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
)
from agent_org_network.knowledge_index import Concept, KnowledgeIndex
from agent_org_network.sqlite_published_index_acceptance import (
    AcceptPublishedIndexCommand,
    PublishedIndexAcceptanceUnavailable,
    SqlitePublishedIndexAcceptance,
)

from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    ProductionAgentCardCommand,
    SqliteProductionAgentCards,
)
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringRunResource,
    AuthoringRunCommand,
    AuthoringSourceRef,
    CurrentAuthoringRunAuthorization,
    ExtractingRun,
    ReviewedRun,
    CompleteAuthoringRunCommand,
    BeginAuthoringRunPublishCommand,
    ReviewAuthoringRunCommand,
    ProductionAuthoringRunConflict,
    ProductionAuthoringRunDenied,
    ProductionAuthoringRunUnavailable,
    PublishingRun,
    SqliteProductionAuthoringRuns,
    StartAuthoringRunCommand,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)


def _invocation(session: str = "s" * 32) -> AuthoringInvocation:
    return AuthoringInvocation(
        session=AuthoringIdentitySessionRef(value=SecretStr(session)),
        org_id="acme",
        principal_id="owner",
        identity_provider="oidc",
    )


class _UserAllow:
    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


class _CardAllow:
    def current(
        self, command: ProductionAgentCardCommand, transaction: sqlite3.Connection
    ) -> CurrentCardRegistrationAuthorization:
        _ = command, transaction
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(
        self,
        command: ProductionAgentCardCommand,
        evidence: CurrentCardRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


class _Allow:
    def current(
        self,
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
        source_set_digest: str,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> CurrentAuthoringRunAuthorization:
        if isinstance(command, StartAuthoringRunCommand):
            run_id = SqliteProductionAuthoringRuns._run_id(command)  # pyright: ignore[reportPrivateUsage]
            resource_fingerprint = SqliteProductionAuthoringRuns._fingerprint(  # pyright: ignore[reportPrivateUsage]
                resource, run_id, source_set_digest
            )
        elif isinstance(command, CompleteAuthoringRunCommand):
            row = transaction.execute(
                "SELECT * FROM production_authoring_runs "
                "WHERE org_id=? AND run_id=?",
                (command.organization_id, command.run_id),
            ).fetchone()
            assert row is not None
            run = ExtractingRun(
                org_id=row["org_id"],
                run_id=row["run_id"],
                agent_id=row["agent_id"],
                owner_id=row["owner_id"],
                card_revision=row["card_revision"],
                card_digest=row["card_digest"],
                source_set_digest=row["source_set_digest"],
                source_count=row["source_count"],
                total_bytes=row["total_bytes"],
                created_at=row["created_at"],
            )
            resource_fingerprint = SqliteProductionAuthoringRuns._completion_fingerprint(  # pyright: ignore[reportPrivateUsage]
                command, run
            )
        elif isinstance(command, ReviewAuthoringRunCommand):
            row = transaction.execute(
                "SELECT * FROM production_authoring_runs WHERE org_id=? AND run_id=?",
                (command.organization_id, command.run_id),
            ).fetchone()
            assert row is not None
            run = ExtractingRun(
                org_id=row["org_id"], run_id=row["run_id"], agent_id=row["agent_id"],
                owner_id=row["owner_id"], card_revision=row["card_revision"],
                card_digest=row["card_digest"], source_set_digest=row["source_set_digest"],
                source_count=row["source_count"], total_bytes=row["total_bytes"], created_at=row["created_at"],
            )
            resource_fingerprint = SqliteProductionAuthoringRuns._review_fingerprint(  # pyright: ignore[reportPrivateUsage]
                command, run  # pyright: ignore[reportArgumentType]
            )
        else:
            assert isinstance(command, BeginAuthoringRunPublishCommand)
            row = transaction.execute(
                "SELECT * FROM production_authoring_runs WHERE org_id=? AND run_id=?",
                (command.organization_id, command.run_id),
            ).fetchone()
            assert row is not None
            resource_fingerprint = SqliteProductionAuthoringRuns._publish_fingerprint(  # pyright: ignore[reportPrivateUsage]
                command,
                ReviewedRun(
                    org_id=row["org_id"], run_id=row["run_id"], agent_id=row["agent_id"],
                    owner_id=row["owner_id"], card_revision=row["card_revision"],
                    card_digest=row["card_digest"], source_set_digest=row["source_set_digest"],
                    source_count=row["source_count"], total_bytes=row["total_bytes"], created_at=row["created_at"],
                    admitted_bundle_digest=row["admitted_bundle_digest"], document_count=row["document_count"],
                    edge_count=row["edge_count"], dropped_count=row["dropped_count"],
                    author_profile_digest=row["author_profile_digest"], completed_at=row["completed_at"],
                    outcome=row["review_outcome"], reviewed_at=row["reviewed_at"],
                ),
            )
        return CurrentAuthoringRunAuthorization(
            policy_version="v1", policy_digest="a" * 64,
            grant_evidence_digest="b" * 64, identity_session_digest="c" * 64,
            identity_evidence_digest="d" * 64,
            resource_fingerprint=resource_fingerprint,
        )

    def verify_precommit(
        self,
        command: AuthoringRunCommand,
        resource: AuthoringRunResource,
        source_set_digest: str,
        evidence: CurrentAuthoringRunAuthorization,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, resource, source_set_digest, evidence, transaction
        return True


def _store(path: Path) -> SqliteProductionAuthoringRuns:
    SqliteProductionRegistryUsers.migrate(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme",
            principal_id="owner",
            idempotency_key="user-1",
            expected_revision=0,
            user_id="owner",
            email="owner@example.com",
        )
    )
    SqliteProductionAgentCards.migrate(path)
    cards = SqliteProductionAgentCards(path, authorize=_CardAllow())
    cards.register(
        ProductionAgentCardCommand(
            org_id="acme",
            principal_id="owner",
            idempotency_key="card-1",
            expected_revision=1,
            card={  # type: ignore[arg-type]
                "agent_id": "support",
                "owner": "owner",
                "team": "support",
                "summary": "support",
                "domains": ["support"],
                "last_reviewed_at": "2026-07-27",
                "maintainer": None,
                "can_answer": ["refund"],
                "cannot_answer": [],
                "approval_when": [],
                "collaborate_when": [],
                "knowledge_sources": ["okf"],
                "trust_labels": ["internal"],
            },
        )
    )
    SqliteProductionAuthoringRuns.migrate(path)
    return SqliteProductionAuthoringRuns(path, authorize=_Allow())


def test_invocation은_mandatory이고_raw_session은_durable_repr0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    command = _bound_command(path)
    before = path.read_bytes()
    with pytest.raises(TypeError):
        store.start(command)  # type: ignore[call-arg]
    assert path.read_bytes() == before

    raw_session = "raw_session_secret_12345678901234"
    invocation = _invocation(raw_session)
    store.start(command, invocation=invocation)
    assert raw_session not in repr(invocation)
    assert raw_session.encode() not in path.read_bytes()


def _command(**changes: object) -> StartAuthoringRunCommand:
    values: dict[str, object] = {
        "org_id": "acme",
        "principal_id": "owner",
        "idempotency_key": "start-1",
        "agent_id": "support",
        "expected_card_revision": 2,
        "expected_card_digest": "",
        "sources": (
            AuthoringSourceRef(
                source_digest="c" * 64, byte_size=12, media_type="text/markdown"
            ),
        ),
    }
    values.update(changes)
    return StartAuthoringRunCommand(**values)  # type: ignore[arg-type]


def _bound_command(path: Path, **changes: object) -> StartAuthoringRunCommand:
    connection = sqlite3.connect(path)
    digest = connection.execute(
        "SELECT card_digest FROM production_agent_cards WHERE org_id='acme'"
    ).fetchone()[0]
    connection.close()
    changes.setdefault("expected_card_digest", digest)
    return _command(**changes)


def test_start는_run_sources_receipt_audit_outbox를_원자확정한다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    result = store.start(_bound_command(path), invocation=_invocation())
    assert result.run.stage == "Extracting"
    assert result.run.revision == 0
    assert result.run.owner_id == "owner"
    assert store.runs("acme") == (result.run,)
    assert store.sources("acme", result.run.run_id) == _bound_command(path).sources
    assert store.counts("acme") == {
        "runs": 1,
        "sources": 1,
        "receipts": 1,
        "audit": 1,
        "outbox": 1,
    }


def test_same_command_32way는_single_write와_exact_replay다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    _store(path)
    command = _bound_command(path)

    def run(_index: int) -> bool:
        return SqliteProductionAuthoringRuns(path, authorize=_Allow()).start(command, invocation=_invocation()).replayed

    with ThreadPoolExecutor(max_workers=32) as pool:
        replayed = list(pool.map(run, range(32)))
    assert replayed.count(False) == 1
    assert replayed.count(True) == 31


def test_card_digest_revision_owner_drift는_write0이다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    for command in (
        _bound_command(path, expected_card_revision=1),
        _bound_command(path, expected_card_digest="d" * 64),
        _bound_command(path, principal_id="other"),
    ):
        with pytest.raises(ProductionAuthoringRunConflict):
            store.start(command, invocation=_invocation())
    assert store.runs("acme") == ()


@pytest.mark.parametrize("field", ["raw", "body", "text", "content", "draft"])
def test_source_ref는_raw_like_field를_받지_않는다(field: str) -> None:
    with pytest.raises(ValidationError):
        AuthoringSourceRef(
            source_digest="c" * 64,
            byte_size=1,
            media_type="text/plain",
            **{field: "secret"},
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"byte_size": True},
        {"byte_size": "1"},
        {"media_type": "text/html"},
        {"source_digest": "C" * 64},
    ],
)
def test_source_ref는_strict_canonical_scalar만_받는다(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "source_digest": "c" * 64,
        "byte_size": 1,
        "media_type": "text/plain",
    }
    values.update(changes)
    with pytest.raises(ValidationError):
        AuthoringSourceRef(**values)  # type: ignore[arg-type]


def test_sources는_tuple_digest정렬_unique_count_total상한을_강제한다() -> None:
    source = AuthoringSourceRef(
        source_digest="c" * 64, byte_size=1, media_type="text/plain"
    )
    with pytest.raises(ValidationError):
        _command(expected_card_digest="d" * 64, sources=[source])
    with pytest.raises(ValidationError):
        _command(expected_card_digest="d" * 64, sources=(source, source))
    with pytest.raises(ValidationError):
        _command(
            expected_card_digest="d" * 64,
            sources=(
                source,
                AuthoringSourceRef(
                    source_digest="b" * 64, byte_size=1, media_type="text/plain"
                ),
            ),
        )
    with pytest.raises(ValidationError):
        _command(
            expected_card_digest="d" * 64,
            sources=tuple(
                AuthoringSourceRef(
                    source_digest=f"{index:064x}",
                    byte_size=1,
                    media_type="text/plain",
                )
                for index in range(1, 34)
            ),
        )
    with pytest.raises(ValidationError):
        _command(
            expected_card_digest="d" * 64,
            sources=tuple(
                AuthoringSourceRef(
                    source_digest=f"{index:064x}",
                    byte_size=4_000_000,
                    media_type="text/plain",
                )
                for index in range(1, 33)
            ),
        )


def test_same_key_different_payload는_conflict다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    store.start(_bound_command(path), invocation=_invocation())
    with pytest.raises(ProductionAuthoringRunConflict):
        store.start(
            _bound_command(
                path,
                sources=(
                    AuthoringSourceRef(
                        source_digest="d" * 64,
                        byte_size=12,
                        media_type="text/plain",
                    ),
                ),
            ),
            invocation=_invocation(),
        )


def test_current_authorization_drift는_write0이다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    _store(path)

    class _DenyPrecommit(_Allow):
        def verify_precommit(
            self,
            command: AuthoringRunCommand,
            resource: AuthoringRunResource,
            source_set_digest: str,
            evidence: CurrentAuthoringRunAuthorization,
            invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> bool:
            _ = command, resource, source_set_digest, evidence, transaction
            return False

    store = SqliteProductionAuthoringRuns(path, authorize=_DenyPrecommit())
    with pytest.raises(ProductionAuthoringRunDenied):
        store.start(_bound_command(path), invocation=_invocation())
    assert store.counts("acme")["runs"] == 0


def test_replay는_current_authorization_snapshot_drift를_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    command = _bound_command(path)
    store.start(command, invocation=_invocation())

    class _Drifted(_Allow):
        def current(
            self,
            command: AuthoringRunCommand,
            resource: AuthoringRunResource,
            source_set_digest: str,
            invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> CurrentAuthoringRunAuthorization:
            return super().current(
                command, resource, source_set_digest, invocation, transaction
            ).model_copy(
                update={
                    "policy_version": "v2",
                    "policy_digest": "d" * 64,
                    "grant_evidence_digest": "e" * 64,
                    "identity_session_digest": "c" * 64,
                    "identity_evidence_digest": "d" * 64,
                }
            )

    assert SqliteProductionAuthoringRuns(
        path, authorize=_Drifted()
    ).start(command, invocation=_invocation("n" * 32)).replayed


def test_start_replay_current_resource_fingerprint_mismatch는_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    command = _bound_command(path)
    store.start(command, invocation=_invocation())
    before = path.read_bytes()

    class _WrongResource(_Allow):
        def current(
            self, command: AuthoringRunCommand, resource: AuthoringRunResource,
            source_set_digest: str, invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> CurrentAuthoringRunAuthorization:
            return super().current(
                command, resource, source_set_digest, invocation, transaction
            ).model_copy(
                update={"resource_fingerprint": "f" * 64}
            )

    with pytest.raises(ProductionAuthoringRunDenied):
        SqliteProductionAuthoringRuns(path, authorize=_WrongResource()).start(
            command, invocation=_invocation("n" * 32)
        )
    assert path.read_bytes() == before


def test_central_rows에는_raw_body_marker가_없다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    store.start(_bound_command(path), invocation=_invocation())
    marker = "PRIVATE-RAW-DOCUMENT-MARKER"
    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'production_authoring_%'"
    ).fetchall()
    for (name,) in rows:
        if str(name).startswith("sqlite_autoindex"):
            continue
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()[0]
        assert marker not in repr(sql)
        if str(sql).startswith("CREATE TABLE"):
            assert marker not in repr(connection.execute(f'SELECT * FROM "{name}"').fetchall())
    connection.close()


def test_O4는_review만_허용하고_중앙_publish_index_git_surface가_없다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    assert hasattr(store, "review")
    assert not hasattr(store, "publish")
    assert not hasattr(store, "index")
    assert not hasattr(store, "commit_git")
    connection = sqlite3.connect(path)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'production_authoring_%'"
        )
    }
    connection.close()
    assert not any(
        marker in name
        for name in names
        for marker in ("publish", "index", "git")
    )


@pytest.mark.parametrize(
    "point",
    ["after_run", "after_source", "after_receipt", "after_audit", "after_outbox", "before_commit"],
)
def test_fault는_companion_부분쓰기를_남기지_않는다(tmp_path: Path, point: str) -> None:
    path = tmp_path / "db.sqlite"
    _store(path)

    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError("fault")

    store = SqliteProductionAuthoringRuns(path, authorize=_Allow(), fault_injector=fault)
    with pytest.raises(RuntimeError):
        store.start(_bound_command(path), invocation=_invocation())
    assert store.counts("acme") == {
        "runs": 0, "sources": 0, "receipts": 0, "audit": 0, "outbox": 0
    }


def test_tamper와_raw_marker는_reader에서_failclosed다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    result = store.start(_bound_command(path), invocation=_invocation())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_authoring_sources_immutable")
    connection.execute(
        "UPDATE production_authoring_source_refs SET source_digest=?",
        ("d" * 64,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.sources("acme", result.run.run_id)


@pytest.mark.parametrize(
    ("table", "mutation"),
    [
        (
            "production_authoring_command_receipts",
            "UPDATE production_authoring_command_receipts SET command_digest='"
            + "d" * 64
            + "'",
        ),
        (
            "production_authoring_audit_intents",
            "UPDATE production_authoring_audit_intents SET identity_evidence_digest='"
            + "e" * 64
            + "'",
        ),
        (
            "production_authoring_audit_intents",
            "DELETE FROM production_authoring_audit_intents",
        ),
        (
            "production_authoring_outbox_intents",
            "DELETE FROM production_authoring_outbox_intents",
        ),
    ],
)
def test_receipt_audit_outbox_missing_tamper는_replay에서_failclosed다(
    tmp_path: Path, table: str, mutation: str
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    command = _bound_command(path)
    store.start(command, invocation=_invocation())
    connection = sqlite3.connect(path)
    trigger_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ).fetchall()
    for (trigger,) in trigger_rows:
        connection.execute(f'DROP TRIGGER "{trigger}"')
    connection.execute(mutation)
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.start(command, invocation=_invocation())


@pytest.mark.parametrize(
    "parent_object",
    [
        "production_agent_card_receipts_immutable",
        "production_registry_user_receipts_immutable",
    ],
)
def test_O1_O2_parent_schema_drift는_open_reader에서_failclosed다(
    tmp_path: Path, parent_object: str
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    connection = sqlite3.connect(path)
    connection.execute(f'DROP TRIGGER "{parent_object}"')
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        SqliteProductionAuthoringRuns(path, authorize=_Allow())
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.runs("acme")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE production_authoring_source_refs SET byte_size=13",
        "DELETE FROM production_authoring_source_refs",
        "UPDATE production_authoring_runs SET source_count=2",
        "DELETE FROM production_authoring_runs",
    ],
)
def test_run_start와_source_refs는_직접변조할수없다(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    store.start(_bound_command(path), invocation=_invocation())
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)
    connection.close()


def test_coordinated_run_source_tamper도_immutable_start_companion_anchor가_거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    result = store.start(_bound_command(path), invocation=_invocation())
    connection = sqlite3.connect(path)
    for trigger in (
        "production_authoring_runs_immutable",
        "production_authoring_sources_immutable",
    ):
        connection.execute(f'DROP TRIGGER "{trigger}"')
    replacement = AuthoringSourceRef(
        source_digest="d" * 64, byte_size=13, media_type="text/plain"
    )
    source_set_digest = sha256(
        json.dumps(
            [replacement.model_dump(mode="json")],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    connection.execute(
        "UPDATE production_authoring_source_refs "
        "SET source_digest=?,byte_size=?,media_type=?",
        (replacement.source_digest, replacement.byte_size, replacement.media_type),
    )
    connection.execute(
        "UPDATE production_authoring_runs "
        "SET source_set_digest=?,source_count=1,total_bytes=13",
        (source_set_digest,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.runs("acme")
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.sources("acme", result.run.run_id)


def _complete_command(
    path: Path, run_id: str, **changes: object
) -> CompleteAuthoringRunCommand:
    connection = sqlite3.connect(path)
    revision, digest = connection.execute(
        "SELECT revision,card_digest FROM production_agent_cards "
        "WHERE org_id='acme' AND agent_id='support'"
    ).fetchone()
    connection.close()
    values: dict[str, object] = {
        "organization_id": "acme",
        "principal_id": "owner",
        "idempotency_key": "complete-1",
        "run_id": run_id,
        "expected_revision": 0,
        "expected_card_revision": revision,
        "expected_card_digest": digest,
        "admitted_bundle_digest": "e" * 64,
        "document_count": 2,
        "edge_count": 3,
        "dropped_count": 1,
        "author_profile_digest": "f" * 64,
    }
    values.update(changes)
    return CompleteAuthoringRunCommand(**values)  # type: ignore[arg-type]


def _publish_command(
    run: ReviewedRun, **changes: object
) -> BeginAuthoringRunPublishCommand:
    values: dict[str, object] = {
        "organization_id": run.org_id,
        "principal_id": run.owner_id,
        "idempotency_key": "publish-1",
        "run_id": run.run_id,
        "expected_card_revision": run.card_revision,
        "expected_card_digest": run.card_digest,
    }
    values.update(changes)
    return BeginAuthoringRunPublishCommand(**values)  # type: ignore[arg-type]


def test_complete는_extracting0을_awaiting_owner_review1로_원자전이한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    result = store.complete(_complete_command(path, started.run.run_id), invocation=_invocation())
    assert result.run.stage == "AwaitingOwnerReview"
    assert result.run.revision == 1
    assert result.run.admitted_bundle_digest == "e" * 64
    assert store.get("acme", started.run.run_id) == result.run
    assert store.counts("acme") == {
        "runs": 1, "sources": 1, "receipts": 2, "audit": 2, "outbox": 2
    }


def test_review는_exact_bundle만_Reviewed2로_전이하고_재시도는안전하다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    awaiting = store.complete(_complete_command(path, started.run.run_id), invocation=_invocation()).run
    command = ReviewAuthoringRunCommand(
        organization_id="acme", principal_id="owner", idempotency_key="review-1",
        run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
        expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
        draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
    )
    result = store.review(command, invocation=_invocation())
    assert result.run.stage == "Reviewed" and result.run.revision == 2
    assert store.review(command, invocation=_invocation()).replayed
    with pytest.raises(ProductionAuthoringRunConflict):
        store.review(command.model_copy(update={"outcome": "Rejected"}), invocation=_invocation())


@pytest.mark.parametrize(
    ("table", "mutation"),
    [
        ("production_authoring_command_receipts", "DELETE FROM production_authoring_command_receipts WHERE action_kind='authoring_run.review'"),
        ("production_authoring_audit_intents", "UPDATE production_authoring_audit_intents SET review_outcome='Rejected' WHERE event_kind='run_reviewed'"),
        ("production_authoring_outbox_intents", "DELETE FROM production_authoring_outbox_intents WHERE kind='authoring.run_reviewed'"),
    ],
)
def test_publish는_exact_approved_review_anchor가_없거나변조되면_failclosed한다(
    tmp_path: Path, table: str, mutation: str
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation()).run
    awaiting = store.complete(_complete_command(path, started.run_id), invocation=_invocation()).run
    review = ReviewAuthoringRunCommand(
        organization_id="acme", principal_id="owner", idempotency_key="review-publish-anchor",
        run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
        expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
        draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
    )
    reviewed = store.review(review, invocation=_invocation()).run
    assert isinstance(reviewed, ReviewedRun)
    connection = sqlite3.connect(path)
    trigger_sql = [
        sql
        for (_trigger, sql) in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
        )
    ]
    for (trigger,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ):
        connection.execute(f'DROP TRIGGER "{trigger}"')
    connection.execute(mutation)
    for sql in trigger_sql:
        connection.execute(sql)
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.begin_publish(_publish_command(reviewed), invocation=_invocation())


@pytest.mark.parametrize(
    ("table", "mutation"),
    [
        ("production_authoring_command_receipts", "UPDATE production_authoring_command_receipts SET result_state='reviewed' WHERE action_kind='authoring_run.publish_begin'"),
        ("production_authoring_audit_intents", "UPDATE production_authoring_audit_intents SET review_outcome='Rejected' WHERE event_kind='run_publish_claimed'"),
        ("production_authoring_outbox_intents", "DELETE FROM production_authoring_outbox_intents WHERE kind='authoring.run_publish_claimed'"),
    ],
)
def test_publish_replay는_모든_companion_field를_exact하게검증한다(
    tmp_path: Path, table: str, mutation: str
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation()).run
    awaiting = store.complete(_complete_command(path, started.run_id), invocation=_invocation()).run
    reviewed = store.review(
        ReviewAuthoringRunCommand(
            organization_id="acme", principal_id="owner", idempotency_key="review-publish-replay",
            run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
            expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
            draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
        ), invocation=_invocation(),
    ).run
    assert isinstance(reviewed, ReviewedRun)
    command = _publish_command(reviewed)
    assert store.begin_publish(command, invocation=_invocation()).replayed is False
    connection = sqlite3.connect(path)
    trigger_sql = [
        sql for (_name, sql) in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
        )
    ]
    for (trigger,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)
    ):
        connection.execute(f'DROP TRIGGER "{trigger}"')
    connection.execute(mutation)
    for sql in trigger_sql:
        connection.execute(sql)
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.begin_publish(command, invocation=_invocation())


def test_review_replay는_current_card_drift와_companion_tamper를_failclosed한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    awaiting = store.complete(_complete_command(path, started.run.run_id), invocation=_invocation()).run
    command = ReviewAuthoringRunCommand(
        organization_id="acme", principal_id="owner", idempotency_key="review-replay",
        run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
        expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
        draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
    )
    store.review(command, invocation=_invocation())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_authoring_audit_immutable")
    connection.execute(
        "UPDATE production_authoring_audit_intents SET edge_count=99 "
        "WHERE event_kind='run_reviewed'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.review(command, invocation=_invocation())


def test_review_replay는_current_card와_authorization_drift를_거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    awaiting = store.complete(_complete_command(path, started.run.run_id), invocation=_invocation()).run
    command = ReviewAuthoringRunCommand(
        organization_id="acme", principal_id="owner", idempotency_key="review-drift",
        run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
        expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
        draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
    )
    store.review(command, invocation=_invocation())

    class _WrongResource(_Allow):
        def current(
            self, command: AuthoringRunCommand, resource: AuthoringRunResource,
            source_set_digest: str, invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> CurrentAuthoringRunAuthorization:
            return super().current(
                command, resource, source_set_digest, invocation, transaction
            ).model_copy(update={"resource_fingerprint": "f" * 64})

    with pytest.raises(ProductionAuthoringRunDenied):
        SqliteProductionAuthoringRuns(path, authorize=_WrongResource()).review(
            command, invocation=_invocation()
        )
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE production_agent_cards SET revision=999 WHERE org_id='acme' "
        "AND agent_id='support'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunConflict):
        store.review(command, invocation=_invocation())


@pytest.mark.parametrize(
    "point",
    ["after_transition", "after_receipt", "after_audit", "after_outbox", "before_commit"],
)
def test_review_fault는_rev1과_review_companion0을_보존한다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "db.sqlite"
    base = _store(path)
    started = base.start(_bound_command(path), invocation=_invocation())
    awaiting = base.complete(_complete_command(path, started.run.run_id), invocation=_invocation()).run
    command = ReviewAuthoringRunCommand(
        organization_id="acme", principal_id="owner", idempotency_key="review-fault",
        run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
        expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
        draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
    )

    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError("fault")

    store = SqliteProductionAuthoringRuns(path, authorize=_Allow(), fault_injector=fault)
    with pytest.raises(RuntimeError):
        store.review(command, invocation=_invocation())
    assert store.get("acme", started.run.run_id).stage == "AwaitingOwnerReview"
    assert store.counts("acme") == {"runs": 1, "sources": 1, "receipts": 2, "audit": 2, "outbox": 2}


def test_complete_same_command_replay와_different_payload_conflict다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    command = _complete_command(path, started.run.run_id)
    assert store.complete(command, invocation=_invocation()).replayed is False
    assert store.complete(command, invocation=_invocation()).replayed is True
    with pytest.raises(ProductionAuthoringRunConflict):
        store.complete(
            command.model_copy(update={"edge_count": 4}), invocation=_invocation()
        )


def test_complete_same_command_32way는_single_transition이다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    command = _complete_command(path, started.run.run_id)

    def run(_index: int) -> bool:
        return SqliteProductionAuthoringRuns(
            path, authorize=_Allow()
        ).complete(command, invocation=_invocation()).replayed

    with ThreadPoolExecutor(max_workers=32) as pool:
        replayed = list(pool.map(run, range(32)))
    assert replayed.count(False) == 1
    assert replayed.count(True) == 31


def test_complete_command는_raw_full_draft_field를_거부한다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    values = _complete_command(path, started.run.run_id).model_dump(mode="python")
    values["draft"] = "PRIVATE RAW BODY"
    with pytest.raises(ValidationError):
        CompleteAuthoringRunCommand(**values)


def test_completion_companion_tamper는_read에서_failclosed다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    store.complete(_complete_command(path, started.run.run_id), invocation=_invocation())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_authoring_audit_immutable")
    connection.execute(
        "UPDATE production_authoring_audit_intents SET edge_count=99 "
        "WHERE event_kind='run_completed'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.get("acme", started.run.run_id)


def test_complete뒤_same_start는_original_extracting0_result를_exact_replay한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    start_command = _bound_command(path)
    started = store.start(start_command, invocation=_invocation())
    store.complete(_complete_command(path, started.run.run_id), invocation=_invocation())
    replay = store.start(start_command, invocation=_invocation())
    assert replay.replayed is True
    assert replay.run == started.run
    assert replay.run.stage == "Extracting"
    assert replay.run.revision == 0


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("owner_id", "other"),
        ("revision", 999),
        ("card_digest", "d" * 64),
    ],
)
def test_complete는_current_card_owner_revision_digest_drift를_거부한다(
    tmp_path: Path, column: str, value: object
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    connection = sqlite3.connect(path)
    if column == "owner_id":
        connection.execute(
            "INSERT INTO production_registry_users "
            "(org_id,user_id,email,manager_id,revision) "
            "VALUES ('acme','other','other@example.com',NULL,999)"
        )
    connection.execute(
        f'UPDATE production_agent_cards SET "{column}"=? WHERE org_id=? AND agent_id=?',
        (value, "acme", "support"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ProductionAuthoringRunConflict):
        store.complete(_complete_command(path, started.run.run_id), invocation=_invocation())


def test_complete_precommit_authorization_deny는_write0이다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    base = _store(path)
    started = base.start(_bound_command(path), invocation=_invocation())

    class _CompletionDeny(_Allow):
        def verify_precommit(
            self,
            command: AuthoringRunCommand,
            resource: AuthoringRunResource,
            source_set_digest: str,
            evidence: CurrentAuthoringRunAuthorization,
            invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> bool:
            _ = resource, source_set_digest, evidence, transaction
            return not isinstance(command, CompleteAuthoringRunCommand)

    store = SqliteProductionAuthoringRuns(path, authorize=_CompletionDeny())
    with pytest.raises(ProductionAuthoringRunDenied):
        store.complete(_complete_command(path, started.run.run_id), invocation=_invocation())
    assert store.get("acme", started.run.run_id).stage == "Extracting"


def test_complete_precommit_authorization_evidence_drift는_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    base = _store(path)
    started = base.start(_bound_command(path), invocation=_invocation())

    class _EvidenceDrift(_Allow):
        calls = 0

        def current(
            self,
            command: AuthoringRunCommand,
            resource: AuthoringRunResource,
            source_set_digest: str,
            invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> CurrentAuthoringRunAuthorization:
            _ = command, resource, source_set_digest, transaction
            self.calls += 1
            return CurrentAuthoringRunAuthorization(
                policy_version=f"v{self.calls}",
                policy_digest="a" * 64,
                grant_evidence_digest="b" * 64,
                identity_session_digest="c" * 64,
                identity_evidence_digest="d" * 64,
                resource_fingerprint="f" * 64,
            )

        def verify_precommit(
            self,
            command: AuthoringRunCommand,
            resource: AuthoringRunResource,
            source_set_digest: str,
            evidence: CurrentAuthoringRunAuthorization,
            invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> bool:
            return (
                self.current(command, resource, source_set_digest, invocation, transaction)
                == evidence
            )

    store = SqliteProductionAuthoringRuns(path, authorize=_EvidenceDrift())
    with pytest.raises(ProductionAuthoringRunDenied):
        store.complete(_complete_command(path, started.run.run_id), invocation=_invocation())
    assert store.get("acme", started.run.run_id).stage == "Extracting"


def test_complete_replay는_current_authorization_evidence_drift를_거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    command = _complete_command(path, started.run.run_id)
    store.complete(command, invocation=_invocation())

    class _CompletionDrift(_Allow):
        def current(
            self,
            command: AuthoringRunCommand,
            resource: AuthoringRunResource,
            source_set_digest: str,
            invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> CurrentAuthoringRunAuthorization:
            return super().current(
                command, resource, source_set_digest, invocation, transaction
            ).model_copy(
                update={
                    "policy_version": "v2",
                    "policy_digest": "d" * 64,
                    "grant_evidence_digest": "e" * 64,
                    "identity_session_digest": "c" * 64,
                    "identity_evidence_digest": "d" * 64,
                }
            )

    assert SqliteProductionAuthoringRuns(
        path, authorize=_CompletionDrift()
    ).complete(command, invocation=_invocation("n" * 32)).replayed


def test_complete_replay_current_resource_fingerprint_mismatch는_write0이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store = _store(path)
    started = store.start(_bound_command(path), invocation=_invocation())
    command = _complete_command(path, started.run.run_id)
    store.complete(command, invocation=_invocation())
    before = path.read_bytes()

    class _WrongResource(_Allow):
        def current(
            self, command: AuthoringRunCommand, resource: AuthoringRunResource,
            source_set_digest: str, invocation: AuthoringInvocation,
            transaction: sqlite3.Connection,
        ) -> CurrentAuthoringRunAuthorization:
            return super().current(
                command, resource, source_set_digest, invocation, transaction
            ).model_copy(
                update={"resource_fingerprint": "f" * 64}
            )

    with pytest.raises(ProductionAuthoringRunDenied):
        SqliteProductionAuthoringRuns(path, authorize=_WrongResource()).complete(
            command, invocation=_invocation("n" * 32)
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "point",
    ["after_transition", "after_receipt", "after_audit", "after_outbox", "before_commit"],
)
def test_complete_fault는_rev0과_companion0을_보존한다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "db.sqlite"
    base = _store(path)
    started = base.start(_bound_command(path), invocation=_invocation())

    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError("fault")

    store = SqliteProductionAuthoringRuns(path, authorize=_Allow(), fault_injector=fault)
    with pytest.raises(RuntimeError):
        store.complete(_complete_command(path, started.run.run_id), invocation=_invocation())
    assert store.get("acme", started.run.run_id).stage == "Extracting"
    assert store.counts("acme")["receipts"] == 1


# ── O5c durable published-index acceptance ────────────────────────────────


def _publishing_run(path: Path, suffix: str = "1") -> tuple[SqliteProductionAuthoringRuns, PublishingRun]:
    store = _store(path)
    started = store.start(
        _bound_command(path, idempotency_key=f"start-o5c-{suffix}"),
        invocation=_invocation(),
    ).run
    awaiting = store.complete(
        _complete_command(path, started.run_id, idempotency_key=f"complete-o5c-{suffix}"),
        invocation=_invocation(),
    ).run
    reviewed = store.review(
        ReviewAuthoringRunCommand(
            organization_id="acme", principal_id="owner", idempotency_key=f"review-o5c-{suffix}",
            run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
            expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
            draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
        ),
        invocation=_invocation(),
    ).run
    return store, store.begin_publish(
        _publish_command(reviewed, idempotency_key=f"claim-o5c-{suffix}"),
        invocation=_invocation(),
    ).run


def _accept_command(run: PublishingRun, suffix: str = "1", *, generated_at: datetime | None = None) -> AcceptPublishedIndexCommand:
    publishing = run
    return AcceptPublishedIndexCommand(
        organization_id="acme", principal_id="owner", idempotency_key=f"accept-o5c-{suffix}",
        run_id=publishing.run_id, expected_card_revision=publishing.card_revision,
        expected_card_digest=publishing.card_digest,
        commit_sha=sha256(f"commit-{suffix}".encode()).hexdigest(),
        committed_tree_index_digest=sha256(f"tree-index-{suffix}".encode()).hexdigest(),
        index=KnowledgeIndex(
            agent_id="support", version=f"v{suffix}",
            generated_at=generated_at or datetime(2026, 7, 29, tzinfo=UTC),
            concepts=(Concept(id=f"refund-{suffix}", label="refund", core_question="refund?", domain="support"),),
        ),
    )


def test_O5c_exact_replay와_Published_read_binding(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    command = _accept_command(publishing)

    first = acceptance.accept(command, invocation=_invocation())
    replay = acceptance.accept(command, invocation=_invocation())

    assert first.run.stage == "Published"
    assert replay.replayed is True
    assert store.get("acme", publishing.run_id) == first.run


def test_O5c_receipt는_O5b_commit_증거를_명시하고_exact_replay에_결박한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    command = _accept_command(publishing, "o5b")

    first = acceptance.accept(command, invocation=_invocation())
    replay = acceptance.accept(command, invocation=_invocation())
    row = sqlite3.connect(path).execute(
        "SELECT commit_sha,committed_tree_index_digest FROM published_index_acceptance_receipts"
    ).fetchone()

    assert first.receipt.commit_sha == command.commit_sha
    assert first.receipt.committed_tree_index_digest == command.committed_tree_index_digest
    assert replay.receipt == first.receipt
    assert row == (command.commit_sha, command.committed_tree_index_digest)


@pytest.mark.parametrize("field", ["commit_sha", "committed_tree_index_digest"])
def test_O5c_O5b_증거가_다르면_같은_idempotency_replay도_conflict다(
    tmp_path: Path, field: str
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    command = _accept_command(publishing)
    acceptance.accept(command, invocation=_invocation())

    with pytest.raises(ProductionAuthoringRunConflict):
        acceptance.accept(
            command.model_copy(update={field: sha256(field.encode()).hexdigest()}),
            invocation=_invocation(),
        )


@pytest.mark.parametrize("field", ["commit_sha", "committed_tree_index_digest"])
def test_O5c_O5b_증거는_정확한_sha256만_수용한다(field: str, tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    _store, publishing = _publishing_run(path)
    command = _accept_command(publishing)

    with pytest.raises(ValidationError):
        AcceptPublishedIndexCommand.model_validate(
            command.model_dump(mode="python") | {field: "not-a-sha256"}
        )


def test_O5c_stale은_거부하고_더새_index_event는_receipt별_append된다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store, first_run = _publishing_run(path, "one")
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    acceptance.accept(_accept_command(first_run, "one", generated_at=datetime(2026, 7, 29, tzinfo=UTC)), invocation=_invocation())

    # A distinct run is required for a distinct immutable O5c semantic receipt.
    started = store.start(_bound_command(path, idempotency_key="start-o5c-two"), invocation=_invocation()).run
    awaiting = store.complete(_complete_command(path, started.run_id, idempotency_key="complete-o5c-two"), invocation=_invocation()).run
    reviewed = store.review(ReviewAuthoringRunCommand(organization_id="acme", principal_id="owner", idempotency_key="review-o5c-two", run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision, expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest, draft_digest=awaiting.admitted_bundle_digest, outcome="Approved"), invocation=_invocation()).run
    second_run = store.begin_publish(_publish_command(reviewed, idempotency_key="claim-o5c-two"), invocation=_invocation()).run
    with pytest.raises(ProductionAuthoringRunConflict):
        acceptance.accept(_accept_command(second_run, "two", generated_at=datetime(2026, 7, 28, tzinfo=UTC)), invocation=_invocation())
    second = acceptance.accept(_accept_command(second_run, "two", generated_at=datetime(2026, 7, 30, tzinfo=UTC)), invocation=_invocation())
    connection = sqlite3.connect(path)
    count = connection.execute("SELECT count(*) FROM published_index_latest_events WHERE org_id='acme' AND agent_id='support'").fetchone()[0]
    connection.close()
    assert second.run.stage == "Published"
    assert count == 2


def test_O5c_generated_at은_offset_문자열이_아닌_UTC_시점으로_staleness를_판정한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store, first_run = _publishing_run(path, "utc")
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    acceptance.accept(
        _accept_command(first_run, "utc", generated_at=datetime(2026, 7, 29, 9, tzinfo=UTC)),
        invocation=_invocation(),
    )

    started = store.start(
        _bound_command(path, idempotency_key="start-o5c-offset"), invocation=_invocation()
    ).run
    awaiting = store.complete(
        _complete_command(path, started.run_id, idempotency_key="complete-o5c-offset"),
        invocation=_invocation(),
    ).run
    reviewed = store.review(
        ReviewAuthoringRunCommand(
            organization_id="acme", principal_id="owner", idempotency_key="review-o5c-offset",
            run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
            expected_card_digest=awaiting.card_digest, source_digest=awaiting.source_set_digest,
            draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
        ),
        invocation=_invocation(),
    ).run
    second_run = store.begin_publish(
        _publish_command(reviewed, idempotency_key="claim-o5c-offset"), invocation=_invocation()
    ).run

    stale_offset = datetime(2026, 7, 29, 10, tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ProductionAuthoringRunConflict):
        acceptance.accept(
            _accept_command(second_run, "offset", generated_at=stale_offset),
            invocation=_invocation(),
        )

    newer_offset = datetime(2026, 7, 29, 19, tzinfo=timezone(timedelta(hours=9)))
    accepted = acceptance.accept(
        _accept_command(second_run, "offset", generated_at=newer_offset),
        invocation=_invocation(),
    )
    connection = sqlite3.connect(path)
    payload = json.loads(
        connection.execute(
            "SELECT payload_json FROM published_index_payloads WHERE run_id=?", (second_run.run_id,)
        ).fetchone()[0]
    )
    event_generated_at = connection.execute(
        "SELECT generated_at FROM published_index_latest_events WHERE receipt_id=?",
        (accepted.receipt.receipt_id,),
    ).fetchone()[0]
    connection.close()

    assert accepted.run.stage == "Published"
    assert payload["generated_at"] == "2026-07-29T19:00:00+09:00"
    assert event_generated_at == "2026-07-29T10:00:00Z"


def test_O5c_naive_generated_at은_admission에서_거부하고_write0이다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    naive = _accept_command(
        publishing, generated_at=datetime(2026, 7, 29, 12, 0, 0)
    )
    before = path.read_bytes()

    with pytest.raises(PublishedIndexAcceptanceUnavailable):
        acceptance.accept(naive, invocation=_invocation())

    assert path.read_bytes() == before


def test_O5c_same_idempotency_different_semantic은_conflict다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    command = _accept_command(publishing)
    acceptance.accept(command, invocation=_invocation())
    with pytest.raises(ProductionAuthoringRunConflict):
        acceptance.accept(command.model_copy(update={"index": command.index.model_copy(update={"version": "other"})}), invocation=_invocation())


def test_O5c_current_authorization_deny는_모든_write를_남기지않는다(tmp_path: Path) -> None:
    class _Deny(_Allow):
        def verify_precommit(self, *args: object, **kwargs: object) -> bool:
            return False

    path = tmp_path / "db.sqlite"
    _unused_store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    denied = SqlitePublishedIndexAcceptance(SqliteProductionAuthoringRuns(path, authorize=_Deny()))
    with pytest.raises(ProductionAuthoringRunDenied):
        denied.accept(_accept_command(publishing), invocation=_invocation())
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM published_index_acceptance_receipts").fetchone()[0] == 0
    connection.close()


@pytest.mark.parametrize("point", ["after_payload", "after_acceptance_receipt", "before_commit"])
def test_O5c_fault는_shared_transaction_부분write0이다(tmp_path: Path, point: str) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)

    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError("fault")

    acceptance = SqlitePublishedIndexAcceptance(store, fault_injector=fault)
    with pytest.raises(RuntimeError):
        acceptance.accept(_accept_command(publishing), invocation=_invocation())
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM published_index_payloads").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM published_index_acceptance_receipts").fetchone()[0] == 0
    assert connection.execute("SELECT stage FROM production_authoring_runs WHERE org_id='acme'").fetchone()[0] == "publishing"
    connection.close()


@pytest.mark.parametrize(
    ("trigger", "statement"),
    [
        (
            "published_index_payloads_immutable",
            "UPDATE published_index_payloads SET payload_json='{}'",
        ),
        (
            "published_index_payloads_immutable",
            "UPDATE published_index_payloads SET payload_json=replace(payload_json,'\"v1\"','\"tampered\"')",
        ),
        (
            "published_index_receipts_immutable",
            "UPDATE published_index_acceptance_receipts SET payload_digest='0' || substr(payload_digest,2)",
        ),
        (
            "published_index_receipts_immutable",
            "UPDATE published_index_acceptance_receipts SET receipt_digest='0' || substr(receipt_digest,2)",
        ),
        (
            "published_index_receipts_immutable",
            "UPDATE published_index_acceptance_receipts SET commit_sha='0' || substr(commit_sha,2)",
        ),
        (
            "published_index_receipts_immutable",
            "UPDATE published_index_acceptance_receipts SET committed_tree_index_digest='0' || substr(committed_tree_index_digest,2)",
        ),
        (
            "published_index_latest_immutable",
            "UPDATE published_index_latest_events SET payload_digest='0' || substr(payload_digest,2)",
        ),
        (
            "production_authoring_receipts_immutable",
            "UPDATE production_authoring_command_receipts SET result_state='reviewed'",
        ),
    ],
)
def test_O5c_payload_receipt_event_control_tamper는_Published_reader를_failclosed한다(
    tmp_path: Path, trigger: str, statement: str
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    SqlitePublishedIndexAcceptance(store).accept(_accept_command(publishing), invocation=_invocation())
    connection = sqlite3.connect(path)
    payload = connection.execute("SELECT payload_json FROM published_index_payloads").fetchone()[0]
    connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(statement)
    connection.commit()
    connection.close()
    assert '"body"' not in payload
    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.get("acme", publishing.run_id)


def test_O5c_O5b_증거와_그에맞춘_receipt_digest_변조도_command_binding으로_거부한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    accepted = SqlitePublishedIndexAcceptance(store).accept(
        _accept_command(publishing), invocation=_invocation()
    )
    changed_commit_sha = sha256(b"tampered-commit").hexdigest()
    connection = sqlite3.connect(path)
    receipt = connection.execute(
        "SELECT * FROM published_index_acceptance_receipts WHERE receipt_id=?",
        (accepted.receipt.receipt_id,),
    ).fetchone()
    assert receipt is not None
    receipt_digest = sha256(json.dumps({
        "receipt_id": receipt[0], "command_digest": receipt[6], "payload_digest": receipt[7],
        "commit_sha": changed_commit_sha, "committed_tree_index_digest": receipt[9],
        "accepted_at": receipt[17],
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    connection.execute("DROP TRIGGER published_index_receipts_immutable")
    connection.execute("DROP TRIGGER production_authoring_runs_immutable")
    connection.execute(
        "UPDATE published_index_acceptance_receipts SET commit_sha=?,receipt_digest=? WHERE receipt_id=?",
        (changed_commit_sha, receipt_digest, accepted.receipt.receipt_id),
    )
    connection.execute(
        "UPDATE production_authoring_runs SET acceptance_receipt_digest=? WHERE org_id=? AND run_id=?",
        (receipt_digest, "acme", publishing.run_id),
    )
    connection.commit()
    connection.close()

    with pytest.raises(ProductionAuthoringRunUnavailable):
        store.get("acme", publishing.run_id)


# ── O5d historical-receipt reconciliation ────────────────────────────────


def test_O5d_현재_권한없이_exact_historical_graph로_Publishing을_Published로_CAS한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    accepted = SqlitePublishedIndexAcceptance(store).accept(
        _accept_command(publishing), invocation=_invocation()
    )

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_authoring_runs_immutable")
    connection.execute(
        "UPDATE production_authoring_runs SET stage='publishing',revision=3,"
        "acceptance_receipt_id=NULL,acceptance_receipt_digest=NULL,published_at=NULL "
        "WHERE org_id='acme' AND run_id=?",
        (publishing.run_id,),
    )
    connection.commit()
    before = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "published_index_payloads", "published_index_acceptance_receipts",
            "published_index_latest_events", "production_authoring_command_receipts",
            "production_authoring_audit_intents", "production_authoring_outbox_intents",
        )
    }
    connection.close()

    # Card transfer/revoke 후에도 O5d는 current authorizer/grant를 호출하지 않는다.
    reconciler = SqlitePublishedIndexAcceptance(store)
    reconciled = reconciler.reconcile(organization_id="acme", run_id=publishing.run_id)

    assert reconciled.run == accepted.run
    assert reconciled.reconciled is True
    connection = sqlite3.connect(path)
    after = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in before
    }
    connection.close()
    assert after == before


def test_O5d_same_Published는_read_only이고_불완전하거나_틀린_historical_graph는_unavailable이다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    acceptance = SqlitePublishedIndexAcceptance(store)
    accepted = acceptance.accept(_accept_command(publishing), invocation=_invocation())
    before = path.read_bytes()

    same = acceptance.reconcile(organization_id="acme", run_id=publishing.run_id)

    assert same.run == accepted.run
    assert same.reconciled is False
    assert path.read_bytes() == before

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_authoring_runs_immutable")
    connection.execute(
        "UPDATE production_authoring_runs SET stage='publishing',revision=3,"
        "acceptance_receipt_id=NULL,acceptance_receipt_digest=NULL,published_at=NULL "
        "WHERE org_id='acme' AND run_id=?",
        (publishing.run_id,),
    )
    connection.execute("DROP TRIGGER published_index_latest_no_delete")
    connection.execute("DELETE FROM published_index_latest_events")
    connection.commit()
    connection.close()

    with pytest.raises(PublishedIndexAcceptanceUnavailable):
        acceptance.reconcile(organization_id="acme", run_id=publishing.run_id)
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT stage FROM production_authoring_runs WHERE org_id='acme' AND run_id=?",
        (publishing.run_id,),
    ).fetchone()[0] == "publishing"
    connection.close()


@pytest.mark.parametrize(
    "drifted_trigger",
    [
        "published_index_payloads_immutable",
        "published_index_acceptance_binds_published_run",
    ],
)
def test_O5d_acceptance_schema_drift는_historical_graph보다_먼저_unavailable이고_write_0이다(
    tmp_path: Path, drifted_trigger: str
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    SqlitePublishedIndexAcceptance(store).accept(_accept_command(publishing), invocation=_invocation())

    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_authoring_runs_immutable")
    connection.execute(
        "UPDATE production_authoring_runs SET stage='publishing',revision=3,"
        "acceptance_receipt_id=NULL,acceptance_receipt_digest=NULL,published_at=NULL "
        "WHERE org_id='acme' AND run_id=?",
        (publishing.run_id,),
    )
    connection.execute(f"DROP TRIGGER {drifted_trigger}")
    before = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "published_index_payloads",
            "published_index_acceptance_receipts",
            "published_index_latest_events",
            "production_authoring_command_receipts",
            "production_authoring_audit_intents",
            "production_authoring_outbox_intents",
        )
    }
    connection.commit()
    connection.close()

    with pytest.raises(PublishedIndexAcceptanceUnavailable):
        SqlitePublishedIndexAcceptance(store).reconcile(
            organization_id="acme", run_id=publishing.run_id
        )

    connection = sqlite3.connect(path)
    after = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in before}
    stage = connection.execute(
        "SELECT stage,revision FROM production_authoring_runs WHERE org_id='acme' AND run_id=?",
        (publishing.run_id,),
    ).fetchone()
    connection.close()
    assert after == before
    assert stage == ("publishing", 3)


@pytest.mark.parametrize("point", ["before_reconciliation_commit"])
def test_O5d_fault와_재시작은_CAS외_부분write를_남기지않는다(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / "db.sqlite"
    store, publishing = _publishing_run(path)
    SqlitePublishedIndexAcceptance.migrate(path)
    SqlitePublishedIndexAcceptance(store).accept(_accept_command(publishing), invocation=_invocation())
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER production_authoring_runs_immutable")
    connection.execute(
        "UPDATE production_authoring_runs SET stage='publishing',revision=3,"
        "acceptance_receipt_id=NULL,acceptance_receipt_digest=NULL,published_at=NULL "
        "WHERE org_id='acme' AND run_id=?", (publishing.run_id,)
    )
    before_receipts = connection.execute(
        "SELECT count(*) FROM production_authoring_command_receipts"
    ).fetchone()[0]
    connection.commit()
    connection.close()

    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError("fault")

    with pytest.raises(RuntimeError):
        SqlitePublishedIndexAcceptance(store, fault_injector=fault).reconcile(
            organization_id="acme", run_id=publishing.run_id
        )
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT stage FROM production_authoring_runs WHERE org_id='acme' AND run_id=?",
        (publishing.run_id,),
    ).fetchone()[0] == "publishing"
    assert connection.execute("SELECT count(*) FROM production_authoring_command_receipts").fetchone()[0] == before_receipts
    connection.close()

    restarted = SqlitePublishedIndexAcceptance(store)
    assert restarted.reconcile(organization_id="acme", run_id=publishing.run_id).run.stage == "Published"
