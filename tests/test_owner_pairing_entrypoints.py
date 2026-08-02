from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from pathlib import Path
import sqlite3
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.owner_api import create_owner_api_app
from agent_org_network.owner_cli import EXIT_CONFIGURATION, main as owner_main
from agent_org_network.owner_composition import (
    OwnerPairingRequest,
    compose_owner,
    load_owner_installation_config,
)


NOW = datetime(2026, 8, 2, 3, 4, 5, tzinfo=UTC)


def _profile(tmp_path: Path) -> Path:
    path = tmp_path / "owner.json"
    path.write_text(
        json.dumps(
            {
                "profile": "local-reference",
                "database_path": str(tmp_path / "owner.sqlite3"),
                "workspace_directory": str(tmp_path / "workspace"),
                "bind_host": "127.0.0.1",
                "port": 8012,
                "central_url": "https://central.example.test",
                "pairing_reference": "pairing-1",
                "secret_store_directory": str(tmp_path / "secrets"),
            }
        ),
        encoding="utf-8",
    )
    return path


def _request() -> dict[str, object]:
    return {
        "intent_id": "intent-1",
        "pairing_code": "p" * 32,
        "central_origin": "https://central.example.test",
        "org_id": "acme",
        "owner_user_id": "owner-1",
        "agent_card_id": "support",
        "agent_card_revision": 3,
        "agent_card_digest": "a" * 64,
        "device_key_thumbprint": "T" * 43,
        "pairing_intent_digest": "b" * 64,
        "issue_receipt_id": "issue-1",
        "issue_receipt_digest": "c" * 64,
        "pairing_expires_at": "2026-08-02T03:09:05Z",
        "redeem_idempotency_key": "redeem-1",
    }


class _Adapter:
    def __init__(self) -> None:
        self.requests: list[OwnerPairingRequest] = []

    def pair(self, config: object, request: OwnerPairingRequest) -> object:
        del config
        self.requests.append(request)
        return {
            "kind": "finalized",
            "profile_id": "profile-1",
            "state": None,
            "binding_digest": "d" * 64,
            "credential_id": "credential-1",
            "credential_generation": 1,
            "credential_public_digest": "e" * 64,
            "bundle_revision": 3,
            "bundle_public_digest": "f" * 64,
            "verification": "paired",
        }


class _MalformedAdapter(_Adapter):
    def pair(self, config: object, request: OwnerPairingRequest) -> object:
        del config, request
        return {"kind": "finalized", "profile_id": "profile-1"}


class _Ready:
    def ready(self, config: object) -> bool:
        del config
        return True


def test_pairing_request_rejects_secret_or_raw_payload_extensions() -> None:
    valid = OwnerPairingRequest.model_validate(_request())
    assert valid.pairing_code.get_secret_value() == "p" * 32
    assert "p" * 32 not in valid.model_dump_json()
    try:
        OwnerPairingRequest.model_validate(_request() | {"token": "no"})
    except ValueError:
        pass
    else:
        raise AssertionError("secret-bearing extension must be rejected")


def test_owner_api_redeem_returns_only_secret_free_projection(tmp_path: Path) -> None:
    config = load_owner_installation_config(_profile(tmp_path))
    adapter = _Adapter()
    app = create_owner_api_app(compose_owner(config, pairing_adapter=adapter))
    http: Any = TestClient(app)
    response = cast(Response, http.post(
        "/v1/pairing/redeem",
        content=json.dumps(_request()),
        headers={"content-type": "application/json"},
    ))
    assert response.status_code == 200
    assert response.json()["kind"] == "finalized"
    assert "pairing_code" not in response.text
    assert adapter.requests[0].pairing_code.get_secret_value() == "p" * 32


def test_owner_api_pairing_routes_fail_closed_without_adapter(tmp_path: Path) -> None:
    config = load_owner_installation_config(_profile(tmp_path))
    client: Any = TestClient(create_owner_api_app(compose_owner(config)))
    assert client.get("/v1/pairing/status").status_code == 503
    assert client.post(
        "/v1/pairing/redeem",
        content=json.dumps(_request()),
        headers={"content-type": "application/json"},
    ).status_code == 503


def test_owner_api_never_serializes_malformed_adapter_result(tmp_path: Path) -> None:
    config = load_owner_installation_config(_profile(tmp_path))
    client: Any = TestClient(
        create_owner_api_app(compose_owner(config, pairing_adapter=_MalformedAdapter()))
    )
    response = cast(
        Response,
        client.post(
            "/v1/pairing/redeem",
            content=json.dumps(_request()),
            headers={"content-type": "application/json"},
        ),
    )
    assert response.status_code == 503


def test_owner_api_status_requires_schema_and_pairing_readiness(tmp_path: Path) -> None:
    config = load_owner_installation_config(_profile(tmp_path))
    app = create_owner_api_app(
        compose_owner(config, pairing_readiness=_Ready(), pairing_adapter=_Adapter())
    )
    client: Any = TestClient(app)
    assert client.get("/v1/pairing/status").status_code == 503
    connection = sqlite3.connect(config.database_path)
    connection.execute(
        "CREATE TABLE aon_installation_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO aon_installation_schema(name,version) VALUES ('owner-installation',1)"
    )
    connection.commit()
    connection.close()
    response = cast(Response, client.get("/v1/pairing/status"))
    assert response.status_code == 200
    assert response.json() == {"status": "paired", "pairing_reference": "pairing-1"}


def test_owner_cli_reads_pairing_request_from_stdin_only(tmp_path: Path) -> None:
    profile = _profile(tmp_path)
    adapter = _Adapter()
    stdout, stderr = io.StringIO(), io.StringIO()
    assert owner_main(
        ["pair", "--profile", str(profile)],
        pairing_adapter=adapter,
        stdin=io.StringIO(json.dumps(_request())),
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert json.loads(stdout.getvalue())["kind"] == "finalized"
    assert "pairing_code" not in stdout.getvalue()

    assert owner_main(
        ["pair", "--profile", str(profile)],
        pairing_adapter=adapter,
        stdin=io.StringIO(json.dumps(_request() | {"token": "forbidden"})),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    ) == EXIT_CONFIGURATION
