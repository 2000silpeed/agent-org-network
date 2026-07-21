"""R5 credential MCP closed-surface regressions."""

from __future__ import annotations

import asyncio
import json
import runpy
import sqlite3
from threading import Barrier, Thread
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, cast

import pytest

import agent_org_network.credential_mcp as credential_mcp
from agent_org_network.credential_mcp import (
    CREDENTIAL_MCP_ACTION_BINDINGS,
    CREDENTIAL_MCP_TOOL_ACTIONS,
    CredentialMcpActionBinding,
    create_credential_mcp_capability,
    create_durable_credential_mcp_server,
    create_enabled_credential_mcp_server,
    create_sealed_credential_mcp_server,
)
from agent_org_network.credential_issue_scoped_operations import (
    create_credential_issue_scoped_operations_capability,
)
from agent_org_network.credential_issue_scoped_orchestration import (
    create_credential_issue_cleanup_readiness_capability,
    create_scoped_credential_issue_bridge_capability,
)
from agent_org_network.credential_scoped_read import (
    create_credential_scoped_read,
    create_credential_scoped_read_capability,
)
from agent_org_network.credential_scoped_revoke import create_credential_scoped_revoke_capability


def _tools(server: object) -> list[Any]:
    return cast(list[Any], asyncio.run(cast(Any, server).list_tools()))


def _call(server: object, name: str, arguments: dict[str, object]) -> dict[str, object]:
    content, structured = asyncio.run(
        cast(Any, server).call_tool(name, {"request": json.dumps(arguments)})
    )
    assert structured in (None, {"result": content[0].text})
    assert len(content) == 1
    return cast(dict[str, object], json.loads(content[0].text))


def _assert_public(value: object) -> None:
    """MCP text and structured result must not disclose internal proof material."""
    encoded = json.dumps(value, sort_keys=True).lower()
    for forbidden in ("secret", "grant", "evidence", "source", "delivery", "principal", "org_id"):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    "arguments",
    ({}, {"request": 7}, {"request": "not-json-secret-value"}, {"request": "{}"}),
)
def test_malformed_mcp_requests_are_generic_and_never_reflect_values(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    server, _provider, _delivery, _read_source, _issue_source = _enabled(
        tmp_path / "malformed.sqlite"
    )
    content, structured = asyncio.run(server.call_tool("get_worker_credential", arguments))
    rendered = "".join(item.text for item in content) + json.dumps(structured, sort_keys=True)
    assert json.loads(content[0].text) == {"status": "unavailable"}
    assert "secret-value" not in rendered
    _assert_public({"text": content[0].text, "structured": structured})


class _ExplodingInput:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"closed credential factory read {name}")


def test_action_matrix_is_exact_and_immutable() -> None:
    assert CREDENTIAL_MCP_ACTION_BINDINGS == (
        CredentialMcpActionBinding("issue_worker_credential", "worker_credential.issue"),
        CredentialMcpActionBinding("list_worker_credentials", "worker_credential.read"),
        CredentialMcpActionBinding("get_worker_credential", "worker_credential.read"),
        CredentialMcpActionBinding("revoke_worker_credential", "worker_credential.revoke"),
    )
    with pytest.raises(TypeError):
        CREDENTIAL_MCP_TOOL_ACTIONS["issue_worker_credential"] = "worker_credential.read"  # type: ignore[index]


@pytest.mark.parametrize(
    "factory",
    (create_durable_credential_mcp_server, create_sealed_credential_mcp_server),
)
def test_public_factories_are_fresh_input_independent_zero_tool_surfaces(factory: object) -> None:
    candidate = cast(Any, factory)
    first = candidate(_ExplodingInput(), counterfeit=_ExplodingInput())
    second = candidate(_ExplodingInput())
    assert first is not second
    assert _tools(first) == []
    assert _tools(second) == []


_ISSUE = runpy.run_path("tests/test_credential_issue_scoped_orchestration.py")
_READ = runpy.run_path("tests/test_credential_scoped_read.py")
_REVOKE = runpy.run_path("tests/test_credential_scoped_revoke.py")


