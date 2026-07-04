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

import logging
import queue
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard, is_safe_path_component
from agent_org_network.knowledge_sync import (
    KnowledgeBundleContent,
    KnowledgeDoc,
    SyncKnowledge,
)
from agent_org_network.okf_index import build_knowledge_index_from_okf
from agent_org_network.runtime import AgentRuntime, Answer
from agent_org_network.runtime_select import select_runtime
from agent_org_network.transport import (
    AuthError,
    CentralFrame,
    DocumentContent,
    FetchDocument,
    Ping,
    PublishIndex,
    PushWork,
    RegisterWorker,
    SubmitAnswer,
    Welcome,
    WorkerRole,
    from_ticket_frame,
    to_answer_frame,
)

# ── 경로 traversal 차단 헬퍼(순수 로직) ─────────────────────────────────────


def _is_safe_path_component(name: str) -> bool:
    """단일 파일명 컴포넌트 검증 — agent_card.is_safe_path_component로 위임(단일 권위).

    자체 화이트리스트를 재정의하지 않는다. 공유 권위는 agent_card.py에 있다(ADR 0028 §15).
    """
    return is_safe_path_component(name)


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
    elif frame_type == "fetch_document":
        model = FetchDocument  # ADR 0028 §15 결정 A — 추가 한 줄(기존 분기 무회귀·새 키)
    else:
        return None
    try:
        return model.model_validate(payload)
    except ValidationError:
        return None


# ── 초안 보류: PendingDraft (owner 워커측 in-flight 상태, ADR 0025 결정 4·T9.7 S2) ────
#
# owner 워커가 LLM 초안을 만든 뒤 HITL on이면 *즉시 SubmitAnswer 하지 않고* owner 검토를
# 기다리는 워커 로컬 상태(CONTEXT "초안 보류(Pending Draft)"). 중앙의 `Answer.mode=
# "draft_only"`(사용자向 노출값)와는 *다른 층* — 이건 *언제 회신하나*(배송 타이밍)이고
# draft_only는 *어떻게 표시하나*(노출 mode)라 축이 다르다. frozen 값 객체라 승인/수정은
# 새 인스턴스를 낳지 않는다(이 값 객체 자체는 변경하지 않고 새 SubmitAnswer만 만든다) —
# "전이 ≠ 기록"의 결이되, 여기선 그냥 store에서 제거되는 것으로 종착한다(별 이력 불요,
# 감사·트랜스크립트 적재는 중앙 몫).


@dataclass(frozen=True)
class PendingDraft:
    """HITL on일 때 즉시 회신하지 않고 owner 검토를 기다리는 워커측 초안(frozen 값 객체).

    `ticket_id`가 store의 조회 키(멱등 키 정신 — `WorkTicket.ticket_id`와 동일 역할).
    `draft_answer`는 LLM이 만든 초안 `Answer`(text·sources·mode). `context`는 그 사용자의
    발화 스레드(ADR 0027 결정 13) — 보류 중에도 잃지 않고 보관해 `submit_pending_draft`가
    필요시 참조할 수 있게 한다(현재 구현은 회신에 재사용하지 않음 — 답은 이미 만들어졌으므로).
    `made_at`은 초안이 만들어진 시각(관측·감사 연결점, 주입 clock).
    """

    ticket_id: str
    question: str
    draft_answer: Answer
    agent_id: str
    context: str | None
    made_at: datetime


# ── 워커 프레임 핸들링(결정론 로직) ─────────────────────────────────────────


