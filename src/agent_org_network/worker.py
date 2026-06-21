"""Owner Worker — owner PC에서 도는 워커 프로세스 (T6.3 슬라이스2b-ii, ADR 0011 결정 6).

owner PC의 작은 실행 주체가 중앙에 *아웃바운드 WebSocket*으로 연결해(중앙은 받기만,
결정 1) 자기 owner 작업 큐의 작업을 받아(중앙이 그 소켓으로 `PushWork`), 로컬 Claude
Code(`ClaudeCodeRuntime` 재사용, ADR 0010)로 답을 만들어 중앙에 회신(`SubmitAnswer`)한다.
한 owner = 한 워커 프로세스(여러 owner면 여러 워커).

설계(결정론과 비결정 분리, 결정 6-6):
  - **결정론 가능한 부분은 순수 로직으로 분리한다** — `WorkerLogic`(프레임 핸들링: PushWork
    수신→`ClaudeCodeRuntime`로 답→`SubmitAnswer` 생성)·`backoff_seconds`(재연결 백오프)·
    `parse_central_frame`(중앙→워커 프레임 복원). 이들은 WS 소켓·실 claude 없이 단위
    테스트한다(FakeRunner 주입, fake send/recv). 게이트(`uv run pytest`)는 이것만 본다.
  - **WS I/O·재연결 루프·실 claude subprocess는 비결정·느림** — `run_worker`가 실
    아웃바운드 WS를 열고 위 순수 로직을 구동한다. 이는 수동 시연 영역(게이트 밖)이다.

카드 출처(결정): 워커는 자기 owner의 `agent_id → AgentCard` 매핑을 보유한다. `PushWork`의
`TicketFrame`은 식별자(`agent_id`)만 싣고 카드 본문은 안 싣는다(CONTEXT — 카드 본문이 아니라
식별자만). 워커가 owner 환경에서 자기 카드(담당 영역·지식 출처)를 들고 있는 게 ADR 0011의
분산 정신("Authority·지식은 owner 환경에 있다")과 정합한다. 못 찾는 agent_id는 graceful
폴백 답을 회신해 작업이 미아가 되지 않게 한다(중앙 큐는 SubmitAnswer로 종착).

미아 없음(결정 6-4): 워커가 죽거나 끊겨도 중앙 큐의 작업은 `release_claims`→재push 또는
timeout→escalation으로 종착한다(2b-i가 보장). 재연결 시 진행 중이던 작업의 중복 처리는
`ticket_id` 멱등(2b-i)으로 흡수된다 — 워커는 같은 작업을 다시 받아 답해도 무방하다.
"""

from collections.abc import Callable
from typing import Any, cast

from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import Answer, ClaudeCodeRuntime
from agent_org_network.transport import (
    AuthError,
    CentralFrame,
    Ping,
    PushWork,
    RegisterWorker,
    SubmitAnswer,
    Welcome,
    from_ticket_frame,
    to_answer_frame,
)

# ── 재연결 백오프(순수 로직) ────────────────────────────────────────────────

DEFAULT_BASE_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 30.0


def backoff_seconds(
    attempt: int,
    base: float = DEFAULT_BASE_BACKOFF_SECONDS,
    cap: float = DEFAULT_MAX_BACKOFF_SECONDS,
) -> float:
    """재연결 시도 횟수에 대한 지수 백오프 대기 시간(초)을 계산한다(순수 함수).

    owner PC는 간헐 연결이 정상이라(결정 6-4) 끊기면 재연결을 반복한다. 너무 자주
    재시도하면 중앙을 두드리므로 지수적으로 늘리되 `cap`에서 멈춘다 — `attempt`(0부터)
    가 커질수록 `base * 2**attempt`, 단 `cap` 상한. attempt 음수는 0으로 본다(방어).
    결정론: 시계·난수 없이 attempt만으로 정해지므로 단위 테스트로 고정한다(지터는 후속).
    """
    if attempt < 0:
        attempt = 0
    # 큰 attempt에서 2**attempt 오버플로/비용을 피해 cap 도달 후엔 곧장 cap.
    if attempt >= 32:
        return cap
    return min(base * (2.0**attempt), cap)


# ── 중앙→워커 프레임 복원(순수 로직) ────────────────────────────────────────