def _capability_parts(path: Path) -> tuple[Any, Any, Any, Any, Any, Any]:
    """Compose the production capabilities against R5.2/R5.4 conformance fixtures."""
    _ISSUE["_r4_r51_r52a_path"](path)
    source, principal, evidence, issuer, delivery, secrets = (
        _ISSUE["_ScopeSource"](),
        _ISSUE["_PrincipalResolver"](),
        _ISSUE["_EvidenceResolver"](),
        _ISSUE["_ApprovalProvider"](),
        _ISSUE["_Delivery"](),
        _ISSUE["_SecretFactory"](),
    )
    server_principal = _ISSUE["_principal"]()
    issue = create_scoped_credential_issue_bridge_capability(
        path=path,
        scoped_capability=create_credential_issue_scoped_operations_capability(
            binding_source=source,
            principal_resolver=principal,
            server_principal=server_principal,
            central_authorizer=_ISSUE["_authorizer"](),
            approval_resolver=evidence,
            approval_provider=issuer,
            delivery=delivery,
        ),
        delivery=delivery,
        secret_factory=secrets,
        cleanup_readiness=create_credential_issue_cleanup_readiness_capability(path),
    )
    read_source = _READ["_ReadSource"]()
    read = create_credential_scoped_read_capability(
        server_principal=server_principal,
        central_authorizer=_READ["_authorizer"](),
        reader_source=read_source,
    )
    revoke_source, provider = _REVOKE["_Source"](), _REVOKE["_Provider"]()
    resolver = _REVOKE["_Resolver"](provider)
    revoke = create_credential_scoped_revoke_capability(
        server_principal=server_principal,
        central_authorizer=_REVOKE["_authority"](),
        reader_source=revoke_source,
        approval_provider=provider,
        approval_resolver=resolver,
        server_clock=lambda: datetime(2026, 7, 20, tzinfo=UTC),
    )
    capability = create_credential_mcp_capability(
        path=path,
        org_id="org",
        server_principal=server_principal,
        issue_capability=issue,
        read_capability=read,
        revoke_capability=revoke,
        server_clock=lambda: datetime(2026, 7, 19, tzinfo=UTC),
    )
    return capability, read, issue, revoke, provider, delivery


def _enabled(path: Path) -> tuple[Any, Any, Any, Any, Any]:
    capability, _read, _issue, _revoke, provider, delivery = _capability_parts(path)
    # These two fixture sources are only returned for deliberate drift tests.
    read_source = capability._read._read._reader_source  # pyright: ignore[reportPrivateUsage]
    issue_source = capability._issue._bridge._scoped_operations._guard.binding_source  # pyright: ignore[reportPrivateUsage]
    return (
        create_enabled_credential_mcp_server(capability),
        provider,
        delivery,
        read_source,
        issue_source,
    )


def test_enabled_surface_is_exact_schema_safe_and_runs_r52_r54_flow(tmp_path: Path) -> None:
    server, provider, delivery, source, _issue_source = _enabled(tmp_path / "credential-mcp.sqlite")
    tools = _tools(server)
    assert [tool.name for tool in tools] == list(CREDENTIAL_MCP_TOOL_ACTIONS)
    forbidden = {"org", "principal", "source", "evidence", "grant", "clock", "secret", "delivery"}
    for tool in tools:
        properties = tool.inputSchema.get("properties", {})
        assert not (set(properties) & forbidden)
    issued = _call(
        server,
        "issue_worker_credential",
        {
            "target_id": "target",
            "credential_id": "credential",
            "agent_card_id": "card",
            "owner_subject_id": "owner",
            "role": "role",
            "request_id": "target",
            "attempt": 1,
        },
    )
    _assert_public(issued)
    assert issued == {"credential_id": "credential", "status": "issued"}
    assert delivery.refs and all("delivery" not in json.dumps(value) for value in (issued,))
    assert (
        _call(
            server,
            "issue_worker_credential",
            {
                "target_id": "target",
                "credential_id": "credential",
                "agent_card_id": "card",
                "owner_subject_id": "owner",
                "role": "role",
                "request_id": "target",
                "attempt": 1,
            },
        )
        == issued
    )
    listed = _call(server, "list_worker_credentials", {})
    assert listed["status"] == "ok" and listed["credentials"]
    _assert_public(listed)
    assert _call(server, "get_worker_credential", {"credential_id": "absent"}) == {
        "status": "not_found_or_denied"
    }
    assert _call(server, "get_worker_credential", {"credential_id": "credential"})["status"] == "ok"
    revoked = _call(
        server,
        "revoke_worker_credential",
        {
            "credential_id": "credential",
            "command_id": "receipt",
            "attempt": 1,
            "expected_generation": 1,
            "expected_revision": 1,
        },
    )
    assert revoked["status"] == "ok"
    assert (
        _call(
            server,
            "revoke_worker_credential",
            {
                "credential_id": "credential",
                "command_id": "receipt",
                "attempt": 1,
                "expected_generation": 1,
                "expected_revision": 1,
            },
        )
        == revoked
    )
    assert provider.calls == 1
    source.available = False
    assert _call(server, "list_worker_credentials", {}) == {"status": "unavailable"}


