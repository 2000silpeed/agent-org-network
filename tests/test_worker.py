"""Owner Worker 결정론 테스트 (T6.3 슬라이스2b-ii) — 실 claude·실 WS 없이.

워커의 *프레임 핸들링 로직*(`WorkerLogic`)·재연결 백오프(`backoff_seconds`)·중앙 프레임
복원(`parse_central_frame`)만 검증한다. 실 claude는 `ClaudeCodeRuntime`에 FakeRunner(고정
답)를 주입해 가리고, WS 소켓은 아예 끌어들이지 않는다(프레임 객체로 직접 입출력). 실 WS·실
claude·재연결 무한 루프(`run_worker`)는 수동 시연 영역이라 여기 들어오지 않는다(ADR 0011 결정
6-6 — 게이트는 결정론만).
"""

from datetime import datetime, timezone

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import ClaudeCodeRuntime
from agent_org_network.transport import (
    AuthError,
    Ping,
    PushWork,
    SubmitAnswer,
    TicketFrame,
    Welcome,
)
from agent_org_network.worker import (
    DEFAULT_MAX_BACKOFF_SECONDS,
    WorkerLogic,
    backoff_seconds,
    parse_central_frame,
)

BASE_TS = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


class _RecordingRunner:
    """프롬프트를 받아 고정 응답을 돌려주며 마지막 프롬프트를 기록(실 claude 대역).

    `cwd`는 ClaudeRunner Protocol(ADR 0013 OKF 소비)의 선택 키워드 — 이 테스트들은 OKF
    번들을 두지 않아 cwd가 전달되지 않지만, 시그니처로 흡수해 Protocol에 부합한다(행위 불변).
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None

    def __call__(self, prompt: str, *, cwd: str | None = None) -> str:
        self.last_prompt = prompt
        return self.reply


def _card(
    agent_id: str = "cs_ops",
    owner: str = "cs_lead",
    knowledge_sources: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="환불 정책과 처리 절차를 안내합니다.",
        domains=["환불"],
        last_reviewed_at=BASE_TS.date(),
        knowledge_sources=knowledge_sources if knowledge_sources is not None else [],
    )


def _push(
    ticket_id: str = "tkt-1",
    agent_id: str = "cs_ops",
    question: str = "환불 되나요?",
) -> PushWork:
    return PushWork(
        ticket=TicketFrame(
            ticket_id=ticket_id,
            agent_id=agent_id,
            question=question,
            enqueued_at=BASE_TS,
        )
    )


def _logic(
    reply: str = "네, 7일 이내 가능합니다.",
    knowledge_sources: list[str] | None = None,
) -> tuple[WorkerLogic, _RecordingRunner]:
    runner = _RecordingRunner(reply)
    card = _card(knowledge_sources=knowledge_sources)
    logic = WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=runner),
    )
    return logic, runner


# ── ① handle_push_work: PushWork → FakeRunner 답 → SubmitAnswer ─────────────


def test_push_work를_받아_SubmitAnswer를_만든다():
    logic, _ = _logic(reply="네, 7일 이내 전액 환불됩니다.")

    submit = logic.handle_push_work(_push(ticket_id="tkt-42", question="환불 되나요?"))

    assert isinstance(submit, SubmitAnswer)
    # ticket_id가 그대로 회신에 실린다(멱등 키, 6-4).
    assert submit.ticket_id == "tkt-42"
    # FakeRunner 고정 답이 AnswerFrame 본문으로(실 claude 없이 결정론).
    assert submit.answer.text == "네, 7일 이내 전액 환불됩니다."


def test_runner에_ticket_question이_전달된다():
    logic, runner = _logic()

    logic.handle_push_work(_push(question="보증 기간 얼마예요?"))

    assert runner.last_prompt is not None
    # 워커가 ticket의 question을 로컬 claude(ClaudeCodeRuntime)에 넘긴다.
    assert "보증 기간 얼마예요?" in runner.last_prompt
    # 카드 페르소나도 프롬프트에 녹는다(ClaudeCodeRuntime 재사용 — 재구현 아님).
    assert "cs_ops" in runner.last_prompt


def test_답의_sources와_mode가_카드에서_보존된다():
    logic, _ = _logic(reply="답", knowledge_sources=["위키/환불정책", "Notion/보상표"])

    submit = logic.handle_push_work(_push())

    # ClaudeCodeRuntime이 카드 knowledge_sources를 Answer.sources로 싣고, to_answer_frame이
    # 그대로 전송 프레임에 보존한다(레이블 출처).
    assert submit.answer.sources == ("위키/환불정책", "Notion/보상표")
    assert submit.answer.mode == "full"


def test_빈_응답이면_ClaudeCodeRuntime_폴백이_회신된다():
    # 실 claude가 빈 답을 줘도 ClaudeCodeRuntime이 폴백 Answer로 흡수 → 작업은 종착한다.
    runner = _RecordingRunner("   \n  ")
    card = _card()
    logic = WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=runner),
    )

    submit = logic.handle_push_work(_push(ticket_id="tkt-x"))

    assert submit.ticket_id == "tkt-x"
    assert "cs_ops" in submit.answer.text  # 폴백 본문(미아 아님)


# ── ② 카드 없는 agent_id → 폴백 답 회신(미아 방지) ──────────────────────────


def test_모르는_agent_id면_폴백_답으로_종착시킨다():
    logic, runner = _logic()  # cards에는 cs_ops만 있음

    submit = logic.handle_push_work(_push(ticket_id="tkt-7", agent_id="unknown_ops"))

    # 카드를 못 찾아도 SubmitAnswer로 회신 → 중앙 큐가 answered로 종착(작업 미아 금지).
    assert isinstance(submit, SubmitAnswer)
    assert submit.ticket_id == "tkt-7"
    assert "unknown_ops" in submit.answer.text
    # 카드가 없으니 claude를 부르지 않는다(runner 미호출).
    assert runner.last_prompt is None


# ── ③ register_frame: owner_id·token 실림 ───────────────────────────────────


def test_register_frame에_owner_id가_실린다():
    logic, _ = _logic()

    reg = logic.register_frame()

    assert reg.type == "register_worker"
    assert reg.owner_id == "cs_lead"
    assert reg.token is None


def test_register_frame에_token이_실린다():
    logic, _ = _logic()

    reg = logic.register_frame(token="secret-123")

    assert reg.token == "secret-123"


# ── ③-b role: 등록 프레임에 워커 등급이 실린다(ADR 0012 결정 2, T6.6 슬라이스 iv) ──


def test_role_미지정이면_primary로_등록된다():
    # 하위호환: role 인자 없이 만들면 기본 primary(기존 워커는 그대로 1차 워커).
    logic, _ = _logic()

    assert logic.role == "primary"
    reg = logic.register_frame()
    assert reg.role == "primary"


def test_backup_role이_등록_프레임에_실린다():
    runner = _RecordingRunner("백업 답")
    card = _card()
    logic = WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=runner),
        role="backup",
    )

    assert logic.role == "backup"
    reg = logic.register_frame()
    # 등급은 register_frame에 실려 중앙이 등급별 레지스트리에 올린다(결정 2).
    assert reg.role == "backup"
    assert reg.owner_id == "cs_lead"


def test_backup_워커도_같은_로직으로_답한다():
    # backup도 같은 WorkerLogic·같은 카드로 답한다 — 답 신뢰 하향(mode=backup)은 워커가
    # 아니라 디스패처가 연결 등급을 진실로 강제한다(결정 4). 워커는 full로 답한다.
    runner = _RecordingRunner("백업이 만든 답")
    card = _card()
    logic = WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=runner),
        role="backup",
    )

    submit = logic.handle_push_work(_push(ticket_id="tkt-b"))

    assert isinstance(submit, SubmitAnswer)
    assert submit.ticket_id == "tkt-b"
    assert submit.answer.text == "백업이 만든 답"
    # 워커 자기보고는 full — backup 하향은 디스패처 몫(연결 등급이 진실, 결정 4).
    assert submit.answer.mode == "full"


def test_role이_register_frame_token과_함께_실린다():
    runner = _RecordingRunner("답")
    card = _card()
    logic = WorkerLogic(
        owner_id=card.owner,
        cards={card.agent_id: card},
        runtime=ClaudeCodeRuntime(runner=runner),
        role="backup",
    )

    reg = logic.register_frame(token="tok-9")

    assert reg.role == "backup"
    assert reg.token == "tok-9"


# ── ④ backoff_seconds: 지수 증가·cap·음수 방어(순수 로직) ───────────────────


def test_backoff는_지수적으로_증가한다():
    assert backoff_seconds(0, base=1.0, cap=100.0) == 1.0
    assert backoff_seconds(1, base=1.0, cap=100.0) == 2.0
    assert backoff_seconds(2, base=1.0, cap=100.0) == 4.0
    assert backoff_seconds(3, base=1.0, cap=100.0) == 8.0


def test_backoff는_cap에서_멈춘다():
    # 충분히 큰 attempt면 cap을 넘지 않는다.
    assert backoff_seconds(10, base=1.0, cap=30.0) == 30.0
    assert backoff_seconds(100, base=1.0, cap=30.0) == 30.0
    # 기본 cap도 초과하지 않는다.
    assert backoff_seconds(50) == DEFAULT_MAX_BACKOFF_SECONDS


def test_backoff_음수_attempt는_0으로_본다():
    assert backoff_seconds(-1, base=1.0, cap=30.0) == 1.0
    assert backoff_seconds(-99, base=2.0, cap=30.0) == 2.0


# ── ⑤ parse_central_frame: 다운스트림 프레임 복원·미지/불량 None ────────────


def test_welcome_프레임_복원():
    frame = parse_central_frame({"type": "welcome"})
    assert isinstance(frame, Welcome)


def test_auth_error_프레임_복원():
    frame = parse_central_frame({"type": "auth_error", "reason": "미인증"})
    assert isinstance(frame, AuthError)
    assert frame.reason == "미인증"


def test_push_work_프레임_복원():
    raw = {
        "type": "push_work",
        "ticket": {
            "ticket_id": "tkt-9",
            "agent_id": "cs_ops",
            "question": "환불?",
            "enqueued_at": BASE_TS.isoformat(),
        },
    }
    frame = parse_central_frame(raw)
    assert isinstance(frame, PushWork)
    assert frame.ticket.ticket_id == "tkt-9"
    assert frame.ticket.question == "환불?"


def test_ping_프레임_복원():
    assert isinstance(parse_central_frame({"type": "ping"}), Ping)


def test_미지_타입은_None():
    assert parse_central_frame({"type": "garbage"}) is None
    assert parse_central_frame({"no_type": 1}) is None


def test_dict가_아니면_None():
    assert parse_central_frame("not a dict") is None
    assert parse_central_frame(None) is None
    assert parse_central_frame(["push_work"]) is None


def test_검증_실패_프레임은_None():
    # push_work인데 ticket 필드가 빠지면 pydantic 검증 실패 → None(워커를 깨지 않음).
    assert parse_central_frame({"type": "push_work"}) is None
    # 미지 필드(extra forbid)도 거부.
    assert parse_central_frame({"type": "welcome", "extra": 1}) is None


# ── ⑥ 워커 발신 프레임이 중앙 파서로 왕복(라운드트립) ───────────────────────


def test_submit_answer가_와이어_왕복으로_보존된다():
    """SubmitAnswer를 JSON으로 직렬화→복원해도 동일(전송 안전).

    워커는 실제로 `model_dump_json()`으로 보내고 중앙(server.py)은 그걸 받아 pydantic으로
    `SubmitAnswer`를 복원한다 — 그 와이어 경계가 깨지지 않는지 결정론으로 고정(실 소켓 없이).
    """
    import json

    logic, _ = _logic(reply="회신 본문")
    submit = logic.handle_push_work(_push(ticket_id="tkt-rt"))

    wire = submit.model_dump_json()
    restored = SubmitAnswer.model_validate(json.loads(wire))

    assert restored.ticket_id == "tkt-rt"
    assert restored.answer.text == "회신 본문"