def parse_central_frame(raw: object) -> CentralFrame | None:
    """중앙이 보낸 JSON을 다운스트림 프레임으로 검증·복원한다(미지/불량은 None).

    `type` 판별 필드로 갈라 pydantic v2로 검증한다. 알 수 없거나 검증 실패면 None을
    돌려 워커가 무시한다(와이어 안전 — 미지 프레임이 워커를 깨뜨리지 않는다). 중앙이
    워커→중앙 업스트림 프레임을 복원하는 것과 대칭(이쪽은 중앙→워커 `CentralFrame`).
    """
    if not isinstance(raw, dict):
        return None
    payload = cast(dict[str, Any], raw)
    frame_type = payload.get("type")
    model: type[CentralFrame]
    if frame_type == "welcome":
        model = Welcome
    elif frame_type == "auth_error":
        model = AuthError
    elif frame_type == "push_work":
        model = PushWork
    elif frame_type == "ping":
        model = Ping
    else:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        return None


# ── 워커 프레임 핸들링(결정론 로직) ─────────────────────────────────────────


class WorkerLogic:
    """워커의 *프레임 핸들링* 결정론 로직 — WS 소켓·재연결 루프와 분리(테스트 가능).

    `PushWork` 한 건을 받아 (1) `agent_id`로 자기 카드를 찾고, (2) `ClaudeCodeRuntime`
    (로컬 claude, `runtime.py` 재사용 — 새 호출 로직 재구현 금지)으로 답을 만들고,
    (3) `SubmitAnswer` 프레임을 만들어 돌려준다. WS·실 claude는 주입으로 가린다 —
    단위 테스트는 FakeRunner를 박은 `ClaudeCodeRuntime`을 넘겨 결정론으로 고정한다.

    카드 매핑: `agent_id → AgentCard`(자기 owner의 카드들). 못 찾는 agent_id는 graceful
    폴백 답을 회신한다 — 작업이 미아로 큐에 떠 있지 않게(SubmitAnswer로 중앙 큐 종착).
    """

    def __init__(
        self,
        owner_id: str,
        cards: dict[str, AgentCard],
        runtime: ClaudeCodeRuntime | None = None,
    ) -> None:
        self._owner_id = owner_id
        self._cards = cards
        # 로컬 claude 호출은 runtime.py의 ClaudeCodeRuntime을 그대로 재사용한다(재구현 금지).
        # 미주입이면 기본 생성자(실 `claude -p`) — 단위 테스트는 FakeRunner 박은 인스턴스 주입.
        self._runtime = runtime if runtime is not None else ClaudeCodeRuntime()

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def register_frame(self, token: str | None = None) -> RegisterWorker:
        """연결 직후 보낼 등록 프레임을 만든다(owner 신원 선언, 결정 6-5).

        `token`은 owner 인증 자리(ADR 0009 → T6.5) — 지금은 None 허용(거부 hook만 존재).
        """
        return RegisterWorker(owner_id=self._owner_id, token=token)

    def handle_push_work(self, push: PushWork) -> SubmitAnswer:
        """`PushWork` 한 건을 처리해 회신할 `SubmitAnswer` 프레임을 만든다(결정론 코어).

        흐름: `TicketFrame` → (연결 owner 귀속으로) `WorkTicket` 복원 → `agent_id`로
        카드 조회 → `ClaudeCodeRuntime.answer(question, card)` → `Answer` → `AnswerFrame`
        → `SubmitAnswer(ticket_id, ...)`. 카드를 못 찾으면 폴백 답(작업 미아 방지). 실
        claude 호출의 timeout/실패 폴백은 `ClaudeCodeRuntime`이 이미 Answer로 흡수한다.
        """
        ticket = from_ticket_frame(push.ticket, self._owner_id)
        card = self._cards.get(ticket.agent_id)
        if card is None:
            # 미등록 agent_id — 이 워커가 답할 카드가 아니다. 작업을 미아로 남기지 않고
            # 폴백 답으로 회신해 중앙 큐를 종착시킨다(answered). 운영상 카드 동기화 누락 신호.
            answer = Answer(
                text=(
                    f"[{ticket.agent_id}] 이 워커(owner '{self._owner_id}')에 해당 담당 "
                    f"영역 카드가 없어 답할 수 없습니다."
                ),
                sources=(),
                mode="full",
            )
        else:
            answer = self._runtime.answer(ticket.question, card)
        return SubmitAnswer(ticket_id=ticket.ticket_id, answer=to_answer_frame(answer))


