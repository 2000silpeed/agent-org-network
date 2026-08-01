"""Current-policy authority contracts for Central browser sessions."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from agent_org_network.central_authority import (
    AUTHORITY_ACTION_MANIFEST,
    AuthenticatedPrincipal,
    BrowserSessionAuthorityAllowed,
    BrowserSessionAuthorityDenied,
    BrowserSessionAuthorityUnavailable,
    FileReloadingBrowserSessionAuthority,
    ResourceRef,
    canonical_policy_digest,
)
from agent_org_network.central_browser_oidc import FakeOidcAuthorizationCodeExchange
from agent_org_network.central_composition import (
    FileReloadingBrowserSessionAuthority as ComposedBrowserSessionAuthority,
    compose_central,
    load_central_installation_config,
    migrate_central_schema,
)


def _policy(*, actions: list[str], org_id: str = "acme") -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "org_id": org_id,
        "policy_version": "v1",
        "content_sha256": "pending",
        "subject_roles": [{"org_id": org_id, "subject_id": "root", "roles": ["admin"]}],
        "role_permissions": [{"role": "admin", "actions": actions}],
        "route_rules": [],
        "worker_bindings": [],
    }
    value["content_sha256"] = canonical_policy_digest(value)
    return value


def _write_policy(path: Path, value: dict[str, object]) -> None:
    pending = path.with_suffix(".pending")
    pending.write_text(yaml.safe_dump(value), encoding="utf-8")
    pending.replace(path)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="root", identity_provider="company", identity_session_id="a" * 64
    )


def _resource() -> ResourceRef:
    return ResourceRef(org_id="acme", kind="browser_session", resource_id="b" * 64)


def test_browser_authority_reloads_policy_for_each_establish_and_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "authority.yaml"
    _write_policy(path, _policy(actions=["session.establish", "session.read"]))
    authority = FileReloadingBrowserSessionAuthority(path, expected_org_id="acme")
    assert type(authority.authorize(_principal(), "session.establish", _resource())) is BrowserSessionAuthorityAllowed
    _write_policy(path, _policy(actions=["session.read"]))
    assert type(authority.authorize(_principal(), "session.establish", _resource())) is BrowserSessionAuthorityDenied
    assert type(authority.authorize(_principal(), "session.read", _resource())) is BrowserSessionAuthorityAllowed
    path.write_text("not: [valid", encoding="utf-8")
    assert type(authority.authorize(_principal(), "session.read", _resource())) is BrowserSessionAuthorityUnavailable
    path.unlink()
    assert type(authority.authorize(_principal(), "session.read", _resource())) is BrowserSessionAuthorityUnavailable
    _write_policy(path, _policy(actions=["session.establish"], org_id="other"))
    assert type(authority.authorize(_principal(), "session.establish", _resource())) is BrowserSessionAuthorityUnavailable


def test_browser_session_actions_are_in_manifest_and_composition_uses_reloader(tmp_path: Path) -> None:
    assert {"session.establish", "session.read"} <= AUTHORITY_ACTION_MANIFEST
    policy_path = tmp_path / "authority.yaml"
    _write_policy(policy_path, _policy(actions=["session.establish", "session.read"]))
    profile = tmp_path / "central.json"
    profile.write_text(json.dumps({
        "profile": "local-reference", "org_id": "acme", "oidc_provider_id": "company",
        "oidc_issuer": "https://idp.example.test", "oidc_audience": "api-client",
        "oidc_jwks_url": "https://idp.example.test/jwks",
        "bootstrap_oidc_device_authorization_url": "https://idp.example.test/device",
        "bootstrap_oidc_device_client_id": "bootstrap-client", "bootstrap_oidc_scope": "openid email",
        "central_public_origin": "https://central.example.test",
        "browser_oidc_authorization_url": "https://idp.example.test/authorize",
        "browser_oidc_token_url": "https://idp.example.test/token", "browser_oidc_client_id": "browser-client",
        "browser_oidc_scope": "openid email", "authority_snapshot_path": str(policy_path),
        "database_path": str(tmp_path / "central.sqlite3"), "data_directory": str(tmp_path / "data"),
        "bind_host": "127.0.0.1", "port": 8010,
    }), encoding="utf-8")
    config = load_central_installation_config(profile)
    migrate_central_schema(config)
    composition = compose_central(
        config,
        browser_code_exchange=FakeOidcAuthorizationCodeExchange(
            issuer=config.oidc_issuer, audience=config.browser_oidc_client_id
        ),
    )
    try:
        assert composition.browser_oidc is not None
        assert type(composition.browser_oidc._authority) is ComposedBrowserSessionAuthority  # pyright: ignore[reportPrivateUsage]
    finally:
        composition.close()
