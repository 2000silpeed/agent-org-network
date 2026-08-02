"""Fast deterministic contract for the installation-entrypoint first slice."""

from __future__ import annotations

import ast
from hashlib import sha256
import io
import json
from pathlib import Path
import tomllib
from typing import Any, Mapping, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from starlette.routing import Route
import yaml

from agent_org_network.central_authority import canonical_policy_digest
from agent_org_network.central_bootstrap_admin import VerifiedBootstrapIdentity
from agent_org_network.central_api import (
    create_central_api_app,
    run_central_api,
)
from agent_org_network.central_cli import (
    EXIT_CONFIGURATION as CENTRAL_CONFIGURATION,
    EXIT_UNAVAILABLE as CENTRAL_UNAVAILABLE,
    build_parser as central_parser,
    main as central_main,
)
import agent_org_network.central_cli as central_cli_module
from agent_org_network.central_web_runtime import CentralWebRuntimeUnavailable
from agent_org_network.central_composition import (
    CentralComposition,
    CentralInstallationConfig,
    CentralProductionUnavailable,
    central_doctor,
    compose_central,
    load_central_installation_config,
    migrate_central_schema,
)
from agent_org_network.owner_api import create_owner_api_app
from agent_org_network.owner_cli import (
    EXIT_CONFIGURATION as OWNER_CONFIGURATION,
    EXIT_UNAVAILABLE as OWNER_UNAVAILABLE,
    build_parser as owner_parser,
    main as owner_main,
)
from agent_org_network.owner_composition import (
    compose_owner,
    load_owner_installation_config,
    owner_doctor,
)
from agent_org_network.installation_contracts import (
    CARD_OWNER_ALLOWED_PROCESSES,
    CARD_OWNER_IMPORT_ALLOWLIST,
    CARD_OWNER_SURFACE_ALLOWLIST,
    CENTRAL_SERVER_ALLOWED_PROCESSES,
    CENTRAL_SERVER_IMPORT_ALLOWLIST,
    CENTRAL_SERVER_SURFACE_ALLOWLIST,
    QUESTION_USER_ALLOWED_PROCESSES,
    QUESTION_USER_IMPORT_ALLOWLIST,
    QUESTION_USER_MCP_TOOL_MANIFEST,
    ArtifactManifest,
    InstallationKind,
)


def _write(path: Path, values: Mapping[str, object]) -> Path:
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def _central_profile(tmp_path: Path, **overrides: object) -> Path:
    authority: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "v1",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "root", "roles": ["requester", "admin"]}
        ],
        "role_permissions": [
            {
                "role": "requester",
                "actions": ["question.create", "question.read"],
            },
            {"role": "admin", "actions": ["user.register"]},
        ],
        "route_rules": [],
        "worker_bindings": [],
    }
    authority["content_sha256"] = canonical_policy_digest(authority)
    authority_path = tmp_path / "authority.yaml"
    authority_path.write_text(yaml.safe_dump(authority), encoding="utf-8")
    data: dict[str, object] = {
        "profile": "local-reference",
        "org_id": "acme",
        "oidc_provider_id": "company-oidc",
        "oidc_issuer": "https://idp.example.test",
        "oidc_audience": "aon-central",
        "oidc_jwks_url": "https://idp.example.test/jwks",
        "bootstrap_oidc_device_authorization_url": "https://idp.example.test/device_authorization",
        "bootstrap_oidc_device_client_id": "aon-central-bootstrap",
        "bootstrap_oidc_scope": "openid email",
        "central_public_origin": "https://central.example.test",
        "browser_oidc_authorization_url": "https://idp.example.test/authorize",
        "browser_oidc_token_url": "https://idp.example.test/token",
        "browser_oidc_client_id": "aon-central-browser",
        "browser_oidc_scope": "openid email",
        "authority_snapshot_path": str(authority_path),
        "database_path": str(tmp_path / "central.sqlite3"),
        "data_directory": str(tmp_path / "central-data"),
        "bind_host": "127.0.0.1",
        "port": 8010,
    }
    data.update(overrides)
    return _write(tmp_path / "central.json", data)


