"""T10.4.6 — on-demand 문서 fetch 결정론 테스트 (ADR 0028 §15 결정 A~F).

게이트 내 결정론 코어만 — 실 WS·실 워커·실 네트워크 0. 다섯 면을 잠근다:
  1. 프레임 DTO 왕복(FetchDocument·DocumentContent·request_id echo).
  2. 양 union 파싱 무회귀(parse_central_frame·_parse_worker_frame·미지 None).
  3. correlation(request_id 발급·매칭 resolve·타임아웃·완료 후 정리·중복 멱등·오프라인).
  4. 워커 읽기(handle_fetch_document: tmp OKF found·없는 파일 found=False·미소유 found=False).
  5. 권한 스코핑(web: 세션 owner=후보+agent_id=후보 통과·비후보 403·미존재 404).
"""

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.agent_card import AgentCard
from agent_org_network.registry import Registry
from agent_org_network.runtime import ClaudeCodeRuntime
from agent_org_network.server import (
    _parse_worker_frame,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.transport import (
    DocumentContent,
    FetchDocument,
    FetchResult,
    PushWork,
    RegisterWorker,
    TicketFrame,
    WebSocketDispatcher,
)
from agent_org_network.user import User
from agent_org_network.web import create_app
from agent_org_network.worker import WorkerLogic, parse_central_frame

BASE_TS = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


def _card(agent_id: str = "cs_ops", owner: str = "cs_lead") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="환불 정책과 처리 절차를 안내합니다.",
        domains=["환불"],
        last_reviewed_at=date(2026, 6, 20),
    )


# ════════════════════════════════════════════════════════════════════════════
# ① 프레임 DTO 왕복 (결정 A)
# ════════════════════════════════════════════════════════════════════════════


def test_FetchDocument_왕복이_필드를_보존한다():
    frame = FetchDocument(agent_id="cs_ops", concept_id="refund_policy", request_id="req-1")
    restored = FetchDocument.model_validate_json(frame.model_dump_json())
    assert restored.type == "fetch_document"
    assert restored.agent_id == "cs_ops"
    assert restored.concept_id == "refund_policy"
    assert restored.request_id == "req-1"


def test_DocumentContent_왕복이_본문과_found를_보존한다():
    frame = DocumentContent(request_id="req-1", found=True, content="# 환불 정책\n7일 이내")
    restored = DocumentContent.model_validate_json(frame.model_dump_json())
    assert restored.type == "document_content"
    assert restored.request_id == "req-1"
    assert restored.found is True
    assert restored.content == "# 환불 정책\n7일 이내"


def test_DocumentContent_content_기본값은_빈문자열():
    frame = DocumentContent(request_id="req-x", found=False)
    assert frame.content == ""


def test_request_id가_요청에서_응답으로_echo된다():
    fetch = FetchDocument(agent_id="cs_ops", concept_id="c1", request_id="corr-42")
    reply = DocumentContent(request_id=fetch.request_id, found=True, content="본문")
    assert reply.request_id == "corr-42"


# ════════════════════════════════════════════════════════════════════════════
# ② 양 union 파싱 무회귀 (결정 A)
# ════════════════════════════════════════════════════════════════════════════


def _dump(frame: Any) -> dict[str, Any]:
    return cast(dict[str, Any], frame.model_dump(mode="json"))


def test_parse_central_frame이_fetch_document를_복원한다():
    frame = FetchDocument(agent_id="cs_ops", concept_id="c1", request_id="r1")
    parsed = parse_central_frame(_dump(frame))
    assert isinstance(parsed, FetchDocument)
    assert parsed.concept_id == "c1"
    assert parsed.request_id == "r1"


def test_parse_central_frame_기존_4종_무회귀():
    from agent_org_network.transport import AuthError, Ping, Welcome

    assert parse_central_frame(_dump(Welcome())) is not None
    assert parse_central_frame(_dump(AuthError(reason="x"))) is not None
    assert parse_central_frame(_dump(Ping())) is not None
    push = PushWork(
        ticket=TicketFrame(
            ticket_id="t1", agent_id="cs_ops", question="q", enqueued_at=BASE_TS
        )
    )
    assert parse_central_frame(_dump(push)) is not None


