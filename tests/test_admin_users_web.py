"""관리 UI 웹 라우트 — 사용자 등록·목록(ADR 0064 · 카드 등록 라우트의 User 축 형제).

TestClient 결정론:
  - 등록 성공(라이브 반영 + 감사 + durable 저널)·422 무효(id 공백·email 없음/형식)·
    409 중복 user_id·422 중복 email·401 미로그인·중앙 모드 503(부분/완전 조립)·목록 조회.
  - 부팅 리플레이 순서 배선(create_app이 user 저널을 card 저널보다 먼저 재생) — 회귀.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.audit import InMemoryAuditLog
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_application import OperationalMutationApproval
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.runtime import StubRuntime
from agent_org_network.sqlite_stores import SqliteRegistryJournal, SqliteUserJournal
from agent_org_network.web import create_app

_SECRET = "test-secret"
_T0 = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _get(client: TestClient, url: str) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(Response, http.get(url))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    http: Any = client
    res: Response = cast(Response, http.post(url, json=payload))
    try:
        body: Any = res.json()
    except Exception:
        body = res.text
    return res.status_code, body


def _login(client: TestClient, user_id: str) -> None:
    status, _ = _post(client, "/login", {"user_id": user_id})
    assert status == 200, f"로그인 실패: {user_id}"


def _payload(**kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "user_id": "alice",
        "email": "alice@company.com",
        "manager": "root_manager",
    }
    defaults.update(kwargs)
    return defaults


# ── 신규 사용자 등록 ──────────────────────────────────────────────────────────


class TestAdminRegisterUser:
    def test_미로그인_401(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        status, _ = _post(client, "/admin/users", _payload())
        assert status == 401

    def test_등록_성공_200_라이브_반영(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(client, "/admin/users", _payload())
        assert status == 200, body
        assert body["registered"] is True
        assert body["user_id"] == "alice"
        assert body["email"] == "alice@company.com"
        assert body["manager"] == "root_manager"
        # 라이브 목록에 즉시 잡힌다.
        _, users = _get(client, "/admin/users")
        assert "alice" in {u["user_id"] for u in users}

    def test_등록_성공시_감사_UserRegistered_기록(self) -> None:
        audit = InMemoryAuditLog()
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET, audit_log=audit)
        client = TestClient(app)
        _login(client, "root_manager")
        _post(client, "/admin/users", _payload())
        registered = [
            r
            for r in audit.records()
            if r.get("action") and r["action"]["kind"] == "UserRegistered"
        ]
        assert len(registered) == 1
        assert registered[0]["action"]["subject_id"] == "alice"

    def test_등록_성공시_durable_저널_append(self, tmp_path: Path) -> None:
        journal = SqliteUserJournal(tmp_path / "u.db")
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET, user_journal=journal)
        client = TestClient(app)
        _login(client, "root_manager")
        status, _ = _post(client, "/admin/users", _payload())
        assert status == 200
        entries = journal.entries()
        assert len(entries) == 1
        assert entries[0].candidate.user_id == "alice"
        assert entries[0].candidate.email == "alice@company.com"

    def test_빈_user_id_422(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(client, "/admin/users", _payload(user_id="  "))
        assert status == 422
        assert "errors" in body["detail"]

    def test_email_없음_422(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(client, "/admin/users", _payload(email=None))
        assert status == 422
        assert "errors" in body["detail"]

    def test_email_형식_오류_422(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(client, "/admin/users", _payload(email="not-an-email"))
        assert status == 422
        assert "errors" in body["detail"]

    def test_미등록_manager_422(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(client, "/admin/users", _payload(manager="ghost_mgr"))
        assert status == 422
        assert "errors" in body["detail"]

    def test_중복_user_id_409(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        # root_manager는 데모에 이미 존재.
        status, _ = _post(client, "/admin/users", _payload(user_id="root_manager"))
        assert status == 409

    def test_중복_email_422(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        # cs_lead의 email이 데모에 이미 존재 — email 전역 유일 admission 거부(422).
        status, body = _post(
            client, "/admin/users", _payload(user_id="new_alice", email="cs.lead@example.com")
        )
        assert status == 422
        assert "errors" in body["detail"]

    def test_manager_없이_루트로_등록(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _post(
            client, "/admin/users", _payload(user_id="lone", email="lone@x.com", manager=None)
        )
        assert status == 200, body
        assert body["manager"] is None


# ── 목록 조회 ─────────────────────────────────────────────────────────────────


class TestAdminListUsers:
    def test_미로그인_401(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        status, _ = _get(client, "/admin/users")
        assert status == 401

    def test_목록_조회_시드_User_노출(self) -> None:
        app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
        client = TestClient(app)
        _login(client, "root_manager")
        status, body = _get(client, "/admin/users")
        assert status == 200
        ids = {u["user_id"] for u in body}
        assert "root_manager" in ids
        # 운영 면 — email·manager 노출 OK.
        root = next(u for u in body if u["user_id"] == "root_manager")
        assert "email" in root and "manager" in root


# ── 중앙 모드 fail-closed (Depth B 미배선 — 카드 R2a 미러) ────────────────────


def _principal(subject_id: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id="session-1",
    )


class TestCentralModeFailClosed:
    def test_중앙_모드_등록_503(self) -> None:
        app = create_app(
            runtime=StubRuntime(),
            governance_principal_resolver=lambda _r: _principal("root_manager"),
        )
        client = TestClient(app)
        res: Response = cast(Response, cast(Any, client).post("/admin/users", json=_payload()))
        assert res.status_code == 503

    def test_중앙_모드_목록_503(self) -> None:
        app = create_app(
            runtime=StubRuntime(),
            governance_principal_resolver=lambda _r: _principal("root_manager"),
        )
        client = TestClient(app)
        status, _ = _get(client, "/admin/users")
        assert status == 503


def _full_central_authorization() -> OperationalAuthorization:
    """resolver·authorization·mutation_approval 3개를 다 갖춘 완전 조립용 authority.

    `test_operational_card_authorization.py`의 `_snapshot`/`_app` 조립을 미러한다 —
    사용자 프로비저닝도 완전 조립에서 여전히 503임을 같은 결로 고정한다.
    """
    permissions = (RolePermission(role="admin", actions=("user.register",)),)
    bindings = (
        SubjectRoleBinding(org_id="acme", subject_id="root_manager", roles=("admin",)),
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test-policy",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json") for binding in bindings],
        "role_permissions": [permission.model_dump(mode="json") for permission in permissions],
        "route_rules": [],
        "worker_bindings": [],
    }
    digest = canonical_policy_digest(document)
    document["content_sha256"] = digest
    snapshot = AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="test-policy",
        content_sha256=digest,
        subject_roles=bindings,
        role_permissions=permissions,
        route_rules=(),
        worker_bindings=(),
    )
    return OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(snapshot),
    )


def _allow_mutation(
    _principal: AuthenticatedPrincipal,
    _action: object,
    _resource: object,
    digest: str,
    fingerprint: str,
) -> OperationalMutationApproval:
    return OperationalMutationApproval(
        outcome="allowed",
        evidence_id="approval-user-test",
        command_digest=digest,
        resource_fingerprint=fingerprint,
    )


class TestCentralModeFullCompositionStillFailClosed:
    """M2 — resolver+authorization+mutation_approval을 **다 갖춰도** User Depth B는

    미배선이라 여전히 503이어야 한다. `_require_operational_composition()`(부분 조립
    503)만 커버하던 기존 테스트의 공백 — 완전 조립에서도 fail-closed가 유지됨을 고정해,
    이 무조건 raise를 지우는 회귀가 `_session_identity` 폴백으로 우회 등록을 여는 걸 잡는다.
    """

    def test_완전_조립_등록도_여전히_503(self) -> None:
        app = create_app(
            runtime=StubRuntime(),
            governance_principal_resolver=lambda _r: _principal("root_manager"),
            operational_authorization=_full_central_authorization(),
            operational_mutation_approval=_allow_mutation,
        )
        client = TestClient(app)
        res: Response = cast(Response, cast(Any, client).post("/admin/users", json=_payload()))
        assert res.status_code == 503

    def test_완전_조립_목록도_여전히_503(self) -> None:
        app = create_app(
            runtime=StubRuntime(),
            governance_principal_resolver=lambda _r: _principal("root_manager"),
            operational_authorization=_full_central_authorization(),
            operational_mutation_approval=_allow_mutation,
        )
        client = TestClient(app)
        status, _ = _get(client, "/admin/users")
        assert status == 503


# ── 부팅 리플레이 순서 배선 회귀 (M1 — create_app이 실제로 user→card 순서를 지키는가) ──


class TestBootReplayOrderWiring:
    """`test_user_journal.py`는 `replay_user_journal`/`replay_registry_journal`을 직접
    올바른 순서로 호출하는 함수 단위 테스트만 있다 — 그 순서를 실제로 강제하는 배선
    (`web.py`의 create_app 본문)은 검증되지 않아, 카드 리플레이 블록이 user 리플레이
    블록보다 앞으로 옮겨져도 함수 단위 테스트는 전부 green인 채로 부팅 시 라이브
    등록 User가 owner인 카드가 조용히 소멸할 수 있었다. 이 테스트는 `create_app`에
    두 저널을 함께 주입해 그 배선 자체를 고정한다.
    """

    def test_create_app는_user_저널을_card_저널보다_먼저_재생한다(
        self, tmp_path: Path
    ) -> None:
        user_journal = SqliteUserJournal(tmp_path / "u.db")
        user_journal.append_register(
            user_id="alice",
            email="alice@company.com",
            manager="root_manager",
            by="op",
            at=_T0,
        )
        card_journal = SqliteRegistryJournal(tmp_path / "c.db")
        card_journal.append_register(
            agent_id="alice_ops",
            owner="alice",  # 시드에 없는 라이브 등록 User — user 저널 재생 후에만 실재.
            team="ops",
            summary="alice 담당",
            domains=["신규"],
            last_reviewed_at="2026-06-20",
            by="op",
            at=_T0,
        )

        app = create_app(
            runtime=StubRuntime(),
            user_journal=user_journal,
            registry_journal=card_journal,
        )
        client = TestClient(app)

        # user 저널이 카드 저널보다 먼저 재생됐다면 alice가 라이브에 있고, alice가 owner인
        # 카드도 owner 실재를 통과해 살아 있다. 순서가 뒤바뀌면 owner 미실재로 카드가
        # 리플레이에서 스킵돼(안전측 스킵) 이 assertion이 실패한다.
        status, users = _get(client, "/admin/users")
        assert status == 200
        assert "alice" in {u["user_id"] for u in users}

        status, cards = _get(client, "/admin/cards")
        assert status == 200
        assert "alice_ops" in {c["agent_id"] for c in cards}
        alice_ops = next(c for c in cards if c["agent_id"] == "alice_ops")
        assert alice_ops["owner"] == "alice"
