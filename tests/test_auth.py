"""T6.5 슬라이스 1+2 — 세션 인증 + 신원 세션화·스코프 강제 테스트.

결정론: TestClient 세션(쿠키 유지) + 고정 session_secret("test-secret").
실 LLM·브라우저·외부 인증 서버 0.

커버 범위:
  슬라이스 1 (세션 + 로그인):
    - POST /login 유효 user → 200, 세션에 신원 고정.
    - POST /login 미존재 user → 401.
    - POST /logout → 세션 클리어, 이후 401 복귀.
    - _session_identity가 세션 없이는 NotAuthenticatedError.

  슬라이스 2 (신원 세션화·스코프):
    - 미로그인 → /inbox/cases·/inbox/backup-reviews·/manager/queue·concur·act·backup-review 전부 401.
    - 로그인 → 자기 /inbox/cases 200.
    - 로그인 → 자기 case concur 200.
    - 세션 owner ≠ case 후보 → concur 403.
    - 세션 owner ≠ item.owner_id → backup-review 403.
    - 세션 신원 ≠ item.manager_id → manager act 403.
    - /manager/queue: 자기 큐만 (세션 manager_id).
    - 채팅 /ask·/ 은 미로그인도 200 (익명 공간 불변).
    - /monitor*: 미로그인 401, 로그인 후 200.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, cast
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
)
from agent_org_network.demo import build_demo
from agent_org_network.manager_queue import (
    FromDeadlock,
    InMemoryManagerQueueStore,
    ManagerItem,
)
from agent_org_network.review import BackupReviewItem, InMemoryBackupReviewStore
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_SECRET = "test-secret"
_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
_DATE = date(2026, 6, 20)


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any


def _result(res: Response) -> HttpResult:
    body: Any = res.json()
    return HttpResult(status=res.status_code, body=body)


def _get(client: TestClient, url: str) -> HttpResult:
    http: Any = client
    return _result(cast(Response, http.get(url)))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> HttpResult:
    http: Any = client
    return _result(cast(Response, http.post(url, json=payload)))


# ── 앱 팩토리 ────────────────────────────────────────────────────────────


def _auth_app() -> FastAPI:
    """세션 미들웨어가 붙은 인증 앱 (데모 레지스트리 그대로)."""
    return create_app(runtime=StubRuntime(), session_secret=_SECRET)


def _legacy_contested_auth_client() -> tuple[TestClient, str]:
    """Request 경계 이전의 session-scoped legacy concurrence fixture."""
    queue_store = InMemoryManagerQueueStore()
    bundle = build_demo(runtime=StubRuntime(), manager_queue_store=queue_store)
    case = ConflictCase(
        intent="보상",
        question="보상 기준이 어떻게 되나요?",
        candidates=(
            Candidate(agent_id="cs_ops", owner="cs_lead"),
            Candidate(agent_id="finance_ops", owner="finance_lead"),
        ),
        opened_at=_NOW,
        case_id="legacy-auth-case",
    )
    bundle.case_store.open_case(case)
    with patch("agent_org_network.web.build_demo", return_value=bundle):
        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            manager_queue_store=queue_store,
        )
    return TestClient(app), case.case_id


def _auth_app_with_queue() -> FastAPI:
    """Manager 큐가 있는 인증 앱."""
    queue_store = InMemoryManagerQueueStore()
    return create_app(
        runtime=StubRuntime(),
        session_secret=_SECRET,
        manager_queue_store=queue_store,
    )


def _auth_app_with_review() -> tuple[FastAPI, InMemoryBackupReviewStore]:
    """BackupReview store가 있는 인증 앱."""
    from agent_org_network.review import BackupReviewService

    review_store = InMemoryBackupReviewStore()
    review_svc = BackupReviewService(review_store)
    app = create_app(
        runtime=StubRuntime(),
        session_secret=_SECRET,
        review_store=review_store,
        review_service=review_svc,
    )
    return app, review_store


def _login(client: TestClient, user_id: str) -> HttpResult:
    return _post(client, "/login", {"user_id": user_id})


def _logout(client: TestClient) -> HttpResult:
    return _post(client, "/logout", {})


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 1 — 세션 + 로그인
# ════════════════════════════════════════════════════════════════════════════


class TestSlice1_Login:
    def test_유효_user_로그인은_200(self) -> None:
        client = TestClient(_auth_app())
        r = _login(client, "cs_lead")
        assert r.status == 200

    def test_미존재_user_로그인은_401(self) -> None:
        client = TestClient(_auth_app())
        r = _login(client, "nobody_invalid")
        assert r.status == 401

    def test_로그인_후_로그아웃_세션_클리어(self) -> None:
        client = TestClient(_auth_app())
        _login(client, "cs_lead")
        _logout(client)
        # 로그아웃 후 인증 엔드포인트는 401을 돌려줘야 한다.
        r = _get(client, "/inbox/cases")
        assert r.status == 401

    def test_로그아웃은_200(self) -> None:
        client = TestClient(_auth_app())
        _login(client, "cs_lead")
        r = _logout(client)
        assert r.status == 200

    def test_root_manager도_로그인_가능(self) -> None:
        client = TestClient(_auth_app())
        r = _login(client, "root_manager")
        assert r.status == 200

    def test_데모_6명_모두_로그인_가능(self) -> None:
        """데모 Registry에 등록된 6명은 모두 /login이 200."""
        valid_users = [
            "root_manager",
            "legal_lead",
            "cs_lead",
            "finance_lead",
            "hr_lead",
            "it_lead",
        ]
        app = _auth_app()
        for user_id in valid_users:
            client = TestClient(app)
            r = _login(client, user_id)
            assert r.status == 200, f"{user_id} 로그인이 200이어야 하는데 {r.status}"


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — 미로그인 401 (운영 엔드포인트 게이트)
# ════════════════════════════════════════════════════════════════════════════


class TestNoAuthLogin_크로스머신_재시연_결함_5a:
    """no-auth 모드(session_secret 미주입)에서 POST /login·/logout이 500이 아니라
    깨끗한 4xx + 안내 메시지를 내야 한다(실 시연 재현 — SessionMiddleware 미부착 시
    `request.session` 접근이 AssertionError→500으로 새던 결함)."""

    def test_noauth_모드_login은_500이_아니라_4xx(self) -> None:
        client = TestClient(create_app(runtime=StubRuntime()))  # session_secret 미주입
        r = _post(client, "/login", {"user_id": "cs_lead"})
        assert r.status != 500
        assert 400 <= r.status < 500
        assert "no-auth" in str(r.body.get("detail", ""))

    def test_noauth_모드_logout은_500이_아니라_4xx(self) -> None:
        client = TestClient(create_app(runtime=StubRuntime()))
        r = _post(client, "/logout", {})
        assert r.status != 500
        assert 400 <= r.status < 500


class TestSlice2_미로그인_401:
    """인증 앱에서 미로그인 상태로 운영 엔드포인트에 접근하면 401."""

    def test_미로그인_inbox_cases는_401(self) -> None:
        client = TestClient(_auth_app())
        assert _get(client, "/inbox/cases").status == 401

    def test_미로그인_inbox_backup_reviews는_401(self) -> None:
        client = TestClient(_auth_app())
        assert _get(client, "/inbox/backup-reviews").status == 401

    def test_미로그인_manager_queue는_401(self) -> None:
        client = TestClient(_auth_app_with_queue())
        assert _get(client, "/manager/queue").status == 401

    def test_미로그인_concur는_401(self) -> None:
        client = TestClient(_auth_app())
        r = _post(
            client,
            "/cases/any-case-id/concur",
            {"on_agent": "cs_ops", "rationale": ""},
        )
        assert r.status == 401

    def test_미로그인_manager_act는_401(self) -> None:
        client = TestClient(_auth_app_with_queue())
        r = _post(
            client,
            "/manager/items/any-item-id/act",
            {"type": "dismiss", "rationale": ""},
        )
        assert r.status == 401

    def test_미로그인_backup_review는_401(self) -> None:
        app, _ = _auth_app_with_review()
        client = TestClient(app)
        r = _post(
            client,
            "/backup-reviews/any-item-id",
            {"type": "approve"},
        )
        assert r.status == 401

    def test_미로그인_monitor는_401(self) -> None:
        client = TestClient(_auth_app())
        assert _get(client, "/monitor").status == 401

    def test_미로그인_monitor_detail는_401(self) -> None:
        client = TestClient(_auth_app())
        assert _get(client, "/monitor/0").status == 401


# ════════════════════════════════════════════════════════════════════════════
# 레거시 가장 경로 차단 — 인증 ON이면 path 신원-지목 경로가 *존재하지 않는다*
# (세션 스코프 우회 방지 — ADR 0016 보안. 인증 OFF 전용 등록)
# ════════════════════════════════════════════════════════════════════════════


class TestSlice2_레거시_가장_경로_차단:
    """인증 ON 앱에는 `/inbox/{owner_id}` 등 path 가장 경로가 등록되지 않는다.

    이게 없으면 로그인 없이 `GET /inbox/cs_lead`로 남의 처리함을 읽어 세션 스코프
    전체가 우회된다(T6.5 목적 무력화). 인증 ON에서 그 경로들이 404(미등록)여야 한다.
    """

    def test_로그인해도_path_owner_inbox는_404(self) -> None:
        # 로그인까지 했어도(세션 있음) path 가장 경로 자체가 없어야 한다.
        client = TestClient(_auth_app())
        _login(client, "cs_lead")
        assert _get(client, "/inbox/finance_lead").status == 404
        assert _get(client, "/inbox/cs_lead").status == 404

    def test_미로그인_path_owner_inbox도_404(self) -> None:
        client = TestClient(_auth_app())
        assert _get(client, "/inbox/cs_lead").status == 404

    def test_path_manager_queue는_404(self) -> None:
        client = TestClient(_auth_app())
        _login(client, "root_manager")
        assert _get(client, "/manager/root_manager").status == 404

    def test_path_backup_reviews는_404(self) -> None:
        client = TestClient(_auth_app())
        _login(client, "cs_lead")
        assert _get(client, "/inbox/cs_lead/backup-reviews").status == 404


# ════════════════════════════════════════════════════════════════════════════
# 채팅 익명 불변식 — 미로그인도 200
# ════════════════════════════════════════════════════════════════════════════


class TestChatAnonymous:
    """채팅 면(/ask·/)은 세션 무관 — ADR 0016 결정 6."""

    def test_미로그인_ask는_200(self) -> None:
        client = TestClient(_auth_app())
        r = _post(client, "/ask", {"question": "안녕하세요"})
        assert r.status == 200

    def test_미로그인_index는_401_아님(self) -> None:
        client = TestClient(_auth_app())
        # FileResponse(index.html) — 파일이 없으면 404, 있으면 200.
        # 어떤 경우든 인증(401)이 아닌 상태여야 한다.
        http: Any = client
        resp = http.get("/")
        assert resp.status_code != 401

    def test_로그인_없이_ask_tracking_200(self) -> None:
        client = TestClient(_auth_app())
        # 없는 토큰 → 404 (인증 관계없음 — 인증 없이 접근 가능).
        r = _get(client, "/ask/없는토큰xyz")
        assert r.status == 404


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — 로그인 후 자기 처리함 조회 (200)
# ════════════════════════════════════════════════════════════════════════════


class TestSlice2_자기_처리함_200:
    """로그인 후 자기 것을 조회하면 200."""

    def test_cs_lead_로그인_후_inbox_cases_200(self) -> None:
        client = TestClient(_auth_app())
        _login(client, "cs_lead")
        r = _get(client, "/inbox/cases")
        assert r.status == 200

    def test_cs_lead_로그인_후_inbox_backup_reviews_200(self) -> None:
        app, _ = _auth_app_with_review()
        client = TestClient(app)
        _login(client, "cs_lead")
        r = _get(client, "/inbox/backup-reviews")
        assert r.status == 200

    def test_root_manager_로그인_후_manager_queue_200(self) -> None:
        client = TestClient(_auth_app_with_queue())
        _login(client, "root_manager")
        r = _get(client, "/manager/queue")
        assert r.status == 200


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — inbox/cases 세션 스코프 (세션 owner 처리함만)
# ════════════════════════════════════════════════════════════════════════════


class TestSlice2_InboxCases_스코프:
    """자기 케이스만 보인다 (세션 owner가 후보인 케이스)."""

    def _make_contested_app(self) -> TestClient:
        """보상 질문으로 cs_lead·finance_lead가 후보인 케이스를 만든다."""
        client = TestClient(_auth_app())
        # 채팅(익명)으로 다툼 생성
        _post(client, "/ask", {"question": "보상 기준이 어떻게 되나요?"})
        return client

    def test_cs_lead_로그인_후_자기_케이스_조회(self) -> None:
        client = self._make_contested_app()
        _login(client, "cs_lead")
        r = _get(client, "/inbox/cases")
        assert r.status == 200
        cases: list[Any] = r.body
        assert len(cases) == 1
        assert cases[0]["intent"] == "보상"

    def test_legal_lead_로그인_후_자기_케이스_없음(self) -> None:
        """legal_lead는 보상 케이스 후보가 아니라 빈 목록."""
        client = self._make_contested_app()
        _login(client, "legal_lead")
        r = _get(client, "/inbox/cases")
        assert r.status == 200
        assert r.body == []


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — concur 세션 스코프
# ════════════════════════════════════════════════════════════════════════════


class TestSlice2_Concur_스코프:
    def _setup_contested(self) -> tuple[TestClient, str]:
        """request_id=None인 legacy 다툼 Case를 명시해 인증 스코프만 검증한다."""
        return _legacy_contested_auth_client()

    def test_cs_lead_자기_케이스_concur_200(self) -> None:
        client, case_id = self._setup_contested()
        _login(client, "cs_lead")
        r = _post(
            client,
            f"/cases/{case_id}/concur",
            {"on_agent": "cs_ops", "rationale": "환불 관련"},
        )
        assert r.status == 200

    def test_세션이_아닌_owner_concur_403(self) -> None:
        """세션이 cs_lead인데 cs_lead가 후보가 아닌 케이스(legal_lead 세션으로 시도)."""
        client, case_id = self._setup_contested()
        # legal_lead로 로그인 후 보상 케이스 concur 시도 → 403 (후보 아님)
        _login(client, "legal_lead")
        r = _post(
            client,
            f"/cases/{case_id}/concur",
            {"on_agent": "cs_ops"},
        )
        assert r.status == 403

    def test_미존재_case는_404(self) -> None:
        """ADR 0016 결정 4: 대상 미존재 → 404 (기존 400은 틀린 기대였음)."""
        client = TestClient(_auth_app())
        _login(client, "cs_lead")
        r = _post(client, "/cases/없는케이스/concur", {"on_agent": "cs_ops"})
        assert r.status == 404

    def test_concur_body에_by_owner_없어도_동작(self) -> None:
        """세션화 후: body에 by_owner 없이 on_agent만 보내도 동작."""
        client, case_id = self._setup_contested()
        _login(client, "cs_lead")
        r = _post(
            client,
            f"/cases/{case_id}/concur",
            {"on_agent": "cs_ops"},  # by_owner 없음
        )
        assert r.status == 200


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — backup-review 세션 스코프
# ════════════════════════════════════════════════════════════════════════════


class TestSlice2_BackupReview_스코프:
    def _make_item(
        self,
        review_store: InMemoryBackupReviewStore,
        owner_id: str = "cs_lead",
    ) -> BackupReviewItem:
        item = BackupReviewItem(
            owner_id=owner_id,
            agent_id="cs_ops",
            question="환불 되나요?",
            backup_answer_text="백업 답변",
            ticket_id="ticket-001",
            snapshot_at=_NOW,
            answered_at=_NOW,
            item_id="item-001",
        )
        review_store.add(item)
        return item

    def test_cs_lead_자기_item_approve_200(self) -> None:
        app, review_store = _auth_app_with_review()
        self._make_item(review_store)
        client = TestClient(app)
        _login(client, "cs_lead")
        r = _post(
            client,
            "/backup-reviews/item-001",
            {"type": "approve", "rationale": "맞음"},
        )
        assert r.status == 200

    def test_다른_owner_item_approve_403(self) -> None:
        """세션이 legal_lead인데 cs_lead 소유 item을 처분하면 403."""
        app, review_store = _auth_app_with_review()
        self._make_item(review_store, owner_id="cs_lead")
        client = TestClient(app)
        _login(client, "legal_lead")
        r = _post(
            client,
            "/backup-reviews/item-001",
            {"type": "approve"},
        )
        assert r.status == 403

    def test_미존재_item_404(self) -> None:
        app, _ = _auth_app_with_review()
        client = TestClient(app)
        _login(client, "cs_lead")
        r = _post(
            client,
            "/backup-reviews/nonexistent",
            {"type": "approve"},
        )
        assert r.status == 404

    def test_backup_review_body에_by_owner_없어도_동작(self) -> None:
        """세션화 후: body에 by_owner 없이 type만 보내도 동작."""
        app, review_store = _auth_app_with_review()
        self._make_item(review_store)
        client = TestClient(app)
        _login(client, "cs_lead")
        r = _post(
            client,
            "/backup-reviews/item-001",
            {"type": "dismiss"},  # by_owner 없음
        )
        assert r.status == 200


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — manager queue 세션 스코프
# ════════════════════════════════════════════════════════════════════════════


class TestSlice2_ManagerQueue_스코프:
    def _make_manager_item(
        self,
        queue_store: InMemoryManagerQueueStore,
        manager_id: str = "root_manager",
    ) -> ManagerItem:
        """FromDeadlock 출처의 ManagerItem을 큐에 적재한다."""
        case = ConflictCase(
            intent="보상",
            question="보상 기준?",
            candidates=(
                Candidate(agent_id="cs_ops", owner="cs_lead"),
                Candidate(agent_id="finance_ops", owner="finance_lead"),
            ),
            opened_at=_NOW,
            case_id="test-case-001",
        )
        item = ManagerItem(
            manager_id=manager_id,
            source=FromDeadlock(case=case, reason="표 갈림"),
            created_at=_NOW,
            item_id="mgr-item-001",
        )
        queue_store.enqueue(item)
        return item

    def test_root_manager_로그인_자기_queue_200(self) -> None:
        queue_store = InMemoryManagerQueueStore()
        self._make_manager_item(queue_store)
        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            manager_queue_store=queue_store,
        )
        client = TestClient(app)
        _login(client, "root_manager")
        r = _get(client, "/manager/queue")
        assert r.status == 200
        items: list[Any] = r.body
        assert len(items) == 1

    def test_manager_queue는_자기_큐만_본다(self) -> None:
        """cs_lead로 로그인하면 cs_lead의 manager_id 큐만 (root_manager 큐 안 보임)."""
        queue_store = InMemoryManagerQueueStore()
        self._make_manager_item(queue_store, manager_id="root_manager")
        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            manager_queue_store=queue_store,
        )
        client = TestClient(app)
        _login(client, "cs_lead")  # cs_lead는 manager_id가 "root_manager"가 아님
        r = _get(client, "/manager/queue")
        assert r.status == 200
        # cs_lead의 manager_id는 root_manager가 아니라 cs_lead 자신이 큐 없음
        items: list[Any] = r.body
        assert items == []

    def test_root_manager_act_자기_item_dismiss_200(self) -> None:
        queue_store = InMemoryManagerQueueStore()
        self._make_manager_item(queue_store)
        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            manager_queue_store=queue_store,
        )
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(
            client,
            "/manager/items/mgr-item-001/act",
            {"type": "dismiss", "rationale": "중복"},
        )
        assert r.status == 200

    def test_cs_lead로_root_manager_item_act_403(self) -> None:
        """세션이 cs_lead인데 root_manager 소유 item을 처분하면 403."""
        queue_store = InMemoryManagerQueueStore()
        self._make_manager_item(queue_store, manager_id="root_manager")
        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            manager_queue_store=queue_store,
        )
        client = TestClient(app)
        _login(client, "cs_lead")
        r = _post(
            client,
            "/manager/items/mgr-item-001/act",
            {"type": "dismiss", "rationale": "시도"},
        )
        assert r.status == 403

    def test_manager_act_body에_by_manager_없어도_동작(self) -> None:
        """세션화 후: body에 by_manager 없이 type만 보내도 동작."""
        queue_store = InMemoryManagerQueueStore()
        self._make_manager_item(queue_store)
        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            manager_queue_store=queue_store,
        )
        client = TestClient(app)
        _login(client, "root_manager")
        r = _post(
            client,
            "/manager/items/mgr-item-001/act",
            {"type": "dismiss"},  # by_manager 없음
        )
        assert r.status == 200

    def test_미존재_item_act_404(self) -> None:
        client = TestClient(_auth_app_with_queue())
        _login(client, "root_manager")
        r = _post(
            client,
            "/manager/items/없는아이템/act",
            {"type": "dismiss"},
        )
        assert r.status == 404


# ════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — 모니터링 인증 게이트
# ════════════════════════════════════════════════════════════════════════════


class TestSlice2_Monitor:
    def test_미로그인_monitor_list는_401(self) -> None:
        client = TestClient(_auth_app())
        assert _get(client, "/monitor").status == 401

    def test_로그인_후_monitor_list_200(self) -> None:
        from agent_org_network.audit import InMemoryAuditLog

        app = create_app(
            runtime=StubRuntime(),
            session_secret=_SECRET,
            audit_log=InMemoryAuditLog(),
        )
        client = TestClient(app)
        _login(client, "cs_lead")
        r = _get(client, "/monitor")
        assert r.status == 200