def test_parse_central_frame_미지_type은_None():
    assert parse_central_frame({"type": "unknown_frame"}) is None
    # document_content는 *워커→중앙* 프레임이라 워커측 파서는 모른다(다운스트림 아님).
    assert parse_central_frame(_dump(DocumentContent(request_id="r", found=False))) is None


def test_parse_worker_frame이_document_content를_복원한다():
    frame = DocumentContent(request_id="r1", found=True, content="본문")
    parsed = _parse_worker_frame(_dump(frame))
    assert isinstance(parsed, DocumentContent)
    assert parsed.found is True
    assert parsed.content == "본문"


def test_parse_worker_frame_기존_5종_무회귀():
    from agent_org_network.transport import (
        Ack,
        AnswerFrame,
        Heartbeat,
        PublishIndex,
        SubmitAnswer,
    )
    from agent_org_network.knowledge_index import KnowledgeIndex

    assert _parse_worker_frame(_dump(RegisterWorker(owner_id="cs_lead"))) is not None
    submit = SubmitAnswer(ticket_id="t1", answer=AnswerFrame(text="a"))
    assert _parse_worker_frame(_dump(submit)) is not None
    idx = PublishIndex(
        index=KnowledgeIndex(
            agent_id="cs_ops", version="okf-1", generated_at=BASE_TS, concepts=()
        )
    )
    assert _parse_worker_frame(_dump(idx)) is not None
    assert _parse_worker_frame(_dump(Heartbeat())) is not None
    assert _parse_worker_frame(_dump(Ack(ticket_id="t1"))) is not None


def test_parse_worker_frame_미지_type은_None():
    assert _parse_worker_frame({"type": "unknown_frame"}) is None
    # fetch_document는 *중앙→워커* 프레임이라 중앙측 파서는 모른다(업스트림 아님).
    assert (
        _parse_worker_frame(
            _dump(FetchDocument(agent_id="a", concept_id="c", request_id="r"))
        )
        is None
    )


# ════════════════════════════════════════════════════════════════════════════
# ③ correlation (결정 B·C) — 발급·매칭·타임아웃·정리·멱등·오프라인
# ════════════════════════════════════════════════════════════════════════════


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def __call__(self, frame: Any) -> None:
        self.sent.append(frame)


def _registry_with_card(owner: str = "cs_lead", agent_id: str = "cs_ops") -> Registry:
    registry = Registry()
    registry.register_user(User(id=owner))
    registry.register(_card(agent_id=agent_id, owner=owner))
    return registry


def _dispatcher_with_worker(
    owner: str = "cs_lead", agent_id: str = "cs_ops"
) -> tuple[WebSocketDispatcher, _Recorder, str]:
    """워커가 연결된 디스패처를 만들고 (디스패처, send 기록, request_id 미정) 반환."""
    disp = WebSocketDispatcher(registry=_registry_with_card(owner, agent_id))
    rec = _Recorder()
    disp.register(RegisterWorker(owner_id=owner), rec)
    return disp, rec, ""


def test_fetch는_연결된_워커에_FetchDocument를_push한다():
    disp, rec, _ = _dispatcher_with_worker()

    # 별 스레드에서 fetch를 돌려 push만 확인(타임아웃 짧게 — resolve 안 함은 다음 테스트).
    import threading

    result: list[FetchResult] = []

    def run() -> None:
        result.append(disp.fetch_document("cs_ops", "refund_policy", timeout=0.2))

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=2.0)

    assert len(rec.sent) == 1
    frame = rec.sent[0]
    assert isinstance(frame, FetchDocument)
    assert frame.agent_id == "cs_ops"
    assert frame.concept_id == "refund_policy"
    assert frame.request_id  # 발급됨
    # resolve 안 했으니 타임아웃.
    assert result[0].status == "timeout"


def test_fetch_resolve가_매칭되어_본문을_돌려준다():
    disp, rec, _ = _dispatcher_with_worker()
    import threading

    result: list[FetchResult] = []

    def run() -> None:
        result.append(disp.fetch_document("cs_ops", "refund_policy", timeout=2.0))

    t = threading.Thread(target=run)
    t.start()
    # push가 나갈 때까지 잠깐 기다린 뒤 그 request_id로 resolve.
    import time

    for _ in range(100):
        if rec.sent:
            break
        time.sleep(0.01)
    req_id = cast(FetchDocument, rec.sent[0]).request_id
    disp.resolve_fetch(DocumentContent(request_id=req_id, found=True, content="# 본문"))
    t.join(timeout=2.0)

    assert result[0].status == "delivered"
    assert result[0].found is True
    assert result[0].content == "# 본문"