def _owner_profile(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "profile": "local-reference",
        "database_path": str(tmp_path / "owner.sqlite3"),
        "workspace_directory": str(tmp_path / "owner-workspace"),
        "bind_host": "127.0.0.1",
        "port": 8012,
    }
    data.update(overrides)
    return _write(tmp_path / "owner.json", data)


def _get(client: TestClient, path: str) -> Response:
    http: Any = client
    return cast(Response, http.get(path))


def _route_paths(app: FastAPI) -> set[str]:
    routes = [route for route in app.routes if isinstance(route, Route)]
    assert len(routes) == len(app.routes)
    return {route.path for route in routes}


def test_console_scripts_and_exact_parser_shape() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {
        "aon-mcp": "agent_org_network.question_user_mcp:main",
        "aon-central": "agent_org_network.central_cli:main",
        "aon-owner": "agent_org_network.owner_cli:main",
    }

    assert central_parser().parse_args(["migrate", "--profile", "/tmp/c.json"]).command == "migrate"
    assert central_parser().parse_args(["doctor", "--profile", "/tmp/c.json"]).command == "doctor"
    assert central_parser().parse_args(["api", "serve", "--profile", "/tmp/c.json"]).api_command == "serve"
    assert central_parser().parse_args(["web", "serve", "--profile", "/tmp/c.json"]).web_command == "serve"
    assert owner_parser().parse_args(["pair", "--profile", "/tmp/o.json"]).command == "pair"
    assert owner_parser().parse_args(["unpair", "--profile", "/tmp/o.json"]).command == "unpair"
    assert owner_parser().parse_args(["doctor", "--profile", "/tmp/o.json"]).command == "doctor"
    assert owner_parser().parse_args(["api", "serve", "--profile", "/tmp/o.json"]).api_command == "serve"
    assert owner_parser().parse_args(["workspace", "serve", "--profile", "/tmp/o.json"]).workspace_command == "serve"
    assert owner_parser().parse_args(["worker", "--profile", "/tmp/o.json"]).command == "worker"
    with pytest.raises(SystemExit, match="2"):
        central_parser().parse_args(["migrate"])
    with pytest.raises(SystemExit, match="2"):
        owner_parser().parse_args(["api", "serve"])


def test_central_migration_doctor_and_api_runner_seam(tmp_path: Path) -> None:
    profile = _central_profile(tmp_path)
    config = load_central_installation_config(profile)
    assert not central_doctor(config)
    assert central_main(["migrate", "--profile", str(profile)]) == 0
    assert central_main(["doctor", "--profile", str(profile)]) == CENTRAL_UNAVAILABLE
    (tmp_path / "central-data").mkdir()
    assert central_main(["doctor", "--profile", str(profile)]) == CENTRAL_UNAVAILABLE

    seen: dict[str, object] = {}

    def runner(app: FastAPI, *, host: str, port: int) -> None:
        seen.update(app=app, host=host, port=port)

    assert central_main(["api", "serve", "--profile", str(profile)], runner=runner) == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8010
    app = cast(FastAPI, seen["app"])
    client = TestClient(app)
    assert _get(client, "/healthz").json() == {"status": "ok"}
    assert _get(client, "/readyz").status_code == 503
    assert _get(client, "/owner-api").status_code == 404
    assert _route_paths(app) == {
        "/admin/agent-cards",
        "/admin/users",
        "/healthz",
        "/onboarding/status",
        "/readyz",
        "/v1/browser-auth/callback",
        "/v1/browser-auth/login/start",
        "/v1/admin/policy",
        "/v1/admin/scorecard",
        "/v1/admin/agent-cards/{card_id}/owner-transfers",
        "/v1/admin/agent-cards/{card_id}/revocations",
            "/v1/admin/policy/revisions",
            "/v1/browser-auth/logout",
            "/v1/browser-auth/session",
            "/v1/console/audit",
            "/v1/console/audit/{audit_id}",
            "/v1/console/feed",
            "/v1/console/org",
            "/v1/inbox/approvals",
            "/v1/inbox/approvals/{approval_item_id}",
            "/v1/inbox/approvals/{approval_item_id}/dispositions",
            "/v1/inbox/approvals/{approval_item_id}/reassignments",
            "/v1/inbox/backup-reviews",
            "/v1/inbox/backup-reviews/{review_id}",
            "/v1/inbox/backup-reviews/{review_id}/dispositions",
            "/v1/inbox/conflicts",
            "/v1/inbox/conflicts/{case_id}",
            "/v1/inbox/conflicts/{case_id}/concurrences",
            "/v1/inbox/reevaluations",
            "/v1/inbox/reevaluations/{reevaluation_id}",
            "/v1/inbox/reevaluations/{reevaluation_id}/dispositions",
            "/v1/questions",
        "/v1/questions/{request_id}",
        "/v1/questions/{request_id}/stream",
        "/v1/questions/{request_id}/feedback",
    }


