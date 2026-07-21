"""T6.6 legacy 검토·위임과 P17 중앙 앱 질문 경계의 결정론 테스트.

ADR 0012 결정 2·4·7·9. `create_central_app`이 `BackupReviewStore`·`BackupReviewService`를
*하나씩* 만들어 legacy 워커 운영 경계에 주입하고 데모 Owner의 위임 스냅샷을 등록한다.
P17.2c-2부터 사용자 `/ask*`는 이 WS 실행 경로를 타지 않는다. durable WorkTicket·lease·
재시작 복구가 없는 원격 질문 실행은 P17.9 전까지 사용자 표면에서 비활성이다.

결정론 경계(ADR 0011 결정 6-6): Fake backup 워커 = TestClient WS 세션(실 네트워크·실
claude·별 프로세스 0). 송신/수신 루프 왕복은 submit 뒤 heartbeat 한 번으로 보장한다
(test_server.py 중복 submit 테스트와 같은 기법). 위임 staleness는 demo_delegations가
utcnow 기준 fresh를 만들고 임계가 30일이라 실행 시점과 무관하게 안정적이다.

`demo_delegations` 자체(위임 메타 구성)도 단위로 본다 — owner별 카드 묶음·snapshot_at fresh.
실 WS·실 claude·실 프로세스·실 동기화 파이프라인은 수동 시연/후속(게이트 밖).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from agent_org_network.demo import demo_delegations


def _client() -> TestClient:
    from agent_org_network.server import create_central_app

    # 데모 모드(session_secret 미주입=인증 OFF): 레거시 path(/inbox/{owner_id}/...)가
    # 허용된다. 프로덕션(OPERATOR_SESSION_SECRET 주입) 인증 회귀는
    # tests/test_security_regression.py TestB1_CentralApp_인증_우회_차단에서 검증.
    return TestClient(create_central_app())


def _ws(client: TestClient) -> Any:
    http: Any = client
    return http.websocket_connect("/worker")


def _recv(conn: WebSocketTestSession) -> dict[str, Any]:
    return cast(dict[str, Any], conn.receive_json())


def _http_get(client: TestClient, url: str) -> Any:
    http: Any = client
    return http.get(url)


def _http_post(client: TestClient, url: str, payload: dict[str, Any]) -> Any:
    http: Any = client
    return http.post(url, json=payload)


# ── demo_delegations: 위임 메타 구성(단위, 결정 3·9) ─────────────────────────


def test_demo_delegations가_owner별_위임을_만든다() -> None:
    snaps = demo_delegations()
    owners = {s.owner_id for s in snaps}
    # 데모 카드 5장 owner가 각각 위임을 갖는다.
    assert owners == {"legal_lead", "cs_lead", "finance_lead", "hr_lead", "it_lead"}


def test_demo_delegations의_agent_ids가_그_owner의_카드다() -> None:
    by_owner = {s.owner_id: s for s in demo_delegations()}
    assert by_owner["cs_lead"].agent_ids == ("cs_ops",)
    assert by_owner["legal_lead"].agent_ids == ("contract_ops",)
    assert by_owner["finance_lead"].agent_ids == ("finance_ops",)


def test_demo_delegations의_snapshot_at은_기본_fresh다() -> None:
    # 미지정이면 utcnow 기준 — staleness 임계 안에 들어 backup push가 허용된다(결정 9).
    before = datetime.now(timezone.utc)
    snaps = demo_delegations()
    after = datetime.now(timezone.utc)
    for s in snaps:
        assert before - timedelta(seconds=5) <= s.snapshot_at <= after + timedelta(seconds=5)


def test_demo_delegations는_snapshot_at_주입을_받는다() -> None:
    # stale 거부 시연 등을 위해 과거 시각을 넘길 수 있다.
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    snaps = demo_delegations(snapshot_at=past)
    assert all(s.snapshot_at == past for s in snaps)


# ── create_central_app: P17 질문과 legacy WS 실행 경계 분리 ────────────────


@pytest.mark.parametrize("role", ["primary", "backup"])
def test_P17_사용자_질문은_연결된_legacy_worker로_dispatch하지_않는다(role: str) -> None:
    """연결된 Owner는 P17 승인을 기다리며 legacy WorkTicket을 만들지 않는다."""
    client = _client()
    with _ws(client) as conn:
        conn.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": role})
        assert _recv(conn)["type"] == "welcome"
        result = _http_post(client, "/ask", {"question": "환불 규정 알려줘"}).json()

    assert result["type"] == "pending"
    assert result["kind"] == "dispatched"
    assert result["state"] == "awaiting_approval"
    assert result["request_id"]
    # 별도 WS WorkTicket ID가 아니라 canonical Request ID 자체를 추적 손잡이로 쓴다.
    assert result["tracking"] == result["request_id"]
    assert "text" not in result
    assert "record_id" not in result
    assert _http_get(client, "/inbox/cs_lead/backup-reviews").json() == []