def test_fetch_미연결_워커면_offline():
    disp = WebSocketDispatcher(registry=_registry_with_card())
    # 워커 연결 없음 — push할 곳이 없다.
    result = disp.fetch_document("cs_ops", "c1", timeout=0.2)
    assert result.status == "offline"


def test_fetch_미등록_agent_id면_offline():
    disp = WebSocketDispatcher(registry=_registry_with_card())
    rec = _Recorder()
    disp.register(RegisterWorker(owner_id="cs_lead"), rec)
    # registry에 없는 agent_id — owner 라우팅 불가.
    result = disp.fetch_document("ghost_ops", "c1", timeout=0.2)
    assert result.status == "offline"
    assert rec.sent == []


def test_fetch_타임아웃_후_슬롯이_정리된다():
    disp, _rec, _ = _dispatcher_with_worker()
    disp.fetch_document("cs_ops", "c1", timeout=0.1)
    # 타임아웃이면 슬롯이 정리돼 누수가 없다(완료 후 정리).
    assert disp._fetch_slots == {}  # pyright: ignore[reportPrivateUsage]


def test_fetch_완료_후_슬롯이_정리된다():
    disp, rec, _ = _dispatcher_with_worker()
    import threading
    import time

    def run() -> None:
        disp.fetch_document("cs_ops", "c1", timeout=2.0)

    t = threading.Thread(target=run)
    t.start()
    for _ in range(100):
        if rec.sent:
            break
        time.sleep(0.01)
    req_id = cast(FetchDocument, rec.sent[0]).request_id
    disp.resolve_fetch(DocumentContent(request_id=req_id, found=True, content="x"))
    t.join(timeout=2.0)
    assert disp._fetch_slots == {}  # pyright: ignore[reportPrivateUsage]


def test_resolve_미지_request_id는_멱등_무시():
    disp, _rec, _ = _dispatcher_with_worker()
    # 대기 슬롯이 없는 request_id로 resolve — 예외 없이 조용히 무시.
    disp.resolve_fetch(DocumentContent(request_id="ghost", found=True, content="x"))
    assert disp._fetch_slots == {}  # pyright: ignore[reportPrivateUsage]


def test_resolve_중복_도착은_멱등():
    disp, rec, _ = _dispatcher_with_worker()
    import threading
    import time

    result: list[FetchResult] = []

    def run() -> None:
        result.append(disp.fetch_document("cs_ops", "c1", timeout=2.0))

    t = threading.Thread(target=run)
    t.start()
    for _ in range(100):
        if rec.sent:
            break
        time.sleep(0.01)
    req_id = cast(FetchDocument, rec.sent[0]).request_id
    disp.resolve_fetch(DocumentContent(request_id=req_id, found=True, content="first"))
    t.join(timeout=2.0)
    # 같은 request_id로 두 번째 도착 — 슬롯이 이미 정리돼 멱등 무시(예외 없음).
    disp.resolve_fetch(DocumentContent(request_id=req_id, found=True, content="second"))
    assert result[0].content == "first"


# ════════════════════════════════════════════════════════════════════════════
# ④ 워커 읽기 (결정 D) — handle_fetch_document
# ════════════════════════════════════════════════════════════════════════════


class _StubRunner:
    """ClaudeRunner Protocol stub — 답 생성은 fetch 테스트와 무관(고정 답)."""

    def __call__(
        self, prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> str:
        return "x"


def _worker_with_okf(tmp_path: Path) -> WorkerLogic:
    card = _card()
    okf_root = tmp_path / "okf"
    agent_dir = okf_root / card.agent_id
    agent_dir.mkdir(parents=True)
    (agent_dir / "refund_policy.md").write_text("# 환불 정책\n7일 이내 가능", encoding="utf-8")
    return WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=_StubRunner()),
        okf_root=okf_root,
    )