def test_central_web_serve_discovers_prebuilt_artifact_and_forwards_child_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _central_profile(tmp_path)
    artifact = object()
    node = Path("/absolute/node")
    spec = object()
    seen: dict[str, object] = {}

    def discover() -> object:
        seen["discovery"] = "default"
        return artifact

    def resolve_node() -> tuple[Path, str]:
        seen["node"] = "resolved"
        return node, "v24.0.0"

    def build(config: CentralInstallationConfig, **kwargs: object) -> object:
        seen["config"] = config
        seen["build"] = kwargs
        return spec

    monkeypatch.setattr(central_cli_module, "discover_default_central_next_artifact", discover)
    monkeypatch.setattr(central_cli_module, "resolve_node_from_path", resolve_node)
    monkeypatch.setattr(central_cli_module, "build_central_web_launch_spec", build)

    assert central_main(
        ["web", "serve", "--profile", str(profile)],
        web_runner=lambda received: 143 if received is spec else 1,  # child SIGTERM exit mapping passes through
    ) == 143
    assert seen["discovery"] == "default"
    assert seen["node"] == "resolved"
    assert seen["build"] == {"artifact": artifact, "node_executable": node, "node_version": "v24.0.0"}
    assert cast(CentralInstallationConfig, seen["config"]).central_public_origin == "https://central.example.test"


def test_central_web_serve_maps_prebuilt_artifact_failure_to_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _central_profile(tmp_path)

    def unavailable() -> object:
        raise CentralWebRuntimeUnavailable()

    monkeypatch.setattr(central_cli_module, "discover_default_central_next_artifact", unavailable)
    assert central_main(
        ["web", "serve", "--profile", str(profile)], stderr=io.StringIO()
    ) == CENTRAL_UNAVAILABLE