# ── 실 WS 워커 셸(수동 시연 영역, 게이트 밖) ────────────────────────────────
#
# 아래는 실 아웃바운드 WS 연결·재연결 루프·실 claude를 묶는다 — 비결정·느림이라 단위
# 테스트에 넣지 않는다(결정 6-6, 위 WorkerLogic만 결정론으로 검증). `websockets`의 *동기*
# 클라이언트(`websockets.sync.client.connect`)를 쓴다 — claude subprocess 호출이 동기라
# async 불필요. 한 소켓에서 프레임을 받아 처리(PushWork→답→SubmitAnswer)·생존 응답(Ping→
# Heartbeat)하고, 끊기면 backoff_seconds 만큼 쉬고 재연결한다.


def run_worker(
    logic: WorkerLogic,
    url: str,
    token: str | None = None,
    *,
    reconnect: bool = True,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """실 아웃바운드 WS로 중앙에 붙어 작업을 처리하는 워커 루프(수동 시연, 게이트 밖).

    연결→`RegisterWorker` 전송→`Welcome`/`AuthError` 확인→프레임 수신 루프(`PushWork`→
    `handle_push_work`→`SubmitAnswer`, `Ping`→`Heartbeat`). 끊기면 `reconnect`면
    `backoff_seconds`만큼 쉬고 재연결(attempt 증가), 성공 시 attempt 리셋. `AuthError`면
    재연결해도 거부되므로 멈춘다(미인증). `sleep`은 주입 가능(기본 `time.sleep`).

    이 함수는 실 소켓·실 claude·무한 루프라 단위 테스트 대상이 아니다 — 결정론은
    `WorkerLogic`/`backoff_seconds`/`parse_central_frame`이 이미 닫았다(결정 6-6).
    """
    import time

    from websockets.exceptions import WebSocketException
    from websockets.sync.client import connect

    sleep_fn = sleep if sleep is not None else time.sleep
    attempt = 0
    while True:
        try:
            with connect(url) as ws:
                ws.send(logic.register_frame(token).model_dump_json())
                first = parse_central_frame(_loads(ws.recv()))
                if isinstance(first, AuthError):
                    print(f"[worker:{logic.owner_id}] 등록 거부(AuthError): {first.reason}")
                    return
                if not isinstance(first, Welcome):
                    print(f"[worker:{logic.owner_id}] 예상치 못한 첫 응답 — 재연결 시도")
                    raise _Reconnect
                print(f"[worker:{logic.owner_id}] 중앙에 등록됨({url}). 작업 대기.")
                attempt = 0  # 연결 성공 → 백오프 리셋
                _serve(ws, logic)
        except _Reconnect:
            pass
        except (OSError, EOFError, WebSocketException) as exc:
            # 연결 끊김/네트워크 오류 — owner PC 간헐 연결이 정상(결정 6-4). OSError는
            # 연결 거부(중앙 미가동), WebSocketException은 작업 중 소켓 끊김(ConnectionClosed).
            # 어느 쪽이든 작업은 중앙 큐에서 release_claims→재push 또는 timeout→escalation
            # 으로 종착한다(2b-i 보장) — 워커는 재연결해 같은 작업을 다시 받아도 무방(멱등).
            print(f"[worker:{logic.owner_id}] 연결 끊김: {exc}")
        except KeyboardInterrupt:
            print(f"\n[worker:{logic.owner_id}] 종료.")
            return
        if not reconnect:
            return
        wait = backoff_seconds(attempt)
        print(f"[worker:{logic.owner_id}] {wait:.0f}초 후 재연결(시도 {attempt + 1}).")
        sleep_fn(wait)
        attempt += 1


class _Reconnect(Exception):
    """내부 신호 — 정상 흐름을 벗어나 재연결 루프로 돌아간다."""


def _serve(ws: Any, logic: WorkerLogic) -> None:
    """등록된 소켓에서 프레임을 받아 처리하는 수신 루프(수동 시연 영역).

    `PushWork`→`handle_push_work`→`SubmitAnswer` 전송, `Ping`→`Heartbeat` 응답. 소켓이
    닫히면 `recv`가 예외를 던져 루프를 빠져나가고 `run_worker`가 재연결한다. 실 claude
    호출(`handle_push_work` 안)은 느리므로 한 작업을 끝낸 뒤 다음 프레임을 받는다.
    """
    from agent_org_network.transport import Heartbeat

    while True:
        frame = parse_central_frame(_loads(ws.recv()))
        if isinstance(frame, PushWork):
            print(
                f"[worker:{logic.owner_id}] 작업 수신 "
                f"ticket={frame.ticket.ticket_id[:8]} agent={frame.ticket.agent_id} "
                f"— 로컬 claude 호출 중…"
            )
            submit = logic.handle_push_work(frame)
            ws.send(submit.model_dump_json())
            print(f"[worker:{logic.owner_id}] 답 회신 ticket={submit.ticket_id[:8]}")
        elif isinstance(frame, Ping):
            ws.send(Heartbeat().model_dump_json())
        # Welcome/AuthError/미지 프레임은 수신 루프에선 무시(등록은 끝났다).


def _loads(raw: Any) -> object:
    """WS recv 페이로드(str | bytes)를 JSON 객체로 파싱한다(수동 시연 헬퍼)."""
    import json

    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8")
    return json.loads(cast(str, raw))


# ── CLI 진입점(수동 시연) ────────────────────────────────────────────────────


DEFAULT_WORKER_URL = "ws://127.0.0.1:8000/worker"


def main() -> None:
    """owner 워커 프로세스 진입점 — `python -m agent_org_network.worker --owner <id>`.

    owner_id를 CLI(`--owner`)/env(`OWNER_ID`)로 받고, 데모 샘플(`cards_for_owner`)에서 그
    owner의 카드 매핑을 채워 `WorkerLogic`을 만든 뒤 실 WS로 중앙에 붙는다(`run_worker`).
    중앙 URL은 `--url`/env(`CENTRAL_URL`, 기본 `ws://127.0.0.1:8000/worker`). 인증 토큰
    자리(`--token`)는 ADR 0009 → T6.5(지금은 거부 hook만). 수동 시연이라 게이트 밖이다.
    """
    import argparse
    import os

    from agent_org_network.demo import cards_for_owner

    parser = argparse.ArgumentParser(description="Agent Org Network — Owner Worker(수동 시연)")
    parser.add_argument(
        "--owner",
        default=os.environ.get("OWNER_ID"),
        help="이 워커가 대리할 owner의 User.id (env OWNER_ID)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CENTRAL_URL", DEFAULT_WORKER_URL),
        help=f"중앙 워커 WS URL (env CENTRAL_URL, 기본 {DEFAULT_WORKER_URL})",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("OWNER_TOKEN"),
        help="owner 인증 토큰 자리(ADR 0009 → T6.5, 지금은 거부 hook만)",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="끊기면 재연결하지 않고 종료(디버그용)",
    )
    args = parser.parse_args()

    owner_id: str | None = args.owner
    if not owner_id:
        parser.error("owner를 지정하세요: --owner <id> 또는 env OWNER_ID")

    cards = cards_for_owner(owner_id)
    if not cards:
        # 데모 샘플에 그 owner의 카드가 없으면 처리할 작업이 없다 — 조기 경고(미아 아님,
        # 중앙 큐의 그 owner 작업은 timeout→escalation으로 종착한다, 2b-i).
        print(
            f"[worker:{owner_id}] 경고: 데모 샘플에 owner '{owner_id}'의 카드가 없습니다. "
            "(legal_lead / cs_lead / finance_lead 중 하나여야 함)"
        )
    logic = WorkerLogic(owner_id=owner_id, cards=cards, runtime=ClaudeCodeRuntime())
    print(
        f"[worker:{owner_id}] 카드 {len(cards)}개({', '.join(cards) or '없음'}) — "
        f"중앙 {args.url} 연결 시도."
    )
    run_worker(logic, url=args.url, token=args.token, reconnect=not args.no_reconnect)


if __name__ == "__main__":
    main()