def test_handle_fetch_document_found_본문회신(tmp_path: Path):
    logic = _worker_with_okf(tmp_path)
    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id="refund_policy", request_id="r1")
    )
    assert doc.request_id == "r1"
    assert doc.found is True
    assert "환불 정책" in doc.content


def test_handle_fetch_document_없는_파일은_found_False(tmp_path: Path):
    logic = _worker_with_okf(tmp_path)
    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id="missing_doc", request_id="r2")
    )
    assert doc.found is False
    assert doc.content == ""
    assert doc.request_id == "r2"


def test_handle_fetch_document_미소유_agent_id는_found_False(tmp_path: Path):
    """사칭 차단 — 자기 _cards에 없는 agent_id면 파일이 있어도 읽지 않는다(결정 D)."""
    logic = _worker_with_okf(tmp_path)
    # 다른 owner의 카드 디렉터리에 파일을 심어도 — 이 워커는 그 카드를 소유하지 않는다.
    okf_root = cast(Path, logic._okf_root)  # pyright: ignore[reportPrivateUsage]
    other_dir = okf_root / "legal_ops"
    other_dir.mkdir(parents=True)
    (other_dir / "secret.md").write_text("남의 비밀 문서", encoding="utf-8")
    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="legal_ops", concept_id="secret", request_id="r3")
    )
    assert doc.found is False
    assert doc.content == ""


def test_handle_fetch_document_okf_root_미주입이면_found_False():
    card = _card()
    logic = WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=_StubRunner()),
        okf_root=None,
    )
    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id="c1", request_id="r4")
    )
    assert doc.found is False


# ════════════════════════════════════════════════════════════════════════════
# ⑤ 권한 스코핑 + web 핸들러 (결정 E) — 가짜 fetch 디스패처 주입
# ════════════════════════════════════════════════════════════════════════════
#
# 케이스는 *실 채팅 흐름*으로 연다(`/ask` 다툼 질문) — 그 케이스가 라우트가 보는 *바로 그*
# bundle.case_store에 든다. _CONTESTED_Q는 cs_ops(cs_lead)·finance_ops(finance_lead)를
# 후보로 건다. 디스패처는 가짜로 fetch_document를 덮어 web 핸들러 분기만 결정론으로 본다.

_SECRET = "test-secret"
_CONTESTED_Q = "보상 기준이 어떻게 되나요?"


class _FakeFetchDispatcher(WebSocketDispatcher):
    """fetch_document를 가짜 결과로 덮는 디스패처(web 핸들러 분기 결정론)."""

    def __init__(self, result: FetchResult) -> None:
        super().__init__()
        self._fake_result = result
        self.calls: list[tuple[str, str]] = []

    def fetch_document(  # type: ignore[override]
        self, agent_id: str, concept_id: str, *, timeout: float = 5.0
    ) -> FetchResult:
        self.calls.append((agent_id, concept_id))
        return self._fake_result


def _fetch_app(result: FetchResult) -> tuple[TestClient, _FakeFetchDispatcher]:
    from agent_org_network.runtime import StubRuntime

    disp = _FakeFetchDispatcher(result)
    app = create_app(runtime=StubRuntime(), dispatcher=disp, session_secret=_SECRET)
    return TestClient(app), disp


def _login(client: TestClient, user_id: str) -> Response:
    http: Any = client
    return cast(Response, http.post("/login", json={"user_id": user_id}))


def _ask(client: TestClient, question: str) -> Response:
    http: Any = client
    return cast(Response, http.post("/ask", json={"question": question}))


def _open_contested_case(client: TestClient) -> str:
    """다툼 질문으로 케이스를 열고 cs_lead 처리함에서 case_id를 읽는다."""
    res = _ask(client, _CONTESTED_Q)
    body: dict[str, Any] = res.json()
    assert body["kind"] == "contested"
    # cs_lead로 로그인해 자기 처리함에서 case_id를 본다.
    _login(client, "cs_lead")
    http: Any = client
    cases: list[dict[str, Any]] = cast(Response, http.get("/inbox/cases")).json()
    assert len(cases) == 1
    return cast(str, cases[0]["case_id"])


