"""RB3 P0 installation and transport boundary contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from agent_org_network.installation_contracts import (
    CARD_OWNER_ALLOWED_PROCESSES,
    CARD_OWNER_IMPORT_ALLOWLIST,
    CARD_OWNER_SURFACE_ALLOWLIST,
    CENTRAL_SERVER_ALLOWED_PROCESSES,
    CENTRAL_SERVER_IMPORT_ALLOWLIST,
    CENTRAL_SERVER_SURFACE_ALLOWLIST,
    QUESTION_USER_IMPORT_ALLOWLIST,
    QUESTION_USER_MCP_TOOL_MANIFEST,
    QUESTION_USER_SURFACE_ALLOWLIST,
    ArtifactManifest,
    CentralOwnerControlEnvelope,
    InstallationKind,
    ListenerBind,
    ListenerPort,
    ListenerScope,
    RunnableLocalReferencePromotionEvidence,
    VersionedPublicRouteNamespace,
)


def _envelope() -> dict[str, object]:
    return {
        "action": "publish",
        "org_id": "org-1",
        "owner_id": "owner-1",
        "agent_card_id": "support-card",
        "card_revision": 7,
        "card_digest": "a" * 64,
        "credential_ref": "keychain:owner-credential-1",
        "idempotency_key": "request-00000001",
        "expected_revision": 12,
    }


def test_installation_kind_is_the_exact_three_artifact_sealed_set() -> None:
    assert set(InstallationKind) == {
        InstallationKind.CENTRAL_SERVER,
        InstallationKind.CARD_OWNER,
        InstallationKind.QUESTION_USER,
    }
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate({"installation": "worker"})


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.8", "owner.example.test"])
def test_card_owner_listener_rejects_wildcard_and_non_loopback_bind(host: str) -> None:
    with pytest.raises(ValidationError):
        ListenerBind(host=host, installation=InstallationKind.CARD_OWNER)


def test_listener_contract_distinguishes_private_loopback_from_public_namespace() -> None:
    owner = ListenerBind(host="127.0.0.1", installation=InstallationKind.CARD_OWNER)
    assert owner.scope is ListenerScope.PRIVATE
    assert ListenerPort(value=8012).value == 8012

    public = VersionedPublicRouteNamespace(version="v1", path="/v1/questions")
    assert public.path == "/v1/questions"
    assert public.allows("/v1/questions/request-1")
    assert not public.allows("/v1/questions-admin")
    with pytest.raises(ValidationError):
        VersionedPublicRouteNamespace(version="1", path="/v1/questions")
    with pytest.raises(ValidationError):
        VersionedPublicRouteNamespace(version="v1", path="/questions")


def test_control_envelope_is_frozen_and_carries_only_control_references() -> None:
    envelope = CentralOwnerControlEnvelope.model_validate(_envelope())
    assert envelope.owner_id == "owner-1"
    assert envelope.agent_card_id == "support-card"
    assert envelope.credential_ref == "keychain:owner-credential-1"
    assert envelope.card_digest == "a" * 64
    with pytest.raises((ValidationError, FrozenInstanceError)):
        envelope.expected_revision = 13  # type: ignore[misc]

    with pytest.raises(ValidationError):
        CentralOwnerControlEnvelope.model_validate(_envelope() | {"action": "migrate"})
    for missing_binding in ("owner_id", "agent_card_id", "card_revision", "card_digest"):
        payload = _envelope()
        del payload[missing_binding]
        with pytest.raises(ValidationError):
            CentralOwnerControlEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "raw_source",
        "full_draft",
        "token",
        "password",
        "secret",
        "source_content",
        "upstream",
    ],
)
def test_control_envelope_fail_closes_raw_draft_secret_and_caller_chosen_upstream(
    forbidden_field: str,
) -> None:
    payload = _envelope() | {forbidden_field: "must-not-cross-the-boundary"}
    with pytest.raises(ValidationError):
        CentralOwnerControlEnvelope.model_validate(payload)


def test_artifact_manifest_allows_only_its_own_processes_routes_tools_and_imports() -> None:
    central = ArtifactManifest(
        installation=InstallationKind.CENTRAL_SERVER,
        processes=CENTRAL_SERVER_ALLOWED_PROCESSES,
        routes=(),
        tools=(),
        imports=CENTRAL_SERVER_IMPORT_ALLOWLIST,
        surfaces=CENTRAL_SERVER_SURFACE_ALLOWLIST,
    )
    owner = ArtifactManifest(
        installation=InstallationKind.CARD_OWNER,
        processes=CARD_OWNER_ALLOWED_PROCESSES,
        routes=(),
        tools=(),
        imports=CARD_OWNER_IMPORT_ALLOWLIST,
        surfaces=CARD_OWNER_SURFACE_ALLOWLIST,
    )
    question_user = ArtifactManifest(
        installation=InstallationKind.QUESTION_USER,
        processes=("question-user-mcp",),
        routes=(),
        tools=QUESTION_USER_MCP_TOOL_MANIFEST,
        imports=QUESTION_USER_IMPORT_ALLOWLIST,
        surfaces=QUESTION_USER_SURFACE_ALLOWLIST,
    )

    assert central.installation is InstallationKind.CENTRAL_SERVER
    assert owner.installation is InstallationKind.CARD_OWNER
    assert question_user.tools == ("ask_org", "get_question")


@pytest.mark.parametrize(
    ("installation", "field", "value"),
    [
        (InstallationKind.CENTRAL_SERVER, "surfaces", ("GET /workspace",)),
        (InstallationKind.CENTRAL_SERVER, "surfaces", ("GET /raw",)),
        (
            InstallationKind.CENTRAL_SERVER,
            "imports",
            (
                "agent_org_network.central_api",
                "agent_org_network.central_cli",
                "agent_org_network.central_composition",
                "agent_org_network.owner_api",
            ),
        ),
        (InstallationKind.CARD_OWNER, "surfaces", ("GET /authority",)),
        (InstallationKind.CARD_OWNER, "processes", ("central-migrate",)),
        (
            InstallationKind.CARD_OWNER,
            "imports",
            (
                "agent_org_network.owner_api",
                "agent_org_network.owner_cli",
                "agent_org_network.owner_composition",
                "agent_org_network.central_composition",
            ),
        ),
        (
            InstallationKind.QUESTION_USER,
            "imports",
            (
                "agent_org_network.question_user_mcp",
                "agent_org_network.admin_users",
            ),
        ),
        (InstallationKind.QUESTION_USER, "tools", ("ask_org", "approve_answer")),
        (InstallationKind.QUESTION_USER, "tools", ("ask_org",)),
        (InstallationKind.CENTRAL_SERVER, "surfaces", ("GET /readyz-unsafe",)),
    ],
)
def test_artifact_manifest_fail_closes_cross_installation_and_mcp_surface(
    installation: InstallationKind, field: str, value: tuple[str, ...]
) -> None:
    manifest: dict[str, object] = {
        "installation": installation,
        "processes": {
            InstallationKind.CENTRAL_SERVER: CENTRAL_SERVER_ALLOWED_PROCESSES,
            InstallationKind.CARD_OWNER: CARD_OWNER_ALLOWED_PROCESSES,
            InstallationKind.QUESTION_USER: ("question-user-mcp",),
        }[installation],
        "routes": (),
        "tools": {
            InstallationKind.CENTRAL_SERVER: (),
            InstallationKind.CARD_OWNER: (),
            InstallationKind.QUESTION_USER: QUESTION_USER_MCP_TOOL_MANIFEST,
        }[installation],
        "imports": {
            InstallationKind.CENTRAL_SERVER: CENTRAL_SERVER_IMPORT_ALLOWLIST,
            InstallationKind.CARD_OWNER: CARD_OWNER_IMPORT_ALLOWLIST,
            InstallationKind.QUESTION_USER: QUESTION_USER_IMPORT_ALLOWLIST,
        }[installation],
        "surfaces": {
            InstallationKind.CENTRAL_SERVER: CENTRAL_SERVER_SURFACE_ALLOWLIST,
            InstallationKind.CARD_OWNER: CARD_OWNER_SURFACE_ALLOWLIST,
            InstallationKind.QUESTION_USER: QUESTION_USER_SURFACE_ALLOWLIST,
        }[installation],
    }
    manifest[field] = value
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(manifest)


def test_runnable_local_reference_promotion_evidence_requires_all_three_installations() -> None:
    evidence = RunnableLocalReferencePromotionEvidence(
        clean_install=True,
        doctor_passed=True,
        separate_processes=True,
        test_oidc_issuer=True,
        loopback_tls_ca=True,
        test_keychain=True,
        card_owner_pairing=True,
        published_revision_receipt=True,
        question_user_mcp=True,
        raw_and_secret_non_egress=True,
    )
    assert evidence.candidate_support_level == "runnable_local_reference"
    assert evidence.is_complete is True

    with pytest.raises(ValidationError):
        RunnableLocalReferencePromotionEvidence(
            clean_install=True,
            doctor_passed=True,
            separate_processes=False,
            test_oidc_issuer=True,
            loopback_tls_ca=True,
            test_keychain=True,
            card_owner_pairing=True,
            published_revision_receipt=True,
            question_user_mcp=True,
            raw_and_secret_non_egress=True,
        )