class WorkerLogic:
    """워커의 *프레임 핸들링* 결정론 로직 — WS 소켓·재연결 루프와 분리(테스트 가능).

    `PushWork` 한 건을 받아 (1) `agent_id`로 자기 카드를 찾고, (2) `ClaudeCodeRuntime`
    (로컬 claude, `runtime.py` 재사용 — 새 호출 로직 재구현 금지)으로 답을 만들고,
    (3) `SubmitAnswer` 프레임을 만들어 돌려준다. WS·실 claude는 주입으로 가린다 —
    단위 테스트는 FakeRunner를 박은 `ClaudeCodeRuntime`을 넘겨 결정론으로 고정한다.

    카드 매핑: `agent_id → AgentCard`(자기 owner의 카드들). 못 찾는 agent_id는 graceful
    폴백 답을 회신한다 — 작업이 미아로 큐에 떠 있지 않게(SubmitAnswer로 중앙 큐 종착).

    등급(`role`, ADR 0012 결정 2, T6.6 슬라이스 iv): 이 워커가 그 owner 안에서 primary
    (owner PC 워커)인지 backup(owner 위임 격리 인스턴스)인지. 등록 프레임(`register_frame`)에
    실려 중앙이 등급별 레지스트리에 올린다. backup 워커도 *같은* WorkerLogic·같은 카드로
    답한다 — 답의 신뢰 하향(mode=backup)은 워커가 아니라 *연결 등급*을 진실로 디스패처가
    강제한다(결정 4, 워커 자기보고 아님). 데모는 같은 카드 데이터를 쓰고, 실 동기화 스냅샷
    기반 카드는 후속(ADR 0012 결정 9 — 연결점만).
    """

    def __init__(
        self,
        owner_id: str,
        cards: dict[str, AgentCard],
        runtime: AgentRuntime,
        role: WorkerRole = "primary",
        okf_root: "Path | None" = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        knowledge_paths: "dict[str, tuple[str, ...]] | None" = None,
    ) -> None:
        self._owner_id = owner_id
        self._cards = cards
        # 명시 지정 지식 동기화 경계(Phase 12 (B)·ADR 0033 결정 3·외부 결정 ①): agent_id →
        # 지정 파일/디렉터리 경로들(okf_root 상대). 담당자가 *명시 지정한* 것만 동기화한다
        # (owner 환경 전체를 올리지 않음). `main`이 env `AON_KNOWLEDGE_PATHS`를 파싱해 채운다
        # (게이트 밖). 미주입이면 지식 동기화 안 함(빈 프레임 — 하위호환).
        self._knowledge_paths: dict[str, tuple[str, ...]] = knowledge_paths or {}
        # owner의 로컬 OKF 번들 루트(`okf_root/{agent_id}/*.md`). publish_frames가 여기서
        # 인덱스를 도출한다(ADR 0028 §14 결정 E). None이면 publish_frames가 빈 리스트(OKF
        # 없는 워커는 배포 안 함·하위호환). *워커측* 호출 — 중앙은 OKF를 안 읽는다(비소유).
        self._okf_root = okf_root
        # 이 워커의 등급(그 owner 안에서의 push 우선순위). 등록 프레임에 실린다 — 하위호환
        # 기본은 primary(기존 워커는 그대로 1차 워커). backup도 같은 로직·카드로 답하고,
        # 신뢰 하향은 디스패처가 연결 등급을 진실로 강제한다(결정 4).
        self._role: WorkerRole = role
        # 답 생성 런타임은 AgentRuntime *포트*로 받는다 — 기본은 ClaudeCodeRuntime(`claude -p`
        # 서브프로세스·로컬 claude 인증), opt-in이면 ClaudeApiRuntime(owner OAuth 인프로세스
        # SDK 스트리밍·ADR 0027 T9.6). 두 구현 모두 같은 포트(answer(question, card, context))라
        # 워커 로직은 무변경(런타임 교체가 종착·라우팅·노출을 안 바꿈). runtime은 필수 주입 —
        # 진입점(main)만 실 기본값을 넣고 단위 테스트는 FakeRunner 박은 인스턴스를 준다.
        self._runtime = runtime
        # 초안 보류 시각 clock(ADR 0025 결정 4·T9.7 S2) — 결정론 테스트가 고정 시각 주입.
        self._clock = clock
        # 워커 로컬 인메모리 초안 보류 store(ticket_id → PendingDraft). HITL on 힌트를 받은
        # 작업이 즉시 회신되지 않고 여기 담긴다. **워커측 TTL 없음**(판정 — 방치된 보류의
        # 종착은 중앙 큐 timeout→escalation 단일 진실이 이미 떠받친다, ADR 0025 결정 4).
        self._pending_drafts: dict[str, PendingDraft] = {}

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def role(self) -> WorkerRole:
        return self._role

    def register_frame(self, token: str | None = None) -> RegisterWorker:
        """연결 직후 보낼 등록 프레임을 만든다(owner 신원·등급 선언, 결정 6-5·ADR 0012 결정 2).

        `token`은 owner 인증 자리(ADR 0009 → T6.5) — 지금은 None 허용(거부 hook만 존재).
        `role`은 이 워커의 등급(primary/backup)을 실어 중앙이 등급별 레지스트리에 올린다.
        """
        return RegisterWorker(owner_id=self._owner_id, token=token, role=self._role)

    def publish_frames(self, generated_at: datetime) -> list[PublishIndex]:
        """자기 소유 카드마다 로컬 OKF에서 KnowledgeIndex를 도출해 PublishIndex로 만든다.

        ADR 0028 §14 결정 E — 워커가 `RegisterWorker`(연결·인증) *직후* 이 프레임들을
        송신한다(실 송신 자리는 `run_worker`의 register 직후·게이트 밖). 결정론 코어:
        `build_knowledge_index_from_okf`(순수·결정론)로 자기 카드들(`self._cards`)→인덱스→
        프레임. `generated_at`은 publish 시점 시각(staleness 키, 결정 C) — 주입 clock으로
        고정해 단위 테스트한다(`handle_push_work`가 FakeRunner로 결정론인 정신). `okf_root`
        미주입이면 빈 리스트(OKF 없는 워커는 배포 안 함). 카드 선언 순서(dict 삽입 순서)대로.

        **중앙은 OKF를 안 읽는다** — 이 도출이 *워커측*에서 일어나고 중앙은 PublishIndex를
        받아 보관만 한다(비소유 강화·데모 시드 지름길 제거 방향).
        """
        if self._okf_root is None:
            return []
        return [
            PublishIndex(
                index=build_knowledge_index_from_okf(
                    card, self._okf_root, generated_at=generated_at
                )
            )
            for card in self._cards.values()
        ]

    def knowledge_sync_frames(self, synced_at: datetime) -> list[SyncKnowledge]:
        """지정 경계(`self._knowledge_paths`)의 파일을 읽어 SyncKnowledge 프레임을 만든다.

        Phase 12 (B)·ADR 0033 결정 3 — 워커가 "답변 실행자"에서 "지식 공급자"로 전환하는
        핵심 발신. `publish_frames`(목차 도출)와 대칭이되 이건 *본문 동기화*다. 흐름:
        자기 소유 카드(`self._cards`)마다 지정 경로(`self._knowledge_paths[agent_id]`)의
        파일들을 `okf_root` 기준으로 읽어 `KnowledgeDoc`(path·body)를 만들고, 그 묶음을
        `KnowledgeBundleContent`(agent_id·documents·version·synced_at)로 싸 `SyncKnowledge`로
        만든다. 실 파일 읽기는 *워커 쪽*이다(중앙은 본문을 받기만 — 비소유 정신 유지).

        결정론: 파일 내용 + 주입 `synced_at`으로 정해진다(`publish_frames`가 주입 clock으로
        결정론인 정신). `version`은 본문 해시로 도출해 같은 본문이면 같은 version(중앙
        `KnowledgeStore.put`이 같은 version을 멱등 무시 — 재송신 안전). okf_root 미주입이거나
        지정 경로가 없는 카드는 프레임 생성 안 함(빈 지정 = 동기화 안 함·하위호환).

        경계 안전: 지정 경로가 디렉터리면 그 아래 파일들을, 파일이면 그 파일을 읽는다.
        okf_root 밖으로 새는 경로(traversal)는 resolve 후 하위 확인으로 차단(handle_fetch_document
        정신). 읽기 실패(없음·권한)는 그 파일만 건너뛴다(부분 실패가 전체를 막지 않음).
        """
        if self._okf_root is None:
            return []
        frames: list[SyncKnowledge] = []
        for agent_id in self._cards:
            paths = self._knowledge_paths.get(agent_id)
            if not paths:
                continue
            docs = self._read_knowledge_docs(agent_id, paths)
            if not docs:
                continue
            version = self._content_version(docs)
            frames.append(
                SyncKnowledge(
                    content=KnowledgeBundleContent(
                        agent_id=agent_id,
                        documents=tuple(docs),
                        version=version,
                        synced_at=synced_at,
                    )
                )
            )
        return frames

    def _read_knowledge_docs(
        self, agent_id: str, paths: tuple[str, ...]
    ) -> list[KnowledgeDoc]:
        """지정 경로(okf_root 상대)의 파일 본문을 읽어 `KnowledgeDoc` 리스트로 만든다(정렬 결정론).

        각 지정 경로가 디렉터리면 그 아래 모든 파일을, 파일이면 그 파일 하나를 읽는다.
        traversal 차단: resolve 후 okf_root 하위임을 확인한다(밖이면 건너뜀). doc.path는
        okf_root 기준 상대 경로(POSIX)로 실어 중앙 admission이 spec 경계와 대조 가능하게 한다.
        """
        assert self._okf_root is not None
        root = self._okf_root.resolve()
        collected: dict[str, str] = {}
        for rel in paths:
            target = (root / rel).resolve()
            if not target.is_relative_to(root):
                continue  # okf_root 밖 — traversal 차단
            if target.is_dir():
                files = sorted(p for p in target.rglob("*") if p.is_file())
            elif target.is_file():
                files = [target]
            else:
                continue  # 없음 — 건너뜀
            for f in files:
                try:
                    body = f.read_text(encoding="utf-8")
                except OSError:
                    continue  # 읽기 실패 — 그 파일만 건너뜀(부분 실패 흡수)
                collected[f.relative_to(root).as_posix()] = body
        return [KnowledgeDoc(path=p, body=collected[p]) for p in sorted(collected)]

    @staticmethod
    def _content_version(docs: list[KnowledgeDoc]) -> str:
        """문서 묶음의 결정론 version(본문 해시) — 같은 본문이면 같은 version(멱등 재송신).

        중앙 `KnowledgeStore.put`이 같은 version을 무시하므로(멱등), 파일이 안 바뀌면
        주기 재송신이 스토어를 흔들지 않는다. sha256 12자로 충분(충돌 무시 가능·짧게).
        """
        import hashlib

        h = hashlib.sha256()
        for doc in docs:
            h.update(doc.path.encode("utf-8"))
            h.update(b"\0")
            h.update(doc.body.encode("utf-8"))
            h.update(b"\0")
        return h.hexdigest()[:12]

    def handle_push_work(self, push: PushWork) -> SubmitAnswer | None:
        """`PushWork` 한 건을 처리한다 — HITL 힌트 off면 즉시 `SubmitAnswer`, on이면 보류.

        흐름: `TicketFrame` → (연결 owner 귀속으로) `WorkTicket` 복원 → `agent_id`로
        카드 조회 → `ClaudeCodeRuntime.answer(question, card)` → `Answer`. 카드를 못 찾으면
        HITL 힌트와 무관하게 *즉시* 폴백 답으로 회신한다(작업 미아 방지 — 검토할 실 초안이
        없다). 카드를 찾아 LLM 초안이 만들어지면:
          - **힌트 off(`push.ticket.hitl=False`, 기본값·하위호환)** — 기존 동작 그대로
            `SubmitAnswer`를 즉시 반환한다.
          - **힌트 on** — *즉시 반환하지 않고* `PendingDraft`를 워커 로컬 store에 담고
            `None`을 반환한다(owner 검토 대기, ADR 0025 결정 4·T9.7 S2). 이후
            `submit_pending_draft`가 승인/수정된 답을 `SubmitAnswer`로 만든다.

        실 claude 호출의 timeout/실패 폴백은 `ClaudeCodeRuntime`이 이미 Answer로 흡수한다.
        """
        ticket = from_ticket_frame(push.ticket, self._owner_id)
        card = self._cards.get(ticket.agent_id)
        if card is None:
            # 미등록 agent_id — 이 워커가 답할 카드가 아니다. HITL 힌트와 무관하게 즉시
            # 폴백 답으로 회신해 중앙 큐를 종착시킨다(answered). 운영상 카드 동기화 누락 신호.
            answer = Answer(
                text=(
                    f"[{ticket.agent_id}] 이 워커(owner '{self._owner_id}')에 해당 담당 "
                    f"영역 카드가 없어 답할 수 없습니다."
                ),
                sources=(),
                mode="full",
            )
            return SubmitAnswer(ticket_id=ticket.ticket_id, answer=to_answer_frame(answer))

        # 분산 WS 경로 맥락 전파(ADR 0027 결정 13·T9.7 S1) — ticket.context가 그 사용자의
        # 발화 스레드를 owner 워커의 런타임까지 나른다(로컬 경로 answer(context=)와 대칭).
        answer = self._runtime.answer(ticket.question, card, context=ticket.context)

        if push.ticket.hitl:
            # HITL on 힌트 — 즉시 회신하지 않고 owner 검토를 기다린다(초안 보류, 결정 4).
            # 토글 진실은 중앙(디스패처가 프레임에 실은 힌트)이라 워커는 그저 지시받아
            # 따른다 — 이 워커가 토글을 소유하지 않는다(ADR 0025 결정 5).
            self._pending_drafts[ticket.ticket_id] = PendingDraft(
                ticket_id=ticket.ticket_id,
                question=ticket.question,
                draft_answer=answer,
                agent_id=ticket.agent_id,
                context=ticket.context,
                made_at=self._clock(),
            )
            return None

        return SubmitAnswer(ticket_id=ticket.ticket_id, answer=to_answer_frame(answer))

    def pending_draft(self, ticket_id: str) -> PendingDraft | None:
        """워커 로컬 초안 보류 store 조회 진입점(결정론 관측·owner 검토 UI 연결점).

        방치된 보류는 여기 그대로 남는다 — 워커는 TTL을 두지 않는다(미아 없음은 중앙 큐
        timeout→escalation이 이미 떠받침, ADR 0025 결정 4).
        """
        return self._pending_drafts.get(ticket_id)

    def pending_drafts(self) -> list[PendingDraft]:
        """보류 중인 초안 전량을 store 삽입 순서(워커가 받은 순서)로 돌려준다(관측·검토 UI 목록).

        owner 로컬 검토 웹(`owner_web.create_owner_app`)의 목록 조회 연결점 — 단건
        `pending_draft`의 목록판. 방어적 복사(새 list) — 호출자가 store를 흔들 수 없다.
        """
        return list(self._pending_drafts.values())

    def submit_pending_draft(
        self, ticket_id: str, edited_text: str | None = None
    ) -> SubmitAnswer:
        """보류 중인 초안을 승인(원문 그대로) 또는 수정 반영해 `SubmitAnswer`로 회신한다.

        owner 검토 루프(CONTEXT "owner 검토 루프")의 종착 진입점 — `edited_text`가 None이면
        보류된 초안 그대로(승인), 아니면 그 텍스트로 교체한 새 `Answer`(source·mode는
        원 초안 보존 — `Answer`는 frozen이라 새 인스턴스로 교체, 파괴적 변경 아님)를 만든다.
        전송 후 보류 항목은 store에서 제거된다(전이 ≠ 기록 — 감사 적재는 중앙 몫).

        `ticket_id`가 store에 없으면(미보류·이미 제출·미지) `KeyError`(호출자 오용 방어 —
        존재 여부는 `pending_draft`로 먼저 확인 가능).
        """
        pending = self._pending_drafts.get(ticket_id)
        if pending is None:
            raise KeyError(f"보류된 초안이 없습니다: ticket_id={ticket_id}")
        answer = pending.draft_answer
        if edited_text is not None:
            answer = Answer(text=edited_text, sources=answer.sources, mode=answer.mode)
        del self._pending_drafts[ticket_id]
        return SubmitAnswer(ticket_id=ticket_id, answer=to_answer_frame(answer))

    def handle_fetch_document(self, fetch: FetchDocument) -> DocumentContent:
        """`FetchDocument` 한 건을 처리해 회신할 `DocumentContent`를 만든다(결정론 코어·결정 D).

        흐름: `agent_id`가 *자기 소유 카드*(`self._cards`)인지 확인 → `okf_root/{agent_id}/
        {concept_id}.md`(concept.id=파일 stem) 읽기 → 본문을 `DocumentContent`로 회신.
        `request_id`는 그대로 echo해 중앙이 요청과 짝짓는다(correlation, 결정 B).

        자기 소유 카드만(사칭 차단·결정 D): `agent_id`가 `self._cards`에 없으면 남의 owner
        카드 문서를 owner 사칭으로 끌어내려는 것이라 `found=False`로 거부 회신한다(워커측
        1차 권한 게이트·§14 워커-소유자 스코핑의 fetch판). 파일 없음·okf_root 미주입도
        `found=False`·`content=""`(예외가 아니라 정상 회신 — 요청이 미아로 안 떠 있게).

        경로 traversal 차단(보안 게이트·B1): `concept_id`·`agent_id` 각각을 화이트리스트로
        검증한 뒤 resolve() 후 is_relative_to(base)로 okf_root/{agent_id} 하위임을 재확인한다.
        거부 시 `found=False`(degradation 일관·예외 아님). 워커가 최종 신뢰 경계.

        `publish_frames`가 OKF를 *읽는 정신*(워커측 도출·중앙은 안 읽음)과 같은 자리 — 단
        이번엔 *목차 도출*이 아니라 *본문 반환*이다(비소유 중계의 owner측 끝).
        """
        if fetch.agent_id not in self._cards or self._okf_root is None:
            # 미소유 카드(사칭) 또는 OKF 루트 미주입 — 거부 회신(found=False).
            return DocumentContent(request_id=fetch.request_id, found=False, content="")
        # ── 경로 traversal 차단 1단계: 화이트리스트(순수 파일명 컴포넌트 여부) ──
        # agent_id·concept_id 각각이 os.sep·'/'·'\\'·'..'·'.'·빈 문자열·절대경로·
        # 선행 '.'(숨김)을 포함하면 거부한다. Path(x).name == x 조건으로 구분자·상대경로
        # 성분을 한 번에 잡고, 예약 stem(".", "..")도 명시 거부한다.
        if not _is_safe_path_component(fetch.agent_id):
            return DocumentContent(request_id=fetch.request_id, found=False, content="")
        if not _is_safe_path_component(fetch.concept_id):
            return DocumentContent(request_id=fetch.request_id, found=False, content="")
        # ── 경로 traversal 차단 2단계: resolve 후 하위 확인(심볼릭·잔여 traversal) ──
        # base를 agent_id 디렉터리로 resolve한 뒤, 대상 파일을 resolve해 base 하위인지 검사.
        # 심볼릭링크가 okf_root 밖을 가리켜도 이 검사가 차단한다(ADR 0028 §15 B1 결정).
        base = (self._okf_root / fetch.agent_id).resolve()
        doc_path = (base / f"{fetch.concept_id}.md").resolve()
        if not doc_path.is_relative_to(base):
            return DocumentContent(request_id=fetch.request_id, found=False, content="")
        if not doc_path.is_file():
            # 파일 없음 — 정상 회신(예외 아님·web가 "문서 없음" 표시).
            return DocumentContent(request_id=fetch.request_id, found=False, content="")
        content = doc_path.read_text(encoding="utf-8")
        return DocumentContent(request_id=fetch.request_id, found=True, content=content)