def _post_doc(
    client: TestClient, case_id: str, agent_id: str, concept_id: str
) -> Response:
    http: Any = client
    return cast(
        Response,
        http.post(
            f"/inbox/cases/{case_id}/document",
            json={"agent_id": agent_id, "concept_id": concept_id},
        ),
    )


def test_세션owner_후보_agent_후보면_통과하고_본문반환():
    client, disp = _fetch_app(FetchResult("delivered", found=True, content="# 환불 정책"))
    case_id = _open_contested_case(client)  # cs_lead 로그인 상태
    res = _post_doc(client, case_id, "cs_ops", "refund_policy")
    assert res.status_code == 200
    body: dict[str, Any] = res.json()
    assert body["found"] is True
    assert body["content"] == "# 환불 정책"
    assert disp.calls == [("cs_ops", "refund_policy")]


def test_비후보_owner_세션은_403():
    client, disp = _fetch_app(FetchResult("delivered", found=True, content="x"))
    case_id = _open_contested_case(client)  # cs_lead 로그인 상태
    # legal_lead는 이 케이스 후보가 아니다 — 재로그인해 시도.
    _login(client, "legal_lead")
    res = _post_doc(client, case_id, "cs_ops", "refund_policy")
    assert res.status_code == 403
    assert disp.calls == []  # 거부면 fetch를 안 보낸다


def test_비후보_agent_id는_403():
    client, disp = _fetch_app(FetchResult("delivered", found=True, content="x"))
    case_id = _open_contested_case(client)  # cs_lead 로그인 상태(후보 owner)
    # hr_ops는 이 케이스 후보가 아니다(세션 owner는 후보지만 agent가 후보 밖).
    res = _post_doc(client, case_id, "hr_ops", "some_concept")
    assert res.status_code == 403
    assert disp.calls == []


def test_미존재_case는_404():
    client, disp = _fetch_app(FetchResult("delivered", found=True, content="x"))
    _login(client, "cs_lead")
    res = _post_doc(client, "ghost-case", "cs_ops", "c1")
    assert res.status_code == 404
    assert disp.calls == []


def test_미로그인은_401():
    client, _ = _fetch_app(FetchResult("delivered", found=True, content="x"))
    case_id = _open_contested_case(client)
    # 로그아웃해 세션을 비운다.
    http: Any = client
    cast(Response, http.post("/logout", json={}))
    res = _post_doc(client, case_id, "cs_ops", "refund_policy")
    assert res.status_code == 401


def test_오프라인_degradation_정상응답():
    client, _ = _fetch_app(FetchResult("offline"))
    case_id = _open_contested_case(client)
    res = _post_doc(client, case_id, "cs_ops", "refund_policy")
    assert res.status_code == 200
    body: dict[str, Any] = res.json()
    assert body["found"] is False
    assert body["available"] is False
    assert "미연결" in body["message"]


def test_타임아웃_degradation_정상응답():
    client, _ = _fetch_app(FetchResult("timeout"))
    case_id = _open_contested_case(client)
    res = _post_doc(client, case_id, "cs_ops", "refund_policy")
    assert res.status_code == 200
    body: dict[str, Any] = res.json()
    assert body["found"] is False
    assert body["available"] is False
    assert "응답 없음" in body["message"]


def test_문서없음_found_False_정상응답():
    client, _ = _fetch_app(FetchResult("delivered", found=False, content=""))
    case_id = _open_contested_case(client)
    res = _post_doc(client, case_id, "cs_ops", "refund_policy")
    assert res.status_code == 200
    body: dict[str, Any] = res.json()
    assert body["found"] is False
    assert body["available"] is True


# ════════════════════════════════════════════════════════════════════════════
# ⑥ 보안: 경로 traversal 차단 (B1·B2 — 음성 테스트)
# ════════════════════════════════════════════════════════════════════════════
#
# 공격 A: agent_id는 자기 소유지만 concept_id에 "../other_owner/pricing" 삽입
#   → 형제 디렉터리 파일 유출 시도(own-cards 게이트 우회).
# 공격 B: concept_id="../../outside" → okf_root 바깥.
# 공격 C: concept_id="/abs/path" → 절대경로 점프.
# 공격 D: agent_id="../other" → agent_id 자체에 구분자 삽입.
# 공격 E: 심볼릭링크가 okf_root 밖을 가리켜도 차단.
# 정상: concept_id="refund_policy"(순수 stem)는 여전히 found=True(무회귀).


