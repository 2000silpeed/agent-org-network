"""RB3.1a Central Question Intake installation composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
from typing import Final, Literal, Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

from agent_org_network.central_authority import (
    AuthorityPolicySnapshot,
    FileReloadingBrowserSessionAuthority,
    SnapshotCentralAuthorizer,
    load_authority_policy_yaml,
)
from agent_org_network.central_bootstrap_admin import (
    BootstrapAdminApplication,
    BootstrapAdminAttestation,
    BootstrapAdminConfig,
    BootstrapAdminResult,
    BootstrapAdminUnavailable,
    BootstrapOidcDeviceAuthorizer,
)
from agent_org_network.central_bootstrap_device import HttpBootstrapOidcDeviceAuthorizer
from agent_org_network.central_bootstrap_sqlite import (
    CentralBootstrapAdminSealStore,
    central_bootstrap_admin_schema_ready,
    migrate_central_bootstrap_admin_schema,
)
from agent_org_network.central_browser_auth import browser_redirect_uri
from agent_org_network.central_browser_auth import BrowserPkceVerifierVault
from agent_org_network.central_browser_auth import new_opaque_browser_handle
from agent_org_network.central_browser_auth_sqlite import (
    CentralBrowserAuthSqliteStore,
    browser_auth_schema_ready,
    migrate_browser_auth_schema,
)
from agent_org_network.central_browser_oidc import (
    BrowserOidcSessionApplication,
    BrowserSessionApplication,
    HttpOidcAuthorizationCodeExchange,
    OidcAuthorizationCodeExchangePort,
)
from agent_org_network.central_oidc_principal import (
    CurrentRegistryOidcPrincipalResolver,
)
from agent_org_network.central_question_intake import CentralQuestionIntakeApplication
from agent_org_network.central_question_request_sqlite import (
    CentralQuestionRequestSqliteStore,
    central_question_request_schema_ready,
    migrate_central_question_request_schema,
)
from agent_org_network.central_operational_evidence import (
    OperationalEvidenceReader,
    OperationalEvidenceProjector,
    central_operational_evidence_schema_ready,
    migrate_central_operational_evidence_schema,
)
from agent_org_network.central_policy_revision import (
    PolicyRevisionApplication,
    SqlitePolicyApprovalPort,
    migrate_central_policy_revision_schema,
    policy_revision_schema_ready,
)
from agent_org_network.central_question_lifecycle import (
    ApprovalDispositionApplication,
    CentralQuestionLifecycleApplication,
    FileReloadingQuestionCreateAuthority,
    QuestionCreateApplication,
    FileReloadingApprovalDispositionAuthority,
    FileReloadingQuestionFeedbackAuthority,
    CentralQuestionLifecycleStore,
    SqliteCentralRegistryRootManagerResolver,
    SqliteProductionCardBindingResolver,
    central_question_lifecycle_schema_ready,
    migrate_central_question_lifecycle_schema,
)
from agent_org_network.central_lifecycle_recovery import (
    CentralLifecycleRecoveryCoordinator,
    CentralPolicyRouter,
    FileReloadingLifecycleRouteAuthority,
)
from agent_org_network.central_inbox_conflict import (
    ConflictConcurrenceApplication,
    ConflictInboxApplication,
    FileReloadingConflictAuthority,
    central_inbox_conflict_schema_ready,
    migrate_central_inbox_conflict_schema,
)
from agent_org_network.central_inbox_approval import (
    ApprovalDispositionInboxApplication,
    ApprovalInboxApplication,
    ApprovalReassignmentApplication,
    FileReloadingApprovalInboxAuthority,
    InboxBoundApprovalDispositionAuthority,
    central_inbox_approval_schema_ready,
    migrate_central_inbox_approval_schema,
)
from agent_org_network.central_inbox_review import (
    BackupReviewDispositionApplication,
    FileReloadingReviewInboxAuthority,
    ReevaluationDispositionApplication,
    ReviewInboxApplication,
    ReviewOutboxProjector,
    ReviewOutboxRecovery,
    central_inbox_review_schema_ready,
    migrate_central_inbox_review_schema,
)
from agent_org_network.central_registry_admission import (
    SessionDerivedRegistryRegistrationFactory,
)
from agent_org_network.oidc import HttpOidcProvider, OidcProvider
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    ProductionRegistryUserDenied,
    SqliteProductionRegistryUsers,
    validate_production_registry_user_rows,
    validate_production_registry_user_connection,
)
from agent_org_network.sqlite_production_agent_cards import (
    SqliteProductionAgentCards,
    validate_production_agent_card_connection,
)


CENTRAL_SCHEMA_NAME: Final = "central-installation"
CENTRAL_SCHEMA_VERSION: Final = 20
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MARKER_TABLE = (
    "CREATE TABLE IF NOT EXISTS aon_installation_schema "
    "(name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
)


class CentralInstallationConfigurationError(ValueError):
    """An installation profile is missing, malformed, or unsafe."""


class CentralProductionUnavailable(CentralInstallationConfigurationError):
    """Production composition is not implemented by RB3.1a."""


class CentralCompositionUnavailable(RuntimeError):
    """The configured local-reference dependencies are not currently capable."""


class MigrationFaultInjector(Protocol):
    def __call__(self, point: str) -> None: ...


def _read_active_policy_snapshot(
    database_path: Path, *, expected_org_id: str
) -> AuthorityPolicySnapshot:
    """Read the current PolicyRevision; never fall back to the YAML file."""
    try:
        with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT r.canonical_document FROM central_active_policy_pointers p "
                "JOIN central_policy_revisions r ON r.revision_id=p.revision_id "
                "WHERE p.org_id=?",
                (expected_org_id,),
            ).fetchone()
        if row is None:
            raise CentralCompositionUnavailable()
        import yaml

        document = json.loads(str(row[0]))
        if not isinstance(document, dict):
            raise CentralCompositionUnavailable()
        return load_authority_policy_yaml(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            expected_org_id=expected_org_id,
        )
    except CentralCompositionUnavailable:
        raise
    except Exception as error:
        raise CentralCompositionUnavailable() from error


@dataclass(frozen=True, slots=True)
class CentralInstallationConfig:
    profile: Literal["local-reference", "production"]
    org_id: str
    oidc_provider_id: str
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str
    bootstrap_oidc_device_authorization_url: str
    bootstrap_oidc_device_client_id: str
    bootstrap_oidc_scope: str
    central_public_origin: str
    browser_oidc_authorization_url: str
    browser_oidc_token_url: str
    browser_oidc_client_id: str
    browser_oidc_scope: str
    authority_snapshot_path: Path
    database_path: Path
    data_directory: Path
    bind_host: str
    port: int


@dataclass(frozen=True, slots=True)
class CentralComposition:
    """One immutable policy plus the durable intake dependencies."""

    config: CentralInstallationConfig
    oidc: OidcProvider | None
    authority_snapshot: AuthorityPolicySnapshot | None
    authority: SnapshotCentralAuthorizer | None
    registry: SqliteProductionRegistryUsers | None
    principals: CurrentRegistryOidcPrincipalResolver | None
    requests: CentralQuestionRequestSqliteStore | None
    intake: CentralQuestionIntakeApplication | None
    bootstrap_seals: CentralBootstrapAdminSealStore | None = None
    browser_auth: CentralBrowserAuthSqliteStore | None = None
    browser_sessions: BrowserSessionApplication | None = None
    browser_oidc: BrowserOidcSessionApplication | None = None
    registry_admission_factory: Callable[[str], SessionDerivedRegistryRegistrationFactory] | None = None
    browser_clock: Callable[[], datetime] | None = None
    question_request_id_factory: Callable[[], str] | None = None
    lifecycle_store: CentralQuestionLifecycleStore | None = None
    lifecycle: CentralQuestionLifecycleApplication | None = None
    lifecycle_recovery: CentralLifecycleRecoveryCoordinator | None = None
    question_create: QuestionCreateApplication | None = None
    approval_disposition_authority: FileReloadingApprovalDispositionAuthority | None = None
    question_feedback_authority: FileReloadingQuestionFeedbackAuthority | None = None
    conflict_inbox: ConflictInboxApplication | None = None
    conflict_concurrence: ConflictConcurrenceApplication | None = None
    approval_inbox: ApprovalInboxApplication | None = None
    approval_disposition: ApprovalDispositionInboxApplication | None = None
    approval_reassignment: ApprovalReassignmentApplication | None = None
    review_inbox: ReviewInboxApplication | None = None
    backup_review_disposition: BackupReviewDispositionApplication | None = None
    reevaluation_disposition: ReevaluationDispositionApplication | None = None
    review_recovery: ReviewOutboxRecovery | None = None
    operational_evidence: OperationalEvidenceReader | None = None
    operational_evidence_projector: OperationalEvidenceProjector | None = None
    policy_revision: PolicyRevisionApplication | None = None

    def schema_ready(self) -> bool:
        try:
            validate_central_installation_config(self.config)
        except CentralInstallationConfigurationError:
            return False
        return _central_schema_ready(self.config.database_path)

    def registry_bootstrapped(self) -> bool:
        if (
            self.registry is None
            or self.bootstrap_seals is None
            or self.authority_snapshot is None
        ):
            return False
        try:
            seal = self.bootstrap_seals.get(self.config.org_id)
            return (
                seal is not None
                and _bootstrap_root_evidence_ready(
                    self.config.database_path,
                    self.config.org_id,
                    seal.registry_user_id,
                )
            )
        except Exception:
            return False

    def intake_ready(self) -> bool:
        return (
            self.config.data_directory.is_dir()
            and self.schema_ready()
            and self.oidc is not None
            and self.authority_snapshot is not None
            and self.authority is not None
            and self.registry is not None
            and self.principals is not None
            and self.requests is not None
            and self.intake is not None
            and self.lifecycle is not None
            and self.lifecycle_recovery is not None
            and self.question_create is not None
            and self.conflict_inbox is not None
            and self.conflict_concurrence is not None
            and self.approval_inbox is not None
            and self.approval_disposition is not None
            and self.approval_reassignment is not None
            and self.review_inbox is not None
            and self.backup_review_disposition is not None
            and self.reevaluation_disposition is not None
            and self.review_recovery is not None
            and self.operational_evidence is not None
            and self.operational_evidence_projector is not None
            and self.policy_revision is not None
            and self.registry_bootstrapped()
        )

    def close(self) -> None:
        """Release every SQLite handle owned by this composition, best-effort/idempotently."""
        for dependency in (
            self.browser_auth,
            self.lifecycle_store,
            self.requests,
            self.bootstrap_seals,
            self.registry,
        ):
            if dependency is None:
                continue
            try:
                dependency.close()
            except Exception:
                pass


def load_central_installation_config(profile_path: Path) -> CentralInstallationConfig:
    """Load the exact local-reference profile without environment fallback."""
    values = _read_profile(profile_path)
    allowed = {
        "profile",
        "org_id",
        "oidc_provider_id",
        "oidc_issuer",
        "oidc_audience",
        "oidc_jwks_url",
        "bootstrap_oidc_device_authorization_url",
        "bootstrap_oidc_device_client_id",
        "bootstrap_oidc_scope",
        "central_public_origin",
        "browser_oidc_authorization_url",
        "browser_oidc_token_url",
        "browser_oidc_client_id",
        "browser_oidc_scope",
        "authority_snapshot_path",
        "database_path",
        "data_directory",
        "bind_host",
        "port",
    }
    if set(values) != allowed:
        raise CentralInstallationConfigurationError("Central profile fields must match exactly")
    profile = _required_string(values, "profile")
    if profile not in {"local-reference", "production"}:
        raise CentralInstallationConfigurationError("unknown Central profile")
    config = CentralInstallationConfig(
        profile=cast(Literal["local-reference", "production"], profile),
        org_id=_required_string(values, "org_id"),
        oidc_provider_id=_required_string(values, "oidc_provider_id"),
        oidc_issuer=_required_string(values, "oidc_issuer"),
        oidc_audience=_required_string(values, "oidc_audience"),
        oidc_jwks_url=_required_string(values, "oidc_jwks_url"),
        bootstrap_oidc_device_authorization_url=_required_string(
            values, "bootstrap_oidc_device_authorization_url"
        ),
        bootstrap_oidc_device_client_id=_required_string(
            values, "bootstrap_oidc_device_client_id"
        ),
        bootstrap_oidc_scope=_required_string(values, "bootstrap_oidc_scope"),
        central_public_origin=_required_string(values, "central_public_origin"),
        browser_oidc_authorization_url=_required_string(values, "browser_oidc_authorization_url"),
        browser_oidc_token_url=_required_string(values, "browser_oidc_token_url"),
        browser_oidc_client_id=_required_string(values, "browser_oidc_client_id"),
        browser_oidc_scope=_required_string(values, "browser_oidc_scope"),
        authority_snapshot_path=_absolute_path(values, "authority_snapshot_path"),
        database_path=_absolute_path(values, "database_path"),
        data_directory=_absolute_path(values, "data_directory"),
        bind_host=_required_string(values, "bind_host"),
        port=_required_int(values, "port"),
    )
    validate_central_installation_config(config)
    return config


def validate_central_installation_config(config: CentralInstallationConfig) -> None:
    """Revalidate public dataclasses and keep production unavailable."""
    if type(config) is not CentralInstallationConfig:
        raise CentralInstallationConfigurationError("validated Central config required")
    if config.profile == "production":
        raise CentralProductionUnavailable("production Central composition unavailable")
    if config.profile != "local-reference":
        raise CentralInstallationConfigurationError("unknown Central profile")
    if (
        _REFERENCE.fullmatch(config.org_id) is None
        or _REFERENCE.fullmatch(config.oidc_provider_id) is None
        or not config.oidc_audience.strip()
        or not _exact_https_url(config.oidc_issuer)
        or not _exact_https_url(config.oidc_jwks_url)
        or not _exact_https_url(config.bootstrap_oidc_device_authorization_url)
        or _REFERENCE.fullmatch(config.bootstrap_oidc_device_client_id) is None
        or not _valid_oidc_scope(config.bootstrap_oidc_scope)
        or not _exact_https_origin(config.central_public_origin)
        or not _exact_https_url(config.browser_oidc_authorization_url)
        or not _exact_https_url(config.browser_oidc_token_url)
        or not _same_https_origin(config.oidc_issuer, config.browser_oidc_authorization_url)
        or not _same_https_origin(config.oidc_issuer, config.browser_oidc_token_url)
        or not _valid_browser_oidc_client_id(config.browser_oidc_client_id)
        or config.browser_oidc_scope != "openid email"
        or not _is_absolute_path(config.authority_snapshot_path)
        or not _is_absolute_path(config.database_path)
        or not _is_absolute_path(config.data_directory)
        or config.bind_host != "127.0.0.1"
        or type(config.port) is not int
        or config.port != 8010
    ):
        raise CentralInstallationConfigurationError("invalid local-reference Central config")


def compose_central(
    config: CentralInstallationConfig,
    *,
    oidc_provider: OidcProvider | None = None,
    browser_code_exchange: OidcAuthorizationCodeExchangePort | None = None,
    browser_pkce_vault: BrowserPkceVerifierVault | None = None,
    browser_random_handle: Callable[[], str] = new_opaque_browser_handle,
    request_id_factory: Callable[[], str] = lambda: uuid4().hex,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CentralComposition:
    """Compose capabilities without creating or migrating any schema."""
    validate_central_installation_config(config)
    oidc = oidc_provider
    if oidc is None:
        try:
            oidc = HttpOidcProvider(
                config.oidc_issuer,
                config.oidc_audience,
                config.oidc_jwks_url,
            )
        except (Exception, SystemExit):
            oidc = None

    snapshot: AuthorityPolicySnapshot | None = None
    authority: SnapshotCentralAuthorizer | None = None
    try:
        snapshot = load_authority_policy_yaml(
            config.authority_snapshot_path.read_text(encoding="utf-8"),
            expected_org_id=config.org_id,
        )
    except Exception:
        snapshot = None
    if _central_schema_ready(config.database_path):
        try:
            # v20 runtime reads are DB-only.  An invalid or changed YAML file
            # must not silently become a process-local fallback.
            authority = SnapshotCentralAuthorizer(
                lambda: _read_active_policy_snapshot(
                    config.database_path, expected_org_id=config.org_id
                )
            )
        except Exception:
            authority = None
    elif snapshot is not None:
        authority = SnapshotCentralAuthorizer(snapshot)

    registry: SqliteProductionRegistryUsers | None = None
    principals: CurrentRegistryOidcPrincipalResolver | None = None
    requests: CentralQuestionRequestSqliteStore | None = None
    intake: CentralQuestionIntakeApplication | None = None
    bootstrap_seals: CentralBootstrapAdminSealStore | None = None
    browser_auth: CentralBrowserAuthSqliteStore | None = None
    browser_sessions: BrowserSessionApplication | None = None
    browser_oidc: BrowserOidcSessionApplication | None = None
    registry_admission_factory: Callable[[str], SessionDerivedRegistryRegistrationFactory] | None = None
    browser_clock: Callable[[], datetime] | None = None
    lifecycle_store: CentralQuestionLifecycleStore | None = None
    lifecycle: CentralQuestionLifecycleApplication | None = None
    lifecycle_recovery: CentralLifecycleRecoveryCoordinator | None = None
    question_create: QuestionCreateApplication | None = None
    approval_disposition_authority: FileReloadingApprovalDispositionAuthority | None = None
    question_feedback_authority: FileReloadingQuestionFeedbackAuthority | None = None
    conflict_inbox: ConflictInboxApplication | None = None
    conflict_concurrence: ConflictConcurrenceApplication | None = None
    approval_inbox: ApprovalInboxApplication | None = None
    approval_disposition: ApprovalDispositionInboxApplication | None = None
    approval_reassignment: ApprovalReassignmentApplication | None = None
    review_inbox: ReviewInboxApplication | None = None
    backup_review_disposition: BackupReviewDispositionApplication | None = None
    reevaluation_disposition: ReevaluationDispositionApplication | None = None
    review_recovery: ReviewOutboxRecovery | None = None
    operational_evidence: OperationalEvidenceReader | None = None
    operational_evidence_projector: OperationalEvidenceProjector | None = None
    policy_revision: PolicyRevisionApplication | None = None
    if _central_schema_ready(config.database_path):
        try:
            # The API lifespan owns startup drain and continuous pending/
            # expired-intent recovery.  Composition only supplies the durable
            # projector capability; it never starts a process-local fallback.
            operational_evidence_projector = OperationalEvidenceProjector(
                config.database_path,
                worker_id="central-operational-runtime",
                clock=clock,
            )
            # The HTTP control plane receives only this durable reader.  It
            # must never substitute a process-local feed/audit fallback.
            operational_evidence = OperationalEvidenceReader(config.database_path)
            policy_revision = PolicyRevisionApplication(
                config.database_path,
                SqlitePolicyApprovalPort(config.database_path, clock=clock),
                clock=clock,
            )
            review_recovery = ReviewOutboxRecovery(
                projector=ReviewOutboxProjector(
                    database_path=config.database_path,
                    worker_id="central-review-startup",
                    clock=clock,
                    lease_duration=timedelta(seconds=30),
                )
            )
            review_recovery.drain()
            registry = SqliteProductionRegistryUsers(
                config.database_path,
                authorize=_ReadOnlyRegistrationAuthorizer(),
            )
            principals = CurrentRegistryOidcPrincipalResolver(
                registry,
                org_id=config.org_id,
                provider_id=config.oidc_provider_id,
                issuer=config.oidc_issuer,
                audience=config.oidc_audience,
            )
            requests = CentralQuestionRequestSqliteStore(config.database_path)
            root_manager_resolver = SqliteCentralRegistryRootManagerResolver()
            lifecycle_store = CentralQuestionLifecycleStore(
                config.database_path,
                root_manager_resolver=root_manager_resolver,
                card_binding_resolver=SqliteProductionCardBindingResolver(),
            )
            if authority is not None:
                approval_disposition_authority = FileReloadingApprovalDispositionAuthority(
                    authority_policy_path=config.authority_snapshot_path, configured_org_id=config.org_id, clock=clock
                )
                question_feedback_authority = FileReloadingQuestionFeedbackAuthority(
                    authority_policy_path=config.authority_snapshot_path, configured_org_id=config.org_id, clock=clock
                )
                lifecycle = CentralQuestionLifecycleApplication(
                    store=lifecycle_store,
                    router=CentralPolicyRouter(
                        database_path=config.database_path, authority_policy_path=config.authority_snapshot_path,
                        org_id=config.org_id,
                    ),
                    route_authority=FileReloadingLifecycleRouteAuthority(
                        authority_policy_path=config.authority_snapshot_path, org_id=config.org_id,
                    ),
                    request_id_factory=request_id_factory,
                    clock=clock,
                    deadline=lambda _org, _state, at: at + timedelta(minutes=5),
                    manager_item_id_factory=lambda: uuid4().hex,
                    root_manager_resolver=root_manager_resolver,
                )
                lifecycle_recovery = CentralLifecycleRecoveryCoordinator(
                    database_path=config.database_path, lifecycle=lifecycle,
                )
                question_create = QuestionCreateApplication(
                    store=lifecycle_store,
                    authority=FileReloadingQuestionCreateAuthority(
                        authority_policy_path=config.authority_snapshot_path, configured_org_id=config.org_id, clock=clock,
                    ),
                    request_id_factory=request_id_factory, clock=clock,
                    deadline=lambda _org, _state, at: at + timedelta(minutes=5),
                )
                conflict_authority = FileReloadingConflictAuthority(
                    authority_policy_path=config.authority_snapshot_path,
                    configured_org_id=config.org_id,
                    clock=clock,
                )
                conflict_inbox = ConflictInboxApplication(
                    database_path=config.database_path,
                    authority=conflict_authority,
                )
                conflict_concurrence = ConflictConcurrenceApplication(
                    database_path=config.database_path,
                    authority=conflict_authority,
                    receipt_id_factory=lambda: uuid4().hex,
                    manager_item_id_factory=lambda: uuid4().hex,
                    clock=clock,
                )
                approval_inbox_authority = FileReloadingApprovalInboxAuthority(
                    authority_policy_path=config.authority_snapshot_path,
                    configured_org_id=config.org_id,
                    disposition_authority=approval_disposition_authority,
                    clock=clock,
                )
                approval_inbox = ApprovalInboxApplication(
                    database_path=config.database_path,
                    authority=approval_inbox_authority,
                )
                approval_disposition_writer = ApprovalDispositionApplication(
                    store=lifecycle_store,
                    authority=InboxBoundApprovalDispositionAuthority(
                        approval_inbox_authority
                    ),
                    record_id_factory=lambda: uuid4().hex,
                    clock=clock,
                )
                approval_disposition = ApprovalDispositionInboxApplication(
                    database_path=config.database_path,
                    disposition=approval_disposition_writer,
                )
                approval_reassignment = ApprovalReassignmentApplication(
                    database_path=config.database_path,
                    authority=approval_inbox_authority,
                    approval_item_id_factory=lambda: uuid4().hex,
                    receipt_id_factory=lambda: uuid4().hex,
                    clock=clock,
                    deadline=lambda _org, at: at + timedelta(minutes=5),
                )
                review_authority = FileReloadingReviewInboxAuthority(
                    authority_policy_path=config.authority_snapshot_path,
                    configured_org_id=config.org_id,
                    clock=clock,
                )
                review_inbox = ReviewInboxApplication(
                    database_path=config.database_path,
                    authority=review_authority,
                )
                backup_review_disposition = (
                    BackupReviewDispositionApplication(
                        database_path=config.database_path,
                        authority=review_authority,
                        receipt_id_factory=lambda: uuid4().hex,
                        correction_record_id_factory=lambda: uuid4().hex,
                        clock=clock,
                    )
                )
                reevaluation_disposition = ReevaluationDispositionApplication(
                    database_path=config.database_path,
                    authority=review_authority,
                    receipt_id_factory=lambda: uuid4().hex,
                    reanswer_request_id_factory=lambda: uuid4().hex,
                    clock=clock,
                )
            bootstrap_seals = CentralBootstrapAdminSealStore(config.database_path)
            browser_auth = CentralBrowserAuthSqliteStore(config.database_path)
            browser_sessions = BrowserSessionApplication(
                provider_id=config.oidc_provider_id,
                transactions=browser_auth,
                authority=FileReloadingBrowserSessionAuthority(
                    config.authority_snapshot_path, expected_org_id=config.org_id
                ),
                clock=clock,
            )
            # The global Registry store remains permanently read-only.  This
            # closure receives only a validated opaque-handle digest at the
            # request boundary and constructs fresh mutable UoWs per request.
            # It retains neither browser cookies nor CSRF/OIDC claim material.
            def _registry_admission_factory(
                session_digest: str,
            ) -> SessionDerivedRegistryRegistrationFactory:
                return SessionDerivedRegistryRegistrationFactory(
                    database_path=config.database_path,
                    authority_snapshot_path=config.authority_snapshot_path,
                    org_id=config.org_id,
                    provider_id=config.oidc_provider_id,
                    session_digest=session_digest,
                    clock=clock,
                )

            registry_admission_factory = _registry_admission_factory
            browser_clock = clock
            browser_verifier: OidcProvider | None = None
            if browser_code_exchange is None:
                try:
                    browser_verifier = HttpOidcProvider(
                        config.oidc_issuer,
                        config.browser_oidc_client_id,
                        config.oidc_jwks_url,
                    )
                except (Exception, SystemExit):
                    browser_verifier = None
            exchange = browser_code_exchange
            if exchange is None and browser_verifier is not None:
                exchange = HttpOidcAuthorizationCodeExchange(
                    token_url=config.browser_oidc_token_url,
                    client_id=config.browser_oidc_client_id,
                    issuer=config.oidc_issuer,
                    id_token_verifier=browser_verifier,
                )
            if exchange is not None and authority is not None:
                browser_oidc = BrowserOidcSessionApplication(
                    org_id=config.org_id,
                    provider_id=config.oidc_provider_id,
                    issuer=config.oidc_issuer,
                    authorization_url=config.browser_oidc_authorization_url,
                    client_id=config.browser_oidc_client_id,
                    scope=config.browser_oidc_scope,
                    redirect_uri=central_browser_oidc_redirect_uri(config),
                    transactions=browser_auth,
                    registry=registry,
                    authority=FileReloadingBrowserSessionAuthority(
                        config.authority_snapshot_path, expected_org_id=config.org_id
                    ),
                    exchange=exchange,
                    vault=browser_pkce_vault or BrowserPkceVerifierVault(),
                    clock=clock,
                    random_handle=browser_random_handle,
                )
            if authority is not None:
                intake = CentralQuestionIntakeApplication(
                    requests=requests,
                    central_authorizer=authority,
                    deadline_policy=_ReceivedDeadlinePolicy(),
                    request_id_factory=request_id_factory,
                    clock=clock,
                )
        except Exception:
            if registry is not None:
                registry.close()
            registry = None
            principals = None
            if requests is not None:
                requests.close()
            if lifecycle_store is not None:
                lifecycle_store.close()
            if bootstrap_seals is not None:
                bootstrap_seals.close()
            if browser_auth is not None:
                browser_auth.close()
            requests = None
            intake = None
            bootstrap_seals = None
            browser_auth = None
            browser_oidc = None
            browser_sessions = None
            registry_admission_factory = None
            browser_clock = None
            lifecycle_store = None
            lifecycle = None
            lifecycle_recovery = None
            question_create = None
            approval_disposition_authority = None
            question_feedback_authority = None
            conflict_inbox = None
            conflict_concurrence = None
            approval_inbox = None
            approval_disposition = None
            approval_reassignment = None
            review_inbox = None
            backup_review_disposition = None
            reevaluation_disposition = None
            review_recovery = None
            operational_evidence = None
            operational_evidence_projector = None
            policy_revision = None
    return CentralComposition(
        config=config,
        oidc=oidc,
        authority_snapshot=snapshot,
        authority=authority,
        registry=registry,
        principals=principals,
        requests=requests,
        intake=intake,
        bootstrap_seals=bootstrap_seals,
        browser_auth=browser_auth,
        browser_sessions=browser_sessions,
        browser_oidc=browser_oidc,
        registry_admission_factory=registry_admission_factory,
        browser_clock=browser_clock,
        question_request_id_factory=request_id_factory,
        lifecycle_store=lifecycle_store,
        lifecycle=lifecycle,
        lifecycle_recovery=lifecycle_recovery,
        question_create=question_create,
        approval_disposition_authority=approval_disposition_authority,
        question_feedback_authority=question_feedback_authority,
        conflict_inbox=conflict_inbox,
        conflict_concurrence=conflict_concurrence,
        approval_inbox=approval_inbox,
        approval_disposition=approval_disposition,
        approval_reassignment=approval_reassignment,
        review_inbox=review_inbox,
        backup_review_disposition=backup_review_disposition,
        reevaluation_disposition=reevaluation_disposition,
        review_recovery=review_recovery,
        operational_evidence=operational_evidence,
        operational_evidence_projector=operational_evidence_projector,
        policy_revision=policy_revision,
    )


def migrate_central_schema(
    config: CentralInstallationConfig,
    *,
    fault_injector: MigrationFaultInjector | None = None,
) -> None:
    """Migrate components first and write the Central marker last."""
    validate_central_installation_config(config)
    fault = fault_injector or _no_migration_fault
    try:
        SqliteProductionRegistryUsers.migrate_v2(config.database_path)
        SqliteProductionAgentCards.migrate(config.database_path)
        migrate_central_question_request_schema(config.database_path)
        migrate_central_question_lifecycle_schema(config.database_path)
        migrate_central_inbox_conflict_schema(config.database_path)
        migrate_central_inbox_approval_schema(config.database_path)
        migrate_central_inbox_review_schema(config.database_path)
        # Component migrations own their own failure atomicity.  This injector
        # models the Central marker boundary specifically, so a fault is never
        # allowed to stop before every component capability has read back.
        migrate_central_bootstrap_admin_schema(config.database_path)
        migrate_browser_auth_schema(config.database_path)
        # v20 is marker-last: mount and validate the durable PolicyRevision
        # component, then bootstrap epoch 1 from the configured strict YAML
        # before the installation marker is advanced.
        migrate_central_policy_revision_schema(config.database_path)
        if not _component_schemas_ready(config.database_path):
            raise CentralCompositionUnavailable()
        fault("before-central-marker")
        connection = sqlite3.connect(config.database_path)
        try:
            with connection:
                connection.execute(_MARKER_TABLE)
                _validate_marker_schema(connection)
                rows = tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT name,version FROM aon_installation_schema ORDER BY name"
                    )
                )
                if not rows:
                    connection.execute(
                        "INSERT INTO aon_installation_schema(name,version) VALUES (?,?)",
                        (CENTRAL_SCHEMA_NAME, 18),
                    )
                elif rows in {
                    ((CENTRAL_SCHEMA_NAME, version),) for version in range(2, 19)
                }:
                    connection.execute(
                        "UPDATE aon_installation_schema SET version=? WHERE name=?",
                        (18, CENTRAL_SCHEMA_NAME),
                    )
                elif rows not in {
                    ((CENTRAL_SCHEMA_NAME, 19),),
                    ((CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION),),
                }:
                    raise CentralCompositionUnavailable()
                # v18 is the last pre-operational-evidence marker.  Its
                # dedicated migration owns the v19 marker-last atomicity.
        finally:
            connection.close()
        migrate_central_operational_evidence_schema(config.database_path)
        try:
            bootstrap_snapshot = load_authority_policy_yaml(
                config.authority_snapshot_path.read_text(encoding="utf-8"),
                expected_org_id=config.org_id,
            )
            bootstrap_app = PolicyRevisionApplication(
                config.database_path,
                SqlitePolicyApprovalPort(config.database_path),
            )
            bootstrap_app.bootstrap(
                org_id=config.org_id,
                actor_user_id="central-bootstrap",
                document=bootstrap_snapshot.model_dump(mode="json"),
            )
        except Exception as error:
            raise CentralCompositionUnavailable() from error
        marker_connection = sqlite3.connect(config.database_path)
        try:
            marker_connection.execute(
                "UPDATE aon_installation_schema SET version=? WHERE name=? AND version=?",
                (CENTRAL_SCHEMA_VERSION, CENTRAL_SCHEMA_NAME, 19),
            )
            if not _marker_contents_ready(marker_connection):
                raise CentralCompositionUnavailable()
            marker_connection.commit()
        finally:
            marker_connection.close()
    except CentralInstallationConfigurationError:
        raise
    except Exception as error:
        raise CentralCompositionUnavailable() from error


def central_doctor(config: CentralInstallationConfig) -> bool:
    """Perform read-only config, component, policy, and bootstrap checks."""
    try:
        composition = compose_central(config)
        try:
            return composition.intake_ready()
        finally:
            composition.close()
    except (CentralInstallationConfigurationError, CentralCompositionUnavailable):
        return False


def bootstrap_central_admin(
    config: CentralInstallationConfig,
    *,
    attestation_path: Path,
    device_authorizer: BootstrapOidcDeviceAuthorizer | None = None,
) -> BootstrapAdminResult:
    """Run the non-browser, one-time bootstrap command after marker read-back."""
    validate_central_installation_config(config)
    if not _central_schema_ready(config.database_path):
        raise BootstrapAdminUnavailable()
    attestation = BootstrapAdminAttestation.load(attestation_path)
    try:
        snapshot = load_authority_policy_yaml(
            config.authority_snapshot_path.read_text(encoding="utf-8"),
            expected_org_id=config.org_id,
        )
        authority = SnapshotCentralAuthorizer(snapshot)
    except Exception as error:
        raise BootstrapAdminUnavailable() from error
    authorizer = device_authorizer
    if authorizer is None:
        try:
            oidc = HttpOidcProvider(
                config.oidc_issuer,
                config.oidc_audience,
                config.oidc_jwks_url,
            )
            authorizer = HttpBootstrapOidcDeviceAuthorizer(
                issuer=config.oidc_issuer,
                audience=config.oidc_audience,
                device_authorization_url=config.bootstrap_oidc_device_authorization_url,
                client_id=config.bootstrap_oidc_device_client_id,
                scope=config.bootstrap_oidc_scope,
                oidc_provider=oidc,
            )
        except Exception as error:
            raise BootstrapAdminUnavailable() from error
    seals = CentralBootstrapAdminSealStore(config.database_path)
    try:
        return BootstrapAdminApplication(
            config=BootstrapAdminConfig(
                org_id=config.org_id,
                oidc_provider_id=config.oidc_provider_id,
                oidc_issuer=config.oidc_issuer,
                oidc_audience=config.oidc_audience,
                authority_policy_digest=snapshot.content_sha256,
            ),
            authority=authority,
            device_authorizer=authorizer,
            registry_path=config.database_path,
            seals=seals,
        ).run(attestation)
    finally:
        seals.close()


def sqlite_schema_ready(database_path: Path, name: str, version: int) -> bool:
    """Read a schema marker without creating a database."""
    if (
        name != CENTRAL_SCHEMA_NAME
        or version != CENTRAL_SCHEMA_VERSION
        or not database_path.is_file()
    ):
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        return _marker_contents_ready(connection)
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _central_schema_ready(database_path: Path) -> bool:
    return sqlite_schema_ready(
        database_path, CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION
    ) and _component_schemas_ready(database_path) and central_bootstrap_admin_schema_ready(
        database_path
    ) and browser_auth_schema_ready(database_path) and central_operational_evidence_schema_ready(database_path)


def _normalized_sql(value: object) -> str:
    return " ".join(str(value or "").split())


def _marker_catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name='aon_installation_schema' "
            "OR tbl_name='aon_installation_schema' ORDER BY type,name"
        )
    )
    normalized_objects = tuple(
        (row[0], row[1], row[2], _normalized_sql(row[3])) for row in objects
    )
    table_info = tuple(
        tuple(row)
        for row in connection.execute("PRAGMA table_info(aon_installation_schema)")
    )
    foreign_keys = tuple(
        tuple(row)
        for row in connection.execute("PRAGMA foreign_key_list(aon_installation_schema)")
    )
    indexes = tuple(
        tuple(row)
        for row in connection.execute("PRAGMA index_list(aon_installation_schema)")
    )
    return normalized_objects, table_info, foreign_keys, indexes


def _canonical_marker_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_MARKER_TABLE)
        return _marker_catalog(connection)
    finally:
        connection.close()


_CANONICAL_MARKER_CATALOG = _canonical_marker_catalog()


def _validate_marker_schema(connection: sqlite3.Connection) -> None:
    if _marker_catalog(connection) != _CANONICAL_MARKER_CATALOG:
        raise CentralCompositionUnavailable()


def _marker_contents_ready(connection: sqlite3.Connection) -> bool:
    _validate_marker_schema(connection)
    rows = tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT name,version FROM aon_installation_schema ORDER BY name"
        )
    )
    return rows == ((CENTRAL_SCHEMA_NAME, CENTRAL_SCHEMA_VERSION),)


def _no_migration_fault(point: str) -> None:
    _ = point


def _component_schemas_ready(database_path: Path) -> bool:
    if not database_path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        validate_production_registry_user_connection(connection)
        validate_production_agent_card_connection(connection)
        return (
            central_question_request_schema_ready(database_path)
            and central_question_lifecycle_schema_ready(database_path)
            and central_inbox_conflict_schema_ready(database_path)
            and central_inbox_approval_schema_ready(database_path)
            and central_inbox_review_schema_ready(database_path)
            and browser_auth_schema_ready(database_path)
            and policy_revision_schema_ready(database_path)
        )
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _bootstrap_root_evidence_ready(
    database_path: Path, org_id: str, registry_user_id: str
) -> bool:
    """Read the sealed root's Registry evidence without constraining later users."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        root = validate_production_registry_user_rows(connection, org_id, registry_user_id)
        return root.manager_id is None
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


