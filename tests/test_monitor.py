"""T5.1 운영 모니터링 — AuditReader + web 라우트 결정론 테스트.

red → green 순서:
1. InMemoryAuditLog.records() / record_at()
2. JsonlAuditLog.records() / record_at()
3. 균일 계약(InMemory == Jsonl 같은 dict 모양)
4. web 라우트(create_app audit_log 주입, GET /monitor, GET /monitor/{index})
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.audit import AuditEntry, InMemoryAuditLog, JsonlAuditLog
from agent_org_network.decision import Routed, Unowned
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_FIXED_DT = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


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


def _dummy_ticket(owner_id: str) -> Any:
    """Delivered 생성에 필요한 최소 WorkTicket 스텁."""
    from agent_org_network.dispatch import WorkTicket

    return WorkTicket(
        ticket_id="t-dummy",
        owner_id=owner_id,
        agent_id="contract_ops",
        question="q",
        enqueued_at=_FIXED_DT,
    )


def _routed_entry() -> AuditEntry:
    card = AgentCard(
        agent_id="contract_ops",
        owner="legal_lead",
        team="legal",
        summary="계약",
        domains=["계약 검토"],
        last_reviewed_at=date(2026, 6, 20),
    )
    decision = Routed(
        primary=card,
        confidence=1.0,
        reason="키워드 일치",
    )
    from agent_org_network.dispatch import Delivered
    from agent_org_network.runtime import Answer

    answer = Answer(text="계약 검토 안내입니다", sources=("위키/계약가이드",), mode="full")
    dispatch_outcome = Delivered(answer=answer, ticket=_dummy_ticket("legal_lead"))
    return AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u1",
        question="이 계약 조건 바꿔도 돼?",
        intent="계약 검토",
        decision=decision,
        dispatch_outcome=dispatch_outcome,
    )


def _unowned_entry() -> AuditEntry:
    decision = Unowned(escalated_to="root", reason="매핑 없음")
    return AuditEntry(
        timestamp=_FIXED_DT,
        user_id="u2",
        question="주차장 정기권 어떻게 갱신해요?",
        intent="주차장",
        decision=decision,
    )


# ── InMemoryAuditLog 읽기 ────────────────────────────────────────────────────


def test_InMemoryAuditLog_빈_records는_빈_리스트() -> None:
    log = InMemoryAuditLog()
    assert log.records() == []


def test_InMemoryAuditLog_2건_기록_records_2개_순서_보존() -> None:
    log = InMemoryAuditLog()
    e0 = _routed_entry()
    e1 = _unowned_entry()
    log.record(e0)
    log.record(e1)

    result = log.records()
    assert len(result) == 2
    assert result[0] == e0.as_record()
    assert result[1] == e1.as_record()


def test_InMemoryAuditLog_record_at_정상_인덱스() -> None:
    log = InMemoryAuditLog()
    e0 = _routed_entry()
    e1 = _unowned_entry()
    log.record(e0)
    log.record(e1)

    assert log.record_at(0) == e0.as_record()
    assert log.record_at(1) == e1.as_record()


def test_InMemoryAuditLog_record_at_범위_밖은_None() -> None:
    log = InMemoryAuditLog()
    log.record(_routed_entry())
    log.record(_unowned_entry())

    assert log.record_at(2) is None


def test_InMemoryAuditLog_record_at_음수는_None() -> None:
    log = InMemoryAuditLog()
    log.record(_routed_entry())

    assert log.record_at(-1) is None


# ── JsonlAuditLog 읽기 ───────────────────────────────────────────────────────


def test_JsonlAuditLog_파일_없으면_records_빈_리스트(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    assert log.records() == []


def test_JsonlAuditLog_파일_없으면_record_at_0은_None(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    assert log.record_at(0) is None


def test_JsonlAuditLog_2건_record_후_records_2개_json_왕복(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    e0 = _routed_entry()
    e1 = _unowned_entry()
    log.record(e0)
    log.record(e1)

    result = log.records()
    assert len(result) == 2
    assert result[0]["user_id"] == "u1"
    assert result[0]["decision"]["disposition"] == "routed"
    assert result[1]["user_id"] == "u2"
    assert result[1]["decision"]["disposition"] == "unowned"


def test_JsonlAuditLog_record_at_1은_두번째_항목(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.record(_routed_entry())
    log.record(_unowned_entry())

    r = log.record_at(1)
    assert r is not None
    assert r["decision"]["disposition"] == "unowned"


def test_JsonlAuditLog_record_at_범위_밖은_None(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.record(_routed_entry())
    log.record(_unowned_entry())

    assert log.record_at(2) is None


def test_JsonlAuditLog_record_at_음수는_None(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.record(_routed_entry())

    assert log.record_at(-1) is None


# ── 균일 계약 — InMemory와 Jsonl이 같은 dict 모양 ────────────────────────────


def test_균일_계약_InMemory와_Jsonl_records_0이_같다(tmp_path: Path) -> None:
    """같은 AuditEntry를 InMemory·Jsonl 양쪽에 record → records()[0]이 동일 dict."""
    e = _routed_entry()

    mem = InMemoryAuditLog()
    mem.record(e)
    mem_record = mem.records()[0]

    jsonl = JsonlAuditLog(tmp_path / "audit.jsonl")
    jsonl.record(e)
    jsonl_record = jsonl.records()[0]

    assert mem_record == jsonl_record


# ── web 라우트 결정론 테스트 ─────────────────────────────────────────────────


@pytest.fixture()
def client_and_audit() -> tuple[TestClient, InMemoryAuditLog]:  # pyright: ignore[reportUnknownParameterType]
    audit = InMemoryAuditLog()
    app: FastAPI = create_app(runtime=StubRuntime(), audit_log=audit)  # pyright: ignore[reportUnknownVariableType]
    return TestClient(app), audit  # pyright: ignore[reportUnknownVariableType]


def test_monitor_view_라우트가_200_HTML을_돌려준다(  # pyright: ignore[reportUnknownParameterType]
    client_and_audit: tuple[TestClient, InMemoryAuditLog],
) -> None:
    """/monitor/view → 200(HTML FileResponse). {index}에 'view'가 안 잡힌다."""
    client, _ = client_and_audit
    http: Any = client
    res = cast(Response, http.get("/monitor/view"))
    # HTML이라 JSON 파싱 없이 상태코드만 확인한다.
    assert res.status_code == 200


def test_monitor_초기_빈_목록(  # pyright: ignore[reportUnknownParameterType]
    client_and_audit: tuple[TestClient, InMemoryAuditLog],
) -> None:
    client, _ = client_and_audit
    res = _get(client, "/monitor")
    assert res.status == 200
    assert res.body == []


def test_monitor_POST_ask는_legacy_audit_목록을_쓰지_않는다(  # pyright: ignore[reportUnknownParameterType]
    client_and_audit: tuple[TestClient, InMemoryAuditLog],
) -> None:
    """P17 질문은 canonical 저장소를 쓰며 legacy Audit Monitor에 이중 기록하지 않는다."""
    client, _ = client_and_audit
    asked = _post(client, "/ask", {"question": "이 계약 조건 바꿔도 돼?"})
    assert asked.status == 200
    assert asked.body["type"] == "answered"

    res = _get(client, "/monitor")
    assert asked.body["request_id"]
    assert asked.body["record_id"]
    assert res.body == []


def test_monitor_P17_ask뒤에도_legacy_detail은_404(  # pyright: ignore[reportUnknownParameterType]
    client_and_audit: tuple[TestClient, InMemoryAuditLog],
) -> None:
    """P17 질문은 legacy Audit index를 만들지 않으므로 임의 index 조회는 404다."""
    client, _ = client_and_audit
    _post(client, "/ask", {"question": "이 계약 조건 바꿔도 돼?"})

    res = _get(client, "/monitor/0")
    assert res.status == 404
    assert res.body == {"detail": "알 수 없는 로그 인덱스"}


def test_monitor_detail_범위_밖은_404(  # pyright: ignore[reportUnknownParameterType]
    client_and_audit: tuple[TestClient, InMemoryAuditLog],
) -> None:
    client, _ = client_and_audit
    res = _get(client, "/monitor/99")
    assert res.status == 404


def test_monitor_detail_음수는_404(  # pyright: ignore[reportUnknownParameterType]
    client_and_audit: tuple[TestClient, InMemoryAuditLog],
) -> None:
    client, _ = client_and_audit
    res = _get(client, "/monitor/-1")
    assert res.status == 404


def test_monitor_P17_질문_2건도_legacy_인덱스를_만들지_않는다(  # pyright: ignore[reportUnknownParameterType]
    client_and_audit: tuple[TestClient, InMemoryAuditLog],
) -> None:
    """Routed·Unowned 모두 Request-first 경로라 legacy Monitor에는 기록하지 않는다."""
    client, _ = client_and_audit

    # 1번: 라우팅 성공(계약)
    _post(client, "/ask", {"question": "계약서 검토해줘"})
    # 2번: Unowned(매핑 없는 질문)
    _post(client, "/ask", {"question": "주차장 정기권 어떻게 갱신해요?"})

    res = _get(client, "/monitor")
    assert res.body == []