def test_catalog_drift_closes_list_without_partial_view(tmp_path: Path) -> None:
    path = tmp_path / "credential-mcp-catalog.sqlite"
    server, _provider, _delivery, _read_source, _issue_source = _enabled(path)
    assert (
        _call(
            server,
            "issue_worker_credential",
            {
                "target_id": "target",
                "credential_id": "credential",
                "agent_card_id": "card",
                "owner_subject_id": "owner",
                "role": "role",
                "request_id": "target",
                "attempt": 1,
            },
        )["status"]
        == "issued"
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "unprojected",
                "org",
                "owner",
                "role",
                1,
                1,
                "active",
                "h" * 64,
                "2026-07-19T00:00:00.000Z",
                None,
                None,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    # No subset is returned when a catalog credential lacks its immutable projection.
    assert _call(server, "list_worker_credentials", {}) == {"status": "unavailable"}


def test_issue_pending_and_closed_paths_never_disclose_ref_or_write(tmp_path: Path) -> None:
    path = tmp_path / "credential-mcp-pending.sqlite"
    server, _provider, delivery, _read_source, _issue_source = _enabled(path)
    delivery.release_error = True
    pending = _call(
        server,
        "issue_worker_credential",
        {
            "target_id": "target",
            "credential_id": "credential",
            "agent_card_id": "card",
            "owner_subject_id": "owner",
            "role": "role",
            "request_id": "target",
            "attempt": 1,
        },
    )
    assert pending == {"credential_id": "credential", "status": "release_pending"}
    _assert_public(pending)

    closed_path = tmp_path / "credential-mcp-closed.sqlite"
    closed, _provider, _delivery, _read_source, issue_source = _enabled(closed_path)
    # Current source failure occurs before the R5.2 reservation write.
    issue_source.available = False
    unavailable = _call(
        closed,
        "issue_worker_credential",
        {
            "target_id": "target",
            "credential_id": "credential",
            "agent_card_id": "card",
            "owner_subject_id": "owner",
            "role": "role",
            "request_id": "target",
            "attempt": 1,
        },
    )
    assert unavailable == {"status": "unavailable"}
    _assert_public(unavailable)
    connection = sqlite3.connect(closed_path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_credentials").fetchone() == (0,)
    finally:
        connection.close()


def test_action_matrix_tamper_closes_handler_and_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server, _provider, _delivery, _read_source, _issue_source = _enabled(
        tmp_path / "matrix-live.sqlite"
    )
    monkeypatch.setattr(
        credential_mcp,
        "CREDENTIAL_MCP_TOOL_ACTIONS",
        MappingProxyType({"issue_worker_credential": "worker_credential.read"}),
    )
    # The handler re-checks the exact immutable lookup; it does not trust its decoration.
    assert _call(
        server,
        "issue_worker_credential",
        {
            "target_id": "target",
            "credential_id": "credential",
            "agent_card_id": "card",
            "owner_subject_id": "owner",
            "role": "role",
            "request_id": "target",
            "attempt": 1,
        },
    ) == {"status": "unavailable"}
    # A missing entry also fails before a replacement server can expose tools.
    with pytest.raises(RuntimeError):
        _enabled(tmp_path / "matrix-new.sqlite")


def test_direct_read_claim_race_never_partially_burns_aggregate(tmp_path: Path) -> None:
    capability, read, issue, revoke, _provider, _delivery = _capability_parts(
        tmp_path / "claim-race.sqlite"
    )
    gate = Barrier(2)
    outcomes: list[tuple[str, object]] = []

    def direct() -> None:
        gate.wait()
        try:
            outcomes.append(("read", create_credential_scoped_read(read)))
        except Exception as error:
            outcomes.append(("read_error", error))

    def enabled() -> None:
        gate.wait()
        try:
            outcomes.append(("enabled", create_enabled_credential_mcp_server(capability)))
        except Exception as error:
            outcomes.append(("enabled_error", error))

    threads = (Thread(target=direct), Thread(target=enabled))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    kinds = {kind for kind, _value in outcomes}
    if "enabled" in kinds:
        server = next(value for kind, value in outcomes if kind == "enabled")
        assert len(_tools(server)) == 4
        assert issue._claimed and read._claimed and revoke._used  # pyright: ignore[reportPrivateUsage]
        assert "read_error" in kinds
    else:
        assert kinds == {"read", "enabled_error"}
        assert read._claimed  # pyright: ignore[reportPrivateUsage]
        assert not issue._claimed and not revoke._used  # pyright: ignore[reportPrivateUsage]


def test_registration_failure_does_not_claim_any_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capability, read, issue, revoke, _provider, _delivery = _capability_parts(
        tmp_path / "register-fail.sqlite"
    )

    class ExplodingFastMcp:
        def __init__(self, _name: str) -> None:
            self.calls = 0

        def tool(self, **_kwargs: object) -> Callable[[Any], Any]:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("decorator failure")
            def decorate(function: Any) -> Any:
                return function
            return decorate

    monkeypatch.setattr(credential_mcp, "FastMCP", ExplodingFastMcp)
    with pytest.raises(RuntimeError, match="decorator failure"):
        create_enabled_credential_mcp_server(capability)
    assert not issue._claimed and not read._claimed and not revoke._used  # pyright: ignore[reportPrivateUsage]


def test_constructor_failure_does_not_claim_any_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capability, read, issue, revoke, _provider, _delivery = _capability_parts(
        tmp_path / "constructor-fail.sqlite"
    )

    def fail_constructor(_name: str) -> object:
        raise RuntimeError("constructor failure")

    monkeypatch.setattr(credential_mcp, "FastMCP", fail_constructor)
    with pytest.raises(RuntimeError, match="constructor failure"):
        create_enabled_credential_mcp_server(capability)
    assert not capability._claimed and not issue._claimed and not read._claimed and not revoke._used  # pyright: ignore[reportPrivateUsage]


def test_mid_registration_matrix_drift_does_not_claim_any_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capability, read, issue, revoke, _provider, _delivery = _capability_parts(
        tmp_path / "matrix-drift.sqlite"
    )

    class DriftingFastMcp:
        def __init__(self, _name: str) -> None:
            self.calls = 0

        def tool(self, **_kwargs: object) -> Callable[[Any], Any]:
            self.calls += 1
            if self.calls == 2:
                monkeypatch.setattr(
                    credential_mcp, "CREDENTIAL_MCP_TOOL_ACTIONS", MappingProxyType[str, str]({})
                )
            def decorate(function: Any) -> Any:
                return function
            return decorate

    monkeypatch.setattr(credential_mcp, "FastMCP", DriftingFastMcp)
    with pytest.raises(RuntimeError, match="매트릭스"):
        create_enabled_credential_mcp_server(capability)
    assert not capability._claimed and not issue._claimed and not read._claimed and not revoke._used  # pyright: ignore[reportPrivateUsage]