# ── ReindexOnCommitListener: OKF 커밋 후 publish_frames 배선 ─────────────────


def _noop_sink(frames: list[PublishIndex]) -> None:
    """기본 no-op publish sink — 실 WS 송신은 T11.7e seam에서 채운다."""


class ReindexOnCommitListener:
    """OKF 커밋 직후 `WorkerLogic.publish_frames`(디스크 재도출)로 PublishIndex를 구성한다.

    `commit_okf_bundle(propagator=...)`의 `ChangeEventListener` duck-typed 구현(ADR 0030 S3).
    커밋 후 OKF는 디스크에 확정 → `publish_frames`(`build_knowledge_index_from_okf` 재도출)이
    정확하다. `reindex_incrementally`를 부르지 않는다 — 그건 저작-시점 메모리 증분으로
    `AuthoredOkf` 반환 + `prior` + `changed_sources`가 필요하며 `OkfChangeEvent`가 그 입력을
    못 댄다.

    **MVP 결정 — 전 카드 재배포**: `event.agent_id`는 바뀐 한 번들이지만 `publish_frames`는
    워커의 *모든 카드*를 재배포한다. 멱등·과배포 무해·놓침 0 정신. 카드 단위 필터는 후속.

    **실 WS 배선**: `publish_sink`는 T11.7e에서 실 WS 송신 콜백으로 채워진다(기본은 no-op).

    **sink 실패 정책(T11.7e E2, 사용자 승인) — 흡수+경고 로그·재시도 큐 없음**: sink는 임의
    전송 콜백(실 소켓 write 등)이라 커밋 시점에 워커가 끊겨 있을 수 있다. 이 실패가
    `commit_okf_bundle` 경계를 넘어가면 안 된다 — 커밋은 이미 디스크에 확정됐고, 전파 실패가
    그 확정을 되돌릴 이유가 없다(전이 ≠ 기록, 커밋은 전이·sink는 그 뒤 부가 통지). 놓친
    publish의 회수는 재연결 백스톱(`run_worker`가 register 직후 `publish_frames` 전량을
    재송신 + 중앙 `generated_at` 멱등 흡수)에 위임한다 — 커밋이 이미 디스크 확정이라 재연결
    시 최신 상태로 다시 도출·재송신하면 데이터 손실이 없다. 그래서 예외를 광범위하게
    (`Exception`) 흡수하는 게 맞다 — sink 구현이 던질 수 있는 예외 종류(연결 끊김·타임아웃·
    직렬화 실패 등)를 이 리스너가 전부 알 수 없고, 어떤 sink 예외든 커밋을 깨면 안 되기
    때문이다. 재-raise하지 않는다.

    관측 속성 `last_frames`: 단위 테스트 단언용. 마지막 on_okf_committed 결과를 기록한다
    (sink 실패 여부와 무관하게 frames 자체는 채워진다 — 흡수 지점은 sink 호출뿐).
    """

    def __init__(
        self,
        worker: WorkerLogic,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        publish_sink: Callable[[list[PublishIndex]], None] = _noop_sink,
    ) -> None:
        self._worker = worker
        self._clock = clock
        self._publish_sink = publish_sink
        self.last_frames: list[PublishIndex] = []

    def on_okf_committed(self, event: Any) -> None:
        frames = self._worker.publish_frames(generated_at=self._clock())
        try:
            self._publish_sink(frames)
        except Exception:
            # 흡수 + 경고 로그(재시도 큐 없음) — 커밋은 이미 디스크 확정, 전파 회수는
            # 재연결 백스톱에 위임(위 docstring 정책 참조). 재-raise하지 않는다.
            logging.warning("publish_sink 실패 — 흡수(재연결 백스톱이 회수)", exc_info=True)
        self.last_frames = frames


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
    outbound: "queue.Queue[SubmitAnswer] | None" = None,
) -> None:
    """실 아웃바운드 WS로 중앙에 붙어 작업을 처리하는 워커 루프(수동 시연, 게이트 밖).

    연결→`RegisterWorker` 전송→`Welcome`/`AuthError` 확인→프레임 수신 루프(`PushWork`→
    `handle_push_work`→`SubmitAnswer`, `Ping`→`Heartbeat`). 끊기면 `reconnect`면
    `backoff_seconds`만큼 쉬고 재연결(attempt 증가), 성공 시 attempt 리셋. `AuthError`면
    재연결해도 거부되므로 멈춘다(미인증). `sleep`은 주입 가능(기본 `time.sleep`).

    `outbound`(owner 검토 UI 겸직 배선·D2·ADR 0025 결정 4): owner 로컬 웹(`owner_web`)이
    다른 스레드에서 처분한 `SubmitAnswer`를 실어 보내는 스레드 안전 큐. 주입되면 `_serve`가
    recv를 짧은 타임아웃으로 폴링하며 매 주기 이 큐를 flush해 활성 소켓으로 송신한다 —
    승인/수정된 초안이 그 owner의 *같은* WS 연결로 회신된다. 미주입(기본)이면 기존 블로킹
    recv 그대로(하위호환·기존 동작 무변경).

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
                    print(
                        f"[worker:{logic.owner_id}|{logic.role}] 등록 거부(AuthError): {first.reason}"
                    )
                    return
                if not isinstance(first, Welcome):
                    print(
                        f"[worker:{logic.owner_id}|{logic.role}] 예상치 못한 첫 응답 — 재연결 시도"
                    )
                    raise _Reconnect
                print(f"[worker:{logic.owner_id}|{logic.role}] 중앙에 등록됨({url}). 작업 대기.")
                attempt = 0  # 연결 성공 → 백오프 리셋
                # 등록 직후 자기 소유 카드들의 KnowledgeIndex를 중앙에 publish한다(ADR 0028
                # §14 결정 E). 결정론 도출은 publish_frames(WorkerLogic)가 하고, 여기서 실
                # WS로 송신만 한다(게이트 밖). 재연결마다 다시 publish해도 중앙 store가
                # generated_at staleness로 중복을 멱등 흡수한다(결정 C). OKF 미주입이면 빈
                # 리스트라 no-op.
                from datetime import timezone as _tz

                for pub in logic.publish_frames(generated_at=datetime.now(tz=_tz.utc)):
                    ws.send(pub.model_dump_json())
                    print(
                        f"[worker:{logic.owner_id}|{logic.role}] 인덱스 배포 "
                        f"agent={pub.index.agent_id} concepts={len(pub.index.concepts)}"
                    )
                # 지식 동기화 시작 시 1회 발신(Phase 12 (B)·ADR 0033 결정 3) — 지정 경계
                # 파일 본문을 SyncKnowledge로 중앙에 올린다. 커밋=이벤트 즉시 반영 정신이되
                # (결정 3), 실 워커는 시작 시 1회 + 주기 재송신(`_serve`가 interval 만큼)으로
                # 낡음을 회수한다. 중앙 store가 version 멱등으로 무변경 재송신을 흡수한다.
                for sync in logic.knowledge_sync_frames(synced_at=datetime.now(tz=_tz.utc)):
                    ws.send(sync.model_dump_json())
                    print(
                        f"[worker:{logic.owner_id}|{logic.role}] 지식 동기화 "
                        f"agent={sync.content.agent_id} docs={len(sync.content.documents)}"
                    )
                _serve(
                    ws,
                    logic,
                    outbound=outbound,
                    knowledge_sync_interval=knowledge_sync_interval_seconds(),
                )
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


def _drain_outbound(ws: Any, logic: WorkerLogic, outbound: "queue.Queue[SubmitAnswer]") -> None:
    """owner 검토 UI가 처분한 `SubmitAnswer`를 활성 소켓으로 flush한다(겸직 배선·게이트 밖).

    `owner_web`의 submit_sink가 다른 스레드에서 이 큐에 넣은 프레임을 비워 송신한다. 소켓이
    끊긴 사이 처분된 항목은 send가 예외를 던져 `_serve`→`run_worker`가 재연결하고, 다음
    연결에서 다시 처분하면 된다(멱등 — ticket_id 키). non-blocking으로 큐가 비면 즉시 반환.
    """
    while True:
        try:
            submit = outbound.get_nowait()
        except queue.Empty:
            return
        ws.send(submit.model_dump_json())
        print(
            f"[worker:{logic.owner_id}|{logic.role}] 검토 답 회신 ticket={submit.ticket_id[:8]}"
        )


def knowledge_sync_interval_seconds() -> int:
    """`AON_KNOWLEDGE_SYNC_INTERVAL_SECONDS` 설정값(기본 0 = 주기 재송신 없음·보수적).

    ADR 0033 ⑤ 기본 관례(커밋=이벤트 즉시 반영·보수적 기본값). 실 커밋 훅 배선은 후속이라
    MVP는 시작 시 1회 + 옵션 주기 재송신으로 낡음을 회수한다. 0이면 주기 재송신 없음(시작 시
    1회만·가장 보수적). 양수면 그 초마다 지정 경계를 다시 읽어 재송신한다(version 멱등으로
    무변경분은 중앙이 흡수). 30분(1800) 같은 보수적 값을 권장한다(stale 임계와 대칭).
    """
    import os

    raw = (os.environ.get("AON_KNOWLEDGE_SYNC_INTERVAL_SECONDS") or "").strip()
    return int(raw) if raw else 0


def _serve(
    ws: Any,
    logic: WorkerLogic,
    outbound: "queue.Queue[SubmitAnswer] | None" = None,
    knowledge_sync_interval: int = 0,
) -> None:
    """등록된 소켓에서 프레임을 받아 처리하는 수신 루프(수동 시연 영역).

    `PushWork`→`handle_push_work`→`SubmitAnswer` 전송, `Ping`→`Heartbeat` 응답. 소켓이
    닫히면 `recv`가 예외를 던져 루프를 빠져나가고 `run_worker`가 재연결한다. 실 claude
    호출(`handle_push_work` 안)은 느리므로 한 작업을 끝낸 뒤 다음 프레임을 받는다.

    `outbound`(owner 검토 UI 겸직): 주입되면 recv를 짧은 타임아웃으로 폴링하며 매 주기
    `_drain_outbound`로 owner가 처분한 답을 활성 소켓으로 송신한다. recv 타임아웃은 정상
    흐름(TimeoutError를 삼켜 다음 폴링으로) — 미주입이면 기존 블로킹 recv 그대로.

    `knowledge_sync_interval`(Phase 12 (B)·ADR 0033 ⑤): 양수면 그 초마다 지식 동기화를
    재송신한다(지정 경계 파일 재독 → SyncKnowledge). 주기 재송신을 켜면 recv를 타임아웃
    폴링으로 돌려(outbound 폴링과 같은 방식) 재송신 타이밍을 잰다. 0이면 주기 재송신 없음
    (시작 시 1회만·`run_worker`가 이미 보냄·하위호환).
    """
    import time as _time

    from agent_org_network.transport import Heartbeat

    # 주기 재송신이 켜지면(interval>0) recv를 폴링 모드로 돌려야 재송신 타이밍을 잰다 —
    # outbound 폴링과 같은 조건. 둘 중 하나라도 켜지면 폴링 모드.
    polling = outbound is not None or knowledge_sync_interval > 0
    last_sync = _time.monotonic()
    while True:
        if knowledge_sync_interval > 0:
            now = _time.monotonic()
            if now - last_sync >= knowledge_sync_interval:
                for sync in logic.knowledge_sync_frames(
                    synced_at=datetime.now(tz=timezone.utc)
                ):
                    ws.send(sync.model_dump_json())
                last_sync = now
        if polling:
            if outbound is not None:
                _drain_outbound(ws, logic, outbound)
            try:
                raw = ws.recv(timeout=0.5)
            except TimeoutError:
                # recv 타임아웃 — 정상. 다음 루프에서 outbound flush·주기 재송신 후 대기.
                continue
            payload = _loads(raw)
        else:
            payload = _loads(ws.recv())
        # 지식 동기화 회신(KnowledgeSyncAck·Phase 12 (B))은 CentralFrame이 아니라 요청-응답
        # 회신이라 parse_central_frame이 None을 준다 — 먼저 판별해 로깅한다(관측). 거부면
        # 사유를 찍어 owner가 지정 경계·민감 필터를 고칠 수 있게 한다(중앙 로그와 대칭).
        if isinstance(payload, dict) and cast(dict[str, Any], payload).get("type") == "knowledge_sync_ack":
            from agent_org_network.knowledge_sync import KnowledgeSyncAck

            try:
                ack = KnowledgeSyncAck.model_validate(payload)
            except ValidationError:
                continue
            if ack.accepted:
                print(f"[worker:{logic.owner_id}|{logic.role}] 지식 동기화 수용 agent={ack.agent_id}")
            else:
                print(
                    f"[worker:{logic.owner_id}|{logic.role}] 지식 동기화 거부 "
                    f"agent={ack.agent_id} reason={ack.reason}"
                )
            continue
        frame = parse_central_frame(cast(object, payload))
        if isinstance(frame, PushWork):
            print(
                f"[worker:{logic.owner_id}|{logic.role}] 작업 수신 "
                f"ticket={frame.ticket.ticket_id[:8]} agent={frame.ticket.agent_id} "
                f"— 로컬 claude 호출 중…"
            )
            submit = logic.handle_push_work(frame)
            if submit is None:
                # HITL on 힌트 — 초안이 보류됐다(owner 검토 대기, ADR 0025 결정 4). 즉시
                # 회신하지 않는다(워커측 TTL 없음 — 종착은 중앙 큐 timeout이 떠받침).
                print(
                    f"[worker:{logic.owner_id}|{logic.role}] 초안 보류 "
                    f"ticket={frame.ticket.ticket_id[:8]} — owner 검토 대기"
                )
                continue
            ws.send(submit.model_dump_json())
            print(f"[worker:{logic.owner_id}|{logic.role}] 답 회신 ticket={submit.ticket_id[:8]}")
        elif isinstance(frame, FetchDocument):
            # on-demand 문서 fetch(ADR 0028 §15 결정 D) — 자기 OKF 문서 본문을 회신한다.
            # 로컬 파일 1개 읽기라 즉시(LLM 0·작업 큐 무관). request_id echo로 correlation.
            doc = logic.handle_fetch_document(frame)
            ws.send(doc.model_dump_json())
            print(
                f"[worker:{logic.owner_id}|{logic.role}] 문서 회신 "
                f"agent={frame.agent_id} concept={frame.concept_id} found={doc.found}"
            )
        elif isinstance(frame, Ping):
            ws.send(Heartbeat().model_dump_json())
        # Welcome/AuthError/미지 프레임은 수신 루프에선 무시(등록은 끝났다).


def _loads(raw: Any) -> object:
    """WS recv 페이로드(str | bytes)를 JSON 객체로 파싱한다(수동 시연 헬퍼)."""
    import json

    if isinstance(raw, bytes | bytearray):
        raw = raw.decode("utf-8")
    return json.loads(cast(str, raw))


# ── 답 생성 런타임 선택(opt-in 공급자 어댑터, ADR 0027 T9.6) ──────────────────
# 공급자 레지스트리·선택 로직은 `runtime_select`로 옮겨 worker.py·web.py가 *공유*한다(단일
# 출처·상단 import). worker는 별도 프로세스, web:app은 인프로세스 — 둘 다 같은 `AON_PROVIDER`
# 선택을 쓴다. `main`은 공개 `select_runtime`을 호출한다.


# ── CLI 진입점(수동 시연) ────────────────────────────────────────────────────


DEFAULT_WORKER_URL = "ws://127.0.0.1:8000/worker"


def main() -> None:
    """owner 워커 프로세스 진입점 — `python -m agent_org_network.worker --owner <id>`.

    owner_id를 CLI(`--owner`)/env(`OWNER_ID`)로 받고, 데모 샘플(`cards_for_owner`)에서 그
    owner의 카드 매핑을 채워 `WorkerLogic`을 만든 뒤 실 WS로 중앙에 붙는다(`run_worker`).
    중앙 URL은 `--url`/env(`CENTRAL_URL`, 기본 `ws://127.0.0.1:8000/worker`). 인증 토큰
    자리(`--token`)는 ADR 0009 → T6.5(지금은 거부 hook만).

    등급(`--role primary|backup`, ADR 0012 결정 2, T6.6 슬라이스 iv): 이 워커가 owner PC
    1차 워커(primary, 기본)인지 owner 위임 격리 백업(backup)인지. backup으로 띄우면 primary가
    부재(미연결/회수)일 때 그 owner 작업을 받아 *같은* 로컬 claude로 답한다 — 답은 디스패처가
    연결 등급을 진실로 mode=backup으로 강제 하향하고(결정 4), owner 처리함에 미검토 검토 항목이
    쌓인다(결정 7). 데모는 같은 카드 데이터로 backup 동작을 보인다(실 동기화 스냅샷은 후속).
    수동 시연이라 게이트 밖이다.
    """
    import argparse
    import os

    from agent_org_network.demo import DEMO_OKF_ROOT, cards_for_owner

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
        "--role",
        default=os.environ.get("WORKER_ROLE", "primary"),
        choices=["primary", "backup"],
        help="워커 등급(ADR 0012 결정 2): primary=owner PC 1차, backup=owner 위임 격리 백업 (env WORKER_ROLE, 기본 primary)",
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
    role: WorkerRole = cast(WorkerRole, args.role)

    cards = cards_for_owner(owner_id)
    if not cards:
        # 데모 샘플에 그 owner의 카드가 없으면 처리할 작업이 없다 — 조기 경고(미아 아님,
        # 중앙 큐의 그 owner 작업은 timeout→escalation으로 종착한다, 2b-i).
        print(
            f"[worker:{owner_id}] 경고: 데모 샘플에 owner '{owner_id}'의 카드가 없습니다. "
            "(legal_lead / cs_lead / finance_lead 중 하나여야 함)"
        )
    # owner OKF 번들 cwd 소비(ADR 0013, T6.7): 기본 ClaudeCodeRuntime에 owner 번들 루트를
    # 주입한다 — 답할 카드의 규약 경로(okf_root/{agent_id})에 번들이 있으면 그 디렉터리를
    # cwd로 읽어 답한다(없으면 기존 tempfile 동작). 데모는 repo okf/지만 의미상 owner 환경.
    # AON_PROVIDER=claude-api면 owner OAuth 인프로세스 SDK 스트리밍으로 교체된다(select_runtime).
    # 지식 동기화 지정 경계(Phase 12 (B)·ADR 0033 결정 3): env `AON_KNOWLEDGE_PATHS`가
    # 설정되면 그 경로들을 자기 소유 카드 전부에 지정한다(okf_root 상대·`;` 또는 `,` 구분).
    # 미설정이면 각 카드의 규약 디렉터리(`{agent_id}`)를 기본 지정한다 — 데모는 okf/{agent_id}
    # 번들이 곧 그 담당자의 명시 지식이므로 자연 기본. 담당자가 세밀 지정하려면 env로 덮는다.
    knowledge_paths: dict[str, tuple[str, ...]]
    _paths_env = (os.environ.get("AON_KNOWLEDGE_PATHS") or "").strip()
    if _paths_env:
        _specified = tuple(
            p.strip() for p in _paths_env.replace(";", ",").split(",") if p.strip()
        )
        knowledge_paths = {aid: _specified for aid in cards}
    else:
        knowledge_paths = {aid: (aid,) for aid in cards}

    logic = WorkerLogic(
        owner_id=owner_id,
        cards=cards,
        runtime=select_runtime(DEMO_OKF_ROOT),
        role=role,
        okf_root=DEMO_OKF_ROOT,
        knowledge_paths=knowledge_paths,
    )
    print(
        f"[worker:{owner_id}|{role}] 카드 {len(cards)}개({', '.join(cards) or '없음'}) — "
        f"중앙 {args.url} 연결 시도."
    )

    # owner 로컬 검토 웹 겸직(opt-in·ADR 0025 결정 4·T9.7 S4·D2): env `AON_OWNER_UI_PORT`가
    # 설정되면 같은 프로세스에서 owner 검토 웹(`owner_web`)을 별도 스레드로 띄운다. WS 워커
    # 루프(`run_worker`, 동기 소켓)와 HTTP 서버(uvicorn)를 한 프로세스가 겸직한다 — 별도
    # 서버 프로세스를 두지 않는다. owner가 UI에서 승인/수정하면 submit_sink가 스레드 안전
    # `outbound` 큐에 SubmitAnswer를 넣고, `_serve`가 그것을 활성 WS로 flush한다(같은 owner
    # 연결로 회신). bind는 127.0.0.1 고정(owner 자기 로컬 면 — 외부 미도달). 미설정이면
    # 기존 동작 100% 무변경(하위호환 — outbound 미주입).
    outbound: queue.Queue[SubmitAnswer] | None = None
    ui_port_raw = os.environ.get("AON_OWNER_UI_PORT")
    if ui_port_raw:
        import threading

        import uvicorn

        from agent_org_network.owner_web import create_owner_app

        outbound = queue.Queue()
        _outbound = outbound  # 클로저 캡처(타입 좁힘)

        def _submit_sink(submit: SubmitAnswer) -> None:
            _outbound.put(submit)

        app = create_owner_app(logic, submit_sink=_submit_sink)
        ui_port = int(ui_port_raw)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=ui_port, log_level="warning")
        )
        threading.Thread(target=server.run, daemon=True).start()
        print(
            f"[worker:{owner_id}|{role}] owner 검토 웹 http://127.0.0.1:{ui_port} "
            "(초안 보류 승인/수정)"
        )

    run_worker(
        logic,
        url=args.url,
        token=args.token,
        reconnect=not args.no_reconnect,
        outbound=outbound,
    )


if __name__ == "__main__":
    main()