def _worker_with_traversal_bait(tmp_path: Path) -> WorkerLogic:
    """traversal 공격이 성공했을 경우 읽힐 '미끼 파일'을 OKF 바깥에 두고
    워커를 반환한다. sanitization이 없으면 이 파일이 읽히고, 있으면 found=False."""
    card = _card()
    okf_root = tmp_path / "okf"
    agent_dir = okf_root / card.agent_id
    agent_dir.mkdir(parents=True)
    # 정상 문서
    (agent_dir / "refund_policy.md").write_text("# 환불 정책\n7일 이내 가능", encoding="utf-8")

    # 공격 A용: 형제 owner 디렉터리에 미끼 파일 (cs_ops/../finance_ops/pricing.md 경로)
    finance_dir = okf_root / "finance_ops"
    finance_dir.mkdir(parents=True)
    (finance_dir / "pricing.md").write_text("SECRET PRICING", encoding="utf-8")

    # 공격 B용: okf_root 바깥에 미끼 파일 (../../outside.md 상대 경로 시도)
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("OUTSIDE SECRET", encoding="utf-8")

    # 공격 C용: 절대 경로 → 파일시스템 임의 위치 (tmp_path 아래에 미끼)
    abs_bait = tmp_path / "abs_secret.md"
    abs_bait.write_text("ABS SECRET", encoding="utf-8")

    return WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=_StubRunner()),
        okf_root=okf_root,
    )


def test_공격A_concept_id에_상대경로_삽입_형제디렉터리_차단(tmp_path: Path):
    """공격 A: concept_id='../finance_ops/pricing' — 형제 owner 파일 유출 시도.

    agent_id=cs_ops(자기 소유)이지만 concept_id에 구분자 삽입으로
    okf_root/cs_ops/../finance_ops/pricing.md를 읽으려 한다.
    sanitization 없으면 해당 파일이 존재해 found=True가 되므로
    테스트는 "미끼 파일이 실제로 존재함"을 전제하고 found=False를 단언한다.
    """
    logic = _worker_with_traversal_bait(tmp_path)
    okf_root = cast(Path, logic._okf_root)  # pyright: ignore[reportPrivateUsage]
    # 미끼 파일이 sanitization 없이 접근 가능한 경로에 실제 존재하는지 확인(전제 보장)
    bait = (okf_root / "cs_ops" / "../finance_ops/pricing.md").resolve()
    assert bait.exists(), "전제: 미끼 파일이 없으면 테스트가 의미 없음"

    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id="../finance_ops/pricing", request_id="sec-A")
    )
    assert doc.found is False, "공격 A: 형제 owner 파일이 읽혀서는 안 된다"
    assert doc.content == ""
    assert doc.request_id == "sec-A"


def test_공격B_concept_id에_루트_탈출_상대경로_차단(tmp_path: Path):
    """공격 B: concept_id='../../outside' — okf_root 바깥 파일 탈출 시도."""
    logic = _worker_with_traversal_bait(tmp_path)
    # 미끼 파일이 실제 존재하는지 확인(전제)
    bait = tmp_path / "outside.md"
    assert bait.exists(), "전제: 미끼 파일이 없으면 테스트가 의미 없음"

    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id="../../outside", request_id="sec-B")
    )
    assert doc.found is False, "공격 B: okf_root 바깥 파일이 읽혀서는 안 된다"
    assert doc.content == ""


def test_공격C_concept_id_절대경로_차단(tmp_path: Path):
    """공격 C: concept_id='/abs/path' — 절대경로로 파일시스템 점프 시도."""
    logic = _worker_with_traversal_bait(tmp_path)
    abs_bait = str(tmp_path / "abs_secret")

    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id=abs_bait, request_id="sec-C")
    )
    assert doc.found is False, "공격 C: 절대경로 concept_id는 거부해야 한다"
    assert doc.content == ""