def test_bootstrap_admin_exact_cli_admits_once_without_raw_identity_egress(
    tmp_path: Path,
) -> None:
    profile = _central_profile(tmp_path)
    assert central_main(["migrate", "--profile", str(profile)]) == 0
    identity = VerifiedBootstrapIdentity(
        issuer="https://idp.example.test", audience="aon-central", subject="root-sub",
        email="root@example.test", email_verified=True,
    )
    def digest(value: str) -> str:
        return sha256(value.encode()).hexdigest()
    attestation = _write(
        tmp_path / "bootstrap-attestation.json",
        {
            "schema_version": 1, "attestation_id": "bootstrap-root", "org_id": "acme",
            "registry_user_id": "root", "oidc_provider_id": "company-oidc",
            "oidc_issuer_digest": digest(identity.issuer),
            "oidc_audience_digest": digest(identity.audience),
            "oidc_subject_digest": digest(identity.issuer + "\x00" + identity.subject),
            "verified_email_digest": digest(identity.email),
            "device_authorization_ref": "device-root", "idempotency_key": "bootstrap-root",
            "expected_registry_revision": 0,
            "authority_policy_digest": canonical_policy_digest(yaml.safe_load((tmp_path / "authority.yaml").read_text(encoding="utf-8"))),
        },
    )

    class Device:
        def authorize(self, **_kwargs: object) -> VerifiedBootstrapIdentity:
            return identity

    stdout, stderr = io.StringIO(), io.StringIO()
    assert central_main(
        ["bootstrap-admin", "--profile", str(profile), "--attestation", str(attestation)],
        bootstrap_device_authorizer=Device(), stdout=stdout, stderr=stderr,
    ) == 0
    public = stdout.getvalue() + stderr.getvalue()
    assert "BootstrapSealed" in public
    assert "root@example.test" not in public
    assert "root-sub" not in public
    assert central_main(
        ["doctor", "--profile", str(profile)], stdout=io.StringIO(), stderr=io.StringIO()
    ) == CENTRAL_UNAVAILABLE  # data directory remains an explicit local install prerequisite
    (tmp_path / "central-data").mkdir()
    assert central_main(
        ["doctor", "--profile", str(profile)], stdout=io.StringIO(), stderr=io.StringIO()
    ) == 0
    denied_values = json.loads(attestation.read_text(encoding="utf-8"))
    denied_values["verified_email_digest"] = "b" * 64
    denied = _write(tmp_path / "denied.json", denied_values)
    assert central_main(
        ["bootstrap-admin", "--profile", str(profile), "--attestation", str(denied)],
        bootstrap_device_authorizer=Device(), stdout=io.StringIO(), stderr=io.StringIO(),
    ) == 77
    conflicting_values = json.loads(attestation.read_text(encoding="utf-8"))
    conflicting_values["attestation_id"] = "other-bootstrap"
    conflicting_values["idempotency_key"] = "other-bootstrap"
    conflicting = _write(tmp_path / "conflicting.json", conflicting_values)
    assert central_main(
        ["bootstrap-admin", "--profile", str(profile), "--attestation", str(conflicting)],
        bootstrap_device_authorizer=Device(), stdout=io.StringIO(), stderr=io.StringIO(),
    ) == 75
    malformed = _write(tmp_path / "malformed.json", {})
    assert central_main(
        ["bootstrap-admin", "--profile", str(profile), "--attestation", str(malformed)],
        bootstrap_device_authorizer=Device(), stdout=io.StringIO(), stderr=io.StringIO(),
    ) == CENTRAL_CONFIGURATION
    unavailable_root = tmp_path / "unmigrated"
    unavailable_root.mkdir()
    unavailable_profile = _central_profile(unavailable_root)
    assert central_main(
        ["bootstrap-admin", "--profile", str(unavailable_profile), "--attestation", str(attestation)],
        bootstrap_device_authorizer=Device(), stdout=io.StringIO(), stderr=io.StringIO(),
    ) == CENTRAL_UNAVAILABLE
    with pytest.raises(SystemExit, match="2"):
        central_parser().parse_args([
            "bootstrap-admin", "--profile", "/tmp/c.json", "--attestation", "/tmp/a.json", "--email", "raw@example.test"
        ])


def test_owner_api_runner_is_literal_loopback_and_unimplemented_commands_are_unavailable(
    tmp_path: Path,
) -> None:
    profile = _owner_profile(tmp_path, pairing_reference="opaque-pairing-reference")
    config = load_owner_installation_config(profile)
    (tmp_path / "owner-workspace").mkdir()
    # There is deliberately no pair/workspace initialization in this slice:
    # doctor may inspect a profile but cannot manufacture an Owner-local DB.
    assert not owner_doctor(config)
    assert owner_main(["doctor", "--profile", str(profile)]) == OWNER_UNAVAILABLE
    for command in ("pair", "unpair", "workspace", "worker"):
        arguments = [command, "--profile", str(profile)]
        if command == "workspace":
            arguments.insert(1, "serve")
        assert owner_main(arguments) == OWNER_UNAVAILABLE

    seen: dict[str, object] = {}

    def runner(app: FastAPI, *, host: str, port: int) -> None:
        seen.update(app=app, host=host, port=port)

    assert owner_main(["api", "serve", "--profile", str(profile)], runner=runner) == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8012
    client = TestClient(cast(FastAPI, seen["app"]))
    assert _get(client, "/healthz").json() == {"status": "ok"}
    assert _get(client, "/readyz").status_code == 503
    assert _get(client, "/migrate").status_code == 404
    assert _get(client, "/authority").status_code == 404
    assert _route_paths(cast(FastAPI, seen["app"])) == {
        "/healthz",
        "/readyz",
        "/v1/pairing/status",
        "/v1/pairing/redeem",
    }


