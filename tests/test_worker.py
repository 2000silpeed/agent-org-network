"""Owner Worker 결정론 테스트 (T6.3 슬라이스2b-ii) — 실 claude·실 WS 없이.

워커의 *프레임 핸들링 로직*(`WorkerLogic`)·재연결 백오프(`backoff_seconds`)·중앙 프레임
복원(`parse_central_frame`)만 검증한다. 실 claude는 `ClaudeCodeRuntime`에 FakeRunner(고정
답)를 주입해 가리고, WS 소켓은 아예 끌어들이지 않는다(프레임 객체로 직접 입출력). 실 WS·실
claude·재연결 무한 루프(`run_worker`)는 수동 시연 영역이라 여기 들어오지 않는다(ADR 0011 결정
6-6 — 게이트는 결정론만).

T11.7b(크로스머신 fan-out 배선): ReindexOnCommitListener 결정론 테스트 — Fake sink·Fake
git gateway·OkfChangeEvent 발화 단일·디스크 재도출 잠금. 실 WS·실 소켓 0.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.git_gateway import (
    BuilderCommitRequest,
    FakeGitGateway,
    OkfFile,
    commit_okf_bundle,
)
from agent_org_network.runtime import ClaudeCodeRuntime
from agent_org_network.transport import (
    AuthError,
    Ping,
    PublishIndex,
    PushWork,
    SubmitAnswer,
    TicketFrame,
    Welcome,
)
from agent_org_network.worker import (
    DEFAULT_MAX_BACKOFF_SECONDS,
    ReindexOnCommitListener,
    WorkerLogic,
    backoff_seconds,
    parse_central_frame,
)

BASE_TS = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


class _RecordingRunner:
    """프롬프트를 받아 고정 응답을 돌려주며 마지막 user 프롬프트·system_prompt를 기록(실 claude 대역).

    `cwd`는 ClaudeRunner Protocol(ADR 0013 OKF 소비)의 선택 키워드 — 이 테스트들은 OKF
    번들을 두지 않아 cwd가 전달되지 않지만, 시그니처로 흡수해 Protocol에 부합한다(행위 불변).
    `system_prompt`(노출 격리·본 작업)도 선택 키워드 — 페르소나가 system으로 분리돼 넘어온다.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    def __call__(
        self, prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> str:
        self.last_prompt = prompt
        self.last_system = system_prompt
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
    # 워커가 ticket의 question을 로컬 claude(ClaudeCodeRuntime)에 user 프롬프트로 넘긴다.
    assert "보증 기간 얼마예요?" in runner.last_prompt
    # 카드 페르소나는 system_prompt로 분리돼 넘어온다(노출 격리·ClaudeCodeRuntime 재사용).
    assert runner.last_system is not None
    assert "cs_ops" in runner.last_system


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


# ════════════════════════════════════════════════════════════════════════════
# T11.7b — ReindexOnCommitListener 결정론 테스트 (크로스머신 fan-out 배선)
# ════════════════════════════════════════════════════════════════════════════

_T11B_TS = datetime(2026, 6, 30, 9, 0, 0, tzinfo=timezone.utc)


class _FakeReindexSink:
    """PublishIndex 목록을 기록하는 Fake sink — 실 WS 송신 대역."""

    def __init__(self) -> None:
        self.received: list[list[PublishIndex]] = []

    def __call__(self, frames: list[PublishIndex]) -> None:
        self.received.append(frames)


def _write_okf_file(root: Path, agent_id: str, filename: str, *, front: str) -> None:
    d = root / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(f"---\n{front}\n---\n\n본문\n", encoding="utf-8")


def _worker_with_okf(owner_id: str, agent_id: str, okf_root: Path) -> WorkerLogic:
    """OKF 루트가 있는 WorkerLogic — publish_frames가 비어있지 않은 워커."""

    class _NullRunner:
        def __call__(self, prompt: str, **kw: object) -> str:
            return "답"

    card = AgentCard(
        agent_id=agent_id,
        owner=owner_id,
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at=BASE_TS.date(),
    )
    return WorkerLogic(
        owner_id=owner_id,
        cards={agent_id: card},
        runtime=ClaudeCodeRuntime(runner=_NullRunner()),
        okf_root=okf_root,
    )


# ── (1) 배선 관통 ────────────────────────────────────────────────────────────


def test_T11_7b_배선_관통_commit_후_리스너_발화_PublishIndex_캡처(tmp_path: Path) -> None:
    """commit_okf_bundle(propagator=ReindexOnCommitListener) → on_okf_committed 1회 →
    publish_frames 호출 → list[PublishIndex] 구성(Fake sink/last_frames 캡처).
    """
    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)
    sink = _FakeReindexSink()
    listener = ReindexOnCommitListener(
        worker=worker,
        clock=lambda: _T11B_TS,
        publish_sink=sink,
    )

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="환불 정책 추가",
    )
    commit_okf_bundle(req, FakeGitGateway(), propagator=listener)

    # sink에 1회 수신됐어야 한다
    assert len(sink.received) == 1
    frames = sink.received[0]
    assert isinstance(frames, list)
    assert len(frames) >= 1
    assert all(isinstance(f, PublishIndex) for f in frames)

    # last_frames 관측 속성도 동일
    assert listener.last_frames == frames


