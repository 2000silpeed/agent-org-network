"""Owner Scorecard 웹 라우트 결정론 테스트 (Phase 13 SC3·ADR 0035·계획 §11 SC3).

실 uvicorn·실 LLM 없이 인프로세스 ASGI TestClient로 잠근다:
  - `GET /supervision/scorecard?owner_id=&days=` — 담당자 자기 성적(현재 윈도 + 자기 추세).
  - `GET /admin/scorecards?days=` — 운영자 전체 뷰(owner_id 알파벳순 고정 — 순위표 아님).

핵심 회귀 방지(ADR 0035 결정 2): 정렬/랭킹 쿼리 파라미터가 없음을 고정하는 테스트 1개.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.answer_record import (
    AnswerFeedback,
    AnswerRecord,
    CorrectionEvent,
    InMemoryAnswerRecordStore,
    InMemoryCorrectionStore,
    InMemoryFeedbackStore,
)
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app

_SECRET = "test-secret"


def _get(client: TestClient, url: str, **kwargs: Any) -> Response:
    http: Any = client
    return cast(Response, http.get(url, **kwargs))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(url, json=payload))


def _json(res: Response) -> dict[str, Any]:
    return cast(dict[str, Any], res.json())


def _list(res: Response) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], res.json())


def _login(client: TestClient, user_id: str) -> None:
    status = _post(client, "/login", {"user_id": user_id}).status_code
    assert status == 200, f"로그인 실패: {user_id}"


def _demo_owner(agent_id: str) -> str:
    from agent_org_network.demo import build_demo

    return build_demo(runtime=StubRuntime()).registry.get(agent_id).owner


# ── 자기 성적 조회 ────────────────────────────────────────────────────────


def test_자기_성적_조회_4축_값() -> None:
    ars = InMemoryAnswerRecordStore()
    fs = InMemoryFeedbackStore()
    cs = InMemoryCorrectionStore()
    ks = InMemoryKnowledgeStore()
    app = create_app(
        runtime=StubRuntime(),
        answer_record_store=ars,
        feedback_store=fs,
        correction_store=cs,
        knowledge_store=ks,
    )
    client = TestClient(app)
    owner = _demo_owner("cs_ops")
    now = datetime.now(UTC)

    ars.add(
        AnswerRecord(
            record_id="r1",
            question="환불 되나요?",
            answer_text="환불 가능합니다",
            answered_by=owner,
            agent_id="cs_ops",
            mode="full",
            session_id=None,
            answered_at=now - timedelta(days=1),
        )
    )
    fs.upsert(
        AnswerFeedback(
            record_id="r1", verdict="bad", submitted_by="q1", submitted_at=now - timedelta(days=1)
        )
    )

    res = _get(client, "/supervision/scorecard", params={"owner_id": owner})
    assert res.status_code == 200
    body = _json(res)
    assert body["owner_id"] == owner
    assert body["quality"]["total_answers"] == 1
    assert body["quality"]["bad_feedback_answers"] == 1
    assert body["quality"]["bad_feedback_rate"] == 1.0
    assert "needs_review_total" in body["supervision"]
    assert "online_ratio" in body["availability"]
    assert "stale_ratio" in body["freshness"]
    assert body["weak_identity_note"] is True


def test_days_파라미터가_윈도에_반영된다() -> None:
    ars = InMemoryAnswerRecordStore()
    app = create_app(runtime=StubRuntime(), answer_record_store=ars)
    client = TestClient(app)
    owner = _demo_owner("cs_ops")
    now = datetime.now(UTC)

    # 40일 전 답변 — 기본 30일 윈도에는 안 잡히지만 days=60이면 잡힌다.
    ars.add(
        AnswerRecord(
            record_id="old",
            question="예전 질문",
            answer_text="예전 답",
            answered_by=owner,
            agent_id="cs_ops",
            mode="full",
            session_id=None,
            answered_at=now - timedelta(days=40),
        )
    )

    default_body = _json(_get(client, "/supervision/scorecard", params={"owner_id": owner}))
    assert default_body["quality"]["total_answers"] == 0

    wide_body = _json(
        _get(client, "/supervision/scorecard", params={"owner_id": owner, "days": 60})
    )
    assert wide_body["quality"]["total_answers"] == 1
    # 윈도 길이가 실제로 바뀌었는지 since/until 폭으로 확인.
    since = datetime.fromisoformat(wide_body["window"]["since"])
    until = datetime.fromisoformat(wide_body["window"]["until"])
    assert (until - since).days == 60


def test_trend_계산이_반영된다() -> None:
    """정정만 많고 bad 피드백 0인 담당자 — Goodhart 축 분리(ADR 0035 결정 1)가 trend에도 유지."""
    ars = InMemoryAnswerRecordStore()
    cs = InMemoryCorrectionStore()
    app = create_app(runtime=StubRuntime(), answer_record_store=ars, correction_store=cs)
    client = TestClient(app)
    owner = _demo_owner("cs_ops")
    now = datetime.now(UTC)

    # 이전 기간(31~60일 전)엔 답 없음, 현재 기간(0~30일 전)엔 답 1건 → total_answers 델타 = +1.
    ars.add(
        AnswerRecord(
            record_id="r1",
            question="q",
            answer_text="a",
            answered_by=owner,
            agent_id="cs_ops",
            mode="full",
            session_id=None,
            answered_at=now - timedelta(days=1),
            needs_correction_review=True,
        )
    )
    cs.append(
        CorrectionEvent(
            event_id="e1",
            record_id="r1",
            by_owner=owner,
            corrected_text="정정본",
            rationale="",
            corrected_at=now - timedelta(hours=1),
        )
    )

    body = _json(_get(client, "/supervision/scorecard", params={"owner_id": owner}))
    trend = body["trend"]
    assert trend is not None
    assert trend["quality_total_answers_delta"] == 1
    # 정정이 있어도 bad 피드백은 0이라 품질 벌점 델타는 0(Goodhart 분리).
    assert trend["quality_bad_feedback_rate_delta"] == 0.0
    # 감독 성실도(처리율)는 정정으로 인해 개선(0 → 1.0) 델타.
    assert trend["supervision_handled_rate_delta"] == 1.0


def test_none_축은_데이터_없음으로_정직_표기() -> None:
    """presence_log 미배선 → online_ratio=None(0으로 위장 안 함)."""
    app = create_app(runtime=StubRuntime())
    client = TestClient(app)
    owner = _demo_owner("cs_ops")

    body = _json(_get(client, "/supervision/scorecard", params={"owner_id": owner}))
    assert body["availability"]["online_ratio"] is None
    assert body["supervision"]["median_handle_seconds"] is None


# ── 운영자 전체 뷰 ────────────────────────────────────────────────────────


def test_admin_scorecards_알파벳순_고정() -> None:
    app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
    client = TestClient(app)
    _login(client, "root_manager")

    body = _list(_get(client, "/admin/scorecards"))
    owner_ids = [item["owner_id"] for item in body]
    assert owner_ids == sorted(owner_ids)
    # 데모 owner 5명이 모두 포함(정렬 결정론 확인).
    assert owner_ids == ["cs_lead", "finance_lead", "hr_lead", "it_lead", "legal_lead"]


def test_admin_scorecards_미로그인_401() -> None:
    app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
    client = TestClient(app)
    res = _get(client, "/admin/scorecards")
    assert res.status_code == 401


def test_admin_scorecards_noauth_모드는_로그인_불요() -> None:
    app = create_app(runtime=StubRuntime())
    client = TestClient(app)
    res = _get(client, "/admin/scorecards")
    assert res.status_code == 200


def test_admin_scorecards_days_파라미터_반영() -> None:
    ars = InMemoryAnswerRecordStore()
    app = create_app(runtime=StubRuntime(), answer_record_store=ars)
    client = TestClient(app)

    body = _list(_get(client, "/admin/scorecards", params={"days": 7}))
    assert len(body) == 5
    for item in body:
        since = datetime.fromisoformat(item["window"]["since"])
        until = datetime.fromisoformat(item["window"]["until"])
        assert (until - since).days == 7


# ── 회귀 방지: 랭킹/정렬 파라미터 없음 고정 (ADR 0035 결정 2) ──────────────


def test_admin_scorecards에_정렬_랭킹_파라미터가_없다() -> None:
    """`sort`·`order_by` 등 임의 쿼리를 보내도 무시되고 여전히 owner_id 알파벳순이다.

    ADR 0035 결정 2(순위표 금지) 회귀 방지 — 이 라우트 시그니처엔애초에 정렬 파라미터가
    없으므로 어떤 정렬 쿼리를 보내도 결과 순서는 절대 바뀌지 않는다.
    """
    app = create_app(runtime=StubRuntime())
    client = TestClient(app)

    baseline = _list(_get(client, "/admin/scorecards"))
    with_sort_query = _list(
        _get(
            client,
            "/admin/scorecards",
            params={"sort": "quality.bad_feedback_rate", "order": "desc", "rank": "true"},
        )
    )
    baseline_ids = [item["owner_id"] for item in baseline]
    sorted_query_ids = [item["owner_id"] for item in with_sort_query]
    assert baseline_ids == sorted_query_ids == sorted(baseline_ids)


# ── 신원 스코핑(auth 모드) ───────────────────────────────────────────────


def test_auth_모드에서_세션_신원으로_스코핑된다() -> None:
    """세션 신원이 owner_id 쿼리를 덮어쓴다 — 남의 owner_id를 쿼리로 보내도 무시."""
    ars = InMemoryAnswerRecordStore()
    app = create_app(
        runtime=StubRuntime(), session_secret=_SECRET, answer_record_store=ars
    )
    client = TestClient(app)
    cs_owner = _demo_owner("cs_ops")
    now = datetime.now(UTC)
    ars.add(
        AnswerRecord(
            record_id="r1",
            question="q",
            answer_text="a",
            answered_by=cs_owner,
            agent_id="cs_ops",
            mode="full",
            session_id=None,
            answered_at=now - timedelta(days=1),
        )
    )

    _login(client, "hr_lead")
    # 쿼리로 cs_lead(남의 owner_id)를 보내도 세션 신원(hr_lead)으로 스코핑된다.
    body = _json(
        _get(client, "/supervision/scorecard", params={"owner_id": cs_owner})
    )
    assert body["owner_id"] == "hr_lead"
    assert body["quality"]["total_answers"] == 0


def test_auth_모드에서_미로그인은_401() -> None:
    app = create_app(runtime=StubRuntime(), session_secret=_SECRET)
    client = TestClient(app)
    res = _get(client, "/supervision/scorecard", params={"owner_id": "cs_lead"})
    assert res.status_code == 401