def test_fully_populated_production_central_is_unavailable_at_every_public_boundary(
    tmp_path: Path,
) -> None:
    authority_snapshot = tmp_path / "authority.yaml"
    profile = _central_profile(
        tmp_path,
        profile="production",
        oidc_issuer="https://idp.example.test",
    )

    with pytest.raises(CentralProductionUnavailable):
        load_central_installation_config(profile)

    runner_called = False

    def runner(app: FastAPI, *, host: str, port: int) -> None:
        nonlocal runner_called
        runner_called = True

    for command in (
        ["migrate"],
        ["doctor"],
        ["api", "serve"],
    ):
        assert (
            central_main([*command, "--profile", str(profile)], runner=runner)
            == CENTRAL_UNAVAILABLE
        )
    assert runner_called is False
    assert not (tmp_path / "central.sqlite3").exists()

    forged = CentralInstallationConfig(
        profile="production",
        org_id="acme",
        oidc_provider_id="company-oidc",
        oidc_issuer="https://idp.example.test",
        oidc_audience="aon-central",
        oidc_jwks_url="https://idp.example.test/jwks",
        bootstrap_oidc_device_authorization_url="https://idp.example.test/device_authorization",
        bootstrap_oidc_device_client_id="aon-central-bootstrap",
        bootstrap_oidc_scope="openid email",
        central_public_origin="https://central.example.test",
        browser_oidc_authorization_url="https://idp.example.test/authorize",
        browser_oidc_token_url="https://idp.example.test/token",
        browser_oidc_client_id="aon-central-browser",
        browser_oidc_scope="openid email",
        authority_snapshot_path=authority_snapshot,
        database_path=tmp_path / "central.sqlite3",
        data_directory=tmp_path / "central-data",
        bind_host="127.0.0.1",
        port=8010,
    )
    composition = CentralComposition(
        config=forged,
        oidc=None,
        authority_snapshot=None,
        authority=None,
        registry=None,
        principals=None,
        requests=None,
        intake=None,
    )
    assert central_doctor(forged) is False
    assert composition.schema_ready() is False
    with pytest.raises(CentralProductionUnavailable):
        compose_central(forged)
    with pytest.raises(CentralProductionUnavailable):
        migrate_central_schema(forged)
    with pytest.raises(CentralProductionUnavailable):
        create_central_api_app(composition)
    with pytest.raises(CentralProductionUnavailable):
        run_central_api(composition, runner)
    assert runner_called is False
    assert not forged.database_path.exists()


@pytest.mark.parametrize(
    ("profile_factory", "overrides", "command", "expected"),
    [
        (_central_profile, {"profile": "unknown"}, ["migrate"], CENTRAL_CONFIGURATION),
        (_central_profile, {"bind_host": "0.0.0.0"}, ["api", "serve"], CENTRAL_CONFIGURATION),
        (
            _central_profile,
            {"profile": "production"},
            ["api", "serve"],
            CENTRAL_UNAVAILABLE,
        ),
        (_owner_profile, {"bind_host": "localhost"}, ["api", "serve"], OWNER_CONFIGURATION),
        (
            _owner_profile,
            {"profile": "production", "central_url": "https://central.example"},
            ["api", "serve"],
            OWNER_CONFIGURATION,
        ),
    ],
)
def test_invalid_or_incomplete_profiles_fail_before_start(
    tmp_path: Path,
    profile_factory: Any,
    overrides: dict[str, object],
    command: list[str],
    expected: int,
) -> None:
    profile = profile_factory(tmp_path, **overrides)
    argv = [*command, "--profile", str(profile)]
    main = central_main if profile_factory is _central_profile else owner_main
    assert main(argv) == expected


_PACKAGE = "agent_org_network"
_PACKAGE_ROOT = Path("src/agent_org_network")