# ── (2) 경로 = 디스크 재도출 잠금 ──────────────────────────────────────────


def test_T11_7b_디스크_재도출_publish_frames_직접_호출과_동치(tmp_path: Path) -> None:
    """리스너 frames == publish_frames(generated_at=clock()) 직접 호출 결과(같은 concepts)."""
    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)

    fixed_clock = lambda: _T11B_TS  # noqa: E731
    sink = _FakeReindexSink()
    listener = ReindexOnCommitListener(worker=worker, clock=fixed_clock, publish_sink=sink)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="커밋",
    )
    commit_okf_bundle(req, FakeGitGateway(), propagator=listener)

    direct = worker.publish_frames(generated_at=_T11B_TS)

    # concepts 목록 일치 + generated_at·agent_id 동일
    assert len(sink.received[0]) == len(direct)
    for a, b in zip(sink.received[0], direct, strict=True):
        a_ids = {c.id for c in a.index.concepts}
        b_ids = {c.id for c in b.index.concepts}
        assert a_ids == b_ids
        assert a.index.generated_at == b.index.generated_at
        assert a.index.agent_id == b.index.agent_id


# ── (3) 발화 단일 — reindex 리스너만·reeval 미발화 ───────────────────────────


def test_T11_7b_발화_단일_reeval_미발화(tmp_path: Path) -> None:
    """commit_okf_bundle propagator 슬롯이 하나임을 구조적으로 실증.

    ① reindex 리스너를 propagator로 연결 → sink 1회 채워짐 + reeval spy는 주입 자체가
       불가(슬롯 하나) — 두 머신이 동시에 발화하지 않음을 배선 수준에서 보증.
    ② reeval spy를 propagator로 연결한 별도 commit → reindex sink는 채워지지 않음(역방향).
    """

    class _SpyReeval:
        called: int = 0

        def on_okf_committed(self, event: object) -> None:
            self.called += 1

    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="커밋",
    )

    # ① reindex 리스너를 propagator로 — sink가 1회 채워진다
    sink_reindex = _FakeReindexSink()
    listener = ReindexOnCommitListener(worker=worker, clock=lambda: _T11B_TS, publish_sink=sink_reindex)
    commit_okf_bundle(req, FakeGitGateway(), propagator=listener)
    assert len(sink_reindex.received) == 1  # reindex 1회 발화

    # ② reeval spy를 propagator로 연결한 별도 commit — reindex sink는 미채워짐(역방향 보증)
    sink_reindex2 = _FakeReindexSink()
    spy_reeval = _SpyReeval()
    commit_okf_bundle(req, FakeGitGateway(), propagator=spy_reeval)
    assert spy_reeval.called == 1       # reeval propagator만 1회 발화
    assert len(sink_reindex2.received) == 0  # reindex sink는 연결 안 됐으므로 미채워짐


# ── (4) 결정론(clock 주입) ───────────────────────────────────────────────────


def test_T11_7b_결정론_같은_clock_같은_generated_at(tmp_path: Path) -> None:
    """같은 commit·같은 주입 clock → 같은 generated_at·같은 프레임(멱등)."""
    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)

    sink1 = _FakeReindexSink()
    sink2 = _FakeReindexSink()
    fixed_ts = datetime(2026, 6, 30, 10, 0, 0, tzinfo=timezone.utc)

    for sink in (sink1, sink2):
        listener = ReindexOnCommitListener(worker=worker, clock=lambda: fixed_ts, publish_sink=sink)
        gw = FakeGitGateway()
        req = BuilderCommitRequest(
            agent_id="cs_ops",
            owner="alice",
            files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
            message="커밋",
        )
        commit_okf_bundle(req, gw, propagator=listener)

    # generated_at 동일
    for f1, f2 in zip(sink1.received[0], sink2.received[0], strict=True):
        assert f1.index.generated_at == f2.index.generated_at == fixed_ts


# ── (5) okf_root None 워커 가드 ─────────────────────────────────────────────


