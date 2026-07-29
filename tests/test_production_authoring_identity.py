# pyright: reportUnknownParameterType=false, reportMissingParameterType=false

from datetime import UTC, datetime, timedelta
from pathlib import Path
from hashlib import sha256
import json
import sqlite3

import pytest
from pydantic import SecretStr

from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
    ProductionAuthoringIdentityUnavailable,
    ProductionAuthoringIdentityVerifier,
)
from agent_org_network.production_identity_sessions import (
    SqliteProductionIdentitySessions,
    VerifiedEmailIdentityProof,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
    ProductionRegistryUserUnavailable,
    validate_production_registry_user_rows,
)
from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    ProductionAgentCardCommand,
    SqliteProductionAgentCards,
)


class _Allow:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction: sqlite3.Connection) -> bool:
        return True


class _CardAllow:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction: sqlite3.Connection) -> bool:
        return True


def _fixture(path: Path):
    SqliteProductionRegistryUsers.migrate(path)
    users = SqliteProductionRegistryUsers(path, authorize=_Allow())
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme", principal_id="owner", idempotency_key="user-1",
            expected_revision=0, user_id="owner", email="owner@example.com",
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
            card={
                "agent_id": "support", "owner": "owner", "team": "support",
                "summary": "support", "domains": ["support"],
                "last_reviewed_at": "2026-07-28", "maintainer": None,
                "can_answer": ["support"], "cannot_answer": [],
                "approval_when": [], "collaborate_when": [],
                "knowledge_sources": ["kb"],
            },  # type: ignore[arg-type]
        )
    )
    SqliteProductionIdentitySessions.migrate(path)
    now = datetime(2026, 7, 28, tzinfo=UTC)
    sessions = SqliteProductionIdentitySessions(
        path, registry=users, configured_org_id="acme", provider_id="corp",
        issuer="https://id.example", clock=lambda: now,
        _identity_session_id_factory=lambda: "s" * 32,
    )
    sessions.establish(
        VerifiedEmailIdentityProof(
            provider_id="corp", issuer="https://id.example",
            email="owner@example.com", email_verified=True,
        ),
        expires_at=now + timedelta(hours=1),
    )
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection, now


def _invocation(**changes: object) -> AuthoringInvocation:
    values: dict[str, object] = {
        "session": AuthoringIdentitySessionRef(value=SecretStr("s" * 32)),
        "org_id": "acme",
        "principal_id": "owner",
        "identity_provider": "corp",
    }
    values.update(changes)
    return AuthoringInvocation(**values)  # type: ignore[arg-type]


def test_same_tx_current_identity는_raw0_digest_evidence만반환한다(tmp_path: Path) -> None:
    connection, now = _fixture(tmp_path / "db.sqlite")
    verifier = ProductionAuthoringIdentityVerifier(
        provider_id="corp", issuer="https://id.example", clock=lambda: now
    )
    before = connection.total_changes
    evidence = verifier.current(_invocation(), connection)
    assert evidence.identity_session_digest != "s" * 32
    assert "s" * 32 not in repr(evidence)
    assert connection.total_changes == before
    assert not connection.in_transaction

    connection.execute("BEGIN")
    verifier.current(_invocation(), connection)
    assert connection.in_transaction
    connection.rollback()


def test_identity는_O2_card_companion_tamper를_same_tx에서_failclosed한다(
    tmp_path: Path,
) -> None:
    connection, now = _fixture(tmp_path / "db.sqlite")
    connection.execute("DROP TRIGGER production_agent_card_audit_immutable")
    connection.execute(
        "UPDATE production_agent_card_audit SET resource_fingerprint=?",
        ("f" * 64,),
    )
    connection.commit()
    verifier = ProductionAuthoringIdentityVerifier(
        provider_id="corp", issuer="https://id.example", clock=lambda: now
    )
    with pytest.raises(ProductionAuthoringIdentityUnavailable):
        verifier.current(_invocation(), connection)


def test_identity는_O2_companion_coordinated_digest_tamper도_failclosed한다(
    tmp_path: Path,
) -> None:
    connection, now = _fixture(tmp_path / "db.sqlite")
    for trigger in (
        "production_agent_card_receipts_immutable",
        "production_agent_card_audit_immutable",
        "production_agent_card_outbox_immutable",
    ):
        connection.execute(f'DROP TRIGGER "{trigger}"')
    for table in (
        "production_agent_card_command_receipts",
        "production_agent_card_audit",
        "production_agent_card_outbox",
    ):
        connection.execute(
            f"UPDATE {table} SET command_digest=?,resource_fingerprint=?",  # noqa: S608
            ("d" * 64, "e" * 64),
        )
    for name, table in (
        ("production_agent_card_receipts_immutable", "production_agent_card_command_receipts"),
        ("production_agent_card_audit_immutable", "production_agent_card_audit"),
        ("production_agent_card_outbox_immutable", "production_agent_card_outbox"),
    ):
        connection.execute(
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} "  # noqa: S608
            "BEGIN SELECT RAISE(ABORT,'immutable'); END"
        )
    connection.commit()
    verifier = ProductionAuthoringIdentityVerifier(
        provider_id="corp", issuer="https://id.example", clock=lambda: now
    )
    with pytest.raises(ProductionAuthoringIdentityUnavailable):
        verifier.current(_invocation(), connection)