def _module_path(module: str) -> Path | None:
    if module == _PACKAGE:
        candidate = _PACKAGE_ROOT / "__init__.py"
    elif module.startswith(f"{_PACKAGE}."):
        relative = module.removeprefix(f"{_PACKAGE}.").replace(".", "/")
        candidate = _PACKAGE_ROOT / f"{relative}.py"
        if not candidate.is_file():
            candidate = _PACKAGE_ROOT / relative / "__init__.py"
    else:
        return None
    return candidate if candidate.is_file() else None


def _imported_internal_modules(module: str) -> set[str]:
    path = _module_path(module)
    if path is None:
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(
                name.name for name in node.names if _module_path(name.name) is not None
            )
        elif isinstance(node, ast.ImportFrom):
            parent = node.module or ""
            if node.level:
                package_parts = module.split(".")[:-1]
                retained = package_parts[: len(package_parts) - (node.level - 1)]
                parent = ".".join((*retained, parent)) if parent else ".".join(retained)
            if _module_path(parent) is not None:
                imported.add(parent)
            for name in node.names:
                candidate = f"{parent}.{name.name}" if parent else name.name
                if _module_path(candidate) is not None:
                    imported.add(candidate)
    return imported


def _transitive_internal_import_graph(root: str) -> tuple[str, ...]:
    pending = [root]
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        if _module_path(module) is None:
            raise AssertionError(f"production root module missing: {module}")
        visited.add(module)
        pending.extend(_imported_internal_modules(module) - visited)
    return tuple(sorted(visited))


def _fastapi_route_graph(app: FastAPI) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"{method} {route.path}"
            for route in app.routes
            if isinstance(route, Route)
            for method in route.methods or ()
        )
    )


def test_production_roots_match_exact_transitive_import_manifests() -> None:
    manifests = (
        ArtifactManifest(
            installation=InstallationKind.CENTRAL_SERVER,
            processes=CENTRAL_SERVER_ALLOWED_PROCESSES,
            imports=_transitive_internal_import_graph("agent_org_network.central_cli"),
            surfaces=CENTRAL_SERVER_SURFACE_ALLOWLIST,
        ),
        ArtifactManifest(
            installation=InstallationKind.CARD_OWNER,
            processes=CARD_OWNER_ALLOWED_PROCESSES,
            imports=_transitive_internal_import_graph("agent_org_network.owner_cli"),
            surfaces=CARD_OWNER_SURFACE_ALLOWLIST,
        ),
        ArtifactManifest(
            installation=InstallationKind.QUESTION_USER,
            processes=QUESTION_USER_ALLOWED_PROCESSES,
            tools=QUESTION_USER_MCP_TOOL_MANIFEST,
            imports=_transitive_internal_import_graph("agent_org_network.question_user_mcp"),
        ),
    )
    assert manifests[0].imports == CENTRAL_SERVER_IMPORT_ALLOWLIST
    assert manifests[1].imports == CARD_OWNER_IMPORT_ALLOWLIST
    assert manifests[2].imports == QUESTION_USER_IMPORT_ALLOWLIST
    assert not {
        "agent_org_network.answer_finalization",
        "agent_org_network.question_resolution",
        "agent_org_network.question_stream",
        "agent_org_network.question_stream_execution",
        "agent_org_network.router",
    } & set(CENTRAL_SERVER_IMPORT_ALLOWLIST)


def test_api_route_graphs_match_exact_installation_manifests(tmp_path: Path) -> None:
    central = _central_profile(tmp_path)
    owner = _owner_profile(tmp_path)
    central_app = create_central_api_app(
        compose_central(load_central_installation_config(central))
    )
    owner_app = create_owner_api_app(compose_owner(load_owner_installation_config(owner)))
    assert _fastapi_route_graph(central_app) == CENTRAL_SERVER_SURFACE_ALLOWLIST
    assert _fastapi_route_graph(owner_app) == CARD_OWNER_SURFACE_ALLOWLIST
    assert _get(TestClient(central_app), "/readyz").status_code == 503
    assert _get(TestClient(owner_app), "/readyz").status_code == 503