class _ReadOnlyRegistrationAuthorizer:
    def current(
        self,
        command: ProductionRegistryUserCommand,
        transaction: sqlite3.Connection,
    ) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        raise ProductionRegistryUserDenied()

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return False


class _ReceivedDeadlinePolicy:
    def deadline_for(
        self,
        org_id: str,
        state_kind: str,
        started_at: datetime,
    ) -> datetime:
        if not org_id or state_kind != "received":
            raise CentralCompositionUnavailable()
        return started_at + timedelta(minutes=5)


def _read_profile(profile_path: Path) -> dict[str, object]:
    if not profile_path.is_file():
        raise CentralInstallationConfigurationError("explicit Central profile path required")
    try:
        loaded: object = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CentralInstallationConfigurationError("invalid Central profile") from error
    if not isinstance(loaded, dict):
        raise CentralInstallationConfigurationError("invalid Central profile")
    result: dict[str, object] = {}
    for key, value in cast(dict[object, object], loaded).items():
        if type(key) is not str:
            raise CentralInstallationConfigurationError("invalid Central profile")
        result[key] = value
    return result


def _required_string(values: dict[str, object], name: str) -> str:
    value = values.get(name)
    if type(value) is not str or not value.strip():
        raise CentralInstallationConfigurationError("required Central setting missing")
    return value