def test_T11_7b_okf_root_None_워커_빈_프레임_예외_없음() -> None:
    """okf_root=None 워커 → publish_frames 빈 리스트 → 리스너도 빈 프레임·예외 0."""

    class _NullRunner:
        def __call__(self, prompt: str, **kw: object) -> str:
            return "답"

    card = AgentCard(
        agent_id="cs_ops",
        owner="alice",
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at=BASE_TS.date(),
    )
    worker = WorkerLogic(
        owner_id="alice",
        cards={"cs_ops": card},
        runtime=ClaudeCodeRuntime(runner=_NullRunner()),
        # okf_root 미주입
    )
    sink = _FakeReindexSink()
    listener = ReindexOnCommitListener(worker=worker, clock=lambda: _T11B_TS, publish_sink=sink)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="내용"),),
        message="커밋",
    )
    commit_okf_bundle(req, FakeGitGateway(), propagator=listener)

    assert listener.last_frames == []
    assert sink.received == [[]]


# ── (6) 실 WS 미선취 가드 ────────────────────────────────────────────────────


def test_T11_7b_기본_sink_no_op_실소켓_0() -> None:
    """sink 미주입 시 기본 no-op — 예외 없이 실행되고 실 소켓 0(T11.7e seam)."""

    class _NullRunner:
        def __call__(self, prompt: str, **kw: object) -> str:
            return "답"

    card = AgentCard(
        agent_id="cs_ops",
        owner="alice",
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at=BASE_TS.date(),
    )
    worker = WorkerLogic(
        owner_id="alice",
        cards={"cs_ops": card},
        runtime=ClaudeCodeRuntime(runner=_NullRunner()),
    )
    # publish_sink 미주입 → 기본 no-op
    listener = ReindexOnCommitListener(worker=worker, clock=lambda: _T11B_TS)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="내용"),),
        message="커밋",
    )
    # 예외 없이 실행돼야 한다
    commit_okf_bundle(req, FakeGitGateway(), propagator=listener)
    assert listener.last_frames == []  # okf_root 없으므로 빈 리스트


# ── (7) 이벤트 정확성 ────────────────────────────────────────────────────────


def test_T11_7b_이벤트_agent_id_sha_committed_at_정확() -> None:
    """commit_okf_bundle이 발화한 OkfChangeEvent가 CommitResult와 일치."""
    from agent_org_network.git_gateway import OkfChangeEvent

    captured_events: list[OkfChangeEvent] = []

    class _EventCapturingListener:
        def on_okf_committed(self, event: object) -> None:
            assert isinstance(event, OkfChangeEvent)
            captured_events.append(event)

    gw = FakeGitGateway()
    listener = _EventCapturingListener()
    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="내용"),),
        message="커밋",
    )
    fixed_ts = datetime(2026, 6, 30, 11, 0, 0, tzinfo=timezone.utc)
    result = commit_okf_bundle(req, gw, propagator=listener, clock=lambda: fixed_ts)

    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev.agent_id == "cs_ops"
    assert ev.new_sha == result.sha
    assert ev.committed_at == fixed_ts


# ── (M1) 기본 clock tz-aware 보증 ────────────────────────────────────────────


def test_T11_7b_기본_clock_미주입_generated_at_tz_aware(tmp_path: Path) -> None:
    """ReindexOnCommitListener 기본 clock(미주입) → generated_at이 tz-aware(tzinfo is not None).

    M1 회귀 방어: clock 기본값이 tz-naive(datetime.now)였으면 generated_at.tzinfo is None →
    InMemoryPublishedIndexStore.put의 generated_at 비교에서 TypeError가 터진다.
    """
    from agent_org_network.two_stage_router import InMemoryPublishedIndexStore

    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)
    sink = _FakeReindexSink()
    # clock 미주입 — 기본값 사용
    listener = ReindexOnCommitListener(worker=worker, publish_sink=sink)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="커밋",
    )
    commit_okf_bundle(req, FakeGitGateway(), propagator=listener)

    assert len(sink.received) == 1
    frames = sink.received[0]
    assert len(frames) >= 1

    # generated_at이 tz-aware여야 한다(M1 핵심 단언)
    for frame in frames:
        assert frame.index.generated_at.tzinfo is not None, (
            "기본 clock generated_at이 tz-naive — InMemoryPublishedIndexStore.put 비교에서 TypeError"
        )

    # InMemoryPublishedIndexStore.put에 통과시켜 TypeError 없이 동률/더-새 판정 검증
    store = InMemoryPublishedIndexStore()
    for frame in frames:
        # 첫 삽입 → True(수용)
        accepted = store.put(frame.index)
        assert accepted is True

    # 같은 인덱스 재삽입 → False(동률 거부·TypeError 없음)
    for frame in frames:
        accepted = store.put(frame.index)
        assert accepted is False


