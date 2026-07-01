"""owner 로컬 검토 웹(`owner_web`) 결정론 테스트 — 실 uvicorn·실 WS 없이(T9.7 S4).

owner 초안 검토 면의 라우트 로직만 잠근다: TestClient(인프로세스 ASGI) + `WorkerLogic`에
보류 초안을 시드 + fake sink로 처분 도달을 관측. 실 소켓·실 uvicorn·실 claude는 게이트 밖
(수동 시연 영역)이라 여기 들어오지 않는다(ADR 0011 결정 6-6·ADR 0025 결정 4 정신).

시드: `WorkerLogic.handle_push_work`에 `hitl=True` PushWork를 흘리면 초안이 즉시 회신되지
않고 `_pending_drafts`에 담긴다(S2 자산). 그 워커의 *같은* 인스턴스를 `create_owner_app`에
넘겨 조회·처분한다. runtime은 FakeRunner를 박은 `ClaudeCodeRuntime`(실 claude 0·결정론).
"""

from datetime import datetime, timezone
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.owner_web import create_owner_app, serialize_pending_draft
from agent_org_network.runtime import Answer, ClaudeCodeRuntime
from agent_org_network.transport import PushWork, SubmitAnswer, TicketFrame
from agent_org_network.worker import PendingDraft, WorkerLogic

BASE_TS = datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)


class _RecordingRunner:
    """고정 응답 실 claude 대역(FakeRunner) — 실 subprocess 0."""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def __call__(
        self, prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> str:
        return self.reply


def _card(agent_id: str = "cs_ops", owner: str = "cs_lead") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="환불 정책과 처리 절차를 안내합니다.",
        domains=["환불"],
        last_reviewed_at=BASE_TS.date(),
    )


def _logic(reply: str = "네, 7일 이내 환불됩니다.") -> WorkerLogic:
    card = _card()
    return WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=_RecordingRunner(reply)),
        clock=lambda: BASE_TS,
    )


def _seed_pending(logic: WorkerLogic, ticket_id: str, question: str = "환불 되나요?") -> None:
    """HITL on PushWork를 흘려 초안을 보류 store에 시드한다(S2 자산 경유)."""
    push = PushWork(
        ticket=TicketFrame(
            ticket_id=ticket_id,
            agent_id="cs_ops",
            question=question,
            enqueued_at=BASE_TS,
            hitl=True,
        )
    )
    submit = logic.handle_push_work(push)
    assert submit is None  # HITL on — 즉시 회신 안 하고 보류.


class _FakeSink:
    """처분된 SubmitAnswer를 관측하는 fake sink(실 WS 대역)."""

    def __init__(self) -> None:
        self.received: list[SubmitAnswer] = []

    def __call__(self, submit: SubmitAnswer) -> None:
        self.received.append(submit)


def _app(logic: WorkerLogic, sink: _FakeSink) -> TestClient:
    return TestClient(create_owner_app(logic, submit_sink=sink))


# pyright strict: starlette TestClient는 httpx 반환을 Unknown으로 노출한다(httpx
# deprecation 스텁). 호출부로 unknown이 새지 않게 명시 `Response`로 좁혀 받는다
# (test_web.py `_get`/`_post` 패턴).


def _get(client: TestClient, url: str) -> Response:
    http: Any = client
    return cast(Response, http.get(url))


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> Response:
    http: Any = client
    return cast(Response, http.post(url, json=payload))


# ── serialize_pending_draft(순수 경계) ──────────────────────────────────────


def test_serialize_pending_draft가_검토_화면_dict로_변환한다():
    draft = PendingDraft(
        ticket_id="tkt-1",
        question="환불 되나요?",
        draft_answer=Answer(text="네 가능합니다.", sources=("정책 v2",), mode="draft_only"),
        agent_id="cs_ops",
        context="이전 대화",
        made_at=BASE_TS,
    )
    d = serialize_pending_draft(draft)
    assert d == {
        "ticket_id": "tkt-1",
        "question": "환불 되나요?",
        "agent_id": "cs_ops",
        "draft_answer": "네 가능합니다.",
        "sources": ["정책 v2"],
        "mode": "draft_only",
        "made_at": BASE_TS.isoformat(),
    }
    # context(발화 스레드)는 검토 화면에 새지 않는다 — 검토 대상은 만들어진 답이다.
    assert "context" not in d


# ── GET /drafts (목록) ──────────────────────────────────────────────────────


def test_빈_목록():
    logic = _logic()
    client = _app(logic, _FakeSink())
    res = _get(client, "/drafts")
    assert res.status_code == 200
    assert res.json() == []