def _required_int(values: dict[str, object], name: str) -> int:
    value = values.get(name)
    if type(value) is not int:
        raise CentralInstallationConfigurationError("required Central integer missing")
    return value


def _absolute_path(values: dict[str, object], name: str) -> Path:
    path = Path(_required_string(values, name))
    if not path.is_absolute():
        raise CentralInstallationConfigurationError("absolute Central path required")
    return path


def _is_absolute_path(value: object) -> bool:
    return isinstance(value, Path) and value.is_absolute()


def _exact_https_url(value: object) -> bool:
    if type(value) is not str or any(ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _valid_oidc_scope(value: object) -> bool:
    """Accept a bounded, space-delimited OIDC scope set from a trusted profile."""
    if type(value) is not str or not value or len(value) > 512:
        return False
    return all(
        bool(part)
        and all(character.isalnum() or character in "._:-/" for character in part)
        for part in value.split(" ")
    )


def _valid_browser_oidc_client_id(value: object) -> bool:
    """Browser public-client identifiers are opaque IdP values, not references."""
    return (
        type(value) is str
        and bool(value)
        and len(value) <= 512
        and not any(ord(character) < 32 for character in value)
    )


def _exact_https_origin(value: object) -> bool:
    if not _exact_https_url(value):
        return False
    parsed = urlsplit(cast(str, value))
    return not parsed.path


def central_browser_oidc_redirect_uri(config: CentralInstallationConfig) -> str:
    """Return the only Central browser OIDC redirect URI after strict validation."""
    validate_central_installation_config(config)
    try:
        return browser_redirect_uri(config.central_public_origin)
    except ValueError as error:
        raise CentralInstallationConfigurationError("invalid Central browser redirect") from error


def _same_https_origin(first: str, second: str) -> bool:
    try:
        first_parts = urlsplit(first)
        second_parts = urlsplit(second)
        return (
            first_parts.scheme == second_parts.scheme == "https"
            and first_parts.hostname == second_parts.hostname
            and first_parts.port == second_parts.port
        )
    except ValueError:
        return False
