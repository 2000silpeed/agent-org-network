"""Authenticated, durable Card Owner pairing-intent issuance."""

from __future__ import annotations

from base64 import b64decode, b64encode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Literal, Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    AuthorityPolicySnapshot,
    ResourceRef,
    SnapshotCentralAuthorizer,
)
from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
    ProductionAuthoringIdentityUnavailable,
    ProductionAuthoringIdentityVerifier,
)
from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardUnavailable,
    validate_production_agent_card_connection,
)
from agent_org_network.owner_credential_envelope import (
    CredentialEnvelopeAad,
    OwnerCredentialEnvelope,
    X25519PublicJwk,
    device_key_thumbprint,
    encrypt_owner_credential_with_verifier,
    parse_owner_credential_envelope,
    serialize_owner_credential_envelope,
)


class CentralOwnerPairingIssueUnavailable(Exception):
    pass


class CentralOwnerPairingIssueConflict(CentralOwnerPairingIssueUnavailable):
    pass


_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class CentralPairingServerKey(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    key_id: str
    key: bytes = Field(repr=False)

    @field_validator("key_id")
    @classmethod
    def _id(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded key id required")
        return value

    @field_validator("key")
    @classmethod
    def _key(cls, value: bytes) -> bytes:
        if len(value) != 32:
            raise ValueError("256-bit server pairing key required")
        return value


class CentralPairingServerKeyProvider(Protocol):
    def current(self) -> CentralPairingServerKey: ...


class IssueOwnerPairingCommand(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    org_id: str
    principal_id: str
    agent_id: str
    expected_card_revision: int
    expected_card_digest: str
    device_class: str
    idempotency_key: str

    @field_validator(
        "org_id", "principal_id", "agent_id", "device_class", "idempotency_key"
    )
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator("expected_card_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value < 1:
            raise ValueError("positive revision required")
        return value

    @field_validator("expected_card_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class PairingIssuancePrincipalEvidence(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    audience: Literal["owner-install"] = "owner-install"
    action: Literal["author.write"] = "author.write"
    org_id: str
    owner_id: str
    agent_id: str
    identity_provider: str
    identity_session_digest: str
    identity_evidence_digest: str
    card_revision: int
    card_digest: str
    policy_version: str
    policy_digest: str
    grant_evidence_digest: str


class IssuedOwnerPairingIntent(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str
    pairing_code: SecretStr
    expires_at: datetime
    evidence: PairingIssuancePrincipalEvidence
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    replayed: bool = False

    @field_validator("intent_id", "issue_receipt_id")
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator("pairing_intent_digest", "issue_receipt_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class RedeemOwnerPairingCommand(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent_id: str
    pairing_code: SecretStr
    device_public_key: X25519PublicJwk
    idempotency_key: str

    @field_validator("intent_id", "idempotency_key")
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator("pairing_code")
    @classmethod
    def _code(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(
            r"[A-Za-z0-9_-]{32,128}", value.get_secret_value()
        ) is None:
            raise ValueError("opaque pairing code required")
        return value


class RedeemedOwnerCredential(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    credential_id: str
    envelope: OwnerCredentialEnvelope
    evidence: PairingIssuancePrincipalEvidence
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    redeem_receipt_id: str
    redeem_receipt_digest: str
    replayed: bool = False

    @field_validator("credential_id", "issue_receipt_id", "redeem_receipt_id")
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator(
        "pairing_intent_digest", "issue_receipt_digest", "redeem_receipt_digest"
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def _pairing_intent_digest(
    *, intent_id: str, expires_at: datetime, evidence: PairingIssuancePrincipalEvidence
) -> str:
    return _digest(
        {
            "intent_id": intent_id,
            "expires_at": expires_at.isoformat(),
            "evidence": evidence.model_dump(mode="json"),
        }
    )


def _issue_receipt_digest(
    *, org_id: str, receipt_id: str, command_digest: str, intent_id: str, created_at: str
) -> str:
    return _digest(
        {
            "kind": "owner-pairing.issue",
            "org_id": org_id,
            "receipt_id": receipt_id,
            "command_digest": command_digest,
            "intent_id": intent_id,
            "created_at": created_at,
        }
    )


def _redeem_receipt_digest(
    *, receipt_id: str, command_digest: str, intent_id: str, credential_id: str, created_at: str
) -> str:
    return _digest(
        {
            "kind": "owner-pairing.redeem",
            "receipt_id": receipt_id,
            "command_digest": command_digest,
            "intent_id": intent_id,
            "credential_id": credential_id,
            "created_at": created_at,
        }
    )


def _issue_command_digest(command: IssueOwnerPairingCommand) -> str:
    return _digest(command.model_dump(mode="json"))


def _redeem_command_digest(
    *,
    intent_id: str,
    idempotency_key: str,
    device_key_thumbprint_value: str,
    code_verifier: str,
) -> str:
    return _digest(
        {
            "intent_id": intent_id,
            "idempotency_key": idempotency_key,
            "device_key_thumbprint": device_key_thumbprint_value,
            "code_verifier": code_verifier,
        }
    )


class ProductionCentralPairingIssueAuthorizer:
    def __init__(
        self,
        *,
        policy_snapshot: Callable[[], AuthorityPolicySnapshot],
        central_authorizer: SnapshotCentralAuthorizer,
        identity_verifier: ProductionAuthoringIdentityVerifier,
    ) -> None:
        if (
            not callable(policy_snapshot)
            or type(central_authorizer) is not SnapshotCentralAuthorizer
            or type(identity_verifier) is not ProductionAuthoringIdentityVerifier
        ):
            raise CentralOwnerPairingIssueUnavailable()
        self._snapshot = policy_snapshot
        self._central = central_authorizer
        self._identity = identity_verifier

    def current(
        self,
        command: IssueOwnerPairingCommand,
        invocation: AuthoringInvocation,
        transaction: sqlite3.Connection,
    ) -> PairingIssuancePrincipalEvidence:
        before = transaction.total_changes
        try:
            identity = self._identity.current(invocation, transaction)
            validate_production_agent_card_connection(transaction)
            card = transaction.execute(
                "SELECT owner_id,revision,card_digest FROM production_agent_cards "
                "WHERE org_id=? AND agent_id=?",
                (command.org_id, command.agent_id),
            ).fetchone()
            if (
                invocation.org_id != command.org_id
                or invocation.principal_id != command.principal_id
                or card is None
                or card[0] != command.principal_id
                or int(card[1]) != command.expected_card_revision
                or card[2] != command.expected_card_digest
            ):
                raise CentralOwnerPairingIssueUnavailable()
            snapshot = self._snapshot()
            principal = AuthenticatedPrincipal(
                org_id=command.org_id,
                subject_id=command.principal_id,
                identity_provider=invocation.identity_provider,
                identity_session_id=invocation.session.value.get_secret_value(),
            )
            resource = ResourceRef(
                org_id=command.org_id,
                kind="agent_card",
                resource_id=command.agent_id,
                owner_subject_id=command.principal_id,
            )
            grant = self._central.authorize(principal, "author.write", resource)
            if (
                type(snapshot) is not AuthorityPolicySnapshot
                or snapshot.org_id != command.org_id
                or type(grant) is not AuthorizationGrant
                or not self._central.verify(
                    grant, principal, "author.write", resource
                )
                or grant.policy_version != snapshot.policy_version
                or grant.policy_digest != snapshot.content_sha256
            ):
                raise CentralOwnerPairingIssueUnavailable()
            return PairingIssuancePrincipalEvidence(
                org_id=command.org_id,
                owner_id=command.principal_id,
                agent_id=command.agent_id,
                identity_provider=invocation.identity_provider,
                identity_session_digest=identity.identity_session_digest,
                identity_evidence_digest=identity.identity_evidence_digest,
                card_revision=command.expected_card_revision,
                card_digest=command.expected_card_digest,
                policy_version=snapshot.policy_version,
                policy_digest=snapshot.content_sha256,
                grant_evidence_digest=_digest(grant.model_dump(mode="json")),
            )
        except CentralOwnerPairingIssueUnavailable:
            raise
        except (
            ProductionAuthoringIdentityUnavailable,
            ProductionAgentCardUnavailable,
            sqlite3.Error,
            ValueError,
        ) as error:
            raise CentralOwnerPairingIssueUnavailable() from error
        finally:
            if transaction.total_changes != before:
                raise CentralOwnerPairingIssueUnavailable()

    def verify_precommit(
        self,
        command: IssueOwnerPairingCommand,
        invocation: AuthoringInvocation,
        evidence: PairingIssuancePrincipalEvidence,
        transaction: sqlite3.Connection,
    ) -> bool:
        try:
            return self.current(command, invocation, transaction) == evidence
        except CentralOwnerPairingIssueUnavailable:
            return False


_SCHEMA = """
CREATE TABLE central_owner_pairing_intents (
 intent_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 agent_id TEXT NOT NULL, device_class TEXT NOT NULL,
 command_digest TEXT NOT NULL, code_verifier TEXT NOT NULL UNIQUE,
 identity_session_digest TEXT NOT NULL, evidence_json TEXT NOT NULL,
 issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','redeemed','expired')),
 redeemed_at TEXT,
 key_id TEXT NOT NULL, nonce TEXT NOT NULL CHECK(length(nonce)=16),
 code_envelope TEXT NOT NULL CHECK(length(code_envelope) BETWEEN 64 AND 256)
) STRICT;
CREATE UNIQUE INDEX central_owner_pairing_one_pending
ON central_owner_pairing_intents(org_id,agent_id) WHERE state='pending';
CREATE TABLE central_owner_pairing_issue_receipts (
 org_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,command_digest TEXT NOT NULL,
 intent_id TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(org_id,idempotency_key)
) STRICT;
CREATE TABLE central_owner_pairing_issue_audit (
 intent_id TEXT PRIMARY KEY,org_id TEXT NOT NULL,owner_id TEXT NOT NULL,
 agent_id TEXT NOT NULL,event_kind TEXT NOT NULL CHECK(event_kind='pairing_intent_issued'),
 command_digest TEXT NOT NULL,created_at TEXT NOT NULL
) STRICT;
CREATE TABLE central_owner_pairing_issue_outbox (
 intent_id TEXT PRIMARY KEY,event_kind TEXT NOT NULL CHECK(event_kind='pairing_intent_issued'),
 payload_digest TEXT NOT NULL,created_at TEXT NOT NULL
) STRICT;
CREATE TRIGGER central_owner_pairing_intent_exact_update BEFORE UPDATE ON central_owner_pairing_intents
WHEN OLD.intent_id!=NEW.intent_id OR OLD.org_id!=NEW.org_id OR OLD.owner_id!=NEW.owner_id
 OR OLD.agent_id!=NEW.agent_id OR OLD.device_class!=NEW.device_class
 OR OLD.command_digest!=NEW.command_digest OR OLD.code_verifier!=NEW.code_verifier
 OR OLD.identity_session_digest!=NEW.identity_session_digest OR OLD.evidence_json!=NEW.evidence_json
 OR OLD.issued_at!=NEW.issued_at OR OLD.expires_at!=NEW.expires_at
 OR OLD.key_id!=NEW.key_id OR OLD.nonce!=NEW.nonce OR OLD.code_envelope!=NEW.code_envelope
 OR OLD.state!='pending' OR NEW.state NOT IN ('redeemed','expired')
 OR OLD.redeemed_at IS NOT NULL
 OR (NEW.state='redeemed' AND NEW.redeemed_at IS NULL)
 OR (NEW.state='expired' AND NEW.redeemed_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'invalid transition'); END;
CREATE TRIGGER central_owner_pairing_intent_no_delete BEFORE DELETE ON central_owner_pairing_intents
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_receipt_immutable BEFORE UPDATE ON central_owner_pairing_issue_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_receipt_no_delete BEFORE DELETE ON central_owner_pairing_issue_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_audit_immutable BEFORE UPDATE ON central_owner_pairing_issue_audit
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_audit_no_delete BEFORE DELETE ON central_owner_pairing_issue_audit
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_outbox_immutable BEFORE UPDATE ON central_owner_pairing_issue_outbox
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_outbox_no_delete BEFORE DELETE ON central_owner_pairing_issue_outbox
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TABLE central_owner_pairing_credentials (
 credential_id TEXT PRIMARY KEY,org_id TEXT NOT NULL,owner_id TEXT NOT NULL,
 agent_id TEXT NOT NULL,device_key_thumbprint TEXT NOT NULL,
 generation INTEGER NOT NULL CHECK(generation=1),
 status TEXT NOT NULL CHECK(status='active'),secret_key_id TEXT NOT NULL,
 secret_verifier TEXT NOT NULL,issued_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 UNIQUE(org_id,agent_id,generation)
) STRICT;
CREATE TABLE central_owner_pairing_replay_envelopes (
 intent_id TEXT PRIMARY KEY,command_digest TEXT NOT NULL,
 device_key_thumbprint TEXT NOT NULL,envelope_json TEXT,
 replay_expires_at TEXT NOT NULL,purged_at TEXT,
 CHECK(
  (envelope_json IS NOT NULL AND purged_at IS NULL)
  OR
  (envelope_json IS NULL AND purged_at IS NOT NULL
   AND replay_expires_at<=purged_at)
 )
) STRICT;
CREATE TABLE central_owner_pairing_redeem_receipts (
 idempotency_key TEXT PRIMARY KEY,command_digest TEXT NOT NULL,
 intent_id TEXT NOT NULL,credential_id TEXT NOT NULL,created_at TEXT NOT NULL
) STRICT;
CREATE TABLE central_owner_pairing_redeem_audit (
 intent_id TEXT PRIMARY KEY,event_kind TEXT NOT NULL CHECK(event_kind='pairing_redeemed'),
 issue_command_digest TEXT NOT NULL,redeem_command_digest TEXT NOT NULL,
 issue_evidence_digest TEXT NOT NULL,current_evidence_digest TEXT NOT NULL,
 credential_id TEXT NOT NULL,aad_digest TEXT NOT NULL,
 credential_fingerprint TEXT NOT NULL,envelope_digest TEXT NOT NULL,
 created_at TEXT NOT NULL
) STRICT;
CREATE TABLE central_owner_pairing_redeem_outbox (
 intent_id TEXT PRIMARY KEY,event_kind TEXT NOT NULL CHECK(event_kind='pairing_redeemed'),
 payload_digest TEXT NOT NULL,created_at TEXT NOT NULL
) STRICT;
CREATE TRIGGER central_owner_pairing_credential_immutable BEFORE UPDATE ON central_owner_pairing_credentials
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_credential_no_delete BEFORE DELETE ON central_owner_pairing_credentials
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_replay_purge_only
BEFORE UPDATE ON central_owner_pairing_replay_envelopes
WHEN OLD.intent_id IS NOT NEW.intent_id
 OR OLD.command_digest IS NOT NEW.command_digest
 OR OLD.device_key_thumbprint IS NOT NEW.device_key_thumbprint
 OR OLD.replay_expires_at IS NOT NEW.replay_expires_at
 OR OLD.envelope_json IS NULL OR NEW.envelope_json IS NOT NULL
 OR OLD.purged_at IS NOT NULL OR NEW.purged_at IS NULL
 OR NEW.replay_expires_at>NEW.purged_at
BEGIN SELECT RAISE(ABORT,'invalid replay purge'); END;
CREATE TRIGGER central_owner_pairing_replay_no_delete BEFORE DELETE ON central_owner_pairing_replay_envelopes
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_redeem_receipt_immutable BEFORE UPDATE ON central_owner_pairing_redeem_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_redeem_receipt_no_delete BEFORE DELETE ON central_owner_pairing_redeem_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_redeem_audit_immutable BEFORE UPDATE ON central_owner_pairing_redeem_audit
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_redeem_audit_no_delete BEFORE DELETE ON central_owner_pairing_redeem_audit
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_redeem_outbox_immutable BEFORE UPDATE ON central_owner_pairing_redeem_outbox
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER central_owner_pairing_redeem_outbox_no_delete BEFORE DELETE ON central_owner_pairing_redeem_outbox
BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    return tuple(
        connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name LIKE 'central_owner_pairing_%' ORDER BY type,name"
        )
    )


def _expected_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _catalog(connection)
    finally:
        connection.close()


_EXPECTED = _expected_catalog()


def _no_fault(_point: str) -> None:
    return None


def _validate_rows(connection: sqlite3.Connection) -> None:
    intents = connection.execute(
        "SELECT intent_id,org_id,owner_id,agent_id,device_class,command_digest,"
        "code_verifier,identity_session_digest,evidence_json,issued_at,expires_at,"
        "state,redeemed_at,key_id,nonce,code_envelope "
        "FROM central_owner_pairing_intents "
        "ORDER BY intent_id"
    ).fetchall()
    if (
        connection.execute(
            "SELECT count(*) FROM central_owner_pairing_issue_receipts"
        ).fetchone()[0]
        != len(intents)
        or connection.execute(
            "SELECT count(*) FROM central_owner_pairing_issue_audit"
        ).fetchone()[0]
        != len(intents)
        or connection.execute(
            "SELECT count(*) FROM central_owner_pairing_issue_outbox"
        ).fetchone()[0]
        != len(intents)
    ):
        raise CentralOwnerPairingIssueUnavailable()
    for intent in intents:
        evidence = PairingIssuancePrincipalEvidence.model_validate_json(intent[8])
        issued = datetime.fromisoformat(intent[9])
        expires = datetime.fromisoformat(intent[10])
        if (
            _canonical(evidence.model_dump(mode="json")).decode("utf-8") != intent[8]
            or evidence.org_id != intent[1]
            or evidence.owner_id != intent[2]
            or evidence.agent_id != intent[3]
            or evidence.identity_session_digest != intent[7]
            or _REF.fullmatch(intent[0]) is None
            or _REF.fullmatch(intent[4]) is None
            or _DIGEST.fullmatch(intent[5]) is None
            or _DIGEST.fullmatch(intent[6]) is None
            or _REF.fullmatch(intent[13]) is None
            or intent[11] not in {"pending", "redeemed", "expired"}
            or (
                (intent[11] == "redeemed") != (intent[12] is not None)
            )
            or issued.tzinfo is None
            or expires.tzinfo is None
            or expires <= issued
            or b64encode(b64decode(intent[14], validate=True)).decode() != intent[14]
            or len(b64decode(intent[14], validate=True)) != 12
            or b64encode(b64decode(intent[15], validate=True)).decode() != intent[15]
            or not 48 <= len(b64decode(intent[15], validate=True)) <= 192
        ):
            raise CentralOwnerPairingIssueUnavailable()
        receipt = connection.execute(
            "SELECT idempotency_key,command_digest,intent_id,created_at "
            "FROM central_owner_pairing_issue_receipts WHERE intent_id=?",
            (intent[0],),
        ).fetchone()
        if receipt is None:
            raise CentralOwnerPairingIssueUnavailable()
        reconstructed = IssueOwnerPairingCommand(
            org_id=intent[1],
            principal_id=intent[2],
            agent_id=intent[3],
            expected_card_revision=evidence.card_revision,
            expected_card_digest=evidence.card_digest,
            device_class=intent[4],
            idempotency_key=receipt[0],
        )
        canonical_command_digest = _issue_command_digest(reconstructed)
        audit = connection.execute(
            "SELECT org_id,owner_id,agent_id,event_kind,command_digest,created_at "
            "FROM central_owner_pairing_issue_audit WHERE intent_id=?",
            (intent[0],),
        ).fetchone()
        outbox = connection.execute(
            "SELECT event_kind,payload_digest,created_at "
            "FROM central_owner_pairing_issue_outbox WHERE intent_id=?",
            (intent[0],),
        ).fetchone()
        if (
            canonical_command_digest != intent[5]
            or receipt
            != (receipt[0], canonical_command_digest, intent[0], intent[9])
            or audit
            != (
                intent[1],
                intent[2],
                intent[3],
                "pairing_intent_issued",
                intent[5],
                intent[9],
            )
            or outbox
            != (
                "pairing_intent_issued",
                _digest([intent[0], intent[5]]),
                intent[9],
            )
        ):
            raise CentralOwnerPairingIssueUnavailable()


def _validate_redeem_rows(connection: sqlite3.Connection) -> None:
    redeemed = connection.execute(
        "SELECT intent_id,org_id,owner_id,agent_id,command_digest,evidence_json,"
        "redeemed_at,code_verifier FROM central_owner_pairing_intents "
        "WHERE state='redeemed' "
        "ORDER BY intent_id"
    ).fetchall()
    for table in (
        "central_owner_pairing_credentials",
        "central_owner_pairing_replay_envelopes",
        "central_owner_pairing_redeem_receipts",
        "central_owner_pairing_redeem_audit",
        "central_owner_pairing_redeem_outbox",
    ):
        if connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] != len(
            redeemed
        ):
            raise CentralOwnerPairingIssueUnavailable()
    for intent in redeemed:
        receipt = connection.execute(
            "SELECT idempotency_key,command_digest,credential_id,created_at FROM "
            "central_owner_pairing_redeem_receipts WHERE intent_id=?",
            (intent[0],),
        ).fetchone()
        replay = connection.execute(
            "SELECT command_digest,device_key_thumbprint,envelope_json,"
            "replay_expires_at,purged_at FROM central_owner_pairing_replay_envelopes "
            "WHERE intent_id=?",
            (intent[0],),
        ).fetchone()
        if receipt is None or replay is None:
            raise CentralOwnerPairingIssueUnavailable()
        replay = cast(tuple[str, str, str | None, str, str | None], replay)
        canonical_redeem_digest = _redeem_command_digest(
            intent_id=intent[0],
            idempotency_key=receipt[0],
            device_key_thumbprint_value=replay[1],
            code_verifier=intent[7],
        )
        if receipt[1] != canonical_redeem_digest or replay[0] != canonical_redeem_digest:
            raise CentralOwnerPairingIssueUnavailable()
        credential = connection.execute(
            "SELECT org_id,owner_id,agent_id,device_key_thumbprint,generation,"
            "status,secret_key_id,secret_verifier,issued_at,expires_at "
            "FROM central_owner_pairing_credentials WHERE credential_id=?",
            (receipt[2],),
        ).fetchone()
        evidence = PairingIssuancePrincipalEvidence.model_validate_json(intent[5])
        evidence_digest = _digest(evidence.model_dump(mode="json"))
        audit = connection.execute(
            "SELECT event_kind,issue_command_digest,redeem_command_digest,"
            "issue_evidence_digest,current_evidence_digest,credential_id,"
            "aad_digest,credential_fingerprint,envelope_digest,created_at "
            "FROM central_owner_pairing_redeem_audit "
            "WHERE intent_id=?",
            (intent[0],),
        ).fetchone()
        if credential is None or audit is None:
            raise CentralOwnerPairingIssueUnavailable()
        credential = cast(tuple[str, str, str, str, int, str, str, str, str, str], credential)
        audit = cast(
            tuple[str, str, str, str, str, str, str, str, str, str], audit
        )
        envelope = (
            parse_owner_credential_envelope(replay[2].encode("utf-8"))
            if isinstance(replay[2], str)
            else None
        )
        aad_digest = (
            sha256(_canonical(envelope.aad.model_dump(mode="json"))).hexdigest()
            if envelope is not None
            else audit[6]
        )
        outbox = connection.execute(
            "SELECT event_kind,payload_digest,created_at FROM "
            "central_owner_pairing_redeem_outbox WHERE intent_id=?",
            (intent[0],),
        ).fetchone()
        credential_fingerprint = _digest(
            [
                receipt[2],
                intent[1],
                intent[2],
                intent[3],
                replay[1],
                1,
                "active",
                credential[6],
                credential[7],
                credential[8],
                credential[9],
            ]
        )
        envelope_digest = (
            sha256(replay[2].encode("utf-8")).hexdigest()
            if isinstance(replay[2], str)
            else audit[8]
        )
        live_envelope_valid = envelope is not None and (
            replay[4] is None
            and envelope.aad.credential_id == receipt[2]
            and envelope.aad.org_id == intent[1]
            and envelope.aad.owner_user_id == intent[2]
            and envelope.aad.agent_card_id == intent[3]
            and envelope.aad.device_key_thumbprint == replay[1]
            and envelope.aad.credential_generation == 1
            and envelope.aad.issued_at == credential[8]
            and envelope.aad.expires_at == credential[9]
        )
        purged_envelope_valid = envelope is None and (
            isinstance(replay[4], str)
            and datetime.fromisoformat(replay[3])
            <= datetime.fromisoformat(replay[4])
            and _DIGEST.fullmatch(audit[6]) is not None
            and _DIGEST.fullmatch(audit[8]) is not None
        )
        if (
            credential
            != (
                intent[1],
                intent[2],
                intent[3],
                replay[1],
                1,
                "active",
                credential[6],
                credential[7],
                credential[8],
                credential[9],
            )
            or _REF.fullmatch(credential[6]) is None
            or _DIGEST.fullmatch(credential[7]) is None
            or not (live_envelope_valid or purged_envelope_valid)
            or receipt[3] != intent[6]
            or audit
            != (
                "pairing_redeemed",
                intent[4],
                canonical_redeem_digest,
                evidence_digest,
                evidence_digest,
                receipt[2],
                aad_digest,
                credential_fingerprint,
                envelope_digest,
                intent[6],
            )
            or outbox
            != (
                "pairing_redeemed",
                _digest([intent[0], receipt[2], aad_digest]),
                intent[6],
            )
            or datetime.fromisoformat(replay[3])
            <= datetime.fromisoformat(intent[6])
        ):
            raise CentralOwnerPairingIssueUnavailable()


class CentralOwnerPairingIssueStore:
    def __init__(
        self,
        path: str | Path,
        *,
        keys: CentralPairingServerKeyProvider,
        authorizer: ProductionCentralPairingIssueAuthorizer,
        clock: Callable[[], datetime],
        ttl_seconds: int = 300,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if (
            type(authorizer) is not ProductionCentralPairingIssueAuthorizer
            or not callable(getattr(keys, "current", None))
            or not callable(clock)
            or not 60 <= ttl_seconds <= 600
        ):
            raise CentralOwnerPairingIssueUnavailable()
        self._path = str(path)
        self._keys = keys
        self._authorizer = authorizer
        self._clock = clock
        self._ttl = ttl_seconds
        self._fault: Callable[[str], None] = fault or _no_fault
        try:
            with sqlite3.connect(self._path) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                existing = connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name LIKE "
                    "'central_owner_pairing_%' LIMIT 1"
                ).fetchone()
                if existing is None:
                    connection.executescript(_SCHEMA)
                if _catalog(connection) != _EXPECTED:
                    raise CentralOwnerPairingIssueUnavailable()
                if existing is not None:
                    _validate_rows(connection)
                    _validate_redeem_rows(connection)
        except CentralOwnerPairingIssueUnavailable:
            raise
        except Exception as error:
            raise CentralOwnerPairingIssueUnavailable() from error

    def issue(
        self,
        command: IssueOwnerPairingCommand,
        invocation: AuthoringInvocation,
    ) -> IssuedOwnerPairingIntent:
        now = self._clock()
        command_digest = _issue_command_digest(command)
        try:
            with sqlite3.connect(self._path, timeout=30) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                if _catalog(connection) != _EXPECTED:
                    raise CentralOwnerPairingIssueUnavailable()
                _validate_rows(connection)
                _validate_redeem_rows(connection)
                evidence = self._authorizer.current(command, invocation, connection)
                receipt = connection.execute(
                    "SELECT command_digest,intent_id FROM central_owner_pairing_issue_receipts "
                    "WHERE org_id=? AND idempotency_key=?",
                    (command.org_id, command.idempotency_key),
                ).fetchone()
                if receipt is not None:
                    if receipt[0] != command_digest:
                        raise CentralOwnerPairingIssueConflict()
                    replay = self._replay(connection, receipt[1])
                    if replay.evidence != evidence:
                        raise CentralOwnerPairingIssueUnavailable()
                    return replay
                connection.execute(
                    "UPDATE central_owner_pairing_intents SET state='expired' "
                    "WHERE org_id=? AND agent_id=? AND state='pending' AND expires_at<=?",
                    (command.org_id, command.agent_id, now.isoformat()),
                )
                if connection.execute(
                    "SELECT 1 FROM central_owner_pairing_intents "
                    "WHERE org_id=? AND agent_id=? AND state='pending'",
                    (command.org_id, command.agent_id),
                ).fetchone():
                    raise CentralOwnerPairingIssueConflict()
                code = secrets.token_urlsafe(32)
                intent_id = "intent-" + secrets.token_urlsafe(24)
                expires = now + timedelta(seconds=self._ttl)
                key = self._keys.current()
                enc_key = hmac.digest(key.key, b"owner-pairing-envelope-v1", "sha256")
                verifier_key = hmac.digest(
                    key.key, b"owner-pairing-verifier-v1", "sha256"
                )
                verifier = hmac.new(
                    verifier_key, code.encode("utf-8"), "sha256"
                ).hexdigest()
                nonce = os.urandom(12)
                envelope = AESGCM(enc_key).encrypt(
                    nonce, code.encode("utf-8"), intent_id.encode("utf-8")
                )
                evidence_json = _canonical(
                    evidence.model_dump(mode="json")
                ).decode("utf-8")
                self._fault("before_intent")
                connection.execute(
                    "INSERT INTO central_owner_pairing_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        intent_id,
                        command.org_id,
                        command.principal_id,
                        command.agent_id,
                        command.device_class,
                        command_digest,
                        verifier,
                        evidence.identity_session_digest,
                        evidence_json,
                        now.isoformat(),
                        expires.isoformat(),
                        "pending",
                        None,
                        key.key_id,
                        b64encode(nonce).decode(),
                        b64encode(envelope).decode(),
                    ),
                )
                connection.execute(
                    "INSERT INTO central_owner_pairing_issue_receipts VALUES(?,?,?,?,?)",
                    (
                        command.org_id,
                        command.idempotency_key,
                        command_digest,
                        intent_id,
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO central_owner_pairing_issue_audit VALUES(?,?,?,?,?,?,?)",
                    (
                        intent_id,
                        command.org_id,
                        command.principal_id,
                        command.agent_id,
                        "pairing_intent_issued",
                        command_digest,
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    "INSERT INTO central_owner_pairing_issue_outbox VALUES(?,?,?,?)",
                    (
                        intent_id,
                        "pairing_intent_issued",
                        _digest([intent_id, command_digest]),
                        now.isoformat(),
                    ),
                )
                self._fault("before_precommit")
                if not self._authorizer.verify_precommit(
                    command, invocation, evidence, connection
                ):
                    raise CentralOwnerPairingIssueUnavailable()
                if _catalog(connection) != _EXPECTED:
                    raise CentralOwnerPairingIssueUnavailable()
                _validate_rows(connection)
                _validate_redeem_rows(connection)
                self._fault("before_commit")
                connection.commit()
                issue_receipt_digest = _issue_receipt_digest(
                    org_id=command.org_id,
                    receipt_id=command.idempotency_key,
                    command_digest=command_digest,
                    intent_id=intent_id,
                    created_at=now.isoformat(),
                )
                return IssuedOwnerPairingIntent(
                    intent_id=intent_id,
                    pairing_code=SecretStr(code),
                    expires_at=expires,
                    evidence=evidence,
                    pairing_intent_digest=_pairing_intent_digest(
                        intent_id=intent_id, expires_at=expires, evidence=evidence
                    ),
                    issue_receipt_id=command.idempotency_key,
                    issue_receipt_digest=issue_receipt_digest,
                )
        except (CentralOwnerPairingIssueUnavailable, CentralOwnerPairingIssueConflict):
            raise
        except Exception as error:
            raise CentralOwnerPairingIssueUnavailable() from error

    def _replay(
        self, connection: sqlite3.Connection, intent_id: str
    ) -> IssuedOwnerPairingIntent:
        row = connection.execute(
            "SELECT key_id,nonce,code_envelope,expires_at,evidence_json,state,"
            "code_verifier,org_id,command_digest "
            "FROM central_owner_pairing_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        receipt = connection.execute(
            "SELECT idempotency_key,command_digest,intent_id,created_at "
            "FROM central_owner_pairing_issue_receipts WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        key = self._keys.current()
        if (
            row is None
            or receipt is None
            or row[0] != key.key_id
            or row[5] != "pending"
            or datetime.fromisoformat(row[3]) <= self._clock()
            or receipt[1] != row[8]
            or receipt[2] != intent_id
        ):
            raise CentralOwnerPairingIssueUnavailable()
        try:
            code = AESGCM(
                hmac.digest(key.key, b"owner-pairing-envelope-v1", "sha256")
            ).decrypt(
                b64decode(row[1], validate=True),
                b64decode(row[2], validate=True),
                intent_id.encode("utf-8"),
            ).decode("utf-8")
            expected_verifier = hmac.new(
                hmac.digest(key.key, b"owner-pairing-verifier-v1", "sha256"),
                code.encode("utf-8"),
                "sha256",
            ).hexdigest()
            if not hmac.compare_digest(expected_verifier, row[6]):
                raise CentralOwnerPairingIssueUnavailable()
            evidence = PairingIssuancePrincipalEvidence.model_validate_json(row[4])
            issue_receipt_digest = _issue_receipt_digest(
                org_id=row[7],
                receipt_id=receipt[0],
                command_digest=receipt[1],
                intent_id=intent_id,
                created_at=receipt[3],
            )
            expires_at = datetime.fromisoformat(row[3])
            return IssuedOwnerPairingIntent(
                intent_id=intent_id,
                pairing_code=SecretStr(code),
                expires_at=expires_at,
                evidence=evidence,
                pairing_intent_digest=_pairing_intent_digest(
                    intent_id=intent_id, expires_at=expires_at, evidence=evidence
                ),
                issue_receipt_id=receipt[0],
                issue_receipt_digest=issue_receipt_digest,
                replayed=True,
            )
        except (InvalidTag, ValueError) as error:
            raise CentralOwnerPairingIssueUnavailable() from error

    def redeem(
        self, command: RedeemOwnerPairingCommand
    ) -> RedeemedOwnerCredential:
        if type(command) is not RedeemOwnerPairingCommand:
            raise CentralOwnerPairingIssueUnavailable()
        now = self._clock()
        now_text = self._utc_second(now)
        thumbprint = device_key_thumbprint(command.device_public_key)
        try:
            with sqlite3.connect(self._path, timeout=30) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                if _catalog(connection) != _EXPECTED:
                    raise CentralOwnerPairingIssueUnavailable()
                _validate_rows(connection)
                _validate_redeem_rows(connection)
                row = connection.execute(
                    "SELECT org_id,owner_id,agent_id,device_class,command_digest,"
                    "code_verifier,identity_session_digest,evidence_json,expires_at,"
                    "state,key_id FROM central_owner_pairing_intents WHERE intent_id=?",
                    (command.intent_id,),
                ).fetchone()
                if row is None or datetime.fromisoformat(row[8]) <= now:
                    raise CentralOwnerPairingIssueUnavailable()
                key = self._keys.current()
                if row[10] != key.key_id:
                    raise CentralOwnerPairingIssueUnavailable()
                verifier_key = hmac.digest(
                    key.key, b"owner-pairing-verifier-v1", "sha256"
                )
                supplied_verifier = hmac.new(
                    verifier_key,
                    command.pairing_code.get_secret_value().encode("utf-8"),
                    "sha256",
                ).hexdigest()
                if not hmac.compare_digest(supplied_verifier, row[5]):
                    raise CentralOwnerPairingIssueUnavailable()
                evidence = PairingIssuancePrincipalEvidence.model_validate_json(
                    row[7]
                )
                issue_receipt = connection.execute(
                    "SELECT idempotency_key,command_digest,intent_id,created_at "
                    "FROM central_owner_pairing_issue_receipts "
                    "WHERE intent_id=?",
                    (command.intent_id,),
                ).fetchone()
                session = connection.execute(
                    "SELECT identity_session_id FROM production_identity_sessions "
                    "WHERE identity_session_digest=?",
                    (row[6],),
                ).fetchone()
                if (
                    issue_receipt is None
                    or session is None
                    or issue_receipt[2] != command.intent_id
                    or issue_receipt[1] != row[4]
                ):
                    raise CentralOwnerPairingIssueUnavailable()
                issue_command = IssueOwnerPairingCommand(
                    org_id=row[0],
                    principal_id=row[1],
                    agent_id=row[2],
                    expected_card_revision=evidence.card_revision,
                    expected_card_digest=evidence.card_digest,
                    device_class=row[3],
                    idempotency_key=issue_receipt[0],
                )
                invocation = AuthoringInvocation(
                    session=AuthoringIdentitySessionRef(
                        value=SecretStr(session[0])
                    ),
                    org_id=row[0],
                    principal_id=row[1],
                    identity_provider=evidence.identity_provider,
                )
                current = self._authorizer.current(
                    issue_command, invocation, connection
                )
                if current != evidence:
                    raise CentralOwnerPairingIssueUnavailable()
                redeem_digest = _redeem_command_digest(
                    intent_id=command.intent_id,
                    idempotency_key=command.idempotency_key,
                    device_key_thumbprint_value=thumbprint,
                    code_verifier=row[5],
                )
                existing = connection.execute(
                    "SELECT command_digest,credential_id,created_at FROM "
                    "central_owner_pairing_redeem_receipts WHERE idempotency_key=?",
                    (command.idempotency_key,),
                ).fetchone()
                if row[9] == "redeemed":
                    if existing is None or existing[0] != redeem_digest:
                        raise CentralOwnerPairingIssueConflict()
                    replay = connection.execute(
                        "SELECT command_digest,device_key_thumbprint,envelope_json,"
                        "replay_expires_at,purged_at FROM "
                        "central_owner_pairing_replay_envelopes "
                        "WHERE intent_id=?",
                        (command.intent_id,),
                    ).fetchone()
                    if (
                        replay is None
                        or replay[0] != redeem_digest
                        or replay[1] != thumbprint
                        or not isinstance(replay[2], str)
                        or replay[4] is not None
                        or datetime.fromisoformat(replay[3]) <= now
                    ):
                        raise CentralOwnerPairingIssueUnavailable()
                    expires_at = datetime.fromisoformat(row[8])
                    return RedeemedOwnerCredential(
                        credential_id=existing[1],
                        envelope=parse_owner_credential_envelope(
                            replay[2].encode("utf-8")
                        ),
                        evidence=evidence,
                        pairing_intent_digest=_pairing_intent_digest(
                            intent_id=command.intent_id,
                            expires_at=expires_at,
                            evidence=evidence,
                        ),
                        issue_receipt_id=issue_receipt[0],
                        issue_receipt_digest=_issue_receipt_digest(
                            org_id=row[0],
                            receipt_id=issue_receipt[0],
                            command_digest=issue_receipt[1],
                            intent_id=command.intent_id,
                            created_at=issue_receipt[3],
                        ),
                        redeem_receipt_id=command.idempotency_key,
                        redeem_receipt_digest=_redeem_receipt_digest(
                            receipt_id=command.idempotency_key,
                            command_digest=existing[0],
                            intent_id=command.intent_id,
                            credential_id=existing[1],
                            created_at=existing[2],
                        ),
                        replayed=True,
                    )
                if row[9] != "pending" or existing is not None:
                    raise CentralOwnerPairingIssueConflict()
                credential_id = "credential-" + secrets.token_urlsafe(24)
                expires = now + timedelta(days=30)
                aad = CredentialEnvelopeAad(
                    agent_card_id=row[2],
                    credential_generation=1,
                    credential_id=credential_id,
                    device_key_thumbprint=thumbprint,
                    expires_at=self._utc_second(expires),
                    issued_at=now_text,
                    org_id=row[0],
                    owner_user_id=row[1],
                    scope=("author.read", "author.write"),
                )

                def secret_verifier(secret: bytes) -> tuple[str, str]:
                    digest = hmac.new(
                        hmac.digest(
                            key.key,
                            b"owner-credential-verifier-v1",
                            "sha256",
                        ),
                        secret,
                        "sha256",
                    ).hexdigest()
                    return key.key_id, digest

                encrypted = encrypt_owner_credential_with_verifier(
                    aad, command.device_public_key, verifier=secret_verifier
                )
                envelope_json = serialize_owner_credential_envelope(
                    encrypted.envelope
                ).decode("utf-8")
                replay_expires = now + timedelta(minutes=2)
                self._fault("before_credential")
                connection.execute(
                    "INSERT INTO central_owner_pairing_credentials "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        credential_id,
                        row[0],
                        row[1],
                        row[2],
                        thumbprint,
                        1,
                        "active",
                        encrypted.verifier_key_id,
                        encrypted.credential_verifier,
                        now_text,
                        self._utc_second(expires),
                    ),
                )
                self._fault("after_credential")
                connection.execute(
                    "INSERT INTO central_owner_pairing_replay_envelopes "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        command.intent_id,
                        redeem_digest,
                        thumbprint,
                        envelope_json,
                        self._utc_second(replay_expires),
                        None,
                    ),
                )
                connection.execute(
                    "INSERT INTO central_owner_pairing_redeem_receipts VALUES(?,?,?,?,?)",
                    (
                        command.idempotency_key,
                        redeem_digest,
                        command.intent_id,
                        credential_id,
                        now_text,
                    ),
                )
                issue_evidence_digest = _digest(
                    evidence.model_dump(mode="json")
                )
                current_evidence_digest = _digest(
                    current.model_dump(mode="json")
                )
                aad_digest = sha256(
                    _canonical(aad.model_dump(mode="json"))
                ).hexdigest()
                connection.execute(
                    "INSERT INTO central_owner_pairing_redeem_audit "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        command.intent_id,
                        "pairing_redeemed",
                        row[4],
                        redeem_digest,
                        issue_evidence_digest,
                        current_evidence_digest,
                        credential_id,
                        aad_digest,
                        _digest(
                            [
                                credential_id,
                                row[0],
                                row[1],
                                row[2],
                                thumbprint,
                                1,
                                "active",
                                encrypted.verifier_key_id,
                                encrypted.credential_verifier,
                                now_text,
                                self._utc_second(expires),
                            ]
                        ),
                        sha256(envelope_json.encode("utf-8")).hexdigest(),
                        now_text,
                    ),
                )
                connection.execute(
                    "INSERT INTO central_owner_pairing_redeem_outbox VALUES(?,?,?,?)",
                    (
                        command.intent_id,
                        "pairing_redeemed",
                        _digest([command.intent_id, credential_id, aad_digest]),
                        now_text,
                    ),
                )
                changed = connection.execute(
                    "UPDATE central_owner_pairing_intents SET state='redeemed',"
                    "redeemed_at=? WHERE intent_id=? AND state='pending'",
                    (now_text, command.intent_id),
                ).rowcount
                if changed != 1:
                    raise CentralOwnerPairingIssueConflict()
                self._fault("before_redeem_precommit")
                if not self._authorizer.verify_precommit(
                    issue_command, invocation, evidence, connection
                ):
                    raise CentralOwnerPairingIssueUnavailable()
                _validate_rows(connection)
                _validate_redeem_rows(connection)
                self._fault("before_redeem_commit")
                connection.commit()
                issue_receipt_digest = _issue_receipt_digest(
                    org_id=row[0],
                    receipt_id=issue_receipt[0],
                    command_digest=issue_receipt[1],
                    intent_id=command.intent_id,
                    created_at=issue_receipt[3],
                )
                return RedeemedOwnerCredential(
                    credential_id=credential_id,
                    envelope=encrypted.envelope,
                    evidence=evidence,
                    pairing_intent_digest=_pairing_intent_digest(
                        intent_id=command.intent_id,
                        expires_at=datetime.fromisoformat(row[8]),
                        evidence=evidence,
                    ),
                    issue_receipt_id=issue_receipt[0],
                    issue_receipt_digest=issue_receipt_digest,
                    redeem_receipt_id=command.idempotency_key,
                    redeem_receipt_digest=_redeem_receipt_digest(
                        receipt_id=command.idempotency_key,
                        command_digest=redeem_digest,
                        intent_id=command.intent_id,
                        credential_id=credential_id,
                        created_at=now_text,
                    ),
                )
        except (CentralOwnerPairingIssueUnavailable, CentralOwnerPairingIssueConflict):
            raise
        except Exception as error:
            raise CentralOwnerPairingIssueUnavailable() from error

    def purge_expired_replay_envelopes(self) -> int:
        now_text = self._utc_second(self._clock())
        try:
            with sqlite3.connect(self._path, timeout=30) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                if _catalog(connection) != _EXPECTED:
                    raise CentralOwnerPairingIssueUnavailable()
                _validate_rows(connection)
                _validate_redeem_rows(connection)
                changed = connection.execute(
                    "UPDATE central_owner_pairing_replay_envelopes "
                    "SET envelope_json=NULL,purged_at=? "
                    "WHERE envelope_json IS NOT NULL AND purged_at IS NULL "
                    "AND replay_expires_at<=?",
                    (now_text, now_text),
                ).rowcount
                self._fault("after_replay_purge")
                _validate_rows(connection)
                _validate_redeem_rows(connection)
                self._fault("before_replay_purge_commit")
                connection.commit()
                return changed
        except CentralOwnerPairingIssueUnavailable:
            raise
        except Exception as error:
            raise CentralOwnerPairingIssueUnavailable() from error

    @staticmethod
    def _utc_second(value: datetime) -> str:
        if (
            value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.microsecond != 0
        ):
            raise CentralOwnerPairingIssueUnavailable()
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CentralOwnerPairingIssueConflict",
    "CentralOwnerPairingIssueStore",
    "CentralOwnerPairingIssueUnavailable",
    "CentralPairingServerKey",
    "CentralPairingServerKeyProvider",
    "IssueOwnerPairingCommand",
    "IssuedOwnerPairingIntent",
    "PairingIssuancePrincipalEvidence",
    "ProductionCentralPairingIssueAuthorizer",
    "RedeemOwnerPairingCommand",
    "RedeemedOwnerCredential",
]