@pytest.mark.parametrize(
    ("mutation", "invocation"),
    [
        ("revoke", _invocation()),
        ("expire", _invocation()),
        ("org", _invocation(org_id="other")),
        ("principal", _invocation(principal_id="other")),
        ("provider", _invocation(identity_provider="other")),
        ("registry", _invocation()),
    ],
)
def test_revoke_expiry_mismatch_companion_drift는_failclosed(
    tmp_path: Path, mutation: str, invocation: AuthoringInvocation
) -> None:
    connection, now = _fixture(tmp_path / "db.sqlite")
    if mutation == "revoke":
        connection.execute("UPDATE production_identity_sessions SET active=0")
    elif mutation == "expire":
        now = now + timedelta(hours=2)
    elif mutation == "registry":
        connection.execute(
            "UPDATE production_registry_users SET email='drift@example.com'"
        )
    connection.commit()
    verifier = ProductionAuthoringIdentityVerifier(
        provider_id="corp", issuer="https://id.example", clock=lambda: now
    )
    with pytest.raises(ProductionAuthoringIdentityUnavailable):
        verifier.current(invocation, connection)


def test_identity_schema_tamper와_secret_repr는_failclosed(tmp_path: Path) -> None:
    connection, now = _fixture(tmp_path / "db.sqlite")
    connection.execute("DROP TRIGGER production_identity_sessions_identity_immutable")
    connection.commit()
    verifier = ProductionAuthoringIdentityVerifier(
        provider_id="corp", issuer="https://id.example", clock=lambda: now
    )
    with pytest.raises(ProductionAuthoringIdentityUnavailable):
        verifier.current(_invocation(), connection)
    ref = AuthoringIdentitySessionRef(value=SecretStr("s" * 32))
    assert "s" * 32 not in repr(ref)


def test_registry_user_rows는_exact_registration_graph를_validate_only검증한다(
    tmp_path: Path,
) -> None:
    connection, _ = _fixture(tmp_path / "db.sqlite")
    before = connection.total_changes
    user = validate_production_registry_user_rows(connection, "acme", "owner")
    assert user.email == "owner@example.com"
    assert connection.total_changes == before


@pytest.mark.parametrize("tamper", ["missing", "forged", "extra", "coordinated"])
def test_registry_registration_companion_tamper는_failclosed다(
    tmp_path: Path, tamper: str
) -> None:
    connection, _ = _fixture(tmp_path / "db.sqlite")
    if tamper == "missing":
        connection.execute("DROP TRIGGER production_registry_user_audit_no_delete")
        connection.execute("DELETE FROM production_registry_user_audit")
    elif tamper in {"forged", "extra"}:
        connection.execute(
            "INSERT INTO production_registry_user_audit "
            "(org_id,action,principal_id,subject_id,approval_evidence_digest,"
            "command_digest,result_revision,email_digest,registry_fingerprint,"
            "resource_fingerprint,created_at) "
            "SELECT org_id,action,principal_id,subject_id,"
            + ("'f' || substr(approval_evidence_digest,2)" if tamper == "forged" else "approval_evidence_digest")
            + ",command_digest,result_revision,email_digest,registry_fingerprint,"
            "resource_fingerprint,created_at FROM production_registry_user_audit"
        )
    else:
        connection.execute(
            "UPDATE production_registry_users SET email='coordinated@example.com'"
        )
    connection.commit()
    with pytest.raises(ProductionRegistryUserUnavailable):
        validate_production_registry_user_rows(connection, "acme", "owner")


def test_identity_evidence_preimage는_email과_registry_fingerprint를_포함한다(
    tmp_path: Path,
) -> None:
    connection, now = _fixture(tmp_path / "db.sqlite")
    verifier = ProductionAuthoringIdentityVerifier(
        provider_id="corp", issuer="https://id.example", clock=lambda: now
    )
    evidence = verifier.current(_invocation(), connection)
    row = connection.execute(
        "SELECT email_digest,registry_fingerprint,provider_digest,expires_at "
        "FROM production_identity_sessions"
    ).fetchone()
    session_digest = sha256(("s" * 32).encode()).hexdigest()
    preimage = {
        "identity_session_digest": session_digest,
        "org_id": "acme",
        "principal_id": "owner",
        "provider_digest": row[2],
        "registry_revision": 1,
        "email_digest": row[0],
        "registry_fingerprint": row[1],
        "expires_at": row[3],
    }
    expected = sha256(
        json.dumps(preimage, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert evidence.identity_evidence_digest == expected
    preimage["email_digest"] = "0" * 64
    changed = sha256(
        json.dumps(preimage, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert changed != evidence.identity_evidence_digest


@pytest.mark.parametrize("forgery", ["ghost", "duplicate", "well_formed_extra"])
def test_org_scoped_reverse_bijection은_orphan_extra_receipt를_거부한다(
    tmp_path: Path, forgery: str
) -> None:
    connection, _ = _fixture(tmp_path / "db.sqlite")
    row = connection.execute(
        "SELECT * FROM production_registry_user_command_receipts"
    ).fetchone()
    values = list(row)
    values[1] = f"forged-{forgery}"
    if forgery == "ghost":
        values[5] = "ghost-user"
    elif forgery == "well_formed_extra":
        values[3] = "other-admin"
    connection.execute(
        "INSERT INTO production_registry_user_command_receipts VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    connection.commit()
    with pytest.raises(ProductionRegistryUserUnavailable):
        validate_production_registry_user_rows(connection, "acme", "owner")