def test_보류_초안_목록을_돌려준다():
    logic = _logic(reply="네, 7일 이내 환불됩니다.")
    _seed_pending(logic, "tkt-a", question="환불 되나요?")
    _seed_pending(logic, "tkt-b", question="교환 되나요?")
    client = _app(logic, _FakeSink())

    res = _get(client, "/drafts")
    assert res.status_code == 200
    body = res.json()
    assert [d["ticket_id"] for d in body] == ["tkt-a", "tkt-b"]  # 삽입 순서
    assert body[0]["question"] == "환불 되나요?"
    assert body[0]["draft_answer"] == "네, 7일 이내 환불됩니다."
    assert body[0]["agent_id"] == "cs_ops"


# ── GET /drafts/{ticket_id} (상세·404) ──────────────────────────────────────


def test_상세_조회():
    logic = _logic(reply="7일 이내 가능")
    _seed_pending(logic, "tkt-x")
    client = _app(logic, _FakeSink())

    res = _get(client, "/drafts/tkt-x")
    assert res.status_code == 200
    assert res.json()["ticket_id"] == "tkt-x"
    assert res.json()["draft_answer"] == "7일 이내 가능"


def test_미존재_상세는_404():
    client = _app(_logic(), _FakeSink())
    res = _get(client, "/drafts/없는거")
    assert res.status_code == 404


# ── POST /drafts/{ticket_id}/submit (승인·수정·404) ─────────────────────────


def test_승인_전송_원문_그대로_sink에_도달하고_보류_제거():
    logic = _logic(reply="네, 7일 이내 환불됩니다.")
    _seed_pending(logic, "tkt-approve")
    sink = _FakeSink()
    client = _app(logic, sink)

    res = _post(client, "/drafts/tkt-approve/submit", {"edited_text": None})
    assert res.status_code == 200
    assert res.json() == {"ticket_id": "tkt-approve", "submitted": True}
    # sink에 원문 그대로 도달.
    assert len(sink.received) == 1
    submit = sink.received[0]
    assert isinstance(submit, SubmitAnswer)
    assert submit.ticket_id == "tkt-approve"
    assert submit.answer.text == "네, 7일 이내 환불됩니다."
    # 보류 store에서 제거됨(전이 ≠ 기록).
    assert logic.pending_draft("tkt-approve") is None
    assert logic.pending_drafts() == []


def test_edited_text_생략시에도_승인으로_처리된다():
    # body에 edited_text를 아예 안 실어도 기본 None → 승인(원문 그대로).
    logic = _logic(reply="원문 답")
    _seed_pending(logic, "tkt-noedit")
    sink = _FakeSink()
    client = _app(logic, sink)

    res = _post(client, "/drafts/tkt-noedit/submit", {})
    assert res.status_code == 200
    assert sink.received[0].answer.text == "원문 답"


def test_수정_전송_edited_text가_반영된다():
    logic = _logic(reply="원래 초안")
    _seed_pending(logic, "tkt-edit")
    sink = _FakeSink()
    client = _app(logic, sink)

    res = _post(client, "/drafts/tkt-edit/submit", {"edited_text": "고쳐 쓴 답입니다."})
    assert res.status_code == 200
    assert len(sink.received) == 1
    assert sink.received[0].ticket_id == "tkt-edit"
    assert sink.received[0].answer.text == "고쳐 쓴 답입니다."
    assert logic.pending_draft("tkt-edit") is None


def test_미존재_submit은_404_이고_sink_무발화():
    sink = _FakeSink()
    client = _app(_logic(), sink)
    res = _post(client, "/drafts/없는거/submit", {"edited_text": None})
    assert res.status_code == 404
    assert sink.received == []


# ── GET / (정적 HTML) ───────────────────────────────────────────────────────


def test_루트는_검토_HTML을_서빙한다():
    client = _app(_logic(), _FakeSink())
    res = _get(client, "/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "초안 검토" in res.text


# ── run_worker 겸직 배선 무회귀(import·시그니처 수준·게이트 밖 실행 아님) ──────


def test_run_worker_outbound는_옵셔널이라_기존_호출_무변경():
    # env 미설정(outbound 미주입) 시 기존 run_worker 경로가 100% 무변경임을 시그니처
    # 수준에서 잠근다 — 실 소켓·실 uvicorn을 띄우지 않는다(게이트 밖). outbound 파라미터
    # 기본값이 None(하위호환)이어야 한다.
    import inspect

    from agent_org_network import worker

    sig = inspect.signature(worker.run_worker)
    assert sig.parameters["outbound"].default is None