def test_공격D_agent_id에_경로_구분자_삽입_차단(tmp_path: Path):
    """공격 D: agent_id='../other' — agent_id 자체에 구분자 삽입 시도."""
    card = _card()
    okf_root = tmp_path / "okf"
    (okf_root / "cs_ops").mkdir(parents=True)
    # 타겟 디렉터리에도 미끼 파일
    other_dir = okf_root / "other"
    other_dir.mkdir(parents=True)
    (other_dir / "secret.md").write_text("OTHER SECRET", encoding="utf-8")

    # agent_id가 _cards에 없으면 기존 게이트가 차단하므로,
    # 여기선 agent_id="../cs_ops"(구분자 포함)를 _cards에 강제 등록해
    # traversal-aware sanitization이 차단하는지 검증한다.
    evil_agent_id = "../other"
    logic = WorkerLogic(
        owner_id=card.owner,
        cards={evil_agent_id: card},  # 구분자 포함 agent_id를 강제 등록
        runtime=ClaudeCodeRuntime(runner=_StubRunner()),
        okf_root=okf_root,
    )
    doc = logic.handle_fetch_document(
        FetchDocument(agent_id=evil_agent_id, concept_id="secret", request_id="sec-D")
    )
    assert doc.found is False, "공격 D: agent_id에 경로 구분자가 있으면 거부해야 한다"
    assert doc.content == ""


def test_공격E_심볼릭링크_okf_바깥_차단(tmp_path: Path):
    """공격 E: concept_id가 okf_root 밖을 가리키는 심볼릭 링크를 통한 탈출 차단.

    okf_root/cs_ops/evil_link.md → tmp_path/secret.md 심볼릭 링크.
    resolve() 후 is_relative_to(base) 검사로 차단한다.
    """
    import os

    card = _card()
    okf_root = tmp_path / "okf"
    agent_dir = okf_root / card.agent_id
    agent_dir.mkdir(parents=True)

    # OKF 바깥 미끼 파일
    secret = tmp_path / "symlink_secret.md"
    secret.write_text("SYMLINK SECRET", encoding="utf-8")

    # OKF 안에 바깥을 가리키는 심볼릭 링크 생성
    link_path = agent_dir / "evil_link.md"
    try:
        os.symlink(str(secret), str(link_path))
    except (OSError, NotImplementedError):
        # 심볼릭링크 생성 불가 환경(Windows 일부) → 건너뜀
        import pytest
        pytest.skip("심볼릭링크 생성 불가 환경")

    logic = WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=_StubRunner()),
        okf_root=okf_root,
    )
    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id="evil_link", request_id="sec-E")
    )
    assert doc.found is False, "공격 E: 심볼릭링크로 okf_root 바깥을 읽으면 안 된다"
    assert doc.content == ""


def test_정상_stem은_여전히_found_True(tmp_path: Path):
    """무회귀: concept_id='refund_policy'(순수 stem)는 found=True여야 한다(데모 안 깨짐)."""
    logic = _worker_with_traversal_bait(tmp_path)
    doc = logic.handle_fetch_document(
        FetchDocument(agent_id="cs_ops", concept_id="refund_policy", request_id="ok-1")
    )
    assert doc.found is True
    assert "환불 정책" in doc.content
    assert doc.request_id == "ok-1"


def test_web_1차방어_concept_id_traversal_거부():
    """web 1차 방어: concept_id에 경로 구분자 포함 시 400 또는 found=False(degradation).

    워커측이 최종 권위이지만 web도 1차 방어를 한다(분산 신뢰 경계 주석 확인).
    FakeFetchDispatcher는 호출되지 않아야 한다(거부면 dispatch 전 차단).
    """
    client, disp = _fetch_app(FetchResult("delivered", found=True, content="SECRET"))
    case_id = _open_contested_case(client)  # cs_lead 로그인 상태
    res = _post_doc(client, case_id, "cs_ops", "../finance_ops/pricing")
    # web 1차 방어: 400 또는 found=False(degradation). 200+found=True는 절대 안 된다.
    assert res.status_code in (400, 422) or (
        res.status_code == 200 and res.json().get("found") is False
    ), f"web 1차 방어 실패: status={res.status_code} body={res.json()}"
    # 구분자 포함 concept_id로는 dispatch가 일어나지 않아야 한다
    assert disp.calls == [], f"web 1차 방어 후 dispatch가 일어남: {disp.calls}"
