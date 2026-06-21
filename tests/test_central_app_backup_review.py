"""T6.6 슬라이스 iv — create_central_app 검토·위임 와이어링 결정론 테스트.

ADR 0012 결정 2·4·7·9. `create_central_app`이 `BackupReviewStore`·`BackupReviewService`를
*하나씩* 만들어 디스패처(생성 트리거)·web(검토 탭·retrieve 덧씌움)에 **같은 인스턴스**로
주입하고, 데모 owner들의 위임 스냅샷을 등록하는지 — 그 결과 backup 답이 한 앱에서
"답함→처리함에 뜸→owner 검토→재회수에 반영"으로 한 바퀴 도는지 검증한다.

결정론 경계(ADR 0011 결정 6-6): Fake backup 워커 = TestClient WS 세션(실 네트워크·실
claude·별 프로세스 0). 송신/수신 루프 왕복은 submit 뒤 heartbeat 한 번으로 보장한다
(test_server.py 중복 submit 테스트와 같은 기법). 위임 staleness는 demo_delegations가
utcnow 기준 fresh를 만들고 임계가 30일이라 실행 시점과 무관하게 안정적이다.

`demo_delegations` 자체(위임 메타 구성)도 단위로 본다 — owner별 카드 묶음·snapshot_at fresh.
실 WS·실 claude·실 프로세스·실 동기화 파이프라인은 수동 시연/후속(게이트 밖).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, cast

from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from agent_org_network.demo import demo_delegations


def _client() -> TestClient:
    from agent_org_network.server import create_central_app

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
    # 데모 카드 3 owner가 각각 위임을 갖는다.
    assert owners == {"legal_lead", "cs_lead", "finance_lead"}


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


# ── create_central_app: 검토 store/service 같은 인스턴스 공유(end-to-end) ─────


def test_backup_답이_처리함_검토탭에_뜬다() -> None:
    """backup 워커가 답하면 그 owner 처리함 백업 검토 탭에 미검토 항목이 뜬다.

    디스패처(생성 트리거)와 web(검토 탭)이 같은 review_store를 봐야 성립 — create_central_app
    와이어링 검증(결정 7-1).
    """
    client = _client()
    with _ws(client) as conn:
        conn.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "backup"})
        assert _recv(conn)["type"] == "welcome"

        r = _http_post(client, "/ask", {"question": "환불 규정 알려줘"})
        assert r.status_code == 200
        push = _recv(conn)
        assert push["type"] == "push_work"
        ticket_id = push["ticket"]["ticket_id"]

        conn.send_json(
            {
                "type": "submit_answer",
                "ticket_id": ticket_id,
                "answer": {"text": "백업이 만든 환불 답", "sources": [], "mode": "full"},
            }
        )
        conn.send_json({"type": "heartbeat"})  # 송신/수신 루프 왕복 보장

    reviews = _http_get(client, "/inbox/cs_lead/backup-reviews").json()
    assert len(reviews) == 1
    assert reviews[0]["owner_id"] == "cs_lead"
    assert reviews[0]["question"] == "환불 규정 알려줘"
    assert reviews[0]["backup_answer_text"] == "백업이 만든 환불 답"
    assert reviews[0]["status"] == "pending_review"


def test_backup_답_회수는_mode_backup으로_하향된다() -> None:
    """backup 워커가 full로 답해도 디스패처가 연결 등급을 진실로 mode=backup 강제(결정 4)."""
    client = _client()
    with _ws(client) as conn:
        conn.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "backup"})
        assert _recv(conn)["type"] == "welcome"
        tracking = _http_post(client, "/ask", {"question": "환불 규정 알려줘"}).json()["tracking"]
        push = _recv(conn)
        conn.send_json(
            {
                "type": "submit_answer",
                "ticket_id": push["ticket"]["ticket_id"],
                "answer": {"text": "백업 답", "sources": [], "mode": "full"},
            }
        )
        conn.send_json({"type": "heartbeat"})

    ans = _http_get(client, f"/ask/{tracking}").json()
    assert ans["type"] == "answered"
    assert ans["mode"] == "backup"
    assert ans["text"] == "백업 답"
    # 담당은 여전히 owner(책임 불변, 결정 5).
    assert ans["answered_by"] == {"owner": "cs_lead", "agent_id": "cs_ops"}


def test_정정_검토_후_재회수에_반영된다() -> None:
    """owner가 Correct하면 retrieve가 정정 text·mode=full을 돌려준다(결정 7-3).

    디스패처·web·ask가 모두 같은 review_store를 봐야 retrieve 덧씌움이 작동 — create_central_app
    와이어링의 핵심(code-reviewer [Major 1]이 web 경로에서 닫은 것을 통합 앱에서 재확인).
    """
    client = _client()
    with _ws(client) as conn:
        conn.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "backup"})
        assert _recv(conn)["type"] == "welcome"
        tracking = _http_post(client, "/ask", {"question": "환불 규정 알려줘"}).json()["tracking"]
        push = _recv(conn)
        conn.send_json(
            {
                "type": "submit_answer",
                "ticket_id": push["ticket"]["ticket_id"],
                "answer": {"text": "백업 답", "sources": [], "mode": "full"},
            }
        )
        conn.send_json({"type": "heartbeat"})

    item_id = _http_get(client, "/inbox/cs_lead/backup-reviews").json()[0]["item_id"]
    pr = _http_post(
        client,
        f"/backup-reviews/{item_id}",
        {"type": "correct", "by_owner": "cs_lead", "corrected_text": "정정된 환불 안내"},
    )
    assert pr.status_code == 200
    assert pr.json()["status"] == "reviewed"

    ans = _http_get(client, f"/ask/{tracking}").json()
    assert ans["type"] == "answered"
    assert ans["mode"] == "full"
    assert ans["text"] == "정정된 환불 안내"
    assert ans["answered_by"] == {"owner": "cs_lead", "agent_id": "cs_ops"}


def test_검토_후_처리함에서_사라진다() -> None:
    """검토 완료 항목은 pending 목록에서 빠진다(pending_for_owner = 미검토만)."""
    client = _client()
    with _ws(client) as conn:
        conn.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "backup"})
        assert _recv(conn)["type"] == "welcome"
        _http_post(client, "/ask", {"question": "환불 규정 알려줘"})
        push = _recv(conn)
        conn.send_json(
            {
                "type": "submit_answer",
                "ticket_id": push["ticket"]["ticket_id"],
                "answer": {"text": "백업 답", "sources": [], "mode": "full"},
            }
        )
        conn.send_json({"type": "heartbeat"})

    item_id = _http_get(client, "/inbox/cs_lead/backup-reviews").json()[0]["item_id"]
    _http_post(client, f"/backup-reviews/{item_id}", {"type": "approve", "by_owner": "cs_lead"})

    after = _http_get(client, "/inbox/cs_lead/backup-reviews").json()
    assert after == []


def test_primary_워커_답은_검토_항목을_만들지_않는다() -> None:
    """primary 답은 owner 실시간이라 검토 불요 — 처리함에 안 뜬다(결정 7-1)."""
    client = _client()
    with _ws(client) as conn:
        conn.send_json({"type": "register_worker", "owner_id": "cs_lead", "role": "primary"})
        assert _recv(conn)["type"] == "welcome"
        _http_post(client, "/ask", {"question": "환불 규정 알려줘"})
        push = _recv(conn)
        conn.send_json(
            {
                "type": "submit_answer",
                "ticket_id": push["ticket"]["ticket_id"],
                "answer": {"text": "실시간 답", "sources": [], "mode": "full"},
            }
        )
        conn.send_json({"type": "heartbeat"})

    reviews = _http_get(client, "/inbox/cs_lead/backup-reviews").json()
    assert reviews == []