# ══════════════════════════════════════════════════════════════════════════════
# T11.7e E2 — 커밋-후 재-publish sink 실패 흡수(ReindexOnCommitListener)
# ══════════════════════════════════════════════════════════════════════════════
#
# 확정 정책(사용자 승인): sink 실패 = 흡수+경고 로그(재시도 큐 없음). 커밋은 이미 디스크
# 확정이고, 전파 회수는 재연결 백스톱(run_worker가 register 직후 publish_frames 전량
# 재송신 + 중앙 generated_at 멱등 흡수)에 위임한다. 따라서 sink 예외가 commit_okf_bundle
# 경계를 넘어가면 안 된다(핵심 단언).


class _RaisingReindexSink:
    """호출 시 항상 예외를 던지는 fake sink — 실 WS 송신 실패(끊김 등)를 흉내."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.call_count = 0

    def __call__(self, frames: list[PublishIndex]) -> None:
        self.call_count += 1
        raise self._exc


def test_T11_7e_정상_sink는_frames를_전달받는다(tmp_path: Path) -> None:
    """무회귀: 정상 sink는 예외 없이 frames를 그대로 전달받는다(기존 동작 보존)."""
    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)
    sink = _FakeReindexSink()
    listener = ReindexOnCommitListener(worker=worker, clock=lambda: _T11B_TS, publish_sink=sink)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="커밋",
    )
    result = commit_okf_bundle(req, FakeGitGateway(), propagator=listener)

    assert result.agent_id == "cs_ops"
    assert len(sink.received) == 1
    assert len(sink.received[0]) >= 1


def test_T11_7e_예외를_던지는_sink는_흡수되고_커밋은_정상_완료된다(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """sink가 예외를 던져도 commit_okf_bundle이 예외 없이 CommitResult를 반환한다.

    경계 단언(핵심): sink 예외가 commit_okf_bundle 밖으로 안 나온다. 커밋은 이미 디스크
    확정(FakeGitGateway가 커밋 결과를 실제로 기록)이고, 경고 로그가 남는다(caplog).
    """
    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)
    failing_sink = _RaisingReindexSink(ConnectionError("워커 소켓 끊김"))
    listener = ReindexOnCommitListener(worker=worker, clock=lambda: _T11B_TS, publish_sink=failing_sink)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="커밋",
    )

    with caplog.at_level(logging.WARNING):
        # 예외 없이 반환돼야 한다 — sink 실패가 commit_okf_bundle 경계를 넘으면 안 됨.
        result = commit_okf_bundle(req, FakeGitGateway(), propagator=listener)

    # 커밋은 정상 완료(디스크/커밋 로그 확정) — sha·agent_id가 정상적으로 채워짐.
    assert result.agent_id == "cs_ops"
    assert result.sha != ""

    # sink는 호출됐지만(흡수 전에 시도는 함) 예외가 삼켜졌다.
    assert failing_sink.call_count == 1

    # 경고 로그가 남는다.
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_T11_7e_예외_sink여도_last_frames는_갱신되지_않는다(tmp_path: Path) -> None:
    """sink 호출이 publish_frames 이후이므로, sink 예외와 무관하게 last_frames는 이미 채워진다.

    흡수 지점이 publish_sink 호출만 감싸므로 frames 자체(도출 결과)는 영향받지 않는다 —
    on_okf_committed 흐름 자체가 깨지지 않는다는 방증.
    """
    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)
    failing_sink = _RaisingReindexSink(RuntimeError("송신 실패"))
    listener = ReindexOnCommitListener(worker=worker, clock=lambda: _T11B_TS, publish_sink=failing_sink)

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="커밋",
    )
    commit_okf_bundle(req, FakeGitGateway(), propagator=listener)

    assert len(listener.last_frames) >= 1


def test_T11_7e_예외_sink_반복_커밋도_계속_흡수된다(tmp_path: Path) -> None:
    """연속 커밋 각각에서 sink가 매번 예외를 던져도 매 커밋이 예외 없이 완료된다(재현성)."""
    _write_okf_file(tmp_path, "cs_ops", "refund.md", front="title: 환불\ndescription: d\ntags: [환불]")
    worker = _worker_with_okf("alice", "cs_ops", tmp_path)
    failing_sink = _RaisingReindexSink(OSError("연결 거부"))
    listener = ReindexOnCommitListener(worker=worker, clock=lambda: _T11B_TS, publish_sink=failing_sink)
    gw = FakeGitGateway()

    req = BuilderCommitRequest(
        agent_id="cs_ops",
        owner="alice",
        files=(OkfFile(path="refund.md", content="---\ntitle: 환불\n---\n"),),
        message="커밋",
    )
    for _ in range(3):
        result = commit_okf_bundle(req, gw, propagator=listener)
        assert result.agent_id == "cs_ops"

    assert failing_sink.call_count == 3
